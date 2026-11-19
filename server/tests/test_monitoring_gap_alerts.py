"""Monitoring gap alerts (alerting plan G-2, migrate-202).

Pure parts (lifecycle step, readings, wording) run without a database; the
evaluator lifecycle, the poller's SNMP poll-state statement and the rule API
validator run on a disposable PostgreSQL (pg_fixture). Notifications are
captured by replacing host_alert_service.dispatch_to_channels.
"""
from __future__ import annotations

import contextlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_rule_scope as rs
from app.services import host_alert_service
from app.services import monitoring_gap_alert_service as gap

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
CHANNEL = "00000000-0000-0000-0000-00000000c0de"
REPO = Path(__file__).resolve().parents[2]


def cond(thr=15.0, op=">="):
    return {"metric": "snmp_unreachable", "operator": op, "threshold": thr}


# ── Lifecycle step ──────────────────────────────────────────────────────────

def step(value, *, open_incident=False, breach=None, clear=None, hold=0, clear_hold=300, now=T0, c=None):
    return gap.decide(value=value, condition=c or cond(), open_incident=open_incident, breach_since=breach,
                      clear_since=clear, hold_s=hold, clear_hold_s=clear_hold, now=now)


def test_gap_raises_at_the_limit_and_after_the_hold():
    assert step(14.9).action == "none"
    assert step(15.0).action == "raise"
    assert step(15.0, c=cond(op=">")).action == "none"
    held = step(20.0, hold=120)
    assert held.action == "hold" and held.breach_since == T0
    assert step(22.0, hold=120, breach=T0, now=T0 + timedelta(seconds=120)).action == "raise"


def test_open_gap_clears_only_once_data_returns_and_stays():
    # Still failing, even below the limit: never a clear.
    assert step(3.0, open_incident=True).action == "none"
    first = step(0.0, open_incident=True)
    assert first.action == "hold" and first.clear_since == T0
    assert step(0.0, open_incident=True, clear=T0, now=T0 + timedelta(seconds=299)).action == "hold"
    assert step(0.0, open_incident=True, clear=T0, now=T0 + timedelta(seconds=300)).action == "resolve"
    assert step(0.0, open_incident=True, clear_hold=0).action == "resolve"


def test_missing_reading_never_raises_or_resolves():
    assert step(None).action == "none"
    d = step(None, open_incident=True, clear=T0)
    assert d.action == "none" and d.clear_since is None


# ── Readings ────────────────────────────────────────────────────────────────

def dev(**kw):
    base = {"status": "up", "snmp_poll_interval": 60, "last_attempt_at": T0 - timedelta(seconds=30),
            "failing_since": T0 - timedelta(minutes=20)}
    base.update(kw)
    return base


def test_snmp_reading():
    assert gap.snmp_reading(dev(), T0) == pytest.approx(20.0)
    assert gap.snmp_reading(dev(failing_since=None), T0) == 0.0
    assert gap.snmp_reading(dev(status="degraded"), T0) == pytest.approx(20.0)
    # Not answering ping: Device Down's business, no reading.
    for status in ("down", "unknown", "maintenance"):
        assert gap.snmp_reading(dev(status=status), T0) is None
    # Not polled for 10 minutes (or 3 intervals when longer): stale, no reading.
    assert gap.snmp_reading(dev(last_attempt_at=T0 - timedelta(seconds=601)), T0) is None
    assert gap.snmp_reading(dev(last_attempt_at=T0 - timedelta(seconds=800), snmp_poll_interval=300), T0) \
        == pytest.approx(20.0)
    assert gap.snmp_reading(dev(last_attempt_at=None), T0) is None


def test_agent_reading():
    hb = T0 - timedelta(seconds=20)
    assert gap.agent_reading(hb, T0 - timedelta(minutes=25), T0) == pytest.approx(25.0)
    assert gap.agent_reading(hb, T0 - timedelta(seconds=90), T0) == 0.0
    # Heartbeat silent: the availability rules' business.
    assert gap.agent_reading(T0 - timedelta(seconds=180), T0 - timedelta(minutes=25), T0) is None
    assert gap.agent_reading(None, T0, T0) is None
    # Never delivered host metrics (APM-only install): no reading.
    assert gap.agent_reading(hb, None, T0) is None


