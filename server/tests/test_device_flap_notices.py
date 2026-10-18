"""Device flap start/stop notices (device_flap_notice_service).

The status writers only record episodes in device_flap_episodes; the server
delivers one start and one stop notice per episode from there, on the 30 s
sweeper cadence (an episode already over when its start notice comes due gets
neither, and one device's notices settle in episode order). These pin who is told (status rules relevant to the episode
class, in scope, not snoozed, inside quiet hours), how (once per channel,
never PagerDuty-type channels), what is recorded (one informational notice
row, rule_id NULL) and the order of operations (settle, then dispatch).

Everything runs on the disposable PostgreSQL of pg_fixture — never the
appliance database — with the channel transport replaced by a recorder.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine
from app.api.v1 import alerts as alerts_api
from app.services import alert_phrasing as ap
from app.services import alert_workflow as wf
from app.services import device_flap_notice_service as fns

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)


# ── Pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trigger,kind,expected", [
    ("down", "reachability", True), ("up", "reachability", False), ("any", "reachability", True),
    ("degraded", "reachability", False),
    ("degraded", "performance", True), ("any", "performance", True),
    ("down", "performance", False), ("up", "performance", False),
    ("down", "both", True), ("degraded", "both", True), ("up", "both", False),
    (None, "performance", True),
])
def test_trigger_relevance_per_episode_class(trigger, kind, expected):
    assert fns.rule_relevant(trigger, kind) is expected


def test_channel_union_is_deduped_in_rule_order():
    a, b, c = ("aaaaaaaa-0000-0000-0000-00000000000%d" % i for i in (1, 2, 3))
    rules = [NS(notify_channels=[a, b]), NS(notify_channels=[b.upper(), c, "not-a-uuid"]),
             NS(notify_channels=None)]
    assert fns.union_channels(rules) == [a, b, c]


def _start_vars(kind):
    return {"hostname": "sw1", "ip": "10.0.0.1", "changes": 5, "window": 30,
            "kind_label": ap.flap_kind_label(kind), "stable": 10, **ap.flap_held_labels(kind)}


def test_default_texts_are_the_documented_ones():
    start = fns._render("start", _start_vars("reachability"))
    assert start["email_subject"] == "[ZenPlus] sw1 is flapping"
    assert start["email_body"] == (
        "sw1 (10.0.0.1) changed status 5 times in the last 30 minutes (up/down). Up/down "
        "notifications for this device are held: a status that lasts 10 minutes still "
        "notifies. Flapping ends after 10 minutes without a change. Other alerts "
        "(interfaces, services, metrics) are not affected.")
    assert start["sms_template"] == ("ZenPlus — sw1 (10.0.0.1) is flapping: 5 status changes in "
                                     "30 min. Up/down alerts held until a status lasts 10 min.")
    stop = fns._render("stop", {"hostname": "sw1", "ip": "10.0.0.1", "STATUS": "UP",
                                "since": "2026-10-14 09:36 UTC",
                                "end_reason_label": ap.flap_end_reason_label("stable")})
    assert stop["email_subject"] == "[ZenPlus] sw1 stopped flapping — UP"
    assert stop["email_body"] == ("sw1 (10.0.0.1) has stopped flapping (status stable). Current "
                                  "status: UP since 2026-10-14 09:36 UTC. Status notifications "
                                  "have resumed.")
    assert len(stop["sms_template"]) < 160 and len(start["sms_template"]) < 160


def test_a_latency_loss_episode_never_says_down_alerts_are_held():
    """Only the flapping class holds its triggers: up/degraded flapping holds
    Degraded, while a Down (a reachability change) still notifies as usual."""
    start = fns._render("start", _start_vars("performance"))
    assert start["email_body"] == (
        "sw1 (10.0.0.1) changed status 5 times in the last 30 minutes (up/degraded). Degraded "
        "(latency/loss) notifications for this device are held: a status that lasts 10 minutes "
        "still notifies. Flapping ends after 10 minutes without a change. Down notifications "
        "and other alerts (interfaces, services, metrics) are not affected.")
    assert start["sms_template"] == ("ZenPlus — sw1 (10.0.0.1) is flapping: 5 status changes in "
                                     "30 min. Degraded alerts held until a status lasts 10 min.")
    assert "Up/down" not in start["email_body"] + start["sms_template"]
    assert ap.flap_held_notice("performance", 10) == (
        "Degraded (latency/loss) notifications for this device are held until a status lasts "
        "10 minutes. Down notifications are not affected.")
    assert ap.flap_held_notice("reachability", 10) == (
        "Up/down notifications for this device are held until a status lasts 10 minutes.")


@pytest.mark.parametrize("kind", ["both", None, "bogus"])
def test_an_episode_of_both_classes_holds_every_status_notification(kind):
    start = fns._render("start", _start_vars(kind))
    assert ("Status notifications for this device are held: a status that lasts 10 minutes "
            "still notifies.") in start["email_body"]
    assert start["email_body"].endswith("Other alerts (interfaces, services, metrics) are not "
                                        "affected.")
    assert "Status alerts held until a status lasts 10 min." in start["sms_template"]
    assert "{" not in start["email_body"] + start["sms_template"]
    assert len(start["sms_template"]) < 160


def _rule(trigger_on="any", metric="ping_status", operator="eq", threshold=0, **kw):
    return NS(trigger_on=trigger_on, metric=metric, operator=operator, threshold=threshold,
              conditions=kw.pop("conditions", None), condition_logic=kw.pop("logic", "AND"),
              notify_while_flapping=kw.pop("notify_while_flapping", False), **kw)


@pytest.mark.parametrize("rule,reach,perf", [
    # The seeded rules (trigger_on defaults to 'any').
    (_rule(), True, False),                                        # Device Down: ping_status == 0
    (_rule(metric="rtt", operator="gt", threshold=100), False, True),        # High Latency
    (_rule(metric="packet_loss", operator="gt", threshold=5), True, True),   # Packet Loss
    # The wizard's explicit triggers store ping_status == 0 whatever the trigger.
    (_rule(trigger_on="down"), True, False),
    (_rule(trigger_on="degraded"), False, True),
    (_rule(trigger_on="up"), False, False),
    (_rule(trigger_on="down", metric="rtt", operator="gt", threshold=100), False, False),
    (_rule(trigger_on=None), True, False),
    # Conditions combine as the status path combines them.
    (_rule(conditions=[{"metric": "ping_status", "operator": "==", "threshold": 0},
                       {"metric": "rtt", "operator": ">", "threshold": 100}], logic="OR"), True, True),
    (_rule(conditions=[{"metric": "ping_status", "operator": "==", "threshold": 0},
                       {"metric": "rtt", "operator": ">", "threshold": 100}]), False, False),
    (_rule(conditions=[{"metric": "ping_status", "operator": "==", "threshold": 1},
                       {"metric": "packet_loss", "operator": ">=", "threshold": 20}]), False, True),
    (_rule(metric="rtt", operator="bogus", threshold=100), False, False),
    # Never held, so never told.
    (_rule(trigger_on="down", notify_while_flapping=True), False, False),
    (_rule(notify_while_flapping=True), False, False),
])
def test_only_rules_a_flap_holds_are_told(rule, reach, perf):
    """The status path's metric gate decides who a flap holds: a rule whose
    conditions cannot match the flapping class's move is never held by it."""
    assert fns.rule_held(rule, "reachability") is reach
    assert fns.rule_held(rule, "performance") is perf
    assert fns.rule_held(rule, "both") is (reach or perf)


