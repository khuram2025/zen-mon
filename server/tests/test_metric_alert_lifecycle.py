"""Metric incident lifecycle (NOTIFY 01): one incident per rule, asset and
dimension, with a breach hold, a clear threshold and a stable clear.

The field report: a server whose memory hovered between 90.1% and 91.5%
against a 90% rule sent 121 "rose above" mails in 48 hours. The host evaluator
compared a 5-minute average once per minute and resolved on the first sample
at or below the threshold, so every dip opened a new incident and a new mail.

Now (network_conditions.evaluate, shared by the host and SNMP evaluators):

* min_duration is a real persistence hold on per-sample history;
* an open incident clears only below its reset threshold — the explicit one,
  or 2 points on the healthy side for percent metrics — and only after that
  held for clear_duration (NULL = 300 s, 0 = immediately);
* missing or stale samples never resolve and never count as clear time;
* a re-breach during the clear hold continues the same incident silently.

The contract is pinned on the pure function first, then replayed through the
real host evaluator against a disposable PostgreSQL with synthetic server ids
and a fake ClickHouse history.
"""
from __future__ import annotations

import bisect
import asyncio
import contextlib
import random
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_notify_state as ns
from app.services import host_alert_service as has
from app.services.network_conditions import (
    AUTO_CLEAR_POINTS, DEFAULT_CLEAR_S, auto_reset_threshold, clear_hold_seconds,
    effective_reset_threshold, evaluate,
)

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = 1_800_000_000.0   # synthetic epoch seconds; nothing reads the wall clock


def memory(operator=">", threshold=90, **extra):
    return [dict(metric="host_memory_pct", operator=operator, threshold=threshold, **extra)]


def series(start, values, step=60):
    return [(start + i * step, v) for i, v in enumerate(values)]


# ══ The contract ════════════════════════════════════════════════════════════

def test_clear_duration_null_is_the_default_and_zero_is_immediate():
    assert clear_hold_seconds(None) == DEFAULT_CLEAR_S == 300
    assert clear_hold_seconds(0) == 0
    assert clear_hold_seconds(900) == 900


def test_min_duration_is_a_persistence_hold_on_samples_not_an_average():
    # 5 breaching minutes after one healthy one: the hold starts at the first
    # breaching sample and completes 300 s later — not a 5-minute mean.
    h = {"host_memory_pct": series(T0, [80, 95, 95, 95, 95, 95, 95])}
    early = evaluate(memory(), "AND", h, now=T0 + 300, hold=300, max_gap=300)
    assert early.breach is None and early.reason == "pending_hold"
    assert evaluate(memory(), "AND", h, now=T0 + 360, hold=300, max_gap=300).breach is True
    # One dip restarts the hold: a mean of 93% is not "above 90 for 5 minutes".
    dip = {"host_memory_pct": series(T0, [95, 95, 95, 89, 95, 95])}
    assert evaluate(memory(), "AND", dip, now=T0 + 300, hold=300, max_gap=300).breach is None


def test_an_open_incident_resolves_only_after_the_clear_hold():
    h = {"host_memory_pct": series(T0, [95, 80, 80, 80, 80, 80, 80])}
    pending = evaluate(memory(), "AND", h, now=T0 + 290, max_gap=300, active=True, clear_hold=300)
    assert pending.breach is None and pending.reason == "pending_clear"
    assert pending.cleared_seconds == 230          # healthy since the 80 at T0+60
    done = evaluate(memory(), "AND", h, now=T0 + 360, max_gap=300, active=True, clear_hold=300)
    assert done.breach is False and done.cleared_seconds == 300
    # clear_duration 0: the first healthy sample resolves.
    assert evaluate(memory(), "AND", h, now=T0 + 60, max_gap=300, active=True,
                    clear_hold=0).breach is False


