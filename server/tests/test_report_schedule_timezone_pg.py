"""Report schedule timezones on a real PostgreSQL (NOTIFY 07).

* migrate-163: every existing schedule is pinned to the organisation timezone
  it runs in today (trimmed, UTC when none is set), and re-applying it changes
  nothing;
* the schedule API stores and returns the schedule's own zone, its next run
  in UTC and the local label, and an organisation timezone change later does
  not move the schedule;
* two scheduler workers ticking at the same moment deliver a due schedule
  once (row claim with FOR UPDATE SKIP LOCKED), and a scheduler that was down
  runs a missed schedule once, then claims it forward.

Runs on the disposable cluster of ``pg_fixture`` only, never the appliance
database; delivery is replaced by a recorder.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import report_scheduler as rs

from inventory_fleet import api_client
from pg_fixture import SCRIPTS_DIR, pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

M163 = SCRIPTS_DIR / "migrate-163-report-schedule-timezone.sql"
UTC = timezone.utc


def _company(db, tz):
    db.execute("INSERT INTO system_settings (key, value) VALUES ('company', $1::jsonb) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
               {"company_name": "Acme", "timezone": tz})       # the fixture's codec encodes jsonb


def _schedule(db, *, name="NOC Daily Briefing", hour=8, tz=None, next_run=None, enabled=True):
    sid = uuid.uuid4()
    db.execute(
        "INSERT INTO report_schedules (id, name, enabled, report_type, period, format, frequency, hour, minute, "
        "timezone, next_run_at) VALUES ($1, $2, $3, 'availability', 'yesterday', 'none', 'daily', $4, 0, $5, $6)",
        sid, name, enabled, hour, tz, next_run)
    return sid


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def test_migration_163_pins_existing_schedules_to_the_organisation_zone(pg_db):
    cols = pg_db.query("SELECT data_type, character_maximum_length FROM information_schema.columns "
                       "WHERE table_name = 'report_schedules' AND column_name = 'timezone'")
    assert cols == [("character varying", 64)]
    # Rows saved before the column existed (NULL), with stray whitespace in the setting.
    _company(pg_db, "  Asia/Riyadh ")
    old = [_schedule(pg_db, name=f"Daily {i}") for i in range(3)]
    chosen = _schedule(pg_db, name="Berlin", tz="Europe/Berlin")
    pg_db.psql_file(M163)
    tz = dict(pg_db.query("SELECT id, timezone FROM report_schedules"))
    assert [tz[i] for i in old] == ["Asia/Riyadh"] * 3
    assert tz[chosen] == "Europe/Berlin"                      # an explicit zone is never overwritten
    # Idempotent: a second run (and a later organisation change) moves nothing.
    _company(pg_db, "UTC")
    pg_db.psql_file(M163)
    assert dict(pg_db.query("SELECT id, timezone FROM report_schedules")) == tz


def test_migration_163_without_an_organisation_zone_uses_utc(pg_db):
    pg_db.execute("DELETE FROM system_settings WHERE key = 'company'")
    sid = _schedule(pg_db)
    pg_db.psql_file(M163)
    assert pg_db.scalar("SELECT timezone FROM report_schedules WHERE id = $1", sid) == "UTC"


def test_api_stores_the_zone_and_an_organisation_change_does_not_move_it(pg_db):
    _company(pg_db, "Asia/Riyadh")
    with api_client(pg_db, role="admin", persist_user=True) as client:
        r = client.post("/api/v1/report-schedules", json={
            "name": "NOC Daily Briefing 08:00", "report_type": "availability", "period": "yesterday",
            "format": "pdf", "frequency": "daily", "hour": 8, "minute": 0})
        assert r.status_code == 201, r.text
        body = r.json()
        sid = body["id"]
        assert body["timezone"] == body["effective_timezone"] == "Asia/Riyadh"
        assert body["local_time_label"] == "08:00 Asia/Riyadh (05:00 UTC)"
        nxt = datetime.fromisoformat(body["next_run_at"])
        assert (nxt.hour, nxt.minute) == (5, 0) and nxt.utcoffset() == timedelta(0)
        assert body["next_run_local"].endswith("T08:00:00+03:00")
        assert body["name_time_mismatch"] is None
        stored = pg_db.query("SELECT timezone, next_run_at FROM report_schedules WHERE id = $1", uuid.UUID(sid))
        assert stored == [("Asia/Riyadh", nxt)]

        # The organisation moves to UTC: the schedule keeps its zone and instant.
        _company(pg_db, "UTC")
        again = client.get(f"/api/v1/report-schedules/{sid}").json()
        assert again["next_run_at"] == body["next_run_at"]
        assert again["local_time_label"] == "08:00 Asia/Riyadh (05:00 UTC)"
        assert again["organisation_timezone"] == "UTC"
        # Pausing and resuming recomputes in the schedule's zone, not the organisation's.
        client.post(f"/api/v1/report-schedules/{sid}/toggle")
        resumed = client.post(f"/api/v1/report-schedules/{sid}/toggle").json()
        assert datetime.fromisoformat(resumed["next_run_at"]).hour == 5

        # A DST zone: the UTC instant is the zone's; an unknown zone is refused.
        berlin = client.put(f"/api/v1/report-schedules/{sid}", json={"timezone": "Europe/Berlin"}).json()
        b_next = datetime.fromisoformat(berlin["next_run_at"])
        assert berlin["timezone"] == "Europe/Berlin"
        assert b_next == rs.compute_next_run({"frequency": "daily", "hour": 8, "minute": 0,
                                              "timezone": "Europe/Berlin"}, "UTC", after=datetime.now(UTC))
        assert client.put(f"/api/v1/report-schedules/{sid}", json={"timezone": "Mars/Olympus"}).status_code == 422
        # Clearing it pins the current organisation zone explicitly.
        cleared = client.put(f"/api/v1/report-schedules/{sid}", json={"timezone": None}).json()
        assert cleared["timezone"] == "UTC"

        # The field case: titled 08:00 but saved as 05:00.
        mis = client.put(f"/api/v1/report-schedules/{sid}", json={"hour": 5, "timezone": "Asia/Riyadh"}).json()
        assert mis["name_time_mismatch"] == "08:00"
        assert mis["local_time_label"] == "05:00 Asia/Riyadh (02:00 UTC)"

    audits = pg_db.query("SELECT action, metadata FROM audit_logs WHERE resource_id = $1 ORDER BY created_at",
                         sid)
    meta = [m if isinstance(m, dict) else json.loads(m) for _a, m in audits]
    assert meta[0]["frequency"] == "daily" and meta[0]["hour"] == 8 and meta[0]["minute"] == 0
    assert meta[0]["timezone"] == "Asia/Riyadh"
    assert meta[-1]["hour"] == 5 and meta[-1]["changed"] == ["hour", "timezone"]


def test_two_workers_deliver_a_due_schedule_once(pg_db, monkeypatch):
    _company(pg_db, "UTC")
    now = datetime.now(UTC)
    sid = _schedule(pg_db, tz="Asia/Riyadh", next_run=now - timedelta(days=3))   # missed while down
    fired: list[str] = []

    async def slow_delivery(db, sched, **kw):
        fired.append(sched["id"])
        await asyncio.sleep(0.3)          # still generating while the other worker ticks
        return {"status": "success", "errors": []}

    monkeypatch.setattr(rs, "generate_and_deliver", slow_delivery)

    async def worker():
        async with session(pg_db) as s:
            return await rs.run_scheduler_tick(s)

    async def both():
        return await asyncio.gather(worker(), worker())

    results = asyncio.run(both())
    assert fired == [str(sid)]
    assert sorted(len(r["fired"]) for r in results) == [0, 1]
    nxt, status = pg_db.query("SELECT next_run_at, last_status FROM report_schedules WHERE id = $1", sid)[0]
    assert status == "success"
    # Claimed forward to the next local 08:00 Riyadh (05:00 UTC), not replayed per missed day.
    assert nxt > now and (nxt.astimezone(UTC).hour, nxt.minute) == (5, 0)
    assert nxt - now <= timedelta(days=1)
    # The next tick finds nothing due.
    assert asyncio.run(worker())["fired"] == []
    assert fired == [str(sid)]