@pytest.mark.parametrize("new_status,values", [
    # A move into down got no reply: no round-trip time (None), never 0 ms.
    ("down", {"ping_status": 0.0, "rtt": None, "packet_loss": 100.0}),
    ("degraded", {"ping_status": 1.0, "rtt": 250.0, "packet_loss": 40.0}),
])
def test_a_rule_the_status_path_fires_is_never_dropped_from_the_notices(new_status, values):
    """_could_fire_into over-approximates the real gate: whenever the status
    path would fire a rule on such a move, the notice gate keeps it."""
    rules = [_rule(), _rule(metric="rtt", operator="gt", threshold=100),
             _rule(metric="packet_loss", operator="gt", threshold=5),
             _rule(metric="rtt", operator="lt", threshold=1),
             _rule(conditions=[{"metric": "ping_status", "operator": "==", "threshold": 1},
                               {"metric": "rtt", "operator": ">", "threshold": 200}])]
    for rule in rules:
        if alert_engine._conditions_match(rule, values):
            assert alert_engine._could_fire_into(rule, new_status), rule


def test_flap_notice_rows_are_notices():
    meta = {"flap_notice": True, "flap_event": "start", "notified": True}
    assert wf.notice_kind(meta) == "flapping"
    assert wf.closure_reason("resolved", meta) == "flapping"
    assert "flapping" in wf.CLOSURE_LABELS
    assert "'flap_notice'" in wf.notice_sql("a") and "'flapping'" in wf.closure_reason_sql("a")


