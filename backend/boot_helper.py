"""Fixed-scope privileged boot interface, run only through pkexec.

Reads the real systemd-boot catalog and, after confirmation and administrator
authentication, sets a ONE-TIME next-boot entry and requests a normal restart.
It never sets the default entry, never edits loader.conf, entries or the ESP,
and never reads user-controlled files or paths. Standard library only.
"""
import fcntl
import fnmatch
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import syslog

BOOTCTL = "@bootctl@"
SYSTEMCTL = "@systemctl@"
EFI_GUID = "4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
EFIVARS = Path("/sys/firmware/efi/efivars")
SAFE_ENV = {"PATH": "/run/current-system/sw/bin:/run/wrappers/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "HOME": "/root", "SYSTEMD_PAGER": "cat"}
ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,254}\Z", re.ASCII)
GRUB_CONFIGS = ("/boot/grub/grub.cfg", "/boot/grub2/grub.cfg", "/boot/efi/EFI/grub/grub.cfg")
LOCK = "/run/lock/noctalia-switch-os.lock"


def run(argv):
    result = subprocess.run(argv, env=SAFE_ENV, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Command failed").strip()[-1600:])
    return result.stdout.strip()


def efi_value(name):
    try:
        raw = (EFIVARS / f"{name}-{EFI_GUID}").read_bytes()
        return raw[4:].decode("utf-16-le").rstrip("\0")
    except (OSError, UnicodeError):
        return ""


def os_name():
    try:
        values = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
        return shlex.split(values.get("PRETTY_NAME", '"Linux"'))[0][:160]
    except (OSError, ValueError, IndexError):
        return "Linux"


def detect_bootloader(read_efi=efi_value, exists=os.path.exists):
    """Identify the running boot manager from the Boot Loader Interface.

    Only systemd-boot is supported for switching. GRUB is recognised and
    reported, never driven: `grub-reboot` needs GRUB_DEFAULT=saved and a
    validated environment block, which cannot be verified safely here.
    """
    if not exists("/sys/firmware/efi"):
        return {"name": "Legacy BIOS", "supported": False,
                "reason": "This system did not boot through UEFI. One-time boot selection requires UEFI and systemd-boot; no boot settings will be changed."}
    loader = read_efi("LoaderInfo")
    if loader.lower().startswith("systemd-boot "):
        return {"name": loader, "supported": True, "reason": ""}
    if any(exists(p) for p in GRUB_CONFIGS):
        return {"name": "GRUB", "supported": False,
                "reason": "GRUB was detected. This release switches systemd-boot entries only; GRUB needs a separately validated saved-entry setup (grub-reboot with GRUB_DEFAULT=saved). No boot settings will be changed."}
    if loader:
        return {"name": loader[:120], "supported": False,
                "reason": "This boot manager does not implement systemd-boot's one-time entry. No boot settings will be changed."}
    return {"name": "Unrecognized bootloader", "supported": False,
            "reason": "The boot manager did not identify itself through the Boot Loader Interface. No boot settings will be changed."}


def clean_title(value):
    return " ".join(str(value).split())[:200]


def family(title, identity):
    text = (title + " " + identity).lower()
    if "windows" in text:
        return "windows"
    if "nixos" in text:
        return "nixos"
    return "linux"


def normalize_entries(raw):
    if not isinstance(raw, list):
        raise RuntimeError("bootctl returned an unsupported entry format")
    result, seen = [], set()
    for item in raw:
        identity = item.get("id") if isinstance(item, dict) else None
        if not isinstance(identity, str) or not ENTRY_ID.fullmatch(identity):
            continue
        if identity in seen or "firmware" in identity.lower() or "reboot-to" in identity.lower():
            continue
        # Kernel/BLS, UKI, or ordinary automatic OS entries only. Do not offer
        # firmware setup, selectors, patterns, or remote UKI download entries.
        if item.get("ukiUrl") or not (item.get("linux") or item.get("efi") or item.get("uki")):
            continue
        seen.add(identity)
        title = clean_title(item.get("showTitle") or item.get("title") or identity)
        result.append({"id": identity, "title": title, "family": family(title, identity),
                       "reported_default": bool(item.get("isDefault")),
                       "current": bool(item.get("isSelected"))})
    return result


def resolve_default(selector, entries):
    if selector == "@saved":
        return next((x["id"] for x in entries if x.get("current")), None)
    if selector and not selector.startswith("@"):
        return next((x["id"] for x in entries if fnmatch.fnmatchcase(x["id"], selector)), None)
    return None


def catalog(runner=run, read_efi=efi_value, detect=None):
    loader = (detect or (lambda: detect_bootloader(read_efi)))()
    if not loader["supported"]:
        return {"supported": False, "bootloader": loader["name"], "running_os": os_name(), "entries": [],
                "windows_found": False, "error": loader["reason"]}
    entries = normalize_entries(json.loads(runner([BOOTCTL, "--no-pager", "--json=short", "list"])))
    if not entries:
        raise RuntimeError("No local, selectable operating-system boot entries were found")
    oneshot, selected = read_efi("LoaderEntryOneShot"), read_efi("LoaderEntrySelected")
    selector = read_efi("LoaderEntryDefault")
    if not selector:
        try:
            esp = runner([BOOTCTL, "--print-esp-path"])
            if not esp.startswith("/") or "\n" in esp:
                raise RuntimeError("Invalid EFI partition path")
            for line in (Path(esp) / "loader/loader.conf").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "default":
                    selector = parts[1]
        except (OSError, RuntimeError):
            pass
    default = resolve_default(selector, entries)
    if default is None and not oneshot:
        default = next((x["id"] for x in entries if x["reported_default"]), None)
    for entry in entries:
        entry["default"] = entry["id"] == default
        entry["current"] = entry["id"] == selected
        entry["next"] = entry["id"] == (oneshot or default)
        entry.pop("reported_default", None)
    return {"supported": True, "bootloader": loader["name"], "running_os": os_name(), "entries": entries,
            "current_entry": selected, "default_entry": default, "oneshot_entry": oneshot or None,
            "next_entry": oneshot or default, "default_selector": selector or None,
            "windows_found": any(e["family"] == "windows" for e in entries)}


def switch_and_reboot(identity, get_catalog=catalog, runner=run, read_efi=efi_value, audit=None):
    # These checks occur before any mutation. Entry labels are never commands.
    if not isinstance(identity, str) or not ENTRY_ID.fullmatch(identity):
        raise ValueError("Invalid boot entry identifier")
    state = get_catalog()
    if not state.get("supported"):
        raise RuntimeError(state.get("error", "Unsupported bootloader"))
    entry = next((x for x in state["entries"] if x["id"] == identity), None)
    if entry is None:
        raise ValueError("The selected entry no longer exists; refresh the operating-system list")
    previous = read_efi("LoaderEntryOneShot")
    try:
        runner([BOOTCTL, "set-oneshot", identity])
        if read_efi("LoaderEntryOneShot") != identity:
            raise RuntimeError("The one-time boot selection could not be verified")
        if audit:
            audit("Confirmed one-time boot destination " + identity)
        # Respect shutdown inhibitors. Never use --force or disable checks.
        runner([SYSTEMCTL, "--check-inhibitors=yes", "reboot"])
    except BaseException as exc:
        try:
            # An empty string clears the one-time entry (bootctl semantics).
            runner([BOOTCTL, "set-oneshot", previous])
        except Exception as restore_error:
            raise RuntimeError(f"Restart was not completed. Restoring the previous one-time entry failed: "
                               f"{restore_error}. Inspect `bootctl status` before restarting.") from exc
        if audit:
            audit("Boot switch failed; no restart requested")
        raise
    return {"state": "restarting", "target": identity, "message": "Restart requested into " + entry["title"]}


def main():
    if os.geteuid() != 0:
        raise PermissionError("Use the installed PolicyKit interface")
    if len(sys.argv) == 2 and sys.argv[1] == "list":
        print(json.dumps(catalog()))
        return
    if len(sys.argv) != 3 or sys.argv[1] != "reboot":
        raise ValueError("Expected list, or reboot with one exact entry ID")
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(LOCK, flags, 0o600)
    with os.fdopen(fd, "r+") as lock:
        metadata = os.fstat(lock.fileno())
        if metadata.st_uid != 0 or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PermissionError("Unsafe boot-operation lock file")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        syslog.openlog("noctalia-switch-os")
        result = switch_and_reboot(sys.argv[2], audit=lambda message: syslog.syslog(syslog.LOG_NOTICE, message))
        print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