def test_a_rebreach_during_the_clear_hold_continues_the_incident():
    h = {"host_memory_pct": series(T0, [95, 80, 80, 80, 92])}
    out = evaluate(memory(), "AND", h, now=T0 + 250, max_gap=300, active=True, clear_hold=300)
    assert out.breach is True                      # same incident, still breaching
    # ... and the clear clock starts again from the next healthy sample.
    h["host_memory_pct"] += series(T0 + 300, [80, 80])
    out = evaluate(memory(), "AND", h, now=T0 + 420, max_gap=300, active=True, clear_hold=300)
    assert out.breach is None and out.cleared_seconds == 120


def test_missing_samples_never_resolve_or_count_as_clear_time():
    # Healthy for 4 minutes, a 10-minute collection gap, healthy again.
    h = {"host_memory_pct": series(T0, [95, 80, 80, 80, 80]) + series(T0 + 900, [80, 80])}
    out = evaluate(memory(), "AND", h, now=T0 + 970, max_gap=300, active=True, clear_hold=300)
    assert out.breach is None and out.reason == "pending_clear"
    assert out.cleared_seconds == 70               # restarted after the gap
    # Nothing at all for an hour: unknown, the incident stays open.
    stale = evaluate(memory(), "AND", {"host_memory_pct": series(T0, [80])}, now=T0 + 3600,
                     max_gap=300, active=True, clear_hold=0)
    assert stale.breach is None and stale.reason == "no_data"
    for bad in (None, float("nan")):
        out = evaluate(memory(), "AND", {"host_memory_pct": series(T0, [80, bad])},
                       now=T0 + 60, max_gap=300, active=True, clear_hold=0)
        assert out.breach is None


def test_percent_metrics_clear_two_points_on_the_healthy_side_by_default():
    assert AUTO_CLEAR_POINTS == 2
    for metric in ("cpu", "memory", "host_cpu_pct", "host_memory_pct",
                   "host_filesystem_pct", "host_disk_util_pct"):
        assert auto_reset_threshold(dict(metric=metric, operator=">", threshold=90)) == 88
        assert auto_reset_threshold(dict(metric=metric, operator="gte", threshold=90)) == 88
        assert auto_reset_threshold(dict(metric=metric, operator="<", threshold=10)) == 12
    # Not for counters, equality or an explicit reset; never outside 0..100.
    assert auto_reset_threshold(dict(metric="if_errors", operator=">", threshold=100)) is None
    assert auto_reset_threshold(dict(metric="cpu", operator="==", threshold=90)) is None
    assert auto_reset_threshold(dict(metric="cpu", operator=">", threshold=1)) is None
    assert auto_reset_threshold(dict(metric="cpu", operator="<", threshold=99)) is None
    explicit = dict(metric="cpu", operator=">", threshold=90, reset_threshold=70)
    assert auto_reset_threshold(explicit) is None and effective_reset_threshold(explicit) == 70
    # A new incident still needs the configured threshold: 89 does not fire.
    h = {"host_memory_pct": series(T0, [89])}
    assert evaluate(memory(), "AND", h, now=T0, max_gap=300).breach is False
    # An open one does not clear at 89 (above 88) but does at 87.9.
    assert evaluate(memory(), "AND", h, now=T0, max_gap=300, active=True).breach is True
    h = {"host_memory_pct": series(T0, [87.9])}
    assert evaluate(memory(), "AND", h, now=T0, max_gap=300, active=True).breach is False


# ══ The host evaluator on a disposable PostgreSQL ═══════════════════════════

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


class History:
    """Fake ClickHouse: {metric: {server: {dimension: [(t, v)]}}}, read by window."""

    def __init__(self):
        self.data: dict = {}

    def add(self, metric, sid, points, dim=""):
        self.data.setdefault(metric, {}).setdefault(sid, {}).setdefault(dim, []).extend(points)

    def fetch(self, metric, since, until):
        out = {}
        for sid, dims in self.data.get(metric, {}).items():
            for dim, pts in dims.items():
                stamps = [t for t, _ in pts]
                lo, hi = bisect.bisect_left(stamps, since), bisect.bisect_right(stamps, until)
                if hi > lo:
                    out.setdefault(sid, {})[dim] = pts[lo:hi]
        return out


