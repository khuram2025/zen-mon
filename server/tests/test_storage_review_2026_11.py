"""Storage review of 2026-11-19 (docs/system-status-review-2026-11-19.md).

Fakes only: no ClickHouse, PostgreSQL or disk is touched.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.v1 import storage_management as api
from app.services import storage_service as svc

EPOCH = datetime(1970, 1, 1)
CUTOFF = datetime(2026, 11, 1)


# ─── Manual purge: only partitions that are provably old ─────────────────────

def test_partition_without_a_date_is_never_older_than_anything():
    """An unpartitioned table (one partition "all") and a table partitioned by a
    Date column report 1970 in max_time. ``max(max_time) < cutoff`` was true for
    them, so "purge older than 30 days" dropped the whole table."""
    assert svc.partition_newest(None, EPOCH) is None
    assert svc.partition_newest(date(1970, 1, 1), EPOCH) is None
    rows = [("host_network_flows", "all", 5_000, 10, EPOCH, EPOCH, date(1970, 1, 1))]
    assert svc._purgeable(rows, CUTOFF) == []


def test_date_partitioned_tables_use_their_date_and_keep_the_current_month():
    old = ("daily_rollup", "202609", 100, 1, EPOCH, EPOCH, date(2026, 9, 30))
    current = ("daily_rollup", "202611", 100, 1, EPOCH, EPOCH, date(2026, 11, 19))
    edge = ("daily_rollup", "202610", 100, 1, EPOCH, EPOCH, date(2026, 10, 31))
    plan = svc._purgeable([current, old, edge], CUTOFF)
    # 31 Oct holds rows up to midnight, which is not *older than* the 1 Nov cutoff.
    assert [p["partition_id"] for p in plan] == ["202609"]
    assert plan[0]["oldest"] is None and plan[0]["newest"] == "2026-10-01T00:00:00"


def test_datetime_partitions_older_than_the_cutoff_are_listed_oldest_first():
    def row(pid, newest):
        return ("ping_metrics", pid, 10, 1, newest - timedelta(days=1), newest, date(1970, 1, 1))
    plan = svc._purgeable([row("b", datetime(2026, 10, 20)), row("a", datetime(2026, 10, 5)),
                           row("c", datetime(2026, 11, 2))], CUTOFF)
    assert [p["partition_id"] for p in plan] == ["a", "b"]
    assert plan[0]["size_bytes"] == 10 and plan[0]["oldest"] == "2026-10-04T00:00:00"


def test_purge_answers_503_and_removes_nothing_when_history_is_down(monkeypatch):
    async def down():
        raise ConnectionError("clickhouse: connection refused 10.0.0.9")
    monkeypatch.setattr(svc, "ch_inventory", down)
    body = api.PurgeRequest(category="flows", older_than_days=30, dry_run=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.purge_data(body, db=None, user=None))
    assert exc.value.status_code == 503 and "Nothing was removed" in exc.value.detail
    assert "10.0.0.9" not in exc.value.detail


# ─── Backups ─────────────────────────────────────────────────────────────────

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class FakeDB:
    """Answers by SQL fragment; records every statement."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.statements.append((sql, params or {}))
        for fragment, rows in self.answers.items():
            if fragment in sql:
                return _Rows(rows(sql, params) if callable(rows) else rows)
        return _Rows([])

    async def commit(self):
        self.commits += 1

    def ran(self, fragment):
        return [s for s, _ in self.statements if fragment in s]


def _schedule(**over):
    now = datetime.now(timezone.utc)
    return {"enabled": True, "frequency": "daily", "weekday": 0, "hour_utc": now.hour,
            "include_clickhouse": False, "keep_last": 2, **over}


def test_a_failed_scheduled_backup_is_not_retried_every_sweep(monkeypatch):
    started = []
    monkeypatch.setattr(svc, "run_backup", lambda *a: started.append(a))
    # A scheduled backup (failed or not) already exists in this window.
    db = FakeDB({"created_by='schedule' AND created_at >": [(1,)]})
    assert asyncio.run(svc._maybe_run_scheduled_backup(db, _schedule())) is None
    window_sql = db.ran("created_by='schedule' AND created_at >")[0]
    assert "failed" not in window_sql, "a failed run must count as this window's attempt"
    assert not started and not db.ran("INSERT INTO storage_backups")