def test_wording():
    r = gap.Reading(key="k", value=15.2, asset="Switch core-1 (10.0.0.1)")
    assert gap.sentence("snmp_unreachable", r) == (
        "SNMP polling of Switch core-1 (10.0.0.1) has failed for 15 minutes while the device answers ping: "
        "check the SNMP credentials or ACL")
    assert gap.sentence("snmp_unreachable", r, recovery=True) == \
        "SNMP polling of Switch core-1 (10.0.0.1) is answered again"
    s = gap.Reading(key="k", value=1.0, asset="Server web-01 (10.0.0.5)")
    assert gap.sentence("agent_no_metrics", s) == (
        "The agent on Server web-01 (10.0.0.5) has sent no metrics for 1 minute although it still sends "
        "heartbeats: check the agent's Server module and its log")
    assert gap.gap_text(150) == "2 hours" and gap.gap_text(3 * 1440) == "3 days"


def test_agent_rules_are_server_rules():
    assert rs.family({"metric": "agent_no_metrics"}) == "server"
    assert rs.family({"metric": "snmp_unreachable"}) == "device"


# ── Evaluator on PostgreSQL ─────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def dispatch(db, channel_ids, ctx):
        calls.append((list(channel_ids), ctx))
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)
    return calls


def add_rule(db, metric, thr, op=">=", **cols):
    cols.setdefault("notify_channels", [CHANNEL])
    cols.setdefault("recovery_alert", True)
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 5}" + ("::jsonb" if c == "notify_channels" else "") for i, c in enumerate(cols))
    return str(db.scalar(
        f"INSERT INTO alert_rules (name, metric, operator, threshold, severity{names}) "
        f"VALUES ($1, $2, $3, $4, 'warning'{vals}) RETURNING id",
        f"{metric} rule", metric, op, thr, *cols.values()))


def add_switch(db, name="core-1", ip="192.0.2.11", status="up"):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, device_type, snmp_enabled) "
        "VALUES ($1, $2, $3, 'switch', true) RETURNING id", name, ip, status))


def poll_state(db, device, *, attempt, failing_since, error="request timeout"):
    db.execute(
        "INSERT INTO device_snmp_poll_state (device_id, last_attempt_at, failing_since, last_error, "
        "consecutive_failures) VALUES ($1, $2, $3, $4, 5) ON CONFLICT (device_id) DO UPDATE SET "
        "last_attempt_at = EXCLUDED.last_attempt_at, failing_since = EXCLUDED.failing_since",
        device, attempt, failing_since, error)


def alerts(db, rule):
    return db.query("SELECT status, message, metadata, device_id::text, server_id::text "
                    "FROM alerts WHERE rule_id = $1 ORDER BY triggered_at", rule)


async def run(db, now):
    async with session(db) as s:
        return await gap.evaluate_monitoring_gap_rules(s, now=now)