@pytest.fixture
def world(pg_db, monkeypatch):
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    server = str(pg_db.scalar(
        "INSERT INTO servers (display_name, hostname, collection_mode, status)"
        " VALUES ('synthetic-a', 'srv-synthetic-a', 'agent', 'healthy') RETURNING id"))
    history = History()
    sent: list[dict] = []

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(has, "fetch_host_history", history.fetch)
    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)
    return NS(db=pg_db, server=server, history=history, sent=sent)


def add_rule(w, metric="host_memory_pct", threshold=90, *, min_duration=300, clear_duration=None,
             recovery_alert=False, conditions=None, operator="gt"):
    return str(w.db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        " clear_duration, recovery_alert, notify_channels, conditions)"
        " VALUES ($1, $2, $3, $4, 'warning', $5, $6, $7, $8::jsonb, $9::jsonb) RETURNING id",
        f"{metric} rule", metric, operator, threshold, min_duration, clear_duration,
        recovery_alert, ["00000000-0000-0000-0000-00000000c0de"], conditions))


async def run(w, now):
    async with session(w.db) as s:
        return await has.evaluate_host_rules(s, now=now)


def incidents(w):
    return w.db.query("SELECT id, status, metadata FROM alerts WHERE server_id = $1"
                      " ORDER BY triggered_at, id", w.server)


async def replay(w, metric, values, *, start=T0, step=60, dim=""):
    """Feed one sample per step and evaluate a few seconds after each (one
    session for the whole replay; every pass commits its own work)."""
    async with session(w.db) as s:
        for i, value in enumerate(values):
            t = start + i * step
            if value is not None:
                w.history.add(metric, w.server, [(t, value)], dim)
            await has.evaluate_host_rules(s, now=t + 5)


@pytest.mark.asyncio
async def test_field_replay_memory_hovering_on_90_for_48_hours_is_one_incident(world):
    """121 mails in the field; one incident and one notification now."""
    w = world
    add_rule(w)                                    # the seeded rule: memory > 90 for 300 s
    rng = random.Random(20261018)
    readings = [round(rng.uniform(89.8, 91.5), 1) for _ in range(48 * 60)]
    assert sum(v <= 90 for v in readings) > 200     # it really does dip below, often
    await replay(w, "host_memory_pct", readings)
    found = incidents(w)
    assert len(found) == 1 and found[0][1] == "active"
    assert len(w.sent) == 1 and not w.sent[0]["is_recovery"]
    # Identity and precision: the mail names the incident and a reading that
    # does not round onto the threshold.
    ctx = w.sent[0]
    assert ctx["alert_id"] == str(found[0][0]) and ctx["delivery_event"] == "trigger"
    assert ctx["incident_ref"].startswith("INC-") and ctx["lifecycle"] == "new"
    assert ctx["headline_metric"]["value"] not in ("90%", "90.0%")


@pytest.mark.asyncio
async def test_without_the_clear_policy_the_same_pattern_is_many_incidents(world):
    """Control: reset at the trigger and an immediate clear reproduce the noise."""
    w = world
    add_rule(w, min_duration=0, clear_duration=0,       # 2-minute average, no hysteresis
             conditions=[dict(metric="host_memory_pct", operator="gt", threshold=90,
                              reset_threshold=90)])
    rng = random.Random(20261018)
    await replay(w, "host_memory_pct", [round(rng.uniform(89.8, 91.5), 1) for _ in range(6 * 60)])
    assert len(incidents(w)) > 5 and len(w.sent) == len(incidents(w))


@pytest.mark.asyncio
async def test_sustained_pressure_still_notifies_once_the_hold_passes(world):
    w = world
    add_rule(w)                                    # memory > 90 on the 5-minute average
    await replay(w, "host_memory_pct", [50] * 10)
    # Three minutes at 96%: the 5-minute average (77.6%) has not crossed yet.
    await replay(w, "host_memory_pct", [96] * 3, start=T0 + 600)
    assert incidents(w) == [] and w.sent == []
    await replay(w, "host_memory_pct", [96] * 15, start=T0 + 780)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["dedupe"] == f"rule:{meta['rule_id']}"
    assert meta["notified"] is True and len(w.sent) == 1


