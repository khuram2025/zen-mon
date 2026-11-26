"""Exact payload and step contract for the 1.25.30 reporting-only overlay."""
import hashlib
import subprocess

BASE = "1.25.30"
VERSION = "1.25.31"
PROFILE = "os-patch-report-only"
HOOK = "scripts/os-patch-overlay.py"


def steps(actions):
    return [{"type": "run_hook", "script": f"hooks/{action}.py", "timeout": 30}
            for action in actions]


def payload(repo):
    sources = {
        "code/.version": ".version",
        "code/updater/agent.py": "updater/agent.py",
        "code/updater/os_patch_check.py": "updater/os_patch_check.py",
        **{f"hooks/{action}.py": HOOK for action in ("check", "install", "restore")},
    }
    files = {name: (repo / path).read_bytes() for name, path in sources.items()}
    original = subprocess.check_output(["git", "show", f"v{BASE}:updater/agent.py"], cwd=repo)
    if hashlib.sha256(original).hexdigest() != "584dcd43b602f7aee7dfa60dd61aae1b2e1454b97fdf50dda3a251e45fc3a7e7":
        raise ValueError("Unexpected base updater")
    files["original-agent.py"] = original
    return files


def verify(repo, manifest, tar):
    expected = payload(repo)
    files = [m.name for m in tar.getmembers() if m.isfile()]
    metadata = {"manifest.json", "manifest.json.sig", "checksums.sha256"}
    if (len(files) != len(set(files)) or set(files) != set(expected) | metadata
            or manifest.get("steps") != steps(["check", "install"])
            or manifest.get("rollback_steps") != steps(["restore"])
            or manifest.get("min_version") != BASE or manifest.get("version") != VERSION
            or manifest.get("agent_artifacts_included") is not False
            or manifest.get("agent_packages") != []):
        raise ValueError("Invalid reporting-only OTA scope")
    for name, data in expected.items():
        if tar.extractfile(name).read() != data:
            raise ValueError(f"Reporting payload differs from release source: {name}")
    expected_sums = {f"{hashlib.sha256(tar.extractfile(n).read()).hexdigest()}  {n}"
                     for n in set(expected) | {"manifest.json"}}
    if set(tar.extractfile("checksums.sha256").read().decode().splitlines()) != expected_sums:
        raise ValueError("Reporting payload checksum list mismatch")
