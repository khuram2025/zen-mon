"""Event alerts: one open row per condition, and expiry (alerting audit G-7).

Unit tests for app.services.event_alerts / alert_expiry, and PostgreSQL tests
(disposable pg_fixture database) for the trap, syslog and Device Tracker
evaluators, the expiry sweep and the /alerts/event-expiry API.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_expiry as expiry
from app.services import alert_workflow as wf
from app.services import event_alerts as ea
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

CHANNEL = "00000000-0000-0000-0000-00000000c0de"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


# ── Unit: occurrence bookkeeping ────────────────────────────────────────────

def test_first_metadata_and_patch_keep_history():
    first = ea.first_metadata(NOW, "link down Gi1/0/1", "e1")
    assert first["occurrences"] == 1 and first["last_seen"] == NOW.isoformat()
    assert first["recent_occurrences"] == [{"at": NOW.isoformat(), "message": "link down Gi1/0/1",
                                            "event_id": "e1"}]
    meta = dict(first)
    for i in range(2, 15):
        meta.update(ea.occurrence_patch(meta, NOW + timedelta(minutes=i), f"m{i}", event_id=f"e{i}"))
    assert meta["occurrences"] == 14 and meta["last_message"] == "m14"
    assert len(meta["recent_occurrences"]) == ea.RECENT_MAX
    assert meta["recent_occurrences"][-1]["event_id"] == "e14"
    assert meta["last_seen"] == (NOW + timedelta(minutes=14)).isoformat()


def test_patch_never_overwrites_identity_or_workflow_keys():
    patch = ea.occurrence_patch({"occurrences": "x"}, NOW, "m", extra={
        "udt_dedupe": "other", "resolved_by": "manual", "notified": False, "port": "Gi1/0/3"})
    assert patch["occurrences"] == 2          # unreadable count -> treated as 1
    assert "udt_dedupe" not in patch and "resolved_by" not in patch and "notified" not in patch
    assert patch["port"] == "Gi1/0/3"


@pytest.mark.parametrize("meta, triggered, minutes_later, due", [
    ({}, NOW, 0.5, False),                                         # paged at trigger, inside cooldown
    ({}, NOW, 2, True),                                            # cooldown (60 s) passed
    ({"notified": False}, NOW, 0.1, True),                         # trigger never paged
    ({"notified": True, "last_notified_at": (NOW + timedelta(minutes=5)).isoformat()}, NOW, 5.5, False),
    ({"notified": True, "last_notified_at": "garbage"}, NOW, 5.5, True),
])
def test_repeat_page_due_follows_the_cooldown(meta, triggered, minutes_later, due):
    assert ea.repeat_page_due(meta, triggered, NOW + timedelta(minutes=minutes_later), 60) is due


def test_repeat_event_is_distinct_per_occurrence():
    assert ea.repeat_event({"occurrences": 3}) == "trigger:repeat:3"
    assert ea.repeat_event({"occurrences": 4}) != ea.repeat_event({"occurrences": 3})


# ── Unit: settings and closure reason ───────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    (None, {"enabled": True, "hours": 168}),
    ("not json", {"enabled": True, "hours": 168}),
    ('{"enabled": false, "hours": 24}', {"enabled": False, "hours": 24}),
    ({"enabled": True, "hours": 0}, {"enabled": True, "hours": 1}),
    ({"hours": 99999}, {"enabled": True, "hours": 8760}),
    ({"hours": True}, {"enabled": True, "hours": 168}),
    ({"hours": "72"}, {"enabled": True, "hours": 168}),
    ({"hours": 36.9, "bogus": 1}, {"enabled": True, "hours": 36}),
])
def test_clean_settings(raw, expected):
    assert expiry.clean_settings(raw) == expected


def test_policy_sentence_and_window_label():
    assert expiry.window_label(168) == "7 days" and expiry.window_label(1) == "1 hour"
    assert expiry.window_label(36) == "36 hours" and expiry.window_label(24) == "1 day"
    assert "after 7 days without a new event" in expiry.policy_sentence({"enabled": True, "hours": 168})
    assert expiry.policy_sentence({"enabled": False, "hours": 168}).startswith("Off:")


def test_expired_is_its_own_closure_reason():
    assert wf.closure_reason("resolved", {"resolved_by": "expired"}) == "expired"
    assert wf.closure_reason("active", {"resolved_by": "expired"}) is None
    assert wf.CLOSURE_LABELS["expired"] == "Expired — no new event within the window"
    assert "'expired' THEN 'expired'" in wf.closure_reason_sql("a")


def test_alert_detail_shows_occurrences_only_for_repeats():
    from app.api.v1.alerts import _evidence
    one = _evidence({"trap": True, **ea.first_metadata(NOW, "x")})
    assert not any(e["label"] == "Occurrences" for e in one)
    meta = {"trap": True, **ea.first_metadata(NOW, "x")}
    meta.update(ea.occurrence_patch(meta, NOW, "y"))
    labels = {e["label"]: e["value"] for e in _evidence(meta)}
    assert labels["Occurrences"] == "2" and labels["Last message"] == "y"


# ── PostgreSQL helpers ──────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


def add_device(db, name, ip):
    return str(db.scalar(
        "INSERT INTO devices (hostname, ip_address, status) VALUES ($1, $2::inet, 'up') RETURNING id", name, ip))


def add_rule(db, name, metric, *, operator="==", threshold=0, cooldown=60, **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", ${i + 7}" for i in range(len(cols)))
    return str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, cooldown"
        f"{names}) VALUES ($1, $2, $3, $4, 'warning', $5::jsonb, $6{vals}) RETURNING id",
        name, metric, operator, threshold, [CHANNEL], cooldown, *cols.values()))


def age(db, alert_id, minutes):
    """Move a row's trigger, last occurrence and last page back in time."""
    db.execute(
        "UPDATE alerts SET triggered_at = triggered_at - make_interval(mins => $2),"
        " metadata = metadata - 'last_notified_at' - 'last_seen' WHERE id = $1", alert_id, minutes)