def test_rolling_mean_tolerates_one_dip_and_keeps_gaps_unknown():
    """CPU averaging 95% with one dip to 89% still breaches a "> 90 for 5 min"
    rule (the pre-1.24.16 semantics); a missing sample stays missing."""
    series = [(i * 60.0, v) for i, v in enumerate([95, 95, 89, 95, 95, None, 95])]
    out = has.rolling_mean(series, 300)
    assert [round(v, 1) if v is not None else None for _, v in out] == \
        [95.0, 95.0, 93.0, 93.5, 93.8, None, 93.5]


@pytest.mark.asyncio
async def test_missing_samples_never_resolve_an_open_incident(world):
    w = world
    add_rule(w, min_duration=0, clear_duration=0, recovery_alert=True)
    await replay(w, "host_memory_pct", [96])
    # The agent goes quiet for two hours: unknown, not healthy.
    for minute in range(1, 120, 10):
        await run(w, T0 + minute * 60)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["data_state"] == "no_data"
    assert len(w.sent) == 1                        # no recovery for missing data


@pytest.mark.asyncio
async def test_clear_hold_then_recovery_notice(world):
    w = world
    add_rule(w, min_duration=0, recovery_alert=True)   # clear_duration NULL -> 300 s
    await replay(w, "host_memory_pct", [96])
    await replay(w, "host_memory_pct", [80] * 5, start=T0 + 60)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["data_state"] == "pending_clear"
    await replay(w, "host_memory_pct", [80] * 2, start=T0 + 360)
    [(alert_id, status, _)] = incidents(w)
    assert status == "resolved"
    assert [c["is_recovery"] for c in w.sent] == [False, True]
    assert w.sent[1]["delivery_event"] == "recovery" and w.sent[1]["alert_id"] == str(alert_id)
    assert w.sent[1]["lifecycle"] == "recovered"


@pytest.mark.asyncio
async def test_a_rebreach_during_the_clear_hold_sends_nothing_new(world):
    w = world
    add_rule(w, min_duration=0, recovery_alert=True)   # 2-minute average
    # Averages 96, 96, 88, 80, 80, 88, 96, 88, 80, 88: cleared, then re-breached
    # inside the 300 s clear hold, which continues the same incident.
    await replay(w, "host_memory_pct", [96, 96, 80, 80, 80, 96, 96, 80, 80, 96])
    [(_, status, _)] = incidents(w)
    assert status == "active" and len(w.sent) == 1


@pytest.mark.asyncio
async def test_an_open_incident_waits_for_the_auto_clear_level(world):
    w = world
    add_rule(w, min_duration=0, clear_duration=0)        # 2-minute average
    await replay(w, "host_memory_pct", [95, 95, 89, 89, 89, 88.5])
    assert [s for _, s, _ in incidents(w)] == ["active"]    # 88.75 is above the 88 clear level
    await replay(w, "host_memory_pct", [87.9, 87.9], start=T0 + 360)
    assert [s for _, s, _ in incidents(w)] == ["resolved"]


@pytest.mark.asyncio
async def test_an_explicit_reset_threshold_wins_over_the_automatic_one(world):
    w = world
    add_rule(w, min_duration=0, clear_duration=0,
             conditions=[dict(metric="host_memory_pct", operator="gt", threshold=90,
                              reset_threshold=70)])
    await replay(w, "host_memory_pct", [95, 95, 80])          # 2-minute averages 95, 95, 87.5
    assert [s for _, s, _ in incidents(w)] == ["active"]
    await replay(w, "host_memory_pct", [69, 69], start=T0 + 180)
    assert [s for _, s, _ in incidents(w)] == ["resolved"]


