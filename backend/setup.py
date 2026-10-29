"""Per-user integration helper: Noctalia plugin source + Niri key bindings.

It never edits NixOS configuration and never needs root. Every change to the
Niri config is backed up first, validated with `niri validate` before it is
written, and can be reverted with `undo`. Existing bindings are never replaced
silently: a conflicting key aborts the change and is reported.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

PROFILE = {
    "name": "noctalia-switch-os",
    "plugin_id": "local/switch-os",
    "source_name": "noctalia-switch-os",
    "share": "noctalia-switch-os",
    # Keys of the original Desk Tools block this project replaces on request.
    "legacy_keys": ["Mod+Shift+P"],
}
MARKER = f"// {PROFILE['name']}: managed by {PROFILE['name']}-setup"
SHARE_CANDIDATES = [Path("/run/current-system/sw/share"), Path.home() / ".nix-profile/share",
                    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "nix/profile/share"]
ETC_BINDS = Path("/etc") / PROFILE["name"] / "niri-binds.kdl"
BIND_LINE = re.compile(r"^\s*([A-Za-z0-9_+]+\+[A-Za-z0-9_]+|[A-Za-z0-9_]+)(?=[\s{])")
INCLUDE = re.compile(r'^\s*include\s+(?:optional=(?:true|false)\s+)?"([^"]+)"', re.M)
LEGACY_BEGIN, LEGACY_END = "// BEGIN DESK TOOLS", "// END DESK TOOLS"


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def state_home():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / PROFILE["name"]


def niri_config(path=None):
    if path:
        return Path(path)
    if os.environ.get("NIRI_CONFIG"):
        return Path(os.environ["NIRI_CONFIG"])
    return config_home() / "niri/config.kdl"


def normalize(chord):
    """Order/case-insensitive chord identity. Super and Mod are treated alike."""
    parts = chord.split("+")
    mods = sorted({"mod" if p.lower() in ("mod", "super", "win") else p.lower() for p in parts[:-1]})
    return "+".join(mods + [parts[-1].lower()])


def code_only(line):
    """Blank out quoted strings and drop a trailing // comment."""
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    return line.split("//", 1)[0]


def binds_in(text):
    """Yield (chord, line_number) for every binding inside top-level `binds { }` blocks."""
    depth, in_binds = 0, False
    for number, raw in enumerate(text.splitlines(), 1):
        line = code_only(raw)
        if depth == 0 and re.match(r"\s*binds\s*\{", line):
            in_binds = True
        elif in_binds and depth == 1:
            match = BIND_LINE.match(line)
            if match:
                yield match.group(1), number
        depth = max(0, depth + line.count("{") - line.count("}"))
        if depth == 0:
            in_binds = False


def collect_binds(config, skip=(), seen=None):
    """Bindings in a config and its includes, excluding files in `skip`."""
    seen = seen if seen is not None else set()
    skipped = {os.path.realpath(str(s)) for s in skip}
    config = Path(os.path.realpath(str(config)))
    if str(config) in seen or str(config) in skipped or not config.is_file():
        return []
    seen.add(str(config))
    text = config.read_text()
    found = [(chord, str(config), line) for chord, line in binds_in(text)]
    for target in INCLUDE.findall(text):
        found += collect_binds(config.parent / os.path.expanduser(target), skip, seen)
    return found


def conflicts(config, wanted, skip=()):
    wanted = {normalize(c): c for c in wanted}
    return [{"key": chord, "file": file, "line": line}
            for chord, file, line in collect_binds(config, skip) if normalize(chord) in wanted]


def find_share(subdir):
    for root in SHARE_CANDIDATES:
        candidate = root / PROFILE["share"] / subdir
        if candidate.exists():
            return candidate
    return None


def plugin_dir_default():
    found = find_share("plugins")
    if found:
        return found
    packaged = os.environ.get(PROFILE["name"].upper().replace("-", "_") + "_SHARE")
    return Path(packaged) / "plugins" if packaged and (Path(packaged) / "plugins").exists() else None


def default_binds_file():
    if ETC_BINDS.exists():
        return ETC_BINDS
    shipped = find_share("niri-binds.kdl")
    if shipped:
        return shipped
    packaged = os.environ.get(PROFILE["name"].upper().replace("-", "_") + "_SHARE")
    if packaged and (Path(packaged) / "niri-binds.kdl").exists():
        return Path(packaged) / "niri-binds.kdl"
    local = Path(__file__).resolve().parent.parent / "niri/binds.kdl"
    return local if local.exists() else None