def test_the_schedule_waits_for_a_running_backup_or_restore(monkeypatch):
    monkeypatch.setattr(svc, "run_backup", lambda *a: pytest.fail("must not start"))
    db = FakeDB({"last_restore_status = 'running'": [("restore",)]})
    assert asyncio.run(svc._maybe_run_scheduled_backup(db, _schedule())) is None
    assert db.ran("pg_advisory_xact_lock") and not db.ran("INSERT INTO storage_backups")


def test_keep_last_keeps_exactly_that_many(monkeypatch):
    removed = []

    async def rm(backup_id):
        removed.append(backup_id)
    monkeypatch.setattr(svc, "delete_backup_files", rm)
    db = FakeDB({"OFFSET :keep": lambda sql, params: [("old-1",), ("old-2",)] if params["keep"] == 2 else []})
    assert asyncio.run(svc.prune_scheduled_backups(db, _schedule(keep_last=2))) == 2
    assert removed == ["old-1", "old-2"] and len(db.ran("DELETE FROM storage_backups")) == 2
    # Pruning counts completed backups only, and never one that is being restored.
    select = db.ran("OFFSET :keep")[0]
    assert "status='completed'" in select and "last_restore_status" in select


def test_nothing_is_pruned_while_a_scheduled_backup_is_running_or_the_schedule_is_off(monkeypatch):
    monkeypatch.setattr(svc, "delete_backup_files", lambda i: pytest.fail("must not delete"))
    running = FakeDB({"created_by='schedule' AND status='running'": [(1,)], "OFFSET :keep": [("x",)]})
    assert asyncio.run(svc.prune_scheduled_backups(running, _schedule())) == 0
    off = FakeDB({"OFFSET :keep": [("x",)]})
    assert asyncio.run(svc.prune_scheduled_backups(off, _schedule(enabled=False))) == 0
    assert off.statements == []


def test_one_backup_or_restore_at_a_time():
    db = FakeDB({"last_restore_status = 'running'": [("backup",)]})
    body = api.CreateBackupRequest()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.create_backup(body, db=db, user=None))
    assert exc.value.status_code == 409 and "backup is already running" in exc.value.detail
    assert not db.ran("INSERT INTO storage_backups")
    # The check holds the lock that serialises it with the start, across workers.
    assert db.statements[0][0].startswith("SELECT pg_advisory_xact_lock")


def test_a_backup_being_restored_cannot_be_deleted(monkeypatch):
    import uuid
    monkeypatch.setattr(svc, "delete_backup_files", lambda i: pytest.fail("must not delete"))
    db = FakeDB({"SELECT status, last_restore_status": [("completed", "running")]})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.delete_backup(uuid.uuid4(), db=db, user=None))
    assert exc.value.status_code == 409 and "being restored" in exc.value.detail


def _row(restore_status, restore_at):
    now = datetime.now(timezone.utc)
    return ("id", now, "admin", "config", "completed", False, 10, None, None, now,
            restore_at, restore_status, None)


def test_restart_is_asked_for_only_until_the_services_restart():
    before = api._SERVICES_STARTED_AT - timedelta(days=30)
    after = api._SERVICES_STARTED_AT + timedelta(seconds=5)
    assert api._backup_row_to_dict(_row("completed", after))["restore_needs_restart"] is True
    # Restored before these services started: they already run on it.
    assert api._backup_row_to_dict(_row("completed", before))["restore_needs_restart"] is False
    assert api._backup_row_to_dict(_row("failed", after))["restore_needs_restart"] is False
    assert api._backup_row_to_dict(_row(None, None))["restore_needs_restart"] is False
    assert api._backup_row_to_dict(_row("completed", after.replace(tzinfo=None)))["restore_needs_restart"] is True


