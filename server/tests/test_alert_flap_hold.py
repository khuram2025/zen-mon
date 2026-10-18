"""Device status alerts while the device is flapping: hold, not drop.

A transition whose class (reachability or performance) is flapping parks every
trigger it raises as a pending hold of at least the policy's flap stable time
(``flap_held``). The hold sweeper confirms it if the device still has that
status when the hold expires — a sustained outage still pages within the
stable time — and cancels it silently otherwise. Recoveries are unchanged: a
trigger paged before the flapping still gets its all-clear, a held trigger
recovers silently. ``notify_while_flapping`` opts a rule out.

The first half drives evaluate_status_change against the in-memory AlertsDB of
test_alert_status_into_up; the second runs the real SQL on the disposable
PostgreSQL of pg_fixture (never the appliance database).
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine
from app.api.v1.alert_engine import StatusChangeEvent, evaluate_status_change
from app.services import alert_hold_service

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_alert_status_into_up import DEVICE, AlertsDB, HoldDB, _pending, device_down_rule


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(alert_engine, "_device_in_maintenance", AsyncMock(return_value=False))
    monkeypatch.setattr(alert_engine, "_offline_sensor_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "_find_suppressing_dependency", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "get_configured_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", True)

    async def _run(db, old, new, **kw):
        event = StatusChangeEvent(device_id=DEVICE, hostname="sw", ip_address="10.0.0.1",
                                  old_status=old, new_status=new, **kw)
        return await evaluate_status_change(event, db=db)
    return _run


@pytest.fixture
def sent(monkeypatch):
    """Webhook dispatches of the status path, as (ctx) per accepted send."""
    calls: list[dict] = []

    async def dispatch(ch_type, cfg, ctx):
        calls.append(ctx)
        return True, None

    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", dispatch)
    monkeypatch.setattr(alert_engine, "decrypt_config", lambda cfg: cfg)
    monkeypatch.setattr(alert_engine.dl, "record", AsyncMock())
    return calls


CHANNEL = "22222222-2222-2222-2222-222222222222"


def _with_channel(db: AlertsDB) -> AlertsDB:
    db.channels[CHANNEL] = NS(name="noc", type="webhook", config={"url": "https://hook.invalid/x"},
                              gateway_id=None, enabled=True)
    return db


def _hold_seconds(row) -> float:
    until = datetime.fromisoformat(row["metadata"]["hold_until"])
    return (until - row["triggered_at"]).total_seconds()


# ── Event contract ───────────────────────────────────────────────────────────

def test_old_payloads_still_validate_and_are_not_flapping():
    event = StatusChangeEvent(device_id=DEVICE, hostname="sw", ip_address="10.0.0.1",
                              old_status="up", new_status="down")
    assert event.flapping is False and event.is_flapping is False
    event = StatusChangeEvent(device_id=DEVICE, hostname="sw", ip_address="10.0.0.1",
                              old_status="up", new_status="down", flapping=True, is_flapping=True)
    assert event.flapping and event.is_flapping


@pytest.mark.asyncio
async def test_each_evaluation_first_takes_the_device_lock(run):
    db = AlertsDB([device_down_rule()])
    await run(db, "up", "down")
    sql, params = db.statements[0]
    assert sql == "SELECT pg_advisory_xact_lock(hashtext(:device_id))"
    assert params == {"device_id": DEVICE}


# ── Hold while flapping ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_flapping_trigger_is_held_for_the_stable_time(run, sent):
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL])
    db = _with_channel(AlertsDB([rule]))
    result = await run(db, "up", "down", flapping=True, is_flapping=True)
    [row] = db.alerts
    assert row["status"] == "pending"
    meta = row["metadata"]
    assert meta["flap_held"] is True and meta["flap_kind"] == "reachability"
    assert meta["notified"] is False
    assert _hold_seconds(row) == 600          # default flap_stable_minutes 10
    assert result["flap_held_alerts"] == 1
    assert sent == []                         # nothing dispatched while held
    # The flags are part of the INSERT itself, never a second UPDATE.
    inserts = [p for s, p in db.statements if s.startswith("INSERT INTO alerts")]
    assert json.loads(inserts[0]["metadata"])["flap_held"] is True
    assert not any(s.startswith("UPDATE alerts SET metadata") for s, _ in db.statements)


@pytest.mark.asyncio
async def test_the_hold_is_the_longer_of_min_duration_and_the_stable_time(run):
    db = AlertsDB([device_down_rule(trigger_on="down", min_duration=1800)])
    db.monitoring = {"flap_stable_minutes": 5}
    await run(db, "up", "down", flapping=True)
    assert _hold_seconds(db.alerts[0]) == 1800

    db = AlertsDB([device_down_rule(trigger_on="down", min_duration=60)])
    db.monitoring = {"flap_stable_minutes": 5}
    await run(db, "up", "down", flapping=True)
    assert _hold_seconds(db.alerts[0]) == 300


@pytest.mark.asyncio
async def test_a_performance_flap_holds_the_degraded_trigger(run):
    db = AlertsDB([device_down_rule(name="Degraded", trigger_on="degraded")])
    await run(db, "up", "degraded", flapping=True)
    [row] = db.alerts
    assert row["status"] == "pending" and row["metadata"]["flap_kind"] == "performance"


@pytest.mark.asyncio
async def test_notify_while_flapping_rules_page_at_once(run, sent):
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL], notify_while_flapping=True)
    db = _with_channel(AlertsDB([rule]))
    await run(db, "up", "down", flapping=True, is_flapping=True)
    [row] = db.alerts
    assert row["status"] == "active" and "flap_held" not in row["metadata"]
    assert row["metadata"]["notified"] is True
    assert [c["event_type"] for c in sent] == ["trigger"]


@pytest.mark.asyncio
async def test_a_transition_whose_own_class_is_not_flapping_is_not_held(run, sent):
    """A device flapping between up and degraded still pages a DOWN at once:
    only the transition's own class holds its triggers."""
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL])
    db = _with_channel(AlertsDB([rule]))
    await run(db, "degraded", "down", flapping=False, is_flapping=True)
    [row] = db.alerts
    assert row["status"] == "active" and row["metadata"]["notified"] is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_hold_while_flapping_needs_migrate_103_like_any_hold(run, monkeypatch, sent):
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", False)
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL])
    db = _with_channel(AlertsDB([rule]))
    await run(db, "up", "down", flapping=True)
    [row] = db.alerts
    assert row["status"] == "active" and "flap_held" not in row["metadata"]