@pytest.mark.asyncio
async def test_snmp_unreachable_lifecycle(pg_db, sent):
    db = pg_db
    sw = add_switch(db)
    rule = add_rule(db, "snmp_unreachable", 15)
    poll_state(db, sw, attempt=T0 - timedelta(seconds=20), failing_since=T0 - timedelta(minutes=10))
    assert (await run(db, T0))["raised"] == 0                       # 10 min: below the limit
    t = T0 + timedelta(minutes=5)
    poll_state(db, sw, attempt=t - timedelta(seconds=20), failing_since=T0 - timedelta(minutes=10))
    assert (await run(db, t))["raised"] == 1
    [(status, message, meta, did, sid)] = alerts(db, rule)
    assert status == "active" and did == sw and sid is None
    assert message == ("SNMP polling of Switch core-1 (192.0.2.11) has failed for 15 minutes while the device "
                       "answers ping: check the SNMP credentials or ACL.")
    assert meta["gap_key"] == f"gap:device:{sw}" and meta["notified"] is True
    [(channels, ctx)] = sent
    assert channels == [CHANNEL] and ctx["delivery_event"] == "trigger" and ctx["alert_id"]
    assert ("Last SNMP error", "request timeout") in ctx["details"]

    # The device stops answering ping: no reading, the incident stays open.
    db.execute("UPDATE devices SET status = 'down' WHERE id = $1", sw)
    await run(db, t + timedelta(minutes=1))
    assert alerts(db, rule)[0][0] == "active" and alerts(db, rule)[0][2]["data_state"] == "missing"
    assert len(sent) == 1

    # Polls answered again: the default 300 s clear hold, then one recovery.
    db.execute("UPDATE devices SET status = 'up' WHERE id = $1", sw)
    t2 = t + timedelta(minutes=2)
    poll_state(db, sw, attempt=t2 - timedelta(seconds=5), failing_since=None)
    await run(db, t2)
    assert alerts(db, rule)[0][0] == "active"
    poll_state(db, sw, attempt=t2 + timedelta(seconds=295), failing_since=None)
    assert (await run(db, t2 + timedelta(seconds=300)))["resolved"] == 1
    assert alerts(db, rule)[0][0] == "resolved"
    _, rctx = sent[-1]
    assert rctx["delivery_event"] == "recovery"
    assert rctx["body"].startswith("SNMP polling of Switch core-1 (192.0.2.11) is answered again.")


@pytest.mark.asyncio
async def test_snmp_unreachable_skips_maintenance_and_exclusions(pg_db, sent):
    db = pg_db
    sw = add_switch(db)
    other = add_switch(db, "core-2", "192.0.2.12")
    rule = add_rule(db, "snmp_unreachable", 15)
    for d in (sw, other):
        poll_state(db, d, attempt=T0 - timedelta(seconds=20), failing_since=T0 - timedelta(minutes=30))
    db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at, reason) "
               "VALUES ('device', $1, $2, $3, 'change')", sw, T0 - timedelta(hours=1), T0 + timedelta(days=3650))
    db.execute("INSERT INTO alert_rule_exclusions (rule_id, asset_kind, device_id, reason) "
               "VALUES ($1, 'device', $2, 'lab')", rule, other)
    assert (await run(db, T0))["raised"] == 0
    assert alerts(db, rule) == [] and sent == []


@pytest.mark.asyncio
async def test_agent_no_metrics_lifecycle(pg_db, sent):
    db = pg_db
    srv = str(db.scalar("INSERT INTO servers (display_name, hostname, primary_ip, collection_mode, tags) "
                        "VALUES ('web-01', 'web-01', '192.0.2.15', 'agent', '[\"web\"]') RETURNING id"))
    db.execute("INSERT INTO agents (agent_uid, server_id, status, last_heartbeat_at, last_metric_at) "
               "VALUES ('uid-web-01', $1, 'online', $2, $3)", srv, T0 - timedelta(seconds=15), T0 - timedelta(minutes=21))
    rule = add_rule(db, "agent_no_metrics", 20, scope_tag="web")
    add_rule(db, "agent_no_metrics", 20, scope_tag="db")            # does not cover web-01
    assert (await run(db, T0))["raised"] == 1
    [(status, message, meta, did, sid)] = alerts(db, rule)
    assert sid == srv and did is None and meta["gap_key"] == f"gap:server:{srv}"
    assert message == ("The agent on Server web-01 (192.0.2.15) has sent no metrics for 21 minutes although it "
                       "still sends heartbeats: check the agent's Server module and its log.")
    assert len(sent) == 1

    # Heartbeat goes silent too: availability's business, nothing resolves.
    db.execute("UPDATE agents SET last_heartbeat_at = $1", T0 - timedelta(minutes=5))
    await run(db, T0 + timedelta(minutes=1))
    assert alerts(db, rule)[0][0] == "active"

    # Metrics flow again, held for clear_duration 0: resolved at once.
    db.execute("UPDATE alert_rules SET clear_duration = 0 WHERE id = $1", rule)
    t = T0 + timedelta(minutes=3)
    db.execute("UPDATE agents SET last_heartbeat_at = $1, last_metric_at = $1", t)
    assert (await run(db, t))["resolved"] == 1
    assert sent[-1][1]["body"].startswith("The agent on Server web-01 (192.0.2.15) is sending metrics again.")