@pytest.fixture
def world(pg_db):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    return pg_db


@pytest.fixture
def sent(monkeypatch):
    from app.services import host_alert_service
    calls = []

    async def dispatch(db_, channel_ids, ctx):
        calls.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)
    return calls


# ── PostgreSQL: traps ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_traps_update_one_open_row_and_page_per_cooldown(world, sent, monkeypatch):
    from app.api.v1 import alert_engine as engine
    from app.services import alert_funnel
    alert_funnel.reset_for_tests()
    monkeypatch.setattr(engine, "_dashboard_url", AsyncMock(return_value=""))
    db = world
    dev = add_device(db, "sw-01", "10.20.0.1")
    rule = add_rule(db, "Link down trap", "trap", trap_oid="1.3.6.1.6.3.1.1.5.3")

    async def trap(oid="1.3.6.1.6.3.1.1.5.3", msg="linkDown", device=dev, event_id=None):
        async with session(db) as s:
            return await engine.evaluate_trap(engine.TrapEvent(
                event_id=event_id or uuid4(), device_id=device, source_ip="10.20.0.1",
                trap_oid=oid, message=msg), s)

    assert (await trap(msg="linkDown ifIndex 3"))["alerts_created"] == 1
    e2 = uuid4()
    out = await trap(msg="linkDown ifIndex 5", event_id=e2)
    assert out["alerts_created"] == 0 and out["alerts_updated"] == 1
    # A retried delivery of the same event changes nothing.
    assert (await trap(msg="linkDown ifIndex 5", event_id=e2))["alerts_updated"] == 0
    rows = db.query("SELECT id, message, metadata FROM alerts WHERE rule_id = $1", rule)
    assert len(rows) == 1
    aid, message, meta = rows[0]
    assert message == "linkDown ifIndex 3"                  # the first event's message stays
    assert meta["occurrences"] == 2 and meta["last_message"] == "linkDown ifIndex 5"
    assert [c["delivery_event"] for c in sent] == ["trigger"]   # second trap inside the cooldown

    # After the cooldown the open row pages again, keyed per occurrence.
    age(db, aid, 10)
    await trap(msg="linkDown ifIndex 7")
    assert [c["delivery_event"] for c in sent] == ["trigger", "trigger:repeat:3"]
    assert all(c["alert_id"] == str(aid) for c in sent)
    meta = db.scalar("SELECT metadata FROM alerts WHERE id = $1", aid)
    assert meta["occurrences"] == 3 and meta["last_notified_at"]

    # Another trap OID is another condition.
    await trap(oid="1.3.6.1.6.3.1.1.5.3.9", msg="other")
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1", rule) == 2

    # Once the row is closed, the next trap raises a new incident.
    db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", aid)
    age(db, aid, 10)
    assert (await trap(msg="linkDown again"))["alerts_created"] == 1
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1 AND status = 'active'", rule) == 2


