#!/usr/bin/env python3
"""Signed first-step version gate, including appliances running older updaters."""
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep the verified payload inventory unchanged
PAYLOAD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PAYLOAD))
from updater.version_policy import validate_transition


def main():
    installed = Path(os.environ.get("ZENPLUS_DIR", "/opt/zenplus"))
    manifest = json.loads((PAYLOAD.parent / "manifest.json").read_text())
    current = (installed / ".version").read_text().splitlines()[0].strip()
    validate_transition(manifest, current)
    if (PAYLOAD / ".version").read_text().splitlines()[0].strip() != manifest["version"]:
        raise ValueError("Signed manifest and payload version mismatch")
    # The API of this release refuses placeholder secrets. Refuse an appliance
    # that still has a placeholder datastore password (nothing changed yet, so
    # no rollback is needed) and replace a weak JWT secret before any service
    # is restarted. See updater/secret_floor.py.
    from updater.secret_floor import SecretFloorError, enforce
    try:
        print(enforce(installed))
    except SecretFloorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    from updater.backup_transaction import begin_transaction
    begin_transaction(manifest['update_id'])
    print(f"Prerequisite verified: {current} -> {manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