def test_alert_detail_evidence_describes_the_episode_of_a_notice_only():
    stop = {"flap_notice": True, "flap_event": "stop", "kind": "both", "changes": 6,
            "window_minutes": 30, "end_reason": "stable", "device_status": "down"}
    labels = {e["label"]: e["value"] for e in alerts_api._evidence(stop)}
    assert labels == {"Flapping": "both", "Status changes": "6", "Flap window (minutes)": "30",
                      "Flapping ended": "stable", "Device status": "down"}
    # The same generic keys on any other row are not flap evidence.
    other = {"changes": 3, "kind": "x", "flap_kind": "reachability"}
    assert {e["label"] for e in alerts_api._evidence(other)} == {"Held while flapping"}


# ── Database world ───────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


async def sweep(db):
    async with session(db) as s:
        return await fns.deliver_pending(s)


@pytest.fixture
def world(pg_db, monkeypatch):
    monkeypatch.setattr(fns, "_SUPPORTED", False)
    monkeypatch.setattr(alert_engine, "_NOTIFY_WHILE_FLAPPING_SUPPORTED", False)
    pg_db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM alert_silences")
    # The senders handle PagerDuty channels, but migrate-001's type CHECK does
    # not list the type yet; lift it here so a stateful channel can be modelled.
    pg_db.execute("ALTER TABLE notification_channels DROP CONSTRAINT notification_channels_type_check")
    device = pg_db.scalar(
        "INSERT INTO devices (hostname, ip_address, status, status_changed_at, tags)"
        " VALUES ('edge-1', '10.66.0.1', 'up', now() - interval '7 minutes', '[\"Branch\"]'::jsonb)"
        " RETURNING id")

    def channel(name, kind):
        return str(pg_db.scalar("INSERT INTO notification_channels (name, type, config)"
                                " VALUES ($1, $2, '{}'::jsonb) RETURNING id", name, kind))

    chans = NS(mail=channel("noc-mail", "email"), hook=channel("noc-hook", "webhook"),
               pd=channel("pager", "pagerduty"), chat=channel("chat", "slack"),
               other=channel("other", "email"))
    calls: list[dict] = []

    async def dispatch(db, channel_ids, ctx):
        # What another connection sees at the moment of sending.
        episode = ctx.get("episode_id")
        calls.append({
            "channels": list(channel_ids), "ctx": ctx,
            "episode_state": pg_db.query(
                "SELECT start_notice, stop_notice FROM device_flap_episodes WHERE id = $1",
                episode)[0] if episode else None,
            "notice_committed": pg_db.scalar("SELECT count(*) FROM alerts WHERE id = $1::uuid",
                                             ctx.get("alert_id")),
        })
        return len(channel_ids)

    monkeypatch.setattr("app.services.host_alert_service.dispatch_to_channels", dispatch)
    return NS(db=pg_db, device=device, ch=chans, calls=calls)


def add_rule(w, name, trigger_on, channels, *, metric="ping_status", **cols):
    names = ["name", "metric", "operator", "threshold", "trigger_on", "notify_channels", *cols]
    values = [name, metric, "==", 0, trigger_on, channels, *cols.values()]
    casts = {"notify_channels": "::jsonb", "schedule_days": "::jsonb",
             "schedule_start": "::text::time", "schedule_end": "::text::time"}
    marks = ", ".join(f"${i + 1}{casts.get(n, '')}" for i, n in enumerate(names))
    return str(w.db.scalar(f"INSERT INTO alert_rules ({', '.join(names)}) VALUES ({marks}) RETURNING id",
                           *values))


def add_episode(w, *, device=None, kind="reachability", started_ago=5, ended=False,
                end_reason=None, start_notice="pending", notice_channels=(), changes=4):
    return w.db.scalar(
        "INSERT INTO device_flap_episodes (device_id, started_at, ended_at, kind, start_changes,"
        " window_minutes, status_at_start, end_reason, start_notice, notice_channels)"
        " VALUES ($1, now() - make_interval(mins => $2), CASE WHEN $3 THEN now() END, $4, $5, 30,"
        " 'up', $6, $7, $8::uuid[]) RETURNING id",
        device or w.device, started_ago, ended, kind, changes, end_reason, start_notice,
        list(notice_channels))


def end_episode(w, ep, reason="stable"):
    w.db.execute("UPDATE device_flap_episodes SET ended_at = now(), end_reason = $2, status_at_end = 'up'"
                 " WHERE id = $1", ep, reason)


def episode(w, ep):
    row = w.db.query("SELECT start_notice, stop_notice, notice_channels FROM device_flap_episodes"
                     " WHERE id = $1", ep)[0]
    return NS(start=row[0], stop=row[1], channels=[str(c) for c in row[2]])


