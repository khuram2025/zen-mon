#!/usr/bin/python3 -I
"""Apply or undo the two-file reporting overlay on its exact signed base."""
import os
from pathlib import Path
import tempfile


def replace(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(action, root, payload):
    if (root / ".version").read_text().splitlines()[0] != "1.25.30":
        raise ValueError("This overlay requires the qualified 1.25.30 base")
    directory = root / "updater"
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Unexpected updater directory")
    agent, probe = directory / "agent.py", directory / "os_patch_check.py"
    original = (payload / "original-agent.py").read_bytes()
    updated = (payload / "code/updater/agent.py").read_bytes()
    collector = (payload / "code/updater/os_patch_check.py").read_bytes()
    if agent.is_symlink() or agent.read_bytes() not in (original, updated):
        raise ValueError("Installed updater differs from the qualified base")
    if probe.is_symlink() or (probe.exists() and probe.read_bytes() != collector):
        raise ValueError("Unexpected pre-existing OS patch collector")
    if action == "check":
        return
    if action == "install":
        replace(probe, collector)
        replace(agent, updated)
    elif action == "restore":
        replace(agent, original)
        probe.unlink(missing_ok=True)
    else:
        raise ValueError("Unknown reporting overlay action")


if __name__ == "__main__":
    hook = Path(__file__).resolve()
    run(hook.stem, Path("/opt/zenplus"), hook.parent.parent)