@pytest.mark.asyncio
async def test_traps_from_unknown_sources_merge_per_source_address(world, sent, monkeypatch):
    from app.api.v1 import alert_engine as engine
    from app.services import alert_funnel
    alert_funnel.reset_for_tests()
    monkeypatch.setattr(engine, "_dashboard_url", AsyncMock(return_value=""))
    db = world
    rule = add_rule(db, "Any trap", "trap")
    for src in ("192.0.2.1", "192.0.2.1", "192.0.2.2"):
        async with session(db) as s:
            await engine.evaluate_trap(engine.TrapEvent(event_id=uuid4(), source_ip=src, trap_oid="1.2.3"), s)
    rows = db.query("SELECT metadata->>'source_ip', (metadata->>'occurrences')::int FROM alerts"
                    " WHERE rule_id = $1 ORDER BY 1", rule)
    assert rows == [("192.0.2.1", 2), ("192.0.2.2", 1)]


# ── PostgreSQL: syslog ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_syslog_updates_one_open_row(world, monkeypatch):
    from app.api.v1 import alert_engine
    from app.services.network_events import ingest_syslog, parse_syslog
    from app.services import alert_funnel
    alert_funnel.reset_for_tests()
    monkeypatch.setattr(alert_engine, "_find_suppressing_dependency", AsyncMock(return_value=None))
    db = world
    add_device(db, "fw-01", "192.0.2.10")
    rule = add_rule(db, "Critical syslog", "syslog", operator="<=", threshold=4, target="link")
    sink = AsyncMock(return_value=1)

    async def log(msg):
        async with session(db) as s:
            return await ingest_syslog(s, parse_syslog(f"<34>Sep 10 12:00:00 fw {msg}".encode(), "192.0.2.10"),
                                       sink)

    assert (await log("link down port1"))["alerts_created"] == 1
    out = await log("link down port2")
    assert out["alerts_created"] == 0 and out["alerts_updated"] == 1
    assert sink.await_count == 1                              # inside the cooldown
    aid, meta = db.query("SELECT id, metadata FROM alerts WHERE rule_id = $1", rule)[0]
    assert meta["occurrences"] == 2 and meta["last_message"] == "link down port2"
    assert len(meta["recent_occurrences"]) == 2

    age(db, aid, 10)
    await log("link down port3")
    assert sink.await_count == 2
    ctx = sink.call_args.args[2]
    assert ctx["delivery_event"] == "trigger:repeat:3" and ctx["alert_id"] == str(aid)
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1", rule) == 1


# ── PostgreSQL: Device Tracker ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_rogue_sightings_update_one_open_row(world):
    from app.services import udt_alert_service as udt
    from app.services import alert_funnel
    alert_funnel.reset_for_tests()
    db = world
    sw = add_device(db, "sw-02", "10.30.0.2")
    rule = add_rule(db, "UDT Rogue Endpoint", "udt_rogue_endpoint", operator=">=", threshold=1, cooldown=300)
    ep = str(db.scalar("INSERT INTO udt_endpoints (mac, hostname, ip_address)"
                       " VALUES ('aa:bb:cc:00:00:01', 'laptop-7', '10.30.1.7') RETURNING id"))
    other = str(db.scalar("INSERT INTO udt_endpoints (mac) VALUES ('aa:bb:cc:00:00:02') RETURNING id"))

    async def run(*endpoints, kind="rogue_detected"):
        for e in endpoints:
            db.execute("INSERT INTO udt_events (event_type, endpoint_id, device_id) VALUES ($1, $2, $3)",
                       kind, e, sw)
        outbox: list = []
        async with session(db) as s:
            raised = await udt._process_events(s, await udt._udt_rules(s), outbox)
            await s.commit()
        return raised, outbox

    raised, outbox = await run(ep, ep, ep)
    assert raised == 1 and len(outbox) == 1
    rows = db.query("SELECT id, metadata FROM alerts WHERE rule_id = $1", rule)
    assert len(rows) == 1 and rows[0][1]["occurrences"] == 3
    aid = rows[0][0]

    # Cooldown over: the open row pages again (keyed per occurrence); a
    # different endpoint is its own condition.
    age(db, aid, 10)
    raised, outbox = await run(ep, other)
    assert raised == 1
    events = sorted(ctx["delivery_event"] for _ch, ctx in outbox)
    assert events == ["trigger", "trigger:repeat:4"]
    assert db.scalar("SELECT count(*) FROM alerts WHERE rule_id = $1", rule) == 2
    assert db.scalar("SELECT (metadata->>'occurrences')::int FROM alerts WHERE id = $1", aid) == 4


