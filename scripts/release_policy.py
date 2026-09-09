"""Release-time prerequisite floor and mandatory migration tooling checks."""
import json
from pathlib import Path

from updater.version_policy import version_key


def validate_policy(root: Path, version: str, minimum: str | None) -> str:
    policy = json.loads((root / "scripts" / "release-policy.json").read_text())
    floor = policy["minimum_supported_upgrade_base"]
    minimum = minimum or floor
    if version_key(minimum) < version_key(floor):
        raise ValueError(f"Minimum version cannot bypass the recorded bridge floor {floor}")
    if version_key(minimum) >= version_key(version):
        raise ValueError("Minimum version must precede the new release")
    for name in policy["required_payload_files"]:
        if not (root / name).is_file():
            raise ValueError(f"Required release safety file missing: {name}")
    return minimum