# ── Recoveries follow their trigger's fate ───────────────────────────────────

@pytest.mark.asyncio
async def test_a_trigger_paged_before_flapping_still_gets_its_all_clear(run, sent):
    """The recovery that starts the flapping (or happens during it) closes a
    DOWN that was paged: its all-clear is the message the operator waits for."""
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL])
    db = _with_channel(AlertsDB([rule]))
    down = db.seed(rule, new_status="down", notified=True)
    await run(db, "down", "up", flapping=True, is_flapping=True)
    assert down["status"] == "resolved" and "hold_cancelled" not in down["metadata"]
    [recovery] = [a for a in db.alerts if a["metadata"].get("is_recovery")]
    assert recovery["status"] == "resolved"
    assert [c["event_type"] for c in sent] == ["recovery"]


@pytest.mark.asyncio
async def test_a_held_trigger_recovers_silently_as_a_cancelled_hold(run, sent):
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL])
    db = _with_channel(AlertsDB([rule]))
    await run(db, "up", "down", flapping=True, is_flapping=True)
    [held] = db.alerts
    await run(db, "down", "up", flapping=True, is_flapping=True)
    assert held["status"] == "resolved"
    assert held["metadata"]["hold_cancelled"] is True     # a notice, not an incident
    assert sent == []                                     # no trigger, no all-clear
    # Nothing became an alert, so nothing recovered: no reset row per cycle.
    assert db.alerts == [held]


