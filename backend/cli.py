"""Unprivileged front end for the Noctalia UI. Root work goes through pkexec.

`status` reads the boot catalog through a read-only PolicyKit action.
`start --entry ID --confirmed` launches a detached worker that asks for
administrator authentication; the root helper re-validates the entry against
a fresh catalog before it changes the one-time boot variable and restarts.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from common import boot_id, lock, read_json, state_dir, write_json

PKEXEC = "/run/wrappers/bin/pkexec"
BOOT_LIST = "@boot_list@"
BOOT_REBOOT = "@boot_reboot@"
NOTIFY = "@notify@"
# pkexec exit codes: 126 = dialog dismissed / auth failed, 127 = not authorized.
AUTH_CODES = {126: "Authentication was cancelled or failed. No boot setting was changed.",
              127: "You are not authorized to switch operating systems from this session. No boot setting was changed."}


def pkexec_path():
    for candidate in (PKEXEC, "/usr/bin/pkexec"):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("pkexec (PolicyKit) is not installed; enable security.polkit on NixOS")


def run_pkexec(argv, runner=subprocess.run, timeout=None):
    return runner([pkexec_path(), *argv], capture_output=True, text=True, timeout=timeout)


def parse_helper(result, fallback):
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        data = {}
    if result.returncode in AUTH_CODES and not data:
        raise RuntimeError(AUTH_CODES[result.returncode])
    if result.returncode or (data.get("error") and data.get("supported", True)):
        detail = data.get("error") or (result.stderr or "").strip()[-1200:] or fallback
        raise RuntimeError(detail)
    return data


def catalog(runner=subprocess.run):
    result = run_pkexec([BOOT_LIST], runner, timeout=25)
    data = parse_helper(result, "Boot entries could not be read. Install the NixOS module and use an active local session.")
    data["operation"] = operation_status()
    return data


def operation_status():
    value = read_json(state_dir() / "boot-operation.json", {"state": "idle"})
    if value.get("boot_id") and value["boot_id"] != boot_id():
        return {"state": "idle", "message": "The previous restart request belongs to an earlier boot"}
    if value.get("state") == "authenticating":
        try:
            os.kill(int(value["pid"]), 0)
        except (OSError, KeyError, ValueError):
            value.update(state="interrupted",
                         message="The restart request ended unexpectedly; refresh boot entries before trying again")
    return value


def boot_worker(identity, runner=subprocess.run, notify=True):
    with lock("boot-operation.lock"):
        path = state_dir() / "boot-operation.json"
        status = {"state": "authenticating", "pid": os.getpid(), "target": identity, "boot_id": boot_id(),
                  "message": "Waiting for administrator authentication"}
        write_json(path, status)
        try:
            response = parse_helper(run_pkexec([BOOT_REBOOT, identity], runner),
                                    "Authentication was cancelled or the restart failed")
            status.update(state="restarting", message=response.get("message", "Restart requested"))
        except Exception as exc:
            status.update(state="failed", message=str(exc))
            if notify and not NOTIFY.startswith("@"):
                subprocess.run([NOTIFY, "Noctalia Boot", str(exc)], check=False)
        write_json(path, status)
        return status


def start(identity, confirmed):
    if not confirmed:
        # The UI passes --confirmed only from its explicit confirmation step.
        raise PermissionError("Restart was not confirmed; nothing was changed")
    if not identity or identity.startswith("-"):
        raise ValueError("Invalid boot entry identifier")
    if operation_status().get("state") == "authenticating":
        raise RuntimeError("Another operating-system switch is waiting for authentication")
    # Entry IDs are passed literally; root validates them against a fresh catalog.
    subprocess.Popen([sys.executable, "-s", str(Path(__file__).resolve()), "worker", "--entry", identity],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"started": True}


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="noctalia-switch-os",
                                     description="Choose the operating system for the next boot (one time).")
    parser.add_argument("action", choices=["status", "start", "worker", "operation"])
    parser.add_argument("--entry")
    parser.add_argument("--confirmed", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.action == "status":
            result = catalog()
        elif args.action == "operation":
            result = operation_status()
        elif args.action == "worker":
            boot_worker(args.entry)
            return 0
        else:
            result = start(args.entry, args.confirmed)
        print(json.dumps(result))
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
