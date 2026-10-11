"""A restored dump must not permanently block new backups."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.core import database
from app.services import storage_service as svc


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_after_postgres', [False, True])
async def test_restore_recovers_its_backup_row(tmp_path, monkeypatch, fail_after_postgres):
    conn = sqlite3.connect(tmp_path / 'state.db', check_same_thread=False)
    conn.execute('CREATE TABLE storage_backups(id TEXT PRIMARY KEY, status TEXT, last_restore_status TEXT, last_restore_error TEXT)')
    conn.execute("INSERT INTO storage_backups VALUES ('backup', 'completed', 'running', NULL)")
    conn.commit()

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, statement, params): conn.execute(str(statement), params)
        async def commit(self): conn.commit()

    def restore_helper(*args):
        # The dump captured its own backup job before backup completion.
        conn.execute("UPDATE storage_backups SET status='running', last_restore_status=NULL")
        conn.commit()
        return SimpleNamespace(returncode=0)

    def schema_restore(*args):
        if fail_after_postgres: raise RuntimeError('Injected telemetry restore failure')
        return 0

    directory = tmp_path / 'backup'; directory.mkdir()
    (directory / 'manifest.json').write_text(json.dumps({}))
    (directory / 'postgres.dump').write_bytes(b'fixture')
    (directory / 'clickhouse_schema.sql').write_text('')
    monkeypatch.setattr(svc, 'BACKUP_ROOT', str(tmp_path))
    monkeypatch.setattr(svc, '_run_helper', restore_helper)
    monkeypatch.setattr(svc, '_ch_apply_schema_sync', schema_restore)
    monkeypatch.setattr(svc, 'write_storage_event', AsyncMock())
    monkeypatch.setattr(database, 'AsyncSessionLocal', Session)
    await svc.run_restore('backup', ['postgres', 'clickhouse_schema'], 'admin')

    status, restore, error = conn.execute('SELECT status,last_restore_status,last_restore_error FROM storage_backups').fetchone()
    assert status == 'completed'  # A subsequent backup may run.
    assert restore == ('failed' if fail_after_postgres else 'completed')
    assert bool(error) == fail_after_postgres
    conn.close()