def notices(w):
    return [NS(rule_id=r[0], status=r[1], severity=r[2], message=r[3], meta=r[4]) for r in w.db.query(
        "SELECT rule_id, status, severity, message, metadata FROM alerts"
        " WHERE metadata->>'flap_notice' = 'true' ORDER BY triggered_at, id")]


# ── Start notice ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_notice_goes_once_per_channel_and_never_to_pagerduty(world):
    w, ch = world, world.ch
    add_rule(w, "Device Down", "down", [ch.mail, ch.pd])
    add_rule(w, "Device Any", "any", [ch.mail, ch.hook])
    add_rule(w, "Device Up", "up", [ch.chat])                # recoveries only: never held
    add_rule(w, "Device Degraded", "degraded", [ch.other])   # not a reachability trigger
    ep = add_episode(w)

    out = await sweep(w.db)

    assert out == {"started": 1, "stopped": 0, "skipped": 0, "failed": 0}
    [call] = w.calls
    assert call["channels"] == [ch.mail, ch.hook]
    ctx = call["ctx"]
    assert ctx["event_type"] == ctx["delivery_kind"] == "flap_start"
    assert ctx["severity"] == "info" and ctx["status"] == "FLAPPING" and ctx["is_recovery"] is False
    assert ctx["subject"] == "[ZenPlus] edge-1 is flapping"
    assert "changed status 4 times in the last 30 minutes (up/down)" in ctx["body"]
    state = episode(w, ep)
    assert state.start == "sent" and state.stop == "pending"
    assert state.channels == [ch.mail, ch.hook]
    [row] = notices(w)
    assert row.rule_id is None and row.status == "resolved" and row.severity == "info"
    assert {k: row.meta[k] for k in ("flap_event", "episode_id", "kind", "changes",
                                     "window_minutes", "notified")} == {
        "flap_event": "start", "episode_id": str(ep), "kind": "reachability", "changes": 4,
        "window_minutes": 30, "notified": True}
    assert ctx["alert_id"] and call["notice_committed"] == 1


@pytest.mark.asyncio
async def test_a_performance_episode_tells_degraded_and_any_rules_only(world):
    w, ch = world, world.ch
    add_rule(w, "Device Down", "down", [ch.mail])
    add_rule(w, "Device Up", "up", [ch.chat])
    add_rule(w, "Device Degraded", "degraded", [ch.hook])
    add_rule(w, "High latency", "any", [ch.other], metric="rtt")
    add_episode(w, kind="performance")
    await sweep(w.db)
    [call] = w.calls
    assert call["channels"] == [ch.hook, ch.other]
    ctx = call["ctx"]
    assert "(up/degraded)" in ctx["body"]
    # A Down still notifies at once: nobody is told Up/down alerts are held.
    assert "Up/down" not in ctx["body"] + ctx["message"] + ctx["notice"]
    assert "Degraded (latency/loss) notifications for this device are held" in ctx["body"]
    assert "Degraded alerts held until a status lasts 10 min." in ctx["message"]
    assert ctx["notice"].endswith("Down notifications are not affected.")


def add_seeded_rules(w, *, down=None, latency=None, loss=None):
    """The default rules (those given channels) exactly as
    scripts/seed-devices.sql creates them: no trigger_on, so the column
    default ('any') applies."""
    ids = []
    for name, metric, op, threshold, channels in (
            ("Device Down", "ping_status", "eq", 0, down),
            ("High Latency", "rtt", "gt", 100, latency),
            ("Packet Loss", "packet_loss", "gt", 5, loss)):
        if channels is None:
            continue
        ids.append(str(w.db.scalar(
            "INSERT INTO alert_rules (name, metric, operator, threshold, duration, severity,"
            " cooldown, notify_channels) VALUES ($1, $2, $3, $4, 180, 'critical', 300, $5::jsonb)"
            " RETURNING id", name, metric, op, threshold, list(channels))))
    assert {r[0] for r in w.db.query("SELECT trigger_on FROM alert_rules")} == {"any"}
    return ids


@pytest.mark.asyncio
async def test_the_seeded_device_down_rule_hears_nothing_of_latency_loss_flapping(world):
    """It fires only on Down (ping_status == 0 on an 'any' rule), so an
    up/degraded episode holds none of its notifications."""
    w, ch = world, world.ch
    add_seeded_rules(w, down=[ch.mail])
    ep = add_episode(w, kind="performance")
    out = await sweep(w.db)
    assert out == {"started": 0, "stopped": 0, "skipped": 1, "failed": 0}
    assert w.calls == [] and notices(w) == []
    assert episode(w, ep).start == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reachability", "both"])
