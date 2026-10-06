"""Bind signed OTA recovery to a completed backup from the same update."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from .rollback import create_backup, restore_backup, cleanup_old_backups
from .trust import require_root_owned

STATE = Path('/var/lib/zenplus-updater')
BACKUPS = STATE / 'backups'
TRANSACTIONS = STATE / 'transactions'

def _prepare() -> None:
    for directory in (STATE, BACKUPS, TRANSACTIONS):
        mode = 0o755 if directory == STATE else 0o700
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
        require_root_owned(directory, what='OTA backup state')
        # PostgreSQL traverses STATE/work for public migration payloads. Only
        # backups and transaction records are private, even with umask 077.
        directory.chmod(mode)

def _marker(update_id: str) -> Path:
    if not isinstance(update_id, str) or not update_id:
        raise ValueError('A signed update identifier is required')
    return TRANSACTIONS / (hashlib.sha256(update_id.encode()).hexdigest() + '.json')

def create_transaction_backup(update_id: str) -> Path:
    _prepare()
    marker = _marker(update_id)
    # A retry must not recover a stale successful attempt if its own backup fails.
    marker.unlink(missing_ok=True)
    backup = Path(create_backup(str(BACKUPS), 'current', include_db=True))
    for required in ('code.tar.gz', 'pg_dump.sql.gz', 'version.txt'):
        if not (backup / required).is_file():
            raise RuntimeError('OTA backup is incomplete')
    with marker.open('x') as stream:
        os.chmod(marker, 0o600)
        json.dump({'backup': backup.name}, stream)
    cleanup_old_backups(str(BACKUPS), 3)
    return backup

def begin_transaction(update_id: str) -> None:
    """Discard a prior attempt before services stop or the backup hook starts."""
    _prepare()
    _marker(update_id).unlink(missing_ok=True)

def restore_transaction_backup(update_id: str) -> bool:
    _prepare()
    marker = _marker(update_id)
    if not marker.exists():
        # No completed backup means no payload changes: backup precedes apply.
        # The rollback's service-start step can still resume the old appliance.
        return False
    require_root_owned(marker, what='OTA backup transaction')
    name = json.loads(marker.read_text())['backup']
    if not isinstance(name, str) or not name.startswith('pre-current-') or Path(name).name != name:
        raise RuntimeError('Invalid OTA backup transaction')
    selected = BACKUPS / name
    require_root_owned(selected, what='Selected OTA backup')
    for required in ('code.tar.gz', 'pg_dump.sql.gz', 'version.txt'):
        require_root_owned(selected / required, what='OTA backup component')
    restore_backup(str(BACKUPS), selected_backup=selected)
    return True
