"""Alerting audit 2026-11-19, phase 1 noise fixes, against a disposable
PostgreSQL (pg_fixture): NCM config-change alerts keep one open row per
device, server health no longer opens alert rows, and migrate-196 moves the
shipped holds of untouched seeded rules into the column the evaluators read."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import ncm_events, server_health_service as shs

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

MIGRATION = Path(__file__).resolve().parents[2] / "scripts" / "migrate-196-alert-noise-fixes.sql"


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def _device(pg_db) -> str:
    did = pg_db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('core-1', '10.9.0.1') RETURNING id")
    pg_db.execute("INSERT INTO device_ncm (device_id, notify_channels) VALUES ($1, '{}'::uuid[])"
                  " ON CONFLICT DO NOTHING", did)
    return str(did)


@pytest.mark.asyncio
async def test_repeated_config_changes_keep_one_open_alert(pg_db):
    pg_db.execute("DELETE FROM alerts")
    did = _device(pg_db)
    async with session(pg_db) as s:
        first = await ncm_events.event(s, did, "config.changed", "change-1")
        second = await ncm_events.event(s, did, "config.changed", "change-2")
        third = await ncm_events.event(s, did, "config.changed", "change-3")
        await s.commit()
    assert first == second == third
    [(status, meta)] = pg_db.query("SELECT status, metadata FROM alerts WHERE device_id = $1", did)
    meta = meta if isinstance(meta, dict) else json.loads(meta)
    assert status == "active" and meta["occurrences"] == 3 and meta["last_event_id"] == "change-3"


@pytest.mark.asyncio
async def test_a_closed_config_change_alert_is_not_reopened_by_the_next_change(pg_db):
    pg_db.execute("DELETE FROM alerts")
    did = _device(pg_db)
    async with session(pg_db) as s:
        first = await ncm_events.event(s, did, "config.changed", "change-1")
        await s.commit()
    pg_db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now()")
    async with session(pg_db) as s:
        second = await ncm_events.event(s, did, "config.changed", "change-2")
        await s.commit()
    assert first != second
    assert pg_db.scalar("SELECT count(*) FROM alerts WHERE status = 'active'") == 1


@pytest.mark.asyncio
async def test_server_health_no_longer_opens_alert_rows_and_still_carries_forward(pg_db):
    pg_db.execute("DELETE FROM alerts")
    pg_db.execute(MIGRATION.read_text())        # servers.health_issues (idempotent)
    sid = str(pg_db.scalar("INSERT INTO servers (hostname, display_name) VALUES ('srv-1', 'srv-1') RETURNING id"))
    # A row the old path left open is closed administratively on the next sync.
    pg_db.execute("INSERT INTO alerts (server_id, severity, message, status, metadata) VALUES"
                  " ($1, 'warning', 'CPU at 95.0% (≥ 90%)', 'active',"
                  " '{\"source\": \"health_threshold\", \"dedupe\": \"health:cpu\"}'::jsonb)", sid)
    async with session(pg_db) as s:
        status, reasons, issues = await shs.compute_server_health(
            s, sid, {"cpu": [{"cpu_total_pct": 99.0}], "memory": [{"used_pct": 40.0}]})
        await shs.store_server_health(s, sid, status, reasons, issues)
        await s.commit()
    assert status == "critical"
    assert pg_db.scalar("SELECT count(*) FROM alerts WHERE status <> 'resolved'") == 0
    [(meta,)] = pg_db.query("SELECT metadata FROM alerts")
    meta = meta if isinstance(meta, dict) else json.loads(meta)
    assert meta["resolved_by"] == "migration" and meta["retired"] == "health_threshold"
    # A disk-only upload keeps the CPU issue (carry-forward from servers.health_issues).
    async with session(pg_db) as s:
        status, reasons, _ = await shs.compute_server_health(s, sid, {})
    assert status == "critical" and any(r.startswith("CPU at 99.0%") for r in reasons)


def _seed_rule(pg_db, name, metric, op, threshold, duration, severity, cooldown):
    return str(pg_db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, duration, severity, cooldown)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        name, metric, op, threshold, duration, severity, cooldown))


def test_migration_moves_shipped_holds_only_where_the_timers_are_as_shipped(pg_db):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules")
    down = _seed_rule(pg_db, "Device Down", "ping_status", "eq", 0, 180, "critical", 300)
    loss = _seed_rule(pg_db, "Packet Loss", "packet_loss", "gt", 5, 300, "warning", 600)
    latency = _seed_rule(pg_db, "High Latency", "rtt", "gt", 100, 300, "warning", 600)
    # The customer's case: the threshold was raised; the rule keeps its timers.
    raised = _seed_rule(pg_db, "Packet Loss", "packet_loss", "gt", 90, 300, "warning", 600)
    # Saved in the editor: min_duration written and mirrored into duration.
    saved = _seed_rule(pg_db, "High Latency", "rtt", "gt", 100, 0, "warning", 600)
    # A channel added does not choose a hold, but keeps the recovery as it was.
    routed = _seed_rule(pg_db, "Device Down", "ping_status", "eq", 0, 180, "critical", 300)
    pg_db.execute("UPDATE alert_rules SET notify_channels = '[\"x\"]'::jsonb WHERE id = $1", routed)
    pg_db.execute(MIGRATION.read_text())
    pg_db.execute(MIGRATION.read_text())                                              # idempotent
    rows = {str(r[0]): r[1:] for r in pg_db.query(
        "SELECT id, min_duration, recovery_alert, threshold FROM alert_rules")}
    assert rows[down][:2] == (180, True)
    assert rows[loss][:2] == (300, False)
    assert rows[latency][:2] == (300, False)
    assert rows[raised] == (0, False, 90)
    assert rows[saved][0] == 0
    assert rows[routed][:2] == (180, False)