@pytest.mark.asyncio
async def test_device_tracker_pass_skips_while_another_worker_holds_it(world):
    from app.services import udt_alert_service as udt
    async with session(world) as holder, session(world) as s:
        await holder.execute(__import__("sqlalchemy").text(
            "SELECT pg_advisory_xact_lock(hashtextextended('zenplus:udt_event_pass', 0))"))
        assert await udt._process_events(s, [], []) == 0
        await holder.rollback()


# ── PostgreSQL: expiry ──────────────────────────────────────────────────────

def _row(db, *, meta, status="active", age_hours=200, device=None, rule=None, resolved=False):
    return str(db.scalar(
        "INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, resolved_at, metadata)"
        " VALUES ($1, $2, $3, 'warning', 'synthetic', now() - make_interval(hours => $4),"
        " CASE WHEN $5 THEN now() END, $6::jsonb) RETURNING id",
        device, rule, status, age_hours, resolved, meta))


@pytest.mark.asyncio
async def test_expiry_closes_only_stale_open_event_alerts(world):
    db = world
    dev = add_device(db, "sw-03", "10.40.0.3")
    rule = add_rule(db, "Trap", "trap")
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    ids = {
        "trap": _row(db, meta={"trap": True}, device=dev, rule=rule),
        "trap_acked": _row(db, meta={"trap": True}, status="acknowledged", device=dev, rule=rule),
        "syslog": _row(db, meta={"syslog_source": "10.40.0.3"}, device=dev, rule=rule),
        "rogue": _row(db, meta={"udt_dedupe": "rogue_detected:aa:bb", "event_type": "rogue_detected"}),
        "moved": _row(db, meta={"udt_dedupe": "endpoint_moved:aa:bb"}),
        "ncm_change": _row(db, meta={"ncm": True, "ncm_event": "config.changed"}, device=dev),
        # Not expired:
        "trap_recent_repeat": _row(db, meta={"trap": True, "last_seen": recent}, device=dev, rule=rule),
        "trap_young": _row(db, meta={"trap": True}, age_hours=3, device=dev, rule=rule),
        "capacity": _row(db, meta={"udt_dedupe": f"capacity:{dev}"}),
        "ncm_backup": _row(db, meta={"ncm": True, "ncm_event": "backup.failed"}, device=dev),
        "metric": _row(db, meta={"metric": "cpu"}, device=dev, rule=rule),
        "baseline": _row(db, meta={"baseline": True, "metric": "cpu"}, device=dev),
        "pending_status": _row(db, meta={"hold_until": recent}, status="pending", device=dev, rule=rule),
        "resolved_trap": _row(db, meta={"trap": True}, status="resolved", resolved=True, device=dev, rule=rule),
    }
    expected = {"trap", "trap_acked", "syslog", "rogue", "moved", "ncm_change"}
    async with session(db) as s:
        assert await expiry.sweep(s, settings={"enabled": True, "hours": 168}) == {"expired": len(expected)}
    rows = {str(r[0]): r for r in db.query(
        "SELECT id, status, metadata->>'resolved_by', resolved_at FROM alerts")}
    for name, aid in ids.items():
        status, by = rows[aid][1], rows[aid][2]
        if name in expected:
            assert (status, by) == ("resolved", "expired"), name
            assert rows[aid][3] is not None
        elif name != "resolved_trap":
            assert by is None and status != "resolved", name
    # Each expiry is a lifecycle event, read back as the "expired" closure.
    kinds = db.query("SELECT kind, source, old_status FROM alert_events WHERE alert_id = $1", ids["trap_acked"])
    assert kinds == [("expired", "system", "acknowledged")]
    reason = db.scalar(f"SELECT {wf.closure_reason_sql('a')} FROM alerts a WHERE a.id = $1", ids["trap"])
    assert reason == "expired"

    # A second pass finds nothing; turned off, nothing is selected at all.
    async with session(db) as s:
        assert await expiry.sweep(s, settings={"enabled": True, "hours": 168}) == {"expired": 0}
        assert await expiry.sweep(s, settings={"enabled": False, "hours": 1}) == {"expired": 0}
    # A shorter window reaches the younger row (but never the recent repeat).
    async with session(db) as s:
        assert await expiry.sweep(s, settings={"enabled": True, "hours": 2}) == {"expired": 1}
    assert db.scalar("SELECT metadata->>'resolved_by' FROM alerts WHERE id = $1", ids["trap_young"]) == "expired"


