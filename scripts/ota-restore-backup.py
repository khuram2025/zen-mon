#!/usr/bin/env python3
"""Use the signed release's restore logic even with an older loaded updater."""
from pathlib import Path
import json
import os
import sys

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("OTA restore must run as root")
    # Restore can replace updater/ on disk; use this authenticated snapshot.
    from updater.backup_transaction import restore_transaction_backup
    manifest = json.loads((CODE.parent / 'manifest.json').read_text())
    restored = restore_transaction_backup(manifest['update_id'])
    print('OTA backup restored' if restored else 'No completed backup; resuming unchanged appliance')
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