# ── The poller's statement (poller/internal/store/postgres_snmp_poll.go) ───

def _go_sql() -> str:
    src = (REPO / "poller/internal/store/postgres_snmp_poll.go").read_text()
    return re.search(r"const recordSNMPPollSQL = `(.*?)`", src, re.S).group(1)


def test_poller_statement_tracks_the_failure_streak(pg_db):
    db = pg_db
    sw = add_switch(db)
    sql = _go_sql()

    def poll(at, ok, err=""):
        db.execute(sql, __import__("uuid").UUID(sw), at, ok, err)

    def row():
        return db.query("SELECT last_attempt_at, last_ok_at, failing_since, last_error, consecutive_failures "
                        "FROM device_snmp_poll_state WHERE device_id = $1", sw)[0]

    poll(T0, True)
    assert row() == (T0, T0, None, None, 0)
    poll(T0 + timedelta(minutes=1), False, "timeout")
    poll(T0 + timedelta(minutes=2), False, "timeout 2")
    assert row() == (T0 + timedelta(minutes=2), T0, T0 + timedelta(minutes=1), "timeout 2", 2)
    # A late write of an older poll never wins.
    poll(T0 + timedelta(seconds=30), True)
    assert row()[2] == T0 + timedelta(minutes=1)
    poll(T0 + timedelta(minutes=3), True)
    assert row() == (T0 + timedelta(minutes=3), T0 + timedelta(minutes=3), None, "timeout 2", 0)
    assert db.scalar("SELECT has_table_privilege('zenplus', 'device_snmp_poll_state', 'INSERT')")


# ── Rule API validator ──────────────────────────────────────────────────────

def test_rule_api_validates_gap_rules(pg_db):
    db = pg_db
    sw = add_switch(db)
    group = str(db.scalar("INSERT INTO device_groups (name) VALUES ('core') RETURNING id"))
    base = {"name": "SNMP gap", "metric": "snmp_unreachable", "operator": ">=", "threshold": 15,
            "severity": "warning", "notify_channels": []}
    agent = {**base, "name": "Agent gap", "metric": "agent_no_metrics", "threshold": 20}
    with api_client(db, role="admin", persist_user=True, username="admin-1") as c:
        r = c.post("/api/v1/alert-rules", json={**base, "device_id": sw})
        assert r.status_code == 201, r.text
        assert c.post("/api/v1/alert-rules", json={**base, "group_id": group, "device_type": "switch"}).status_code == 201
        assert c.post("/api/v1/alert-rules", json={**agent, "scope_tag": "web"}).status_code == 201
        assert c.post("/api/v1/alert-rules", json={**agent, "group_id": group}).status_code == 201
        bad = [
            {**base, "operator": "<"},
            {**base, "threshold": 0},
            {**base, "threshold": 20000},
            {**base, "trigger_on": "down"},
            {**base, "conditions": [{"metric": "snmp_unreachable", "operator": ">=", "threshold": 15},
                                    {"metric": "rtt", "operator": ">", "threshold": 5}]},
            {**base, "conditions": [{"metric": "snmp_unreachable", "operator": ">=", "threshold": 15,
                                     "reset_threshold": 5}]},
            {**agent, "device_id": sw},
            {**agent, "device_type": "switch"},
        ]
        for body in bad:
            assert c.post("/api/v1/alert-rules", json=body).status_code == 422, body
        rid = r.json()["id"]
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"operator": "<="}).status_code == 422
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"threshold": 30}).status_code == 200
