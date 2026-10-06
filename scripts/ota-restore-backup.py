#!/usr/bin/env python3
"""Use the signed release's restore logic even with an older loaded updater."""
from pathlib import Path
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("OTA restore must run as root")
    # Restore can replace updater/ on disk; use this authenticated snapshot.
    from updater.rollback import restore_backup
    restore_backup("/opt/zenplus/updater/backups")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
