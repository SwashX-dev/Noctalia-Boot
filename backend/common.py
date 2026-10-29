"""User-owned private state. Never imported by the privileged boot helper."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

APP = "noctalia-switch-os"


def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")


def state_dir():
    path = state_root() / APP
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown-boot"


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Atomic 0600 write: a crash leaves either the old or the new file."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def lock(name):
    with (state_dir() / name).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another boot operation is running; try again in a moment") from exc
        yield
