"""Release-time prerequisite floor, required checkpoints and mandatory migration tooling checks.

A required checkpoint is a release every appliance must install on its way to
anything newer, because it carries a one-time transition (installer catalog,
updater change, data transform, trust key) that later cumulative packages do
not repeat. Releases stay cumulative between checkpoints: an appliance goes
from its installed version to the next checkpoint above it, then the next, then
the newest release. scripts/release-policy.json lists the checkpoints with the
reason for each; minimum_supported_upgrade_base is always the newest one.
"""
import json
import sys
from pathlib import Path

from updater.version_policy import version_key


def load_policy(root: Path) -> dict:
    policy = json.loads((root / "scripts" / "release-policy.json").read_text())
    floor = policy["minimum_supported_upgrade_base"]
    checkpoints = policy.get("required_checkpoints") or [{"version": floor, "reason": policy.get("reason", "")}]
    versions = [entry.get("version") for entry in checkpoints]
    keys = [version_key(version) for version in versions]
    if keys != sorted(set(keys)):
        raise ValueError("required_checkpoints must be unique and in ascending version order")
    if any(not str(entry.get("reason") or "").strip() for entry in checkpoints):
        raise ValueError("Every required checkpoint needs a reason")
    if versions[-1] != floor:
        raise ValueError(f"minimum_supported_upgrade_base {floor} must be the newest required checkpoint {versions[-1]}")
    blocked = set(versions) & set(policy.get("blocked_prerequisite_versions", []))
    if blocked:
        raise ValueError(f"A blocked prerequisite cannot be a required checkpoint: {sorted(blocked)}")
    policy["required_checkpoints"] = checkpoints
    return policy


def default_minimum(root: Path, version: str) -> str:
    """The newest required checkpoint below this version: its signed minimum unless a higher one is named."""
    below = [entry["version"] for entry in load_policy(root)["required_checkpoints"]
             if version_key(entry["version"]) < version_key(version)]
    if not below:
        raise ValueError(f"Release {version} is not above any required checkpoint")
    return below[-1]


def validate_policy(root: Path, version: str, minimum: str | None) -> str:
    policy = load_policy(root)
    floor = default_minimum(root, version)
    minimum = minimum or floor
    if version_key(minimum) < version_key(floor):
        raise ValueError(f"Minimum version cannot bypass the recorded bridge floor {floor}")
    if minimum in policy.get("blocked_prerequisite_versions", []):
        raise ValueError(f"Minimum version {minimum} cannot be used as a required bridge; its signed path omits the installer catalog")
    if version_key(minimum) >= version_key(version):
        raise ValueError("Minimum version must precede the new release")
    for name in policy["required_payload_files"]:
        if not (root / name).is_file():
            raise ValueError(f"Required release safety file missing: {name}")
    return minimum


if __name__ == "__main__":
    # release.sh: python scripts/release_policy.py default-minimum <root> <version>
    if len(sys.argv) != 4 or sys.argv[1] != "default-minimum":
        raise SystemExit("usage: release_policy.py default-minimum <root> <version>")
    print(default_minimum(Path(sys.argv[2]), sys.argv[3]))
