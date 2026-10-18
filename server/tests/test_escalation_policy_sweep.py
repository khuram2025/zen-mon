"""Policy escalation sweep (alert_escalation_service) on a disposable PostgreSQL.

The sweep resolves each step's targets — on-call schedules, users, channels —
at the moment it fires, claims the step with a compare-and-set on
``metadata.escalation`` and only then sends, through dispatch_to_channels with
the people in ``ctx["recipients"]``. Covered here:

* two concurrent sweeps page a step once;
* steps follow their deadlines (injected clock); a server alert whose rule is
  only in metadata.rule_id is routed;
* an expired acknowledgement re-opens the incident and re-notifies the step;
* unresolved reviews reach the owner after acknowledgement;
* resolution sends the all-clear to every channel and person notified;
* an empty step and an all-failed step use the fallback, with coverage_gap /
  route_failed on the timeline;
* precedence: a priority default policy takes over a legacy-levels rule; a
  legacy rule without one keeps its inline escalation.

Only dispatch_to_channels is faked; nothing reaches an installed database.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_escalation_service as esc
from app.services import host_alert_service as has

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)


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
    db = pg_db
    now = datetime.now(timezone.utc)

    def user(name, email, sms=None):
        uid = db.scalar("INSERT INTO users (username, email, password_hash, full_name, role)"
                        " VALUES ($1, $2, '!', $3, 'operator') RETURNING id", name, email, name.title())
        if sms:
            db.execute("INSERT INTO user_contact_methods (user_id, kind, address, label)"
                       " VALUES ($1, 'sms', $2, 'Mobile')", uid, sms)
        return str(uid)

    def schedule(name, members):
        sid = db.scalar("INSERT INTO oncall_schedules (name, timezone) VALUES ($1, 'Asia/Riyadh') RETURNING id",
                        name)
        db.execute("INSERT INTO oncall_layers (schedule_id, name, position, shift_hours, handoff_at, members)"
                   " VALUES ($1, 'Rotation', 0, 168, $2, $3::uuid[])", sid, now - timedelta(days=1), members)
        return str(sid)

    def channel(name):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config) VALUES ($1, 'email',"
                             " $2::jsonb) RETURNING id", name, {"recipients": f"{name}@example.test"}))

    u = NS(primary=user("aisha", "aisha@example.test"), backup=user("omar", "omar@example.test", "+966501234567"),
           lead=user("lena", "lena@example.test"))
    sch = NS(primary=schedule("Primary", [u.primary]), backup=schedule("Backup", [u.backup]),
             empty=schedule("Nobody", []))
    ch = NS(noc=channel("noc"), fb=channel("fallback"), legacy=channel("legacy"))

    def policy(name, steps, *, prio=("P1",), fallback=None, **kw):
        pid = db.scalar(
            "INSERT INTO escalation_policies (name, default_priorities, repeat_minutes, max_repeats,"
            " review_minutes, ack_expiry_minutes, fallback_targets) VALUES ($1, $2::varchar[], $3, $4, $5, $6,"
            " $7::jsonb) RETURNING id", name, list(prio), kw.get("repeat"), kw.get("max_repeats", 0),
            kw.get("review"), kw.get("expiry"), fallback if fallback is not None else [{"type": "channel", "id": ch.fb}])
        for i, (delay, targets) in enumerate(steps):
            db.execute("INSERT INTO escalation_steps (policy_id, position, delay_minutes, name, targets)"
                       " VALUES ($1, $2, $3, $4, $5::jsonb)", pid, i, delay, f"Step {i}", targets)
        return str(pid)

    server = db.scalar("INSERT INTO servers (display_name, hostname) VALUES ('db-01', 'db-01') RETURNING id")

    def rule(name, *, priority=None, policy_id=None, levels=None, severity="critical"):
        return str(db.scalar(
            "INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels, priority,"
            " escalation_policy_id, escalation_levels) VALUES ($1, 'host_memory_pct', 'gt', 90, $2, '[]'::jsonb,"
            " $3, $4, $5::jsonb) RETURNING id", name, severity, priority, policy_id, levels))

    def alert(rule_id, *, minutes_ago=1, status="active", priority=None, meta=None, ack_ago=None,
              assigned=None, severity="critical"):
        m = {"notified": True, "rule_id": rule_id, "dedupe": f"mem:{rule_id}", **(meta or {})}
        return str(db.scalar(
            "INSERT INTO alerts (server_id, severity, message, status, triggered_at, acknowledged_at,"
            " assigned_to, priority, metadata) VALUES ($1, $2, 'Memory 97%', $3, $4, $5, $6, $7, $8::jsonb)"
            " RETURNING id", server, severity, status, now - timedelta(minutes=minutes_ago),
            (now - timedelta(minutes=ack_ago)) if ack_ago is not None else None, assigned, priority, m))

    pages: list[NS] = []
    behaviour = NS(delay=0.0, results=[])

    async def dispatch(db_, channels, ctx):
        await asyncio.sleep(behaviour.delay)
        pages.append(NS(channels=list(channels), ctx=ctx,
                        people=sorted(r["address"] for r in ctx.get("recipients") or [])))
        return behaviour.results.pop(0) if behaviour.results else 1

    monkeypatch.setattr(has, "dispatch_to_channels", dispatch)

    def events(alert_id, kind=None):
        rows = db.query("SELECT kind, detail, reason FROM alert_events WHERE alert_id = $1 ORDER BY at, id",
                        alert_id)
        return [r for r in rows if kind is None or r[0] == kind]

    def state(alert_id):
        return db.scalar("SELECT metadata->'escalation' FROM alerts WHERE id = $1", alert_id)

    return NS(db=db, now=now, u=u, sch=sch, ch=ch, policy=policy, rule=rule, alert=alert, pages=pages,
              behaviour=behaviour, events=events, state=state)


async def sweep(w, *, plus_minutes: float = 0):
    async with session(w.db) as s:
        return await esc.sweep_policy_escalations(
            s, now=datetime.now(timezone.utc) + timedelta(minutes=plus_minutes))


def sched(i):
    return {"type": "schedule", "id": i}


def chan(i):
    return {"type": "channel", "id": i}


@pytest.mark.asyncio
async def test_two_concurrent_sweeps_page_a_step_once(world):
    w = world
    pol = w.policy("P1 matrix", [(0, [sched(w.sch.primary), chan(w.ch.noc)]), (5, [sched(w.sch.backup)])])
    alert_id = w.alert(w.rule("Memory critical", priority="P1"), priority="P1")
    w.behaviour.delay = 0.3
    results = await asyncio.wait_for(asyncio.gather(sweep(w), sweep(w)), 30)
    assert sum(r["escalated"] for r in results) == 1 and len(w.pages) == 1
    page = w.pages[0]
    assert page.ctx["delivery_event"] == "escalation:S0:r0"
    assert page.channels == [w.ch.noc] and page.people == ["aisha@example.test"]
    assert page.ctx["priority"] == "P1" and page.ctx["lifecycle"] == "new"
    assert page.ctx["incident_ref"].startswith("INC-") and page.ctx["groupable"] is False
    assert len(w.events(alert_id, "escalated")) == 1
    st = w.state(alert_id)
    assert st["mode"] == "policy" and st["policy_id"] == pol and st["step"] == 0
    assert st["channels"] == [w.ch.noc] and [p["address"] for p in st["people"]] == ["aisha@example.test"]


@pytest.mark.asyncio
async def test_steps_follow_their_deadlines(world):
    w = world
    w.policy("P1 matrix", [(0, [sched(w.sch.primary)]), (5, [sched(w.sch.backup)])])
    alert_id = w.alert(w.rule("Memory critical", priority="P1"))       # unstamped: rule priority
    await sweep(w)
    assert (await sweep(w, plus_minutes=3))["escalated"] == 0
    await sweep(w, plus_minutes=5)
    assert [p.ctx["delivery_event"] for p in w.pages] == ["escalation:S0:r0", "escalation:S1:r0"]
    assert w.pages[1].people == ["+966501234567", "omar@example.test"]
    assert w.pages[1].ctx["lifecycle"] == "escalated" and "Escalation S1" in w.pages[1].ctx["subject"]
    assert w.state(alert_id)["history"].keys() == {"0", "1"}


@pytest.mark.asyncio
async def test_expired_acknowledgement_reopens_and_renotifies(world):
    w = world
    w.policy("P1 matrix", [(0, [sched(w.sch.primary)])], expiry=60)
    alert_id = w.alert(w.rule("Memory critical", priority="P1"), status="acknowledged",
                       minutes_ago=90, ack_ago=70)
    counts = await sweep(w)
    assert counts["ack_expired"] == 1
    assert w.db.scalar("SELECT status FROM alerts WHERE id = $1", alert_id) == "active"
    assert w.pages[0].ctx["delivery_event"] == "ack_expired:1"
    assert w.pages[0].people == ["aisha@example.test"]
    (kind, detail, reason), = w.events(alert_id, "ack_expired")
    assert detail["expiry_minutes"] == 60 and "60 minutes" in reason


@pytest.mark.asyncio
async def test_reviews_continue_after_acknowledgement_to_the_owner(world):
    w = world
    w.policy("P1 matrix", [(0, [sched(w.sch.primary)])], review=15)
    alert_id = w.alert(w.rule("Memory critical", priority="P1"), minutes_ago=1)
    await sweep(w)                                                          # S0
    w.db.execute("UPDATE alerts SET status = 'acknowledged', acknowledged_at = now(),"
                 " acknowledged_by = $2, assigned_to = $2 WHERE id = $1", alert_id, w.u.lead)
    assert (await sweep(w, plus_minutes=10))["reviews"] == 0
    assert (await sweep(w, plus_minutes=15))["reviews"] == 1
    review = w.pages[-1]
    assert review.ctx["delivery_event"] == "review:1"
    assert review.people == ["aisha@example.test", "lena@example.test"]     # last step + owner
    assert "Unresolved update 1" in review.ctx["subject"]
    assert len(w.events(alert_id, "review")) == 1


@pytest.mark.asyncio
async def test_resolution_sends_the_all_clear_to_everyone_notified(world):
    w = world
    w.policy("P1 matrix", [(0, [sched(w.sch.primary), chan(w.ch.noc)]), (5, [sched(w.sch.backup)])])
    alert_id = w.alert(w.rule("Memory critical", priority="P1"))
    await sweep(w)
    await sweep(w, plus_minutes=6)
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", alert_id)
    counts = await sweep(w, plus_minutes=7)
    assert counts["closed"] == 1
    clear = w.pages[-1]
    assert clear.ctx["delivery_event"] == "all_clear" and clear.channels == [w.ch.noc]
    assert clear.people == ["+966501234567", "aisha@example.test", "omar@example.test"]
    assert w.state(alert_id)["closed"] is True
    assert (await sweep(w, plus_minutes=8))["closed"] == 0                   # once


@pytest.mark.asyncio
async def test_recovery_before_a_timer_cancels_pending_steps(world):
    w = world
    w.policy("P1 matrix", [(0, [sched(w.sch.primary)]), (5, [sched(w.sch.backup)])])
    alert_id = w.alert(w.rule("Memory critical", priority="P1"))
    await sweep(w)
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", alert_id)
    await sweep(w, plus_minutes=10)
    assert [p.ctx["delivery_event"] for p in w.pages] == ["escalation:S0:r0", "all_clear"]


@pytest.mark.asyncio
async def test_an_empty_step_uses_the_fallback_with_a_coverage_gap(world):
    w = world
    w.policy("P1 matrix", [(0, [sched(w.sch.empty)])])
    alert_id = w.alert(w.rule("Memory critical", priority="P1"))
    counts = await sweep(w)
    assert counts["fallbacks"] == 1
    assert w.pages[0].ctx["delivery_event"] == "fallback:S0" and w.pages[0].channels == [w.ch.fb]
    (kind, detail, reason), = w.events(alert_id, "coverage_gap")
    assert detail["fallback"] is True and any("Nobody is on call in Nobody" in g for g in detail["gaps"])


@pytest.mark.asyncio
async def test_all_deliveries_failed_records_route_failed_and_uses_the_fallback(world):
    w = world
    w.policy("P1 matrix", [(0, [chan(w.ch.noc)])])
    alert_id = w.alert(w.rule("Memory critical", priority="P1"))
    w.behaviour.results = [0, 1]                    # the step fails, the fallback is accepted
    await sweep(w)
    assert [p.ctx["delivery_event"] for p in w.pages] == ["escalation:S0:r0", "fallback:S0"]
    assert w.pages[1].channels == [w.ch.fb]
    (kind, detail, reason), = w.events(alert_id, "route_failed")
    assert detail["fallback"] is True and detail["event"] == "escalation:S0:r0"
    assert w.ch.fb in w.state(alert_id)["channels"]


@pytest.mark.asyncio
async def test_a_priority_default_never_takes_over_a_rule_with_its_own_tiers(world):
    w = world
    levels = [{"after_minutes": 1, "notify_channels": [w.ch.legacy]}]
    legacy_rule = w.rule("Disk warning", severity="warning", levels=levels)
    plain_rule = w.rule("Swap warning", severity="warning")
    # A P3 default exists (e.g. the recommended matrix was created).
    w.policy("P3 queue", [(0, [chan(w.ch.noc)])], prio=("P3",))
    a_legacy = str(w.db.scalar(
        "INSERT INTO alerts (rule_id, severity, message, status, triggered_at, metadata)"
        " VALUES ($1, 'warning', 'Disk 91%', 'active', now() - interval '5 minutes', '{\"notified\": true}')"
        " RETURNING id", legacy_rule))
    a_plain = str(w.db.scalar(
        "INSERT INTO alerts (rule_id, severity, message, status, triggered_at, metadata)"
        " VALUES ($1, 'warning', 'Swap 92%', 'active', now() - interval '5 minutes', '{\"notified\": true}')"
        " RETURNING id", plain_rule))
    async with session(w.db) as s:
        await esc.sweep_escalations(s)
    by_channel = {tuple(p.channels): p.ctx["delivery_event"] for p in w.pages}
    # The rule with its own tiers keeps escalating through them...
    assert by_channel[(w.ch.legacy,)] == "escalation:L1:r0"
    assert w.state(a_legacy).get("mode") != "policy"
    # ...while a rule without tiers is routed by the P3 default.
    assert by_channel[(w.ch.noc,)] == "escalation:S0:r0"
    assert w.state(a_plain)["mode"] == "policy"


@pytest.mark.asyncio
async def test_unannounced_incidents_do_not_escalate(world):
    w = world
    w.policy("P1 matrix", [(0, [chan(w.ch.noc)])])
    rule = w.rule("Memory critical", priority="P1")
    w.alert(rule, meta={"notified": False})
    await sweep(w)
    assert w.pages == []


@pytest.mark.asyncio
async def test_route_explanation(world):
    w = world
    pol = w.policy("P1 matrix", [(0, [sched(w.sch.primary)]), (5, [sched(w.sch.backup)])], review=15)
    alert_id = w.alert(w.rule("Memory critical", priority="P1"), priority="P1")
    await sweep(w)
    async with session(w.db) as s:
        out = await esc.explain_route(s, alert_id)
    assert out["policy"] == {"id": pol, "name": "P1 matrix", "source": "priority_default"}
    assert out["state"]["step"] == 0 and out["state"]["next_action"] == "step:1"
    assert out["steps"][0]["notified_at"] and out["steps"][0]["current"]
    assert {r["address"] for r in out["steps"][1]["recipients"]} == {"o***@example.test", "•••567"}  # masked
    async with session(w.db) as s:
        revealed = await esc.explain_route(s, alert_id, reveal=True)
    assert {r["address"] for r in revealed["steps"][1]["recipients"]} == {"omar@example.test", "+966501234567"}