@pytest.mark.asyncio
async def test_expired_rows_send_no_all_clear(world, monkeypatch):
    """The escalation sweeper closes the escalation state of an expired row
    without announcing it."""
    from app.services import alert_escalation_service as esc
    db = world
    dev = add_device(db, "sw-04", "10.40.0.4")
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, cooldown,"
        " escalation_levels) VALUES ('Trap', 'trap', '==', 0, 'warning', $1::jsonb, 60,"
        " $2::jsonb) RETURNING id", [CHANNEL], [{"after_minutes": 5, "channels": [CHANNEL]}]))
    aid = _row(db, meta={"trap": True, "escalation": {"level": 1, "channels": [CHANNEL],
                                                      "last_at": "2026-01-01T00:00:00+00:00"}},
               device=dev, rule=rule)
    async with session(db) as s:
        assert (await expiry.sweep(s, settings={"enabled": True, "hours": 168}))["expired"] == 1
    clear = AsyncMock(return_value=1)
    monkeypatch.setattr(esc, "_dispatch_all_clear", clear)
    async with session(db) as s:
        await esc.sweep_legacy_escalations(s)
    clear.assert_not_awaited()
    assert db.scalar("SELECT metadata->'escalation'->>'closed' FROM alerts WHERE id = $1", aid) == "true"


@pytest.mark.asyncio
async def test_run_pass_is_rate_limited(world, monkeypatch):
    calls = []

    async def fake_locked(name, work, factory=None):
        calls.append(name)
        return {"expired": 0}

    monkeypatch.setattr("app.services.alert_worker_lock.run_pass", fake_locked)
    expiry.reset_for_tests()
    assert await expiry.run_pass() == {"expired": 0}
    assert await expiry.run_pass() is None                    # within the minute
    assert await expiry.run_pass(force=True) == {"expired": 0}
    assert calls == ["zenplus:alert_expiry_pass"] * 2
    expiry.reset_for_tests()


# ── PostgreSQL: API ─────────────────────────────────────────────────────────

def test_event_expiry_api_reads_saves_and_audits(world):
    db = world
    _row(db, meta={"trap": True})
    with api_client(db, role="expiry-viewer", permissions=["alerts.view"]) as c:
        r = c.get("/api/v1/alerts/event-expiry")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True and body["hours"] == 168 and body["due_now"] == 1
        assert "7 days" in body["summary"] and body["limits"] == {"hours": [1, 8760]}
        assert c.put("/api/v1/alerts/event-expiry", json={"enabled": False, "hours": 24}).status_code == 403
    with api_client(db, role="expiry-manager", permissions=["alerts.view", "alerts.manage"],
                    persist_user=True, username="noc-lead") as c:
        assert c.put("/api/v1/alerts/event-expiry", json={"enabled": True, "hours": 0}).status_code == 422
        r = c.put("/api/v1/alerts/event-expiry", json={"enabled": True, "hours": 24})
        assert r.status_code == 200 and r.json()["hours"] == 24 and "1 day" in r.json()["summary"]
        assert c.get("/api/v1/alerts/event-expiry").json()["hours"] == 24
    assert db.scalar("SELECT value FROM system_settings WHERE key = 'alert_event_expiry'") == {
        "enabled": True, "hours": 24}
    audit = db.query("SELECT action, metadata FROM audit_logs WHERE action = 'alert.event_expiry.update'")
    assert len(audit) == 1 and audit[0][1]["before"]["hours"] == 168 and audit[0][1]["after"]["hours"] == 24