@pytest.mark.asyncio
async def test_filesystem_incidents_are_per_mount(world):
    w = world
    rule = add_rule(w, "host_filesystem_pct", 85, min_duration=0, clear_duration=0)
    w.history.add("host_filesystem_pct", w.server, [(T0, 96)], "/data")
    w.history.add("host_filesystem_pct", w.server, [(T0, 50)], "/")
    await run(w, T0 + 5)
    [(_, status, meta)] = incidents(w)
    assert status == "active" and meta["dedupe"] == f"rule:{rule}:mount:/data"
    assert meta["mount"] == "/data"
    # A second mount fills: its own incident and its own notification.
    w.history.add("host_filesystem_pct", w.server, [(T0 + 60, 96)], "/data")
    w.history.add("host_filesystem_pct", w.server, [(T0 + 60, 97)], "/")
    await run(w, T0 + 65)
    assert sorted(m["mount"] for _, _, m in incidents(w)) == ["/", "/data"]
    assert len(w.sent) == 2
    # /data is cleaned up: only its incident resolves.
    w.history.add("host_filesystem_pct", w.server, [(T0 + 120, 40)], "/data")
    w.history.add("host_filesystem_pct", w.server, [(T0 + 120, 97)], "/")
    await run(w, T0 + 125)
    assert {m["mount"]: s for _, s, m in incidents(w)} == {"/data": "resolved", "/": "active"}


@pytest.mark.asyncio
async def test_an_open_pre_upgrade_filesystem_incident_is_continued_per_mount(world):
    w = world
    rule = add_rule(w, "host_filesystem_pct", 85, min_duration=0, clear_duration=0)
    legacy = w.db.scalar(
        "INSERT INTO alerts (server_id, rule_id, severity, message, status, metadata)"
        " VALUES ($1, $2, 'warning', 'Filesystem: srv — /data at 96%', 'active', $3::jsonb)"
        " RETURNING id", w.server, rule,
        {"dedupe": f"rule:{rule}", "source": "host_metric", "rule_id": rule})
    w.history.add("host_filesystem_pct", w.server, [(T0, 96)], "/data")
    w.history.add("host_filesystem_pct", w.server, [(T0, 60)], "/")
    await run(w, T0 + 5)
    [(alert_id, status, meta)] = incidents(w)
    assert alert_id == legacy and status == "active"
    assert meta["dedupe"] == f"rule:{rule}:mount:/data" and meta["legacy_dedupe"] == f"rule:{rule}"
    assert w.sent == []                             # continued, not announced again


@pytest.mark.asyncio
async def test_a_quiet_hours_trigger_is_sent_late_exactly_once(world):
    w = world
    rule = add_rule(w, min_duration=0, clear_duration=0)
    # Outside the schedule window: a rule for tomorrow's weekday only.
    tomorrow = datetime.now(timezone.utc).isoweekday() % 7 + 1
    w.db.execute("UPDATE alert_rules SET schedule_days = $2::jsonb WHERE id = $1", rule, [tomorrow])
    await replay(w, "host_memory_pct", [96])
    [(alert_id, _, meta)] = incidents(w)
    assert meta["notified"] is False and w.sent == []
    w.db.execute("UPDATE alert_rules SET schedule_days = NULL WHERE id = $1", rule)
    await replay(w, "host_memory_pct", [96, 96], start=T0 + 60)
    [(_, _, meta)] = incidents(w)
    assert meta["notified"] is True and len(w.sent) == 1
    assert w.sent[0]["alert_id"] == str(alert_id) and w.sent[0]["delivery_event"] == "trigger"


# ══ The SNMP (network) evaluator: the same clear policy ═════════════════════

@pytest.fixture
def snmp(pg_db, monkeypatch):
    from app.services import network_alert_service as worker
    from app.services import network_history

    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules")
    device = str(pg_db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, snmp_enabled, snmp_poll_interval)"
        " VALUES ('fw-synthetic-1', '192.0.2.77', 'up', true, 60) RETURNING id"))
    points: list = []
    sent: list[dict] = []

    def fetch_history(metrics, since, until):
        return {device: {"cpu": [(t, v) for t, v in points if since <= t <= until]}}, {}

    async def dispatch(db, channel_ids, ctx):
        sent.append(ctx)
        return len(channel_ids)

    monkeypatch.setattr(network_history, "fetch_history", fetch_history)
    monkeypatch.setattr(worker, "dispatch_to_channels", dispatch)
    rule = str(pg_db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, severity, min_duration,"
        " recovery_alert, notify_channels) VALUES ('Device CPU', 'cpu', '>', 90, 'warning', 300,"
        " true, $1::jsonb) RETURNING id", ["00000000-0000-0000-0000-00000000c0de"]))
    return NS(db=pg_db, device=device, points=points, sent=sent, worker=worker, rule=rule)