@pytest.mark.asyncio
async def test_only_the_paged_rule_recovers_when_another_rule_was_held(run, sent):
    """Two rules, one recovery: the rule whose DOWN was paged before the
    flapping sends its all-clear, the rule whose DOWN is still held recovers
    without a row or a message."""
    paged = device_down_rule(name="NOC down", trigger_on="down", notify_channels=[CHANNEL])
    quiet = device_down_rule(name="Ops down", trigger_on="down", notify_channels=[CHANNEL])
    db = _with_channel(AlertsDB([paged, quiet]))
    down = db.seed(paged, new_status="down", notified=True, minutes_ago=20)
    held = db.seed(quiet, status="pending", new_status="down", notified=False, flap_held=True,
                   hold_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    await run(db, "down", "up", flapping=True, is_flapping=True)
    assert down["metadata"]["resolved_by_recovery"] is True and "hold_cancelled" not in down["metadata"]
    assert held["status"] == "resolved" and held["metadata"]["hold_cancelled"] is True
    recoveries = [a for a in db.alerts if a["metadata"].get("is_recovery")]
    assert [str(a["rule_id"]) for a in recoveries] == [str(paged.id)]
    assert [(c["event_type"], c["rule_id"]) for c in sent] == [("recovery", str(paged.id))]


def _loss_rule(**kw):
    """The seeded 'Packet Loss' rule (any, packet_loss > 5): it matches a
    move into degraded and a move into down alike."""
    return device_down_rule(name="Packet Loss", trigger_on="any", metric="packet_loss",
                            operator="gt", threshold=5, notify_channels=[CHANNEL], **kw)


@pytest.mark.asyncio
async def test_a_paged_degraded_followed_by_a_held_down_still_gets_its_all_clear(run, sent):
    """Default policy: up->degraded pages (latency/loss not flapping), the
    DOWN after it is held (up/down flapping), and the device is back inside
    the hold. The DEGRADED that was paged must get its recovery."""
    db = _with_channel(AlertsDB([_loss_rule()]))
    await run(db, "up", "degraded", packet_loss=0.33, is_flapping=True)
    [paged] = db.alerts
    assert paged["status"] == "active" and paged["metadata"]["notified"] is True
    await run(db, "degraded", "down", packet_loss=1.0, flapping=True, is_flapping=True)
    held = db.alerts[-1]
    assert held["status"] == "pending" and held["metadata"]["flap_held"] is True
    assert paged["status"] == "active"          # a hold supersedes nothing that was sent
    await run(db, "down", "up", flapping=True, is_flapping=True)
    assert paged["status"] == "resolved" and paged["metadata"]["resolved_by_recovery"] is True
    assert held["status"] == "resolved" and held["metadata"]["hold_cancelled"] is True
    assert [c["event_type"] for c in sent] == ["trigger", "recovery"]
    [recovery] = [a for a in db.alerts if a["metadata"].get("is_recovery")]
    assert recovery["rule_id"] == paged["rule_id"]
    assert sent[1]["alert_id"] == str(paged["id"])        # the all-clear is about the page


@pytest.mark.asyncio
async def test_a_paged_down_followed_by_a_held_degraded_still_gets_its_all_clear(run, sent):
    db = _with_channel(AlertsDB([_loss_rule()]))
    await run(db, "up", "down", packet_loss=1.0)
    [paged] = db.alerts
    await run(db, "down", "degraded", packet_loss=0.33, flapping=True, is_flapping=True)
    assert db.alerts[-1]["status"] == "pending" and paged["status"] == "active"
    await run(db, "degraded", "up", flapping=True, is_flapping=True)
    assert paged["metadata"]["resolved_by_recovery"] is True
    assert [c["event_type"] for c in sent] == ["trigger", "recovery"]


@pytest.mark.asyncio
async def test_a_new_hold_still_replaces_an_older_hold(run):
    db = AlertsDB([_loss_rule()])
    await run(db, "up", "down", packet_loss=1.0, flapping=True)
    first = db.alerts[-1]
    await run(db, "down", "degraded", packet_loss=0.33, flapping=True)
    second = db.alerts[-1]
    assert first["status"] == "resolved" and first["metadata"]["hold_cancelled"] is True
    assert first["metadata"]["superseded_by_status"] == "degraded"
    assert second["status"] == "pending"


@pytest.mark.asyncio
async def test_a_trigger_that_is_not_held_still_supersedes_a_paged_one(run):
    db = AlertsDB([_loss_rule()])
    await run(db, "up", "down", packet_loss=1.0)
    paged = db.alerts[-1]
    await run(db, "down", "degraded", packet_loss=0.33)
    assert paged["status"] == "resolved"
    assert paged["metadata"]["resolved_by"] == "status_transition"
    assert db.alerts[-1]["status"] == "active"


@pytest.mark.asyncio
async def test_a_superseded_hold_is_a_cancelled_hold(run):
    rule = device_down_rule(trigger_on="any")             # 'any' rule: ping_status == 0
    db = AlertsDB([rule])
    pending = db.seed(rule, status="pending", new_status="down", notified=False,
                      hold_until=datetime.now(timezone.utc).isoformat())
    active = db.seed(rule, status="active", new_status="degraded")
    await run(db, "degraded", "down")
    assert pending["status"] == active["status"] == "resolved"
    assert pending["metadata"]["hold_cancelled"] is True
    assert "hold_cancelled" not in active["metadata"]


# ── Cooldown never matches a notice row ──────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_is_never_started_by_a_flap_notice(run, sent):
    """A sent "stopped flapping" notice has no rule: the first real DOWN after
    it must page, whatever the rule's cooldown."""
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL], cooldown=300)
    db = _with_channel(AlertsDB([rule]))
    db.seed(None, status="resolved", new_status="up", minutes_ago=1, notified=True,
            flap_notice=True, flap_event="stop")
    await run(db, "up", "down")
    row = db.alerts[-1]
    assert row["status"] == "active" and row["metadata"]["notified"] is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_cooldown_still_applies_to_the_rules_own_trigger(run, sent):
    rule = device_down_rule(trigger_on="down", notify_channels=[CHANNEL], cooldown=300)
    db = _with_channel(AlertsDB([rule]))
    db.seed(rule, status="resolved", new_status="down", minutes_ago=1, notified=True)
    await run(db, "up", "down")
    assert db.alerts[-1]["metadata"]["notified"] is False
    assert sent == []


