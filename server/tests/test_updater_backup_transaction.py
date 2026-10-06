"""Recovery must select only this attempt's completed root-owned backup."""
import json
import os
from pathlib import Path
import pytest
from updater import backup_transaction as transaction
from updater.trust import require_root_owned

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX ownership boundary')

@pytest.fixture
def state(monkeypatch, tmp_path):
    base = tmp_path / 'state'
    monkeypatch.setattr(transaction, 'STATE', base)
    monkeypatch.setattr(transaction, 'BACKUPS', base / 'backups')
    monkeypatch.setattr(transaction, 'TRANSACTIONS', base / 'transactions')
    def checked(path, **kwargs):
        require_root_owned(path, trusted_uid=tmp_path.stat().st_uid, stop_at=tmp_path, **kwargs)
    monkeypatch.setattr(transaction, 'require_root_owned', checked)
    transaction._prepare()
    return base

def test_missing_transaction_never_restores_an_older_backup(monkeypatch, state):
    (transaction.BACKUPS / 'pre-current-old').mkdir(mode=0o700)
    calls = []
    monkeypatch.setattr(transaction, 'restore_backup', lambda *a, **kw: calls.append(a))
    assert transaction.restore_transaction_backup('new-attempt') is False
    assert calls == []

def test_failed_retry_clears_previous_success_marker(monkeypatch, state):
    marker = transaction._marker('same-release')
    marker.write_text('{"backup":"pre-current-old"}')
    marker.chmod(0o600)
    def failed(*a, **kw): raise RuntimeError('disk full')
    monkeypatch.setattr(transaction, 'create_backup', failed)
    with pytest.raises(RuntimeError, match='disk full'):
        transaction.create_transaction_backup('same-release')
    assert not marker.exists()
    assert transaction.restore_transaction_backup('same-release') is False

def test_restore_selects_recorded_backup_instead_of_newest(monkeypatch, state):
    recorded = transaction.BACKUPS / 'pre-current-20200101'
    recorded.mkdir(mode=0o700)
    (transaction.BACKUPS / 'pre-current-20300101').mkdir(mode=0o700)
    for name in ('code.tar.gz','pg_dump.sql.gz','version.txt'):
        path = recorded / name
        path.write_text('fixture')
        path.chmod(0o600)
    marker = transaction._marker('selected-release')
    marker.write_text(json.dumps({'backup':recorded.name}))
    marker.chmod(0o600)
    calls = []
    monkeypatch.setattr(transaction, 'restore_backup', lambda path, **kw: calls.append(kw['selected_backup']))
    assert transaction.restore_transaction_backup('selected-release') is True
    assert calls == [recorded]

def test_incomplete_backup_cannot_become_recovery_target(monkeypatch, state):
    incomplete = transaction.BACKUPS / 'pre-current-incomplete'
    incomplete.mkdir(mode=0o700)
    (incomplete / 'code.tar.gz').write_text('fixture')
    monkeypatch.setattr(transaction, 'create_backup', lambda *a, **kw: str(incomplete))
    with pytest.raises(RuntimeError, match='incomplete'):
        transaction.create_transaction_backup('incomplete-release')
    assert not transaction._marker('incomplete-release').exists()

def test_preflight_clears_stale_marker_before_backup_process_starts(state):
    marker = transaction._marker('retried-release')
    marker.write_text('{"backup":"pre-current-old"}')
    marker.chmod(0o600)
    transaction.begin_transaction('retried-release')
    assert not marker.exists()
    assert transaction.restore_transaction_backup('retried-release') is False

def test_private_umask_keeps_migration_parent_traversable(state):
    import stat
    transaction.STATE.chmod(0o700)
    previous = os.umask(0o077)
    try: transaction._prepare()
    finally: os.umask(previous)
    assert stat.S_IMODE(transaction.STATE.stat().st_mode) == 0o755
    assert stat.S_IMODE(transaction.BACKUPS.stat().st_mode) == 0o700
    assert stat.S_IMODE(transaction.TRANSACTIONS.stat().st_mode) == 0o700