def stable_binds(binds, config):
    """Never include a /nix/store path directly: it disappears after GC."""
    if not str(binds).startswith("/nix/store/"):
        return binds
    copy = config.parent / (PROFILE["name"] + "-binds.kdl")
    copy.write_text(binds.read_text())
    return copy


def run_noctalia(*args):
    binary = shutil.which("noctalia")
    if not binary:
        return False, "noctalia is not on PATH"
    try:
        result = subprocess.run([binary, "msg", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def validate_niri(path):
    binary = shutil.which("niri")
    if not binary:
        return True, "niri is not on PATH; validation skipped"
    result = subprocess.run([binary, "validate", "-c", str(path)], capture_output=True, text=True, timeout=30)
    return result.returncode == 0, (result.stderr or result.stdout).strip()[-1500:]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def remove_legacy_lines(text, keys):
    """Drop only the given keys inside the original Desk Tools marker block."""
    out, inside, removed = [], False, []
    wanted = {normalize(k) for k in keys}
    for line in text.splitlines(keepends=True):
        if LEGACY_BEGIN in line:
            inside = True
        elif LEGACY_END in line:
            inside = False
        match = BIND_LINE.match(line)
        if inside and match and normalize(match.group(1)) in wanted:
            removed.append(match.group(1))
            continue
        out.append(line)
    return "".join(out), removed


def comment_out_keys(text, keys):
    """Comment out (not delete) top-level bindings for `keys`; reversible with undo."""
    wanted = {normalize(k) for k in keys}
    lines, removed = text.splitlines(keepends=True), []
    for chord, number in binds_in(text):
        if normalize(chord) in wanted:
            line = lines[number - 1]
            if line.count("{") != line.count("}"):
                raise RuntimeError(f"{chord} spans several lines; edit it by hand")
            lines[number - 1] = f"    // {PROFILE['name']}-setup replaced: " + line.lstrip()
            removed.append(chord)
    return "".join(lines), removed


def write_atomic(path, text):
    temp = path.with_name("." + path.name + f".{PROFILE['name']}.tmp")
    temp.write_text(text)
    os.chmod(temp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
    os.replace(temp, path)


def cmd_check(args):
    config = niri_config(args.niri_config)
    binds = Path(args.bindings_file) if args.bindings_file else default_binds_file()
    keys = [c for c, _ in binds_in(binds.read_text())] if binds else []
    text = config.read_text() if config.exists() else ""
    report = {
        "niri_config": str(config), "niri_config_exists": config.exists(),
        "bindings_file": str(binds) if binds else None, "keys": keys,
        "include_present": MARKER in text,
        "conflicts": conflicts(config, keys, skip=[binds] if binds else []) if config.exists() else [],
        "plugin_dir": str(plugin_dir_default()) if plugin_dir_default() else None,
    }
    ok, listing = run_noctalia("plugins", "list")
    report["noctalia_running"] = ok
    report["plugin_enabled"] = ok and any(l.startswith(PROFILE["plugin_id"] + " ") and " enabled" in l
                                          for l in listing.splitlines())
    print(json.dumps(report, indent=2))
    return 0 if not report["conflicts"] else 3


def cmd_apply(args):
    config = niri_config(args.niri_config)
    binds = Path(args.bindings_file) if args.bindings_file else default_binds_file()
    messages = []
    if not args.no_niri:
        if not binds or not binds.exists():
            raise RuntimeError("No bindings file found; pass --bindings-file")
        if not config.exists():
            raise RuntimeError(f"Niri config {config} does not exist; create it or pass --niri-config")
        original = config.read_text()
        if MARKER in original:
            messages.append("Niri include already present; no change")
        else:
            text, removed = original, []
            if args.replace_desk_tools:
                text, removed = remove_legacy_lines(text, PROFILE["legacy_keys"])
            if args.replace:
                text, replaced = comment_out_keys(text, args.replace)
                removed += replaced
            keys = [c for c, _ in binds_in(binds.read_text())]
            stage = config.with_name("." + config.name + f".{PROFILE['name']}.check.kdl")
            stage.write_text(text)
            try:
                found = conflicts(stage, keys, skip=[binds])
            finally:
                stage.unlink()
            if found:
                detail = "; ".join(f"{c['key']} at {c['file']}:{c['line']}" for c in found)
                hint = (" Use --replace-desk-tools to replace the original Desk Tools bindings."
                        if LEGACY_BEGIN in original else "")
                raise RuntimeError("Key binding conflict, nothing changed: " + detail + "." + hint +
                                   " Use --replace KEY to comment out a binding in your main config,"
                                   " or choose other keys in the bindings file / NixOS module.")
            include_path = str(stable_binds(binds, config))
            text = text.rstrip("\n") + f'\n\n{MARKER}\ninclude optional=true "{include_path}"\n'
            stage = config.with_name("." + config.name + f".{PROFILE['name']}.new.kdl")
            stage.write_text(text)
            try:
                ok, detail = validate_niri(stage)
            finally:
                stage.unlink()
            if not ok:
                raise RuntimeError("niri validate rejected the new config; nothing changed: " + detail)
            if args.dry_run:
                print(json.dumps({"dry_run": True, "removed_legacy": removed, "new_config": text}))
                return 0
            backup_dir = state_home() / "backups" / time.strftime("%Y%m%d-%H%M%S")
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (backup_dir / "config.kdl").write_text(original)
            write_atomic(config, text)
            (backup_dir / "manifest.json").write_text(json.dumps(
                {"config": str(config), "before": sha(original), "after": sha(text), "removed_legacy": removed}))
            messages.append(f"Niri include added (backup in {backup_dir})")
            if removed:
                messages.append("Replaced existing bindings: " + ", ".join(removed))
    if not args.no_noctalia:
        plugin_dir = Path(args.plugin_dir) if args.plugin_dir else plugin_dir_default()
        if not plugin_dir:
            messages.append("Plugin directory not found; install the package, then rerun with --plugin-dir")
        else:
            ok, out = run_noctalia("plugins", "source", "add", PROFILE["source_name"], "path", str(plugin_dir))
            messages.append(("Noctalia source added" if ok else "Noctalia source not added: " + out))
            ok, out = run_noctalia("plugins", "enable", PROFILE["plugin_id"])
            messages.append(("Plugin enabled" if ok else "Plugin not enabled: " + out))
    print(json.dumps({"ok": True, "messages": messages}, indent=2))
    return 0


def latest_backup():
    root = state_home() / "backups"
    entries = sorted(p for p in root.iterdir() if (p / "manifest.json").exists()) if root.exists() else []
    return entries[-1] if entries else None


def cmd_undo(args):
    backup = latest_backup()
    messages = []
    if backup:
        manifest = json.loads((backup / "manifest.json").read_text())
        config = Path(manifest["config"])
        current = config.read_text() if config.exists() else ""
        if sha(current) != manifest["after"] and not args.force:
            raise RuntimeError(f"{config} changed after setup; refusing to overwrite. Inspect {backup} or pass --force.")
        write_atomic(config, (backup / "config.kdl").read_text())
        (backup / "manifest.json").rename(backup / "manifest.restored.json")
        messages.append(f"Restored {config} from {backup}")
    else:
        messages.append("No Niri backup to restore")
    if not args.keep_plugin:
        messages.append(run_noctalia("plugins", "disable", PROFILE["plugin_id"])[1] or "Plugin disabled")
        messages.append(run_noctalia("plugins", "source", "remove", PROFILE["source_name"])[1] or "Source removed")
    print(json.dumps({"ok": True, "messages": messages}, indent=2))
    return 0


def cmd_remove(args):
    config = niri_config(args.niri_config)
    messages = []
    if config.exists():
        lines = config.read_text().splitlines(keepends=True)
        out, skip_next = [], False
        for line in lines:
            if MARKER in line:
                skip_next = True
                continue
            if skip_next and line.lstrip().startswith("include"):
                skip_next = False
                continue
            skip_next = False
            out.append(line)
        text = "".join(out)
        if text != "".join(lines):
            write_atomic(config, text)
            messages.append("Removed the managed Niri include")
    messages.append(run_noctalia("plugins", "disable", PROFILE["plugin_id"])[1] or "Plugin disabled")
    messages.append(run_noctalia("plugins", "source", "remove", PROFILE["source_name"])[1] or "Source removed")
    print(json.dumps({"ok": True, "messages": messages}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog=PROFILE["name"] + "-setup", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["check", "apply", "undo", "remove"]:
        p = sub.add_parser(name)
        p.add_argument("--niri-config")
        if name in ("check", "apply"):
            p.add_argument("--bindings-file")
        if name == "apply":
            p.add_argument("--plugin-dir")
            p.add_argument("--no-niri", action="store_true")
            p.add_argument("--no-noctalia", action="store_true")
            p.add_argument("--replace-desk-tools", action="store_true")
            p.add_argument("--replace", action="append", metavar="KEY", default=[],
                           help="comment out an existing binding for KEY in the main Niri config")
            p.add_argument("--dry-run", action="store_true")
        if name == "undo":
            p.add_argument("--force", action="store_true")
            p.add_argument("--keep-plugin", action="store_true")
    args = parser.parse_args(argv)
    try:
        return {"check": cmd_check, "apply": cmd_apply, "undo": cmd_undo, "remove": cmd_remove}[args.command](args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
