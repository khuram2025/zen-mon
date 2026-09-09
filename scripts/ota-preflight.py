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
    print(f"Prerequisite verified: {current} -> {manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
