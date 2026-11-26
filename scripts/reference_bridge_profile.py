"""Signed profile for the one-appliance legacy component delivery test."""
import hashlib

PROFILE = "reference-os-report-bridge"
BASE = "1.25.1"
VERSION = "1.25.9001"
TAG = f"zenplus-{PROFILE}-v{VERSION}"
REFERENCE = "09933c9f-0e63-4bb1-8d2b-a51f7af53bf3"
STEPS = [{"type": "run_hook", "script": "hooks/assess.py", "timeout": 240}]


def payload(repo):
    return {"code/.version": (VERSION + "\n").encode(),
            "hooks/assess.py": (repo / "scripts/reference-os-report-bridge.py").read_bytes(),
            "hooks/report_component.py": (repo / "updater/report_component.py").read_bytes()}


def verify(repo, manifest, archive):
    files = payload(repo)
    inventory = {k: hashlib.sha256(data).hexdigest() for k, data in files.items()}
    metadata = {"manifest.json", "manifest.json.sig", "checksums.sha256"}
    names = [m.name for m in archive.getmembers() if m.isfile()]
    if (len(names) != len(set(names)) or set(names) != set(files) | metadata
            or manifest.get("source", {}).get("tag") != TAG or manifest.get("release_profile") != PROFILE
            or manifest.get("version") != VERSION or manifest.get("min_version") != BASE
            or manifest.get("arch") != "amd64" or manifest.get("target_enrollment") != REFERENCE
            or manifest.get("steps") != STEPS or manifest.get("rollback_steps") != []
            or manifest.get("agent_artifacts_included") is not False or manifest.get("agent_packages") != []
            or manifest.get("payload_files") != inventory):
        raise ValueError("Unexpected reference-only OTA bridge scope")
    checksums = {f"{hashlib.sha256(archive.extractfile(n).read()).hexdigest()}  {n}"
                 for n in set(files) | {"manifest.json"}}
    if set(archive.extractfile("checksums.sha256").read().decode().splitlines()) != checksums:
        raise ValueError("Bridge checksum list mismatch")