async def snmp_replay(n, values, *, start=T0, step=60):
    async with session(n.db) as s:
        for i, value in enumerate(values):
            t = start + i * step
            n.points.append((t, value))
            await n.worker.evaluate_network_rules(s, now=t + 5)


def snmp_incidents(n):
    return n.db.query("SELECT id, status, metadata FROM alerts WHERE device_id = $1"
                      " ORDER BY triggered_at, id", n.device)


@pytest.mark.asyncio
async def test_snmp_cpu_hovering_on_the_threshold_is_one_incident(snmp):
    n = snmp
    rng = random.Random(7)
    await snmp_replay(n, [round(rng.uniform(89.5, 91.5), 1) for _ in range(6 * 60)])
    [(alert_id, status, meta)] = snmp_incidents(n)
    assert status == "active" and meta["notified"] is True
    assert len(n.sent) == 1
    # The network trigger now names its incident (the delivery log row and
    # the durable delivery key need it).
    assert n.sent[0]["alert_id"] == str(alert_id) and n.sent[0]["delivery_event"] == "trigger"


@pytest.mark.asyncio
async def test_snmp_recovery_waits_for_the_clear_hold_and_names_the_incident(snmp):
    n = snmp
    await snmp_replay(n, [96] * 6)
    [(alert_id, status, _)] = snmp_incidents(n)
    assert status == "active"
    await snmp_replay(n, [50] * 5, start=T0 + 360)
    [(_, status, meta)] = snmp_incidents(n)
    assert status == "active" and meta["data_state"] == "pending_clear"
    await snmp_replay(n, [50] * 2, start=T0 + 660)
    [(_, status, _)] = snmp_incidents(n)
    assert status == "resolved"
    assert [c.get("is_recovery") for c in n.sent] == [False, True]
    assert n.sent[1]["alert_id"] == str(alert_id) and n.sent[1]["delivery_event"] == "recovery"


@pytest.mark.asyncio
async def test_snmp_trigger_in_flight_is_not_resent_by_the_next_pass(snmp):
    """The late-trigger race: a new incident is "sending" until its outcome is
    stamped, and "sending" is not pending — no other pass re-sends it."""
    n = snmp
    n.db.execute("UPDATE alert_rules SET min_duration = 0 WHERE id = $1", n.rule)
    stamps = []
    rivals = []
    real_stamp = ns.stamp

    async def rival_pass():
        async with session(n.db) as other:
            await n.worker.evaluate_network_rules(other, now=T0 + 6)

    async def slow_stamp(db, alert_id, sent):
        # The incident and intent now hold the dimension lock until commit.
        # Run the contender concurrently; awaiting it here would deadlock the
        # test by withholding the first transaction's commit from both passes.
        if not stamps:
            stamps.append(alert_id)
            rivals.append(asyncio.create_task(rival_pass()))
            await asyncio.sleep(.05)
            assert not rivals[0].done()
        await real_stamp(db, alert_id, sent)

    from unittest.mock import patch
    with patch.object(n.worker.ns, "stamp", slow_stamp):
        await snmp_replay(n, [96])
        await asyncio.wait_for(asyncio.gather(*rivals), 10)
    assert len(snmp_incidents(n)) == 1 and len(n.sent) == 1


@pytest.mark.asyncio
async def test_rule_api_round_trips_clear_duration_and_dedupes_channels(snmp, monkeypatch):
    from app.api.v1 import alert_rules
    from app.services import alert_rule_scope as rs

    async def allow(*a, **k):
        return None

    monkeypatch.setattr(rs, "guard_rule_change", allow)
    monkeypatch.setattr(alert_rules, "write_audit_log", allow)
    user = NS(id=None, role="admin", scope_tags=[])
    c = "00000000-0000-0000-0000-00000000c0de"
    async with session(snmp.db) as s:
        created = await alert_rules.create_alert_rule(alert_rules.AlertRuleCreate(
            name="Memory", metric="memory", operator=">", threshold=90, clear_duration=600,
            notify_channels=[c, c]), s, user)
        assert created["clear_duration"] == 600 and created["notify_channels"] == [c]
        updated = await alert_rules.update_alert_rule(created["id"], alert_rules.AlertRuleUpdate(
            clear_duration=None, notify_channels=[c, c]), s, user)
        assert updated["clear_duration"] is None and updated["notify_channels"] == [c]