def test_stale_restores_are_failed_by_the_sweep(monkeypatch):
    monkeypatch.setattr(svc, "write_watchdog_policy", lambda s: True)
    monkeypatch.setattr(svc, "watchdog_status", lambda: {"alive": True})

    async def nothing(db):
        return None
    monkeypatch.setattr(svc, "import_watchdog_audit", nothing)
    db = FakeDB({"pg_try_advisory_xact_lock": [(True,)]})
    asyncio.run(svc.storage_sweep_tick(db))
    stale = db.ran("last_restore_status='failed'")
    assert stale and "last_restore_status='running'" in stale[0]


# ─── Retention ───────────────────────────────────────────────────────────────

class _User:
    username = "admin"


def test_retention_is_saved_even_when_history_cannot_take_it_yet(monkeypatch):
    order = []

    async def upsert(db, key, value):
        order.append(("upsert", key))

    async def audit(*a, **k):
        order.append(("audit",))

    async def apply(db, value, actor):
        order.append(("apply",))
        raise ConnectionError("clickhouse down")

    monkeypatch.setattr(svc, "_upsert_setting", upsert)
    monkeypatch.setattr(svc, "write_watchdog_policy", lambda v: order.append(("watchdog",)))
    monkeypatch.setattr(svc, "apply_retention", apply)
    monkeypatch.setattr(api, "write_audit_log", audit)

    class DB(FakeDB):
        async def commit(self):
            order.append(("commit",))

    result = asyncio.run(api.update_retention(api.RetentionUpdate(categories={"flows": 14}), db=DB(), user=_User()))
    assert result["pending"] is True and "will be applied" in result["message"]
    # Saved and committed before the watchdog gets it and before history is asked.
    assert order[:5] == [("upsert", svc.RETENTION_SETTINGS_KEY), ("audit",), ("commit",), ("watchdog",), ("apply",)]
    assert ("upsert", svc.RETENTION_PENDING_KEY) in order and order[-1] == ("commit",)


def test_pending_retention_is_applied_by_the_sweep_once_history_answers(monkeypatch):
    calls = []

    async def apply(db, settings, actor):
        calls.append(actor)
        if len(calls) == 1:
            raise ConnectionError("still down")
        return [{"table": "flow_records", "from": 30, "to": 14}]

    monkeypatch.setattr(svc, "apply_retention", apply)
    pending = {"system_settings WHERE key = :key": lambda sql, p: [({"since": "x"},)]}
    db = FakeDB(pending)
    assert asyncio.run(svc.apply_pending_retention(db)) is None           # still down: stays pending
    assert not db.ran("DELETE FROM system_settings")
    assert asyncio.run(svc.apply_pending_retention(db)) == [{"table": "flow_records", "from": 30, "to": 14}]
    assert db.ran("DELETE FROM system_settings")
    # Nothing pending: history is not asked at all.
    assert asyncio.run(svc.apply_pending_retention(FakeDB())) is None and len(calls) == 2


# ─── Upload ──────────────────────────────────────────────────────────────────

def test_an_upload_never_overwrites_a_backup_the_appliance_holds(tmp_path, monkeypatch):
    import io
    import json
    import tarfile

    bid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    root, ch = tmp_path / "appliance", tmp_path / "clickhouse"
    (root / bid).mkdir(parents=True)
    (root / bid / "postgres.dump").write_bytes(b"the backup being taken")
    (ch / "appliance").mkdir(parents=True)
    monkeypatch.setattr(svc, "BACKUP_ROOT", str(root))
    monkeypatch.setattr(svc, "CH_BACKUP_HOST_ROOT", str(ch))
    manifest = json.dumps({"id": bid, "kind": "config", "created_at": "2026-11-19T00:00:00+00:00",
                           "app_version": "1.25.17", "include_clickhouse": False,
                           "clickhouse_tables": [], "clickhouse_backup_name": None}).encode()
    archive = tmp_path / "again.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in ((f"{bid}/manifest.json", manifest), (f"{bid}/postgres.dump", b"other")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(api.BackupAlreadyPresent):
        api._extract_uploaded_backup(str(archive))
    assert (root / bid / "postgres.dump").read_bytes() == b"the backup being taken"
