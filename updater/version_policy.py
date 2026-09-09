"""Version and prerequisite checks shared by the updater and payload preflight."""
from __future__ import annotations

import re


class VersionPolicyError(ValueError):
    pass


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise VersionPolicyError(f"Invalid version: {value!r}; repair the installed version before updating")
    return tuple(map(int, value.split(".")))


def validate_transition(metadata, current):
    installed = version_key(current)
    target = version_key(metadata.get("version"))
    if target <= installed:
        raise VersionPolicyError("Update version must be newer than the installed version")
    minimum = metadata.get("min_version")
    if minimum:
        required = version_key(minimum)
        if required >= target:
            raise VersionPolicyError("Minimum version must be older than the target release")
        if installed < required:
            raise VersionPolicyError(
                f"Release {metadata['version']} requires {minimum} first (installed {current}); "
                "install the required intermediate release before continuing"
            )


def validate_manifest_transition(manifest, offer, current, payload_version):
    validate_transition(offer, current)
    validate_transition(manifest, current)
    if manifest.get("version") != offer.get("version") or payload_version != manifest.get("version"):
        raise VersionPolicyError("Signed manifest, offered release, and payload versions do not match")
    if (manifest.get("min_version") or None) != (offer.get("min_version") or None):
        raise VersionPolicyError("Offered prerequisite differs from the signed manifest")