async def test_the_seeded_device_down_rule_still_hears_of_up_down_flapping(world, kind):
    w, ch = world, world.ch
    add_seeded_rules(w, down=[ch.mail])
    add_episode(w, kind=kind)
    assert (await sweep(w.db))["started"] == 1
    [call] = w.calls
    assert call["channels"] == [ch.mail]


@pytest.mark.asyncio
async def test_seeded_metric_rules_hear_only_of_the_flapping_they_can_fire_on(world):
    """High Latency (rtt > 100) never fires on a move into down (no answer, no
    round-trip time); Packet Loss (> 5%) does (100% loss)."""
    w, ch = world, world.ch
    down, latency, loss = add_seeded_rules(w, down=[ch.mail], latency=[ch.hook], loss=[ch.chat])
    add_episode(w, kind="reachability")
    await sweep(w.db)
    [call] = w.calls
    assert call["channels"] == [ch.mail, ch.chat]
    [row] = notices(w)
    assert sorted(row.meta["target_rule_ids"]) == sorted([down, loss])

    w.calls.clear()
    w.db.execute("DELETE FROM device_flap_episodes")
    add_episode(w, kind="performance")
    await sweep(w.db)
    [call] = w.calls
    assert call["channels"] == [ch.hook, ch.chat]


@pytest.mark.asyncio
async def test_a_rule_that_notifies_while_flapping_is_not_told_it_is_held(world):
    """Its Down pages at once, so 'held' and later 'resumed' would both be
    false for a channel only it reaches."""
    w, ch = world, world.ch
    add_rule(w, "Core Down", "down", [ch.hook], notify_while_flapping=True)
    add_rule(w, "Device Down", "down", [ch.mail])
    add_episode(w)
    await sweep(w.db)
    [call] = w.calls
    assert call["channels"] == [ch.mail]


@pytest.mark.asyncio
async def test_only_rules_that_notify_while_flapping_means_no_notice(world):
    w, ch = world, world.ch
    add_rule(w, "Core Down", "down", [ch.hook], notify_while_flapping=True)
    ep = add_episode(w)
    out = await sweep(w.db)
    assert out["skipped"] == 1 and out["started"] == 0
    assert w.calls == [] and notices(w) == []
    assert episode(w, ep).start == "skipped"
    end_episode(w, ep)
    await sweep(w.db)
    assert w.calls == [] and episode(w, ep).stop == "skipped"


@pytest.mark.asyncio
async def test_only_in_scope_status_rules_are_told(world):
    w, ch = world, world.ch
    other = w.db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('x', '10.66.9.9') RETURNING id")
    add_rule(w, "Branch devices", "down", [ch.mail], scope_tag="branch")      # tag match, any case
    add_rule(w, "Other device", "down", [ch.hook], device_id=other)
    add_rule(w, "Core tag", "down", [ch.chat], scope_tag="core")
    add_rule(w, "CPU", "any", [ch.other], metric="cpu")                      # not a status rule
    add_rule(w, "Disabled", "down", [ch.hook], enabled=False)
    add_episode(w)
    await sweep(w.db)
    [call] = w.calls
    assert call["channels"] == [ch.mail]


@pytest.mark.asyncio
async def test_quiet_hours_and_snoozes_drop_a_rules_channels(world):
    w, ch = world, world.ch
    now = datetime.now(timezone.utc)
    start, end = ((now + timedelta(hours=h)).strftime("%H:%M") for h in (2, 3))
    quiet = add_rule(w, "Office hours", "down", [ch.mail], schedule_start=start, schedule_end=end)
    snoozed = add_rule(w, "Snoozed", "down", [ch.chat])
    add_rule(w, "Always", "down", [ch.hook])
    w.db.execute("INSERT INTO alert_silences (device_id, dedupe, until)"
                 " VALUES ($1, $2, now() + interval '1 hour')", w.device, f"rule:{snoozed}")
    add_episode(w)
    await sweep(w.db)
    [call] = w.calls
    assert call["channels"] == [ch.hook]
    [row] = notices(w)
    assert row.meta["quiet_hours_rule_ids"] == [quiet] and row.meta["snoozed_rule_ids"] == [snoozed]