# ── Map webhooks ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_map_webhooks_still_fire_and_carry_is_flapping(run, monkeypatch):
    posts = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            posts.append(json)

    monkeypatch.setattr(alert_engine, "NotificationHTTPClient", Client)
    db = AlertsDB([])
    db.map_hooks = [NS(id=uuid4(), name="core", webhook_url="https://map.invalid/hook")]
    await run(db, "up", "degraded", flapping=True, is_flapping=True)
    [payload] = posts
    assert payload["is_flapping"] is True and payload["new_status"] == "degraded"


# ── Hold sweeper ─────────────────────────────────────────────────────────────

@pytest.fixture
def sweeper(monkeypatch):
    monkeypatch.setattr(alert_hold_service, "get_configured_timezone", AsyncMock(return_value="UTC"))
    dispatch = AsyncMock(return_value=1)
    monkeypatch.setattr(alert_hold_service, "_dispatch_confirmed", dispatch)
    return dispatch


def _flap_pending(**kw):
    row = _pending(trigger_on="down", **kw)
    row.metadata.update({"flap_held": True, "flap_kind": "reachability", "notified": False})
    return row


@pytest.mark.asyncio
async def test_a_flap_hold_is_confirmed_when_the_status_lasted(sweeper):
    db = HoldDB(_flap_pending(new_status="down", device_status="down"))
    out = await alert_hold_service.sweep_pending_holds(db)
    assert out["confirmed"] == 1
    status, extra = db.settled[0]
    assert status == "active"
    # Escalation counts from here, not from the transition.
    assert datetime.fromisoformat(extra["notified_at"]) >= datetime.now(timezone.utc) - timedelta(minutes=1)
    sweeper.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_flap_hold_is_cancelled_silently_when_the_status_changed(sweeper):
    db = HoldDB(_flap_pending(new_status="down", device_status="up"))
    out = await alert_hold_service.sweep_pending_holds(db)
    assert out["cancelled"] == 1 and out["confirmed"] == 0
    assert db.settled[0][0] == "resolved" and db.settled[0][1]["hold_cancelled"] is True
    sweeper.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_confirmed_hold_stamps_notified_at(sweeper):
    db = HoldDB(_pending(new_status="down", device_status="down"))
    await alert_hold_service.sweep_pending_holds(db)
    assert "notified_at" in db.settled[0][1]


class _LoggingHoldDB(HoldDB):
    def __init__(self, row):
        super().__init__(row)
        self.sql: list[str] = []

    async def execute(self, statement, params=None):
        self.sql.append(" ".join(str(statement).split()))
        return await super().execute(statement, params)

    def index(self, fragment):
        return next(i for i, s in enumerate(self.sql) if fragment in s)


_LOCK = "pg_advisory_xact_lock(hashtext(:device_id))"
_SETTLE = "SET status = :status"
_SUPERSEDE = "UPDATE alerts SET status = 'resolved'"