@pytest.mark.asyncio
async def test_host_rule_api_exposes_the_clear_policy(world, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import host_alert_rules as api
    from app.services import alert_rule_scope as rs

    async def allow(*a, **k):
        return None

    monkeypatch.setattr(rs, "guard_rule_change", allow)
    monkeypatch.setattr(api, "write_audit_log", allow)
    user = NS(id=None, role="admin", scope_tags=[])
    c = "00000000-0000-0000-0000-00000000c0de"
    async with session(world.db) as s:
        rule = await api.create_host_rule(api.HostRuleCreate(
            name="Memory", metric="host_memory_pct", operator="gt", threshold=90,
            min_duration=300, notify_channels=[c, c], recovery_alert=True), s, user)
        assert rule["clear_duration"] is None and rule["reset_threshold"] is None
        assert rule["auto_reset_threshold"] == 88 and rule["recovery_alert"] is True
        assert rule["notify_channels"] == [c]
        rule = await api.update_host_rule(rule["id"], api.HostRuleUpdate(
            reset_threshold=80, clear_duration=600), s, user)
        assert rule["reset_threshold"] == 80 and rule["clear_duration"] == 600
        stored = world.db.scalar("SELECT conditions FROM alert_rules WHERE id = $1", rule["id"])
        assert stored == [{"metric": "host_memory_pct", "operator": "gt", "threshold": 90.0,
                           "reset_threshold": 80.0}]
        # A threshold change keeps the explicit reset (the automatic level is
        # still reported, for the editor's placeholder); clearing it returns to auto.
        rule = await api.update_host_rule(rule["id"], api.HostRuleUpdate(threshold=95), s, user)
        assert rule["reset_threshold"] == 80 and rule["auto_reset_threshold"] == 93
        rule = await api.update_host_rule(rule["id"], api.HostRuleUpdate(reset_threshold=None), s, user)
        assert rule["reset_threshold"] is None
        assert world.db.scalar("SELECT conditions FROM alert_rules WHERE id = $1", rule["id"]) is None
        with pytest.raises(HTTPException) as bad:
            await api.update_host_rule(rule["id"], api.HostRuleUpdate(reset_threshold=97), s, user)
        assert bad.value.status_code == 422


def test_host_history_reads_per_sample_rows_per_mount(monkeypatch):
    from app.core import database
    seen = {}

    def query(sql, parameters):
        seen["sql"], seen["params"] = sql, parameters
        return NS(result_rows=[("s1", "/data", T0, 91.0), ("s1", "/data", T0 + 60, None),
                               ("s1", "/", T0, 40.0), ("s2", "/", T0, 99.5)])

    monkeypatch.setattr(database, "get_clickhouse_client", lambda: NS(query=query))
    out = has.fetch_host_history("host_filesystem_pct", T0 - 900, T0 + 60)
    assert out == {"s1": {"/data": [(T0, 91.0), (T0 + 60, None)], "/": [(T0, 40.0)]},
                   "s2": {"/": [(T0, 99.5)]}}
    assert "host_filesystem_metrics" in seen["sql"] and "fs_type" in seen["sql"]   # capacity filter
    assert seen["params"]["since"].timestamp() == T0 - 900
    # Per sample, not an average over a window.
    for metric in ("host_cpu_pct", "host_memory_pct"):
        has.fetch_host_history(metric, T0 - 900, T0)
        assert "avg(" not in seen["sql"]
    monkeypatch.setattr(has, "HISTORY_ROW_LIMIT", 4)
    with pytest.raises(RuntimeError, match="limit"):
        has.fetch_host_history("host_filesystem_pct", T0 - 900, T0)