@pytest.mark.asyncio
async def test_nobody_to_tell_records_a_quiet_notice_and_skips(world):
    w, ch = world, world.ch
    now = datetime.now(timezone.utc)
    start, end = ((now + timedelta(hours=h)).strftime("%H:%M") for h in (2, 3))
    add_rule(w, "Office hours", "down", [ch.mail], schedule_start=start, schedule_end=end)
    ep = add_episode(w)
    out = await sweep(w.db)
    assert out["skipped"] == 1 and w.calls == []
    assert episode(w, ep).start == "skipped"
    [row] = notices(w)
    assert row.meta["notified"] is False


@pytest.mark.asyncio
async def test_no_matching_rule_records_nothing(world):
    ep = add_episode(world)
    await sweep(world.db)
    assert episode(world, ep).start == "skipped" and notices(world) == [] and world.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["status", "window", "sensor_offline"])
async def test_maintenance_or_an_offline_sensor_skips_both_notices(world, monkeypatch, how):
    w, ch = world, world.ch
    add_rule(w, "Device Down", "down", [ch.mail])
    if how == "status":
        w.db.execute("UPDATE devices SET status = 'maintenance' WHERE id = $1", w.device)
    elif how == "window":
        w.db.execute("INSERT INTO device_maintenance (scope_type, scope_device_id, starts_at, ends_at)"
                     " VALUES ('device', $1, now() - interval '1 hour', now() + interval '1 hour')",
                     w.device)
    else:
        monkeypatch.setattr(alert_engine, "_offline_sensor_owner",
                            AsyncMock(return_value={"id": "s1", "name": "branch-sensor"}))
    started = add_episode(w)
    ended = add_episode(w, device=w.db.scalar(
        "INSERT INTO devices (hostname, ip_address, status) VALUES ('edge-2', '10.66.0.2', 'up')"
        " RETURNING id"), ended=True, end_reason="maintenance", start_notice="sent",
        notice_channels=[ch.mail])
    if how != "sensor_offline":
        # The second device is in maintenance too (its episode ended for it).
        w.db.execute("UPDATE devices SET status = 'maintenance' WHERE hostname = 'edge-2'")
    await sweep(w.db)
    assert w.calls == [] and notices(w) == []
    assert episode(w, started).start == "skipped"
    assert episode(w, ended).stop == "skipped"


# ── Stop notice ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_goes_only_to_the_channels_the_start_reached(world):
    w, ch = world, world.ch
    add_rule(w, "Device Down", "down", [ch.hook])       # added after the start: not told
    w.db.execute("UPDATE devices SET status = 'down', status_changed_at = now() - interval '12 minutes'"
                 " WHERE id = $1", w.device)
    ep = add_episode(w, started_ago=40, ended=True, end_reason="stable", start_notice="sent",
                     notice_channels=[ch.mail, ch.pd])
    out = await sweep(w.db)
    assert out["stopped"] == 1
    [call] = w.calls
    assert call["channels"] == [ch.mail]                 # a stateful channel never gets one
    ctx = call["ctx"]
    assert ctx["event_type"] == ctx["delivery_kind"] == "flap_stop"
    assert ctx["subject"] == "[ZenPlus] edge-1 stopped flapping — DOWN"
    assert ctx["body"].startswith("edge-1 (10.66.0.1) has stopped flapping (status stable). "
                                  "Current status: DOWN since ")
    assert ctx["resolved"] is False and ctx["is_recovery"] is False
    assert episode(w, ep).stop == "sent"
    [row] = notices(w)
    assert row.meta["flap_event"] == "stop" and row.meta["end_reason"] == "stable"
    assert row.meta["device_status"] == "down" and row.rule_id is None


@pytest.mark.asyncio
async def test_a_stop_back_to_up_reads_as_an_all_clear(world):
    w = world
    add_episode(w, ended=True, end_reason="disabled", start_notice="sent", notice_channels=[w.ch.mail])
    await sweep(w.db)
    [call] = w.calls
    assert call["ctx"]["resolved"] is True and call["ctx"]["status"] == "UP"
    assert "(flap detection turned off)" in call["ctx"]["body"]


@pytest.mark.asyncio
async def test_the_stop_follows_the_start(world):
    w, ch = world, world.ch
    add_rule(w, "Device Down", "down", [ch.mail])
    ep = add_episode(w)
    await sweep(w.db)
    end_episode(w, ep)
    await sweep(w.db)
    assert [c["ctx"]["event_type"] for c in w.calls] == ["flap_start", "flap_stop"]
    assert w.calls[1]["channels"] == [ch.mail]
    state = episode(w, ep)
    assert (state.start, state.stop) == ("sent", "sent")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,label", [
    ("owner_change", "polling owner changed or went offline"),
    ("reseed", "status re-synchronised"),
    ("not_polled", "no longer polled"),
])
async def test_a_writer_closed_episode_still_tells_whoever_heard_it_start(world, reason, label):
    """A reseed, an ownership move or the end of polling closes an episode
    without a state machine event; the stop still reaches the start's
    channels, so nobody is left believing notifications are held."""
    w = world
    add_episode(w, ended=True, end_reason=reason, start_notice="sent", notice_channels=[w.ch.mail])
    out = await sweep(w.db)
    assert out["stopped"] == 1
    [call] = w.calls
    assert call["channels"] == [w.ch.mail] and f"({label})" in call["ctx"]["body"]