@pytest.mark.asyncio
async def test_confirming_a_device_hold_supersedes_under_the_device_lock(sweeper):
    """The engine leaves a sent trigger open while a newer one is held; the
    confirmation supersedes it, holding the engine's per-device lock first."""
    db = _LoggingHoldDB(_flap_pending(new_status="down", device_status="down"))
    await alert_hold_service.sweep_pending_holds(db)
    assert db.index(_LOCK) < db.index(_SETTLE) < db.index(_SUPERSEDE)
    supersede = db.sql[db.index(_SUPERSEDE)]
    assert "WHERE status IN ('active', 'acknowledged')" in supersede and "id <> :id" in supersede


@pytest.mark.asyncio
async def test_a_cancelled_hold_or_a_service_hold_supersedes_nothing(sweeper):
    db = _LoggingHoldDB(_flap_pending(new_status="down", device_status="up"))
    await alert_hold_service.sweep_pending_holds(db)
    assert not any(_SUPERSEDE in s for s in db.sql)
    row = _pending(new_status="down", device_status=None)
    row.service_check_id, row.check_status = uuid4(), "down"
    db = _LoggingHoldDB(row)
    assert (await alert_hold_service.sweep_pending_holds(db))["confirmed"] == 1
    assert not any(_SUPERSEDE in s or _LOCK in s for s in db.sql)


@pytest.mark.asyncio
async def test_a_confirmed_flap_hold_says_why_it_waited(monkeypatch):
    captured = {}

    async def dispatch(db, channels, ctx):
        captured.update(ctx)
        return 1

    monkeypatch.setattr("app.services.host_alert_service.dispatch_to_channels", dispatch)
    monkeypatch.setattr(alert_engine, "_dashboard_url", AsyncMock(return_value=""))
    row = _flap_pending(new_status="down", device_status="down")
    await alert_hold_service._dispatch_confirmed(None, row, datetime.now(timezone.utc))
    assert captured["notice"].startswith("Flapping: this device kept changing status")
    assert "DOWN" in captured["notice"]


