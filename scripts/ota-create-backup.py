#!/usr/bin/env python3
"""Create a completed, root-owned backup tied to this signed release."""
from pathlib import Path
import json
import os
import sys

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit('OTA backup must run as root')
    from updater.backup_transaction import create_transaction_backup
    manifest = json.loads((CODE.parent / 'manifest.json').read_text())
    previous = os.umask(0o077)
    try:
        backup = create_transaction_backup(manifest['update_id'])
    finally:
        os.umask(previous)
    print('Completed OTA backup:', backup.name)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