@pytest.mark.asyncio
async def test_an_episode_that_ended_before_its_start_notice_stays_quiet(world):
    """Delivered late (API restart, a sensor batch that opened and closed
    it), a start notice would announce a hold that is already over."""
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    ep = add_episode(w, ended=True, end_reason="owner_change")
    out = await sweep(w.db)
    assert out == {"started": 0, "stopped": 0, "skipped": 2, "failed": 0}
    assert w.calls == [] and notices(w) == []
    state = episode(w, ep)
    assert (state.start, state.stop) == ("skipped", "skipped")


@pytest.mark.asyncio
async def test_an_unannounced_episode_ends_silently(world):
    ep = add_episode(world)                                       # no rule: start skipped
    await sweep(world.db)
    end_episode(world, ep)
    await sweep(world.db)
    assert world.calls == [] and notices(world) == []
    state = episode(world, ep)
    assert (state.start, state.stop) == ("skipped", "skipped")


@pytest.mark.asyncio
async def test_a_stop_waits_for_its_start_to_settle(world, monkeypatch):
    """An ended episode whose start notice is still pending (another worker
    has it) is not stopped first: the stop would go to no channels."""
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    ep = add_episode(w, ended=True, end_reason="stable")
    monkeypatch.setitem(fns._NEXT_SQL, "start",
                        fns._NEXT_SQL["start"].replace("WHERE e.start_notice = 'pending'", "WHERE false"))
    await sweep(w.db)
    state = episode(w, ep)
    assert (state.start, state.stop) == ("pending", "pending") and w.calls == []


# ── Ordering and delivery guarantees ─────────────────────────────────────────

@contextlib.asynccontextmanager
async def holding_rows(db, sql, *args):
    """Another worker mid-claim: an open transaction holding the row lock."""
    import asyncpg

    conn = await asyncpg.connect(db.url.replace("postgresql+asyncpg://", "postgresql://"))
    txn = conn.transaction()
    await txn.start()
    try:
        await conn.execute(sql, *args)
        yield conn
    finally:
        await txn.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_the_episode_is_settled_and_committed_before_anything_is_sent(world):
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    add_episode(w)
    await sweep(w.db)
    [call] = w.calls
    assert call["episode_state"][0] == "sent"     # seen from another connection
    assert call["notice_committed"] == 1


@pytest.mark.asyncio
async def test_a_failed_dispatch_is_never_repeated(world, monkeypatch):
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    ep = add_episode(w)
    boom = AsyncMock(side_effect=RuntimeError("smtp down"))
    monkeypatch.setattr("app.services.host_alert_service.dispatch_to_channels", boom)
    out = await sweep(w.db)
    assert boom.await_count == 1 and out["started"] == 1
    assert episode(w, ep).start == "sent"
    await sweep(w.db)
    assert boom.await_count == 1


@pytest.mark.asyncio
async def test_episodes_are_delivered_in_start_order_and_locked_ones_wait(world):
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    second = w.db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('edge-2', '10.66.0.2')"
                         " RETURNING id")
    third = w.db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('edge-3', '10.66.0.3')"
                        " RETURNING id")
    late = add_episode(w, started_ago=1)
    early = add_episode(w, device=second, started_ago=9)
    locked = add_episode(w, device=third, started_ago=5)
    async with holding_rows(w.db, "SELECT 1 FROM device_flap_episodes WHERE id = $1 FOR UPDATE", locked):
        await sweep(w.db)
    assert [c["ctx"]["episode_id"] for c in w.calls] == [str(early), str(late)]
    assert episode(w, locked).start == "pending"
    await sweep(w.db)
    assert w.calls[-1]["ctx"]["episode_id"] == str(locked)