# ══ The real SQL, on a disposable PostgreSQL ═════════════════════════════════

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.fixture
def world(pg_db, monkeypatch):
    """One device and one Device Down rule (no channels: nothing is sent)."""
    monkeypatch.setattr(alert_engine, "_NOTIFY_WHILE_FLAPPING_SUPPORTED", False)
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", None)
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules")
    device = pg_db.scalar("INSERT INTO devices (hostname, ip_address, status) "
                          "VALUES ('flappy', '10.77.0.1', 'up') RETURNING id")
    rule = pg_db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, trigger_on,"
                        " recovery_alert, cooldown, min_duration) VALUES ('Device Down', 'ping_status',"
                        " '==', 0, 'down', true, 300, 0) RETURNING id")
    pg_db.execute("INSERT INTO system_settings (key, value)"
                  " VALUES ('monitoring', '{\"flap_stable_minutes\": 4}'::jsonb)"
                  " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    return NS(db=pg_db, device=str(device), rule=str(rule))


async def _evaluate(w, old, new, **kw):
    async with session(w.db) as s:
        return await evaluate_status_change(StatusChangeEvent(
            device_id=w.device, hostname="flappy", ip_address="10.77.0.1",
            old_status=old, new_status=new, **kw), db=s)


def _rows(w):
    return w.db.query("SELECT status, metadata, triggered_at FROM alerts WHERE device_id = $1"
                      " ORDER BY triggered_at, id", w.device)


@pytest.mark.asyncio
async def test_postgres_flap_hold_confirms_a_sustained_outage(world):
    w = world
    await _evaluate(w, "up", "down", flapping=True, is_flapping=True)
    [(status, meta, triggered)] = _rows(w)
    assert status == "pending" and meta["flap_held"] is True and meta["flap_kind"] == "reachability"
    hold = datetime.fromisoformat(meta["hold_until"]) - triggered
    assert abs(hold.total_seconds() - 240) < 1             # the stored flap_stable_minutes

    # The device stays DOWN past the hold: the sweeper confirms it.
    w.db.execute("UPDATE devices SET status = 'down' WHERE id = $1", w.device)
    w.db.execute("UPDATE alerts SET metadata = metadata || jsonb_build_object('hold_until', now()::text)")
    async with session(w.db) as s:
        out = await alert_hold_service.sweep_pending_holds(s)
    assert out["confirmed"] == 1
    [(status, meta, _)] = _rows(w)
    assert status == "active" and meta["notified"] is True and meta["notified_at"]


def _loss_rule_world(w):
    """The world's rule as the seeded 'Packet Loss' (any, packet_loss > 5),
    without cooldown: it matches degraded and down alike."""
    w.db.execute("UPDATE alert_rules SET trigger_on = 'any', metric = 'packet_loss',"
                 " operator = 'gt', threshold = 5, cooldown = 0 WHERE id = $1", w.rule)


def _by_status(w):
    return {m["new_status"]: (s, m) for s, m, _ in _rows(w) if not m.get("is_recovery")}


@pytest.mark.asyncio
async def test_postgres_a_confirmed_hold_supersedes_the_trigger_sent_before_it(world):
    w = world
    _loss_rule_world(w)
    await _evaluate(w, "up", "degraded", packet_loss=0.33)
    await _evaluate(w, "degraded", "down", packet_loss=1.0, flapping=True, is_flapping=True)
    rows = _by_status(w)
    assert rows["degraded"][0] == "active" and rows["down"][0] == "pending"

    w.db.execute("UPDATE devices SET status = 'down' WHERE id = $1", w.device)
    w.db.execute("UPDATE alerts SET metadata = metadata || jsonb_build_object('hold_until', now()::text)"
                 " WHERE status = 'pending'")
    async with session(w.db) as s:
        out = await alert_hold_service.sweep_pending_holds(s)
    assert out["confirmed"] == 1
    rows = _by_status(w)
    assert rows["down"][0] == "active"
    status, meta = rows["degraded"]
    assert status == "resolved" and meta["resolved_by"] == "status_transition"
    assert meta["superseded_by_status"] == "down" and "hold_cancelled" not in meta


@pytest.mark.asyncio
async def test_postgres_a_trigger_sent_before_a_cancelled_hold_gets_its_recovery(world):
    w = world
    _loss_rule_world(w)
    await _evaluate(w, "up", "degraded", packet_loss=0.33)
    await _evaluate(w, "degraded", "down", packet_loss=1.0, flapping=True, is_flapping=True)
    await _evaluate(w, "down", "up", flapping=True, is_flapping=True)
    rows = _rows(w)
    assert all(s == "resolved" for s, _, _ in rows)
    by = {("recovery" if m.get("is_recovery") else m["new_status"]): m for _, m, _ in rows}
    assert by["degraded"]["resolved_by_recovery"] is True and "hold_cancelled" not in by["degraded"]
    assert by["down"]["hold_cancelled"] is True
    assert "recovery" in by                                 # the all-clear for the page


@pytest.mark.asyncio
async def test_postgres_flap_hold_is_cancelled_by_the_next_up(world):
    w = world
    await _evaluate(w, "up", "down", flapping=True, is_flapping=True)
    await _evaluate(w, "down", "up", flapping=True, is_flapping=True)
    rows = _rows(w)
    held = [m for s, m, _ in rows if m.get("flap_held")]
    assert held and held[0]["hold_cancelled"] is True
    assert all(s == "resolved" for s, _, _ in rows)
    # Notices only: nothing here is an incident.
    from app.services.alert_workflow import notice_sql
    assert w.db.scalar(f"SELECT count(*) FROM alerts a WHERE a.device_id = $1 AND NOT {notice_sql('a')}",
                       w.device) == 0


@pytest.mark.asyncio
async def test_postgres_alert_center_shows_the_hold_and_keeps_notices_out(world):
    """A held trigger is pending (not an active incident) and says why and
    until when; a flap notice row is in the notices population only."""
    from app.api.v1 import alerts as alerts_api
    from app.services import alert_service as svc

    w = world
    await _evaluate(w, "up", "down", flapping=True, is_flapping=True)
    w.db.execute("INSERT INTO alerts (device_id, rule_id, status, severity, message, resolved_at, metadata)"
                 " VALUES ($1, NULL, 'resolved', 'info', 'flappy is flapping', now(),"
                 " '{\"flap_notice\": true, \"flap_event\": \"start\", \"notified\": true}')", w.device)
    async with session(w.db) as s:
        summary = await svc.summary(s, svc.AlertFilters(view="open"), window_hours=24)
        pending, _ = await svc.list_rows(s, svc.AlertFilters(view="pending"))
        ctx = await alerts_api._enrich(s, pending, None)
        [item] = [alerts_api.serialize(r, ctx) for r in pending]
        _, incidents = await svc.list_rows(s, svc.AlertFilters(view="all"))
        notices, n_notices = await svc.list_rows(s, svc.AlertFilters(view="all", population="notices"))
        nctx = await alerts_api._enrich(s, notices, None)
        [notice] = [alerts_api.serialize(r, nctx) for r in notices]

    assert summary["open"]["pending"] == 1 and summary["open"]["flap_held"] == 1
    assert summary["open"]["active"] == 0
    assert item["status"] == "pending" and item["flap_held"] is True
    assert item["flap_kind"] == "reachability"
    assert item["hold_until"] == item["metadata"]["hold_until"]
    assert incidents == 1 and n_notices == 1
    assert notice["is_notice"] is True and notice["notice_kind"] == "flapping"
    assert notice["flap_held"] is False and notice["hold_until"] is None
    assert notice["closure"]["reason"] == "flapping"


@pytest.mark.asyncio
async def test_postgres_notify_while_flapping_is_read_from_the_rule(world):
    w = world
    w.db.execute("UPDATE alert_rules SET notify_while_flapping = true WHERE id = $1", w.rule)
    await _evaluate(w, "up", "down", flapping=True, is_flapping=True)
    [(status, meta, _)] = _rows(w)
    assert status == "active" and "flap_held" not in meta
    assert alert_engine._NOTIFY_WHILE_FLAPPING_SUPPORTED is True


@pytest.mark.asyncio
async def test_postgres_cooldown_ignores_notice_rows(world):
    w = world
    w.db.execute("INSERT INTO alerts (device_id, rule_id, status, severity, message, resolved_at, metadata)"
                 " VALUES ($1, NULL, 'resolved', 'info', 'stopped flapping', now(),"
                 " '{\"flap_notice\": true, \"flap_event\": \"stop\", \"notified\": true}')", w.device)
    rule = NS(id=w.rule, cooldown=300)
    async with session(w.db) as s:
        assert await alert_engine._within_cooldown(s, rule, device_id=w.device) is False


@pytest.mark.asyncio
async def test_postgres_rule_api_round_trips_notify_while_flapping(world, monkeypatch):
    from uuid import UUID

    from app.api.v1 import alert_rules as api

    monkeypatch.setattr(api.rs, "guard_rule_change", AsyncMock())
    monkeypatch.setattr(api.rs, "affected", AsyncMock(return_value={}))
    monkeypatch.setattr(api, "write_audit_log", AsyncMock())
    user = NS(id=None, role="admin")
    body = dict(name="Core router down", metric="ping_status", operator="==", threshold=0,
                trigger_on="down")
    async with session(world.db) as s:
        plain = await api.create_alert_rule(api.AlertRuleCreate(**body), db=s, user=user)
    async with session(world.db) as s:
        core = await api.create_alert_rule(
            api.AlertRuleCreate(**body, notify_while_flapping=True), db=s, user=user)
    assert plain["notify_while_flapping"] is False and core["notify_while_flapping"] is True

    async with session(world.db) as s:
        off = await api.update_alert_rule(UUID(core["id"]),
                                          api.AlertRuleUpdate(notify_while_flapping=False),
                                          db=s, user=user)
    assert off["notify_while_flapping"] is False
    async with session(world.db) as s:
        # An explicit null leaves the NOT NULL column alone.
        kept = await api.update_alert_rule(UUID(core["id"]),
                                           api.AlertRuleUpdate(name="Core", notify_while_flapping=None),
                                           db=s, user=user)
        listed = await api.get_alert_rule(UUID(core["id"]), db=s, user=user)
    assert kept["name"] == "Core" and kept["notify_while_flapping"] is False
    assert listed["notify_while_flapping"] is False


@pytest.mark.asyncio
async def test_postgres_the_hold_sweep_runs_before_the_priority_migration(world):
    """Code updated before migrate-164: alert_rules.priority does not exist
    yet, and held alerts must still be confirmed."""
    from app.services import alert_funnel

    w = world
    await _evaluate(w, "up", "down", flapping=True, is_flapping=True)
    w.db.execute("ALTER TABLE alert_rules DROP COLUMN priority CASCADE")
    w.db.execute("UPDATE devices SET status = 'down' WHERE id = $1", w.device)
    w.db.execute("UPDATE alerts SET metadata = metadata || jsonb_build_object('hold_until', now()::text)")
    alert_funnel.reset_for_tests()
    try:
        async with session(w.db) as s:
            out = await alert_hold_service.sweep_pending_holds(s)
    finally:
        alert_funnel.reset_for_tests()
    assert out["confirmed"] == 1
    [(status, meta, _)] = _rows(w)
    assert status == "active" and meta["notified"] is True
