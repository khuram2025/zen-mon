"""Signed persistent collector update for the enrolled bridge reference."""
import hashlib
import subprocess

from reference_bridge_profile import REFERENCE, verify_scope

PROFILE = "reference-os-patch-reporter"
VERSION = "1.25.9002"
BASE = "1.25.9001"
TAG = f"zenplus-{PROFILE}-v{VERSION}"
OLD = "9b8138407eece28772705f9ffed2b7af6372bf19c7e72e2956e0c6e654059709"
NEW = "13be78c1344583f09a6d2ecace971f7196dc1e2219f9debfee5b5356e85f51ea"


def hook(action):
    return {"type": "run_hook", "script": f"hooks/{action}.py", "timeout": 30}


STEPS = [hook("check"), hook("install")]
ROLLBACK_STEPS = [hook("restore")]


def payload(repo):
    old = subprocess.check_output(["git", "show", "v1.25.31:updater/os_patch_check.py"], cwd=repo)
    new = (repo / "updater/os_patch_check.py").read_bytes()
    if (hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest()) != (OLD, NEW):
        raise ValueError("Unexpected reporter revision")
    script = (repo / "scripts/reference-reporter-overlay.py").read_bytes()
    return {"code/.version": (VERSION + "\n").encode(), "original.py": old, "reporter.py": new,
            **{step["script"]: script for step in STEPS + ROLLBACK_STEPS}}


def verify(repo, manifest, archive):
    verify_scope(manifest, archive, payload(repo), TAG, PROFILE, VERSION, BASE, STEPS, ROLLBACK_STEPS)