@pytest.mark.asyncio
async def test_one_devices_notices_never_pass_each_other(world):
    """After downtime a device can have an ended episode whose stop is still
    due and a newer open one: the stop goes first, the next start after it."""
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    first = add_episode(w, started_ago=40, ended=True, end_reason="stable", start_notice="sent",
                        notice_channels=[w.ch.mail])
    second = add_episode(w, started_ago=2)
    await sweep(w.db)
    assert [c["ctx"]["episode_id"] for c in w.calls] == [str(first)]
    assert episode(w, second).start == "pending"
    await sweep(w.db)
    assert [(c["ctx"]["episode_id"], c["ctx"]["event_type"]) for c in w.calls] == [
        (str(first), "flap_stop"), (str(second), "flap_start")]


@pytest.mark.asyncio
async def test_an_episode_waits_while_an_earlier_one_is_being_settled(world):
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    first = add_episode(w, started_ago=40, ended=True, end_reason="stable", start_notice="sent",
                        notice_channels=[w.ch.mail])
    second = add_episode(w, started_ago=2)
    async with holding_rows(w.db, "SELECT 1 FROM device_flap_episodes WHERE id = $1 FOR UPDATE", first):
        await sweep(w.db)
    assert w.calls == []
    assert episode(w, first).stop == "pending" and episode(w, second).start == "pending"


@pytest.mark.asyncio
async def test_a_claim_never_deadlocks_with_a_writer_closing_the_episode(world):
    """A status writer holds the device row and then closes its open episode.
    The claim takes the device row first as well, so it waits for the writer
    instead of holding the episode the writer needs (the notice row's foreign
    key would then wait for the device: a deadlock)."""
    import asyncpg

    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    ep = add_episode(w)
    conn = await asyncpg.connect(w.db.url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        txn = conn.transaction()
        await txn.start()
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute("SELECT 1 FROM devices WHERE id = $1 FOR UPDATE", w.device)
        task = asyncio.create_task(sweep(w.db))
        waiting = 0
        for _ in range(200):
            # pg_stat_activity is a per-transaction snapshot: refresh it.
            await conn.execute("SELECT pg_stat_clear_snapshot()")
            waiting = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'")
            if waiting or task.done():
                break
            await asyncio.sleep(0.05)
        assert waiting == 1                       # the sweep waits for the writer's device
        await conn.execute("UPDATE device_flap_episodes SET ended_at = now(), end_reason = 'reseed'"
                           " WHERE id = $1", ep)
        await txn.commit()
        out = await asyncio.wait_for(task, 15)
    finally:
        await conn.close()
    # The writer closed the episode before anyone was told: both stay quiet.
    assert out == {"started": 0, "stopped": 0, "skipped": 2, "failed": 0}
    assert (episode(w, ep).start, episode(w, ep).stop) == ("skipped", "skipped")


@pytest.mark.asyncio
async def test_a_sweep_that_cannot_load_its_rules_leaves_the_episode_pending(world, monkeypatch):
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    ep = add_episode(w)
    policy = alert_engine._status_policy
    monkeypatch.setattr(alert_engine, "_status_policy", AsyncMock(side_effect=RuntimeError("db gone")))
    with pytest.raises(RuntimeError):
        await sweep(w.db)
    assert episode(w, ep).start == "pending" and w.calls == [] and notices(w) == []
    monkeypatch.setattr(alert_engine, "_status_policy", policy)
    assert (await sweep(w.db))["started"] == 1 and episode(w, ep).start == "sent"


@pytest.mark.asyncio
async def test_a_database_without_migrate_161_is_a_no_op(world):
    world.db.execute("DROP TABLE device_flap_episodes")
    assert await sweep(world.db) == {"started": 0, "stopped": 0, "skipped": 0, "failed": 0}


# ── Notice rows stay out of incident counts ──────────────────────────────────

@pytest.mark.asyncio
async def test_device_counts_ignore_notice_rows(world, monkeypatch):
    w = world
    add_rule(w, "Device Down", "down", [w.ch.mail])
    ep = add_episode(w)
    await sweep(w.db)
    end_episode(w, ep)
    await sweep(w.db)
    assert len(notices(w)) == 2
    w.db.execute("INSERT INTO alerts (device_id, status, severity, message)"
                 " VALUES ($1, 'active', 'critical', 'Device Down: edge-1 is DOWN')", w.device)
    monkeypatch.setattr(alerts_api.scoping, "visible_tags", AsyncMock(return_value=None))
    async with session(w.db) as s:
        out = await alerts_api.alert_device_counts(hours=24, from_time=None, to_time=None,
                                                   db=s, user=NS(id=None, role="admin"))
    counts = out["devices"][str(w.device)]
    assert counts["total"] == 1 and counts["info"] == 0
    assert counts["active"] == 1 and counts["critical"] == 1
