#!/opt/zenplus/venv/bin/python -I
"""Replace only the known collector on the enrolled 1.25.9001 reference."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile


def apply(action, root, payload, enrollment):
    if enrollment != "09933c9f-0e63-4bb1-8d2b-a51f7af53bf3":
        raise ValueError("Wrong reference enrollment")
    firmware = (root / ".version").read_text().splitlines()[0]
    if firmware != "1.25.9001" and not (action == "restore" and firmware == "1.25.9002"):
        raise ValueError("Wrong reference firmware")
    directory = root / "updater"
    probe = directory / "os_patch_check.py"
    original = (payload / "original.py").read_bytes()
    corrected = (payload / "reporter.py").read_bytes()
    if directory.is_symlink() or probe.is_symlink() or not probe.is_file():
        raise ValueError("Unexpected collector location")
    agent = (directory / "agent.py").read_bytes()
    if hashlib.sha256(agent).hexdigest() != "a183f5a789ab89f8da4ca4f64270495cdb52879c3a30f4d114a649a86fcec46c":
        raise ValueError("Unexpected installed agent")
    current = probe.read_bytes()
    if current not in (original, corrected):
        raise ValueError("Unexpected installed collector")
    if action == "check":
        return
    desired = {"install": corrected, "restore": original}[action]
    if current == desired:
        return
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(desired)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
            os.replace(temporary, probe)
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    root = Path("/opt/zenplus")
    sys.path.insert(0, str(root))
    from updater.config import load_config
    from updater.trust import require_root_owned
    for path in (root, root / ".version", root / "updater", root / "updater/agent.py", root / "updater/os_patch_check.py"):
        require_root_owned(path, what="reference reporter installation")
    hook = Path(__file__).resolve()
    apply(hook.stem, root, hook.parent.parent, load_config().appliance.id)
