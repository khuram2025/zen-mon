"""Warning grouping and notification budgets (NOTIFY 02), on a disposable PostgreSQL.

A correlated burst of warnings on one site becomes ONE summary per channel
listing every incident; another site stays separate; critical (and P1/P2)
notifications, recoveries and non-groupable sends go out at once; budget
overflow is held into a digest, never dropped; a digest whose incidents all
recovered is cancelled with its reason; two concurrent release sweeps send a
digest once; with the settings off nothing changes; every hold is on the
incident timeline with its reason and release condition. PagerDuty is never
held or budgeted (its incidents resolve by their own dedup key); a failed
digest is retried (three attempts) before it is marked failed; a manual
release is never re-claimed by the sweep mid-send; direct pages use the only
enabled gateway when none is flagged default.

Only the transports (SMTP, webhooks) and the dashboard base URL are faked.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace as NS

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine
from app.services import alert_delivery_log as dl
from app.services import host_alert_service as has
from app.services import notification_grouping as ng

from inventory_fleet import api_client
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
    dl.reset_for_tests()
    ng.reset_for_tests()
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM notification_gateways")
    db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
               " VALUES ('mail', 'smtp', $1::jsonb, true, true)",
               {"host": "smtp.invalid", "from_email": "zenplus@example.invalid"})

    def channel(name, kind, config):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config)"
                             " VALUES ($1, $2, $3::jsonb) RETURNING id", name, kind, config))

    ch = NS(noc=channel("noc", "email", {"recipients": "ops@example.test, noc@example.test"}),
            hook=channel("hook", "webhook", {"url": "https://hooks.example.invalid/zenplus"}))
    sites, devices = {}, {}
    for site in ("Riyadh DC", "Jeddah branch"):
        sid = db.scalar("INSERT INTO sites (name) VALUES ($1) RETURNING id", site)
        sensor = db.scalar("INSERT INTO sensors (name, site_id, status) VALUES ($1, $2, 'online') RETURNING id",
                           f"sensor-{site}", sid)
        sites[site] = sid
        devices[site] = sensor
    rule = str(db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, severity, notify_channels)"
                         " VALUES ('Memory high', 'memory', '>', 90, 'warning', $1::jsonb) RETURNING id",
                         [ch.noc, ch.hook]))
    counter = {"n": 0}

    def device(site):
        counter["n"] += 1
        n = counter["n"]
        return str(db.scalar("INSERT INTO devices (hostname, ip_address, default_sensor_id, tags)"
                             " VALUES ($1, $2::inet, $3, '[\"team-a\"]'::jsonb) RETURNING id",
                             f"sw-{n:02d}", f"10.250.{n // 200}.{n % 200 + 1}", devices[site]))

    def alert(site="Riyadh DC", severity="warning", priority=None):
        dev = device(site)
        return str(db.scalar(
            "INSERT INTO alerts (device_id, rule_id, severity, priority, message, status, metadata)"
            " VALUES ($1, $2, $3, $4, 'Memory high', 'active', '{}'::jsonb) RETURNING id",
            dev, rule, severity, priority))

    messages: list[dict] = []
    hooks: list[dict] = []
    behaviour = NS(delay=0.0, fail_email=False)

    async def send_email(gw, recipients, subject, body, html_body=None):
        await asyncio.sleep(behaviour.delay)
        if behaviour.fail_email:
            return False
        messages.append({"to": sorted(r.strip().lower() for r in recipients.split(",")),
                         "subject": subject, "body": body, "html": html_body or ""})
        return True

    async def send_hook(kind, cfg, ctx):
        await asyncio.sleep(behaviour.delay)
        hooks.append(ctx)
        return True, None

    async def dashboard_url(db_, path="/alerts"):
        return f"https://zen.example.test{path}"

    monkeypatch.setattr(alert_engine, "_send_email", send_email)
    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", send_hook)
    monkeypatch.setattr(alert_engine, "_dashboard_url", dashboard_url)

    def settings(**kw):
        db.execute("INSERT INTO system_settings (key, value) VALUES ('notification_grouping', $1::jsonb)"
                   " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                   {**ng.RECOMMENDED_SETTINGS, **kw})

    # The senders handle PagerDuty, but migrate-001's type CHECK does not list
    # it yet; lift it on this disposable database to model the channel.
    db.execute("ALTER TABLE notification_channels DROP CONSTRAINT IF EXISTS notification_channels_type_check")
    ch.pd = channel("pager", "pagerduty", {"routing_key": "R0UT1NGKEY"})
    return NS(db=db, ch=ch, rule=rule, alert=alert, messages=messages, hooks=hooks,
              behaviour=behaviour, settings=settings, sites=sites)


def ctx(alert_id, *, event="trigger", groupable=True, severity="warning", priority="P3", **extra):
    recovery = event == "recovery"
    return {"subject": f"[{severity.upper()}] Memory high — {alert_id[:8]}", "body": "Memory high.",
            "message": "ZenPlus WARNING — Memory high", "rule_name": "Memory high",
            "hostname": f"host-{alert_id[:4]}", "severity": severity,
            "status": "RESOLVED" if recovery else "ALERT", "is_recovery": recovery,
            "condition_label": "Memory above 90%", "headline_metric": {"value": "91.2%"},
            "alert_id": alert_id, "delivery_event": event, "priority": priority,
            "groupable": groupable and not recovery, **extra}


async def dispatch(w, alert_id, channels=None, **kw):
    async with session(w.db) as s:
        return await has.dispatch_to_channels(s, channels if channels is not None else [w.ch.noc, w.ch.hook],
                                              ctx(alert_id, **kw))


async def release(w, **kw):
    w.db.execute("UPDATE notification_digests SET release_at = now() - interval '1 second' WHERE status = 'open'")
    async with session(w.db) as s:
        return await ng.release_due(s, **kw)


def digests(w):
    return w.db.query("SELECT d.id::text, d.group_key, d.group_label, d.channel_id::text, d.recipient, d.status,"
                      " d.reason, d.release_condition, d.item_count, d.error FROM notification_digests d"
                      " ORDER BY d.group_label, d.channel_id, d.recipient")


@pytest.mark.asyncio
async def test_a_correlated_burst_becomes_one_digest_per_channel_listing_all_twenty(world):
    w = world
    w.settings()
    ids = [w.alert() for _ in range(20)]
    for aid in ids:
        assert await dispatch(w, aid) == 2           # held counts as handled for both channels
    assert w.messages == [] and w.hooks == []
    rows = digests(w)
    assert len(rows) == 2 and {r[3] for r in rows} == {w.ch.noc, w.ch.hook}
    assert all(r[5] == "open" and r[6] == "grouping" and r[8] == 20 for r in rows)
    assert rows[0][2] == "Riyadh DC · Memory"
    email_digest = next(r for r in rows if r[3] == w.ch.noc)
    assert email_digest[4] == "noc@example.test,ops@example.test"
    assert "group wait 60 s" in email_digest[7] and "never held" in email_digest[7]

    counts = await release(w)
    assert counts["sent"] == 2
    assert len(w.messages) == 1 and len(w.hooks) == 1
    mail = w.messages[0]
    assert mail["to"] == ["noc@example.test", "ops@example.test"]
    assert mail["subject"].startswith("[WARNING] [P3] [Digest] 20 warnings — Riyadh DC · Memory")
    from app.services.alert_phrasing import incident_ref
    for aid in ids:
        assert incident_ref(aid) in mail["html"] and incident_ref(aid) in mail["body"]
        assert f"https://zen.example.test/alerts/{aid}" in mail["html"]
    payload = w.hooks[0]
    assert payload["type"] == "digest" and len(payload["digest"]["items"]) == 20
    assert {s[5] for s in digests(w)} == {"sent"}
    # Durable key for the summary: event digest:<id>, anchored on the first incident.
    keys = w.db.query("SELECT event, state FROM alert_delivery_keys WHERE event LIKE 'digest:%'")
    assert {k[1] for k in keys} == {"accepted"} and len({k[0] for k in keys}) == 2
    # The delivery log says what happened to every held trigger.
    assert w.db.scalar("SELECT count(*) FROM alert_deliveries WHERE outcome = 'held'") == 40


@pytest.mark.asyncio
async def test_an_independent_site_stays_separate(world):
    w = world
    w.settings()
    for _ in range(20):
        await dispatch(w, w.alert("Riyadh DC"))
    for _ in range(3):
        await dispatch(w, w.alert("Jeddah branch"))
    rows = digests(w)
    assert len(rows) == 4
    by_label = {}
    for r in rows:
        by_label.setdefault(r[2], []).append(r[8])
    assert by_label == {"Jeddah branch · Memory": [3, 3], "Riyadh DC · Memory": [20, 20]}
    await release(w)
    subjects = [m["subject"] for m in w.messages]
    assert len(subjects) == 2
    assert any("3 warnings — Jeddah branch · Memory" in s for s in subjects)
    assert any("20 warnings — Riyadh DC · Memory" in s for s in subjects)


@pytest.mark.asyncio
async def test_critical_and_p2_notifications_bypass_the_wait(world):
    w = world
    w.settings()
    crit = w.alert(severity="critical")
    assert await dispatch(w, crit, [w.ch.noc], severity="critical", priority="P2") == 1
    high = w.alert(priority="P2")                       # a warning a rule ranked P2
    assert await dispatch(w, high, [w.ch.noc], priority=None) == 1
    p1 = w.alert()
    assert await dispatch(w, p1, [w.ch.noc], priority="P1") == 1
    assert len(w.messages) == 3 and digests(w) == []


@pytest.mark.asyncio
async def test_recoveries_escalations_and_non_groupable_sends_are_never_held(world):
    w = world
    w.settings()
    told = w.alert()
    assert await dispatch(w, told, [w.ch.noc], groupable=False) == 1     # caller says: not holdable
    assert await dispatch(w, told, [w.ch.noc], event="recovery") == 1
    assert await dispatch(w, told, [w.ch.noc], event="escalation:S1:r0") == 1
    assert await dispatch(w, told, [w.ch.noc], event="review:1", delivery_kind="review") == 1
    assert len(w.messages) == 4 and digests(w) == []


@pytest.mark.asyncio
async def test_a_recovery_before_release_is_listed_in_the_summary_not_sent_alone(world):
    w = world
    w.settings()
    held, other = w.alert(), w.alert()
    await dispatch(w, held, [w.ch.noc])
    await dispatch(w, other, [w.ch.noc])
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", held)
    assert await dispatch(w, held, [w.ch.noc], event="recovery") == 1
    assert w.messages == []                            # nobody heard the trigger: no lone all-clear
    await release(w)
    assert len(w.messages) == 1
    mail = w.messages[0]
    assert "1 warning — Riyadh DC · Memory (1 already recovered)" in mail["subject"]
    assert "Already recovered" in mail["html"]
    kinds = {r[0] for r in w.db.query("SELECT kind FROM alert_events WHERE alert_id = $1", held)}
    assert {"notification_held", "notification_merged", "notification_released"} <= kinds


@pytest.mark.asyncio
async def test_budget_overflow_goes_to_a_digest_and_is_never_dropped(world):
    w = world
    w.settings(group_wait_seconds=0, budget_per_recipient_per_hour=3)
    for _ in range(3):                                   # three non-critical mails this hour
        assert await dispatch(w, w.alert(), [w.ch.noc]) == 1
    assert len(w.messages) == 3
    crit = w.alert(severity="critical")                  # critical: reserved capacity, still sent
    assert await dispatch(w, crit, [w.ch.noc], severity="critical", priority="P2") == 1
    over = [w.alert() for _ in range(2)]
    for aid in over:
        assert await dispatch(w, aid, [w.ch.noc]) == 1   # handled: held, not dropped
    assert len(w.messages) == 4
    rows = digests(w)
    assert len(rows) == 2                                # one per recipient over budget
    assert {r[4] for r in rows} == {"ops@example.test", "noc@example.test"}
    assert all(r[6] == "budget" and r[8] == 2 for r in rows)
    assert "hourly budget (3)" in rows[0][7]
    await release(w)
    assert sorted(m["to"][0] for m in w.messages[4:]) == ["noc@example.test", "ops@example.test"]
    assert all(len(m["to"]) == 1 for m in w.messages[4:])
    ev = w.db.query("SELECT reason, detail->>'reason_code' FROM alert_events"
                    " WHERE alert_id = $1 AND kind = 'notification_held'", over[0])
    assert ev and all(code == "budget" and "budget" in reason for reason, code in ev)


@pytest.mark.asyncio
async def test_a_digest_whose_incidents_all_recovered_is_cancelled_with_its_reason(world):
    w = world
    w.settings()
    ids = [w.alert(), w.alert()]
    for aid in ids:
        await dispatch(w, aid, [w.ch.noc])
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now()")
    counts = await release(w)
    assert counts["cancelled"] == 1 and w.messages == []
    (row,) = digests(w)
    assert row[5] == "cancelled" and "All 2 held incidents recovered before release" in row[9]
    assert w.db.scalar("SELECT count(*) FROM alert_events WHERE kind = 'notification_cancelled'") == 2


@pytest.mark.asyncio
async def test_two_concurrent_release_sweeps_send_once(world):
    w = world
    w.settings()
    for _ in range(5):
        await dispatch(w, w.alert(), [w.ch.noc])
    w.behaviour.delay = 0.3
    w.db.execute("UPDATE notification_digests SET release_at = now() - interval '1 second'")

    async def sweep():
        async with session(w.db) as s:
            return await ng.release_due(s)

    a, b = await asyncio.wait_for(asyncio.gather(sweep(), sweep()), 20)
    assert a["sent"] + b["sent"] == 1
    assert len(w.messages) == 1
    # A retry after the fact (stale claim, release-now on a sent digest) does not resend.
    async with session(w.db) as s:
        with pytest.raises(ng.ReleaseConflict):
            await ng.release_now(s, digests(w)[0][0])
    assert len(w.messages) == 1


@pytest.mark.asyncio
async def test_settings_off_leaves_behaviour_unchanged(world):
    w = world                                            # no settings row: defaults, disabled
    assert ng.clean_settings(None)["enabled"] is False
    ids = [w.alert() for _ in range(20)]
    for aid in ids:
        assert await dispatch(w, aid) == 2
    assert len(w.messages) == 20 and len(w.hooks) == 20 and digests(w) == []
    assert w.db.scalar("SELECT count(*) FROM alert_events WHERE kind LIKE 'notification_%'") == 0
    # A stored but disabled configuration is the same.
    w.settings(enabled=False)
    await dispatch(w, w.alert())
    assert len(w.messages) == 21 and digests(w) == []


@pytest.mark.asyncio
async def test_a_retried_trigger_is_held_once(world):
    w = world
    w.settings()
    aid = w.alert()
    assert await dispatch(w, aid, [w.ch.noc]) == 1
    assert await dispatch(w, aid, [w.ch.noc]) == 1       # a second worker / a retry
    (row,) = digests(w)
    assert row[8] == 1
    assert w.db.scalar("SELECT count(*) FROM notification_digest_items") == 1


@pytest.mark.asyncio
async def test_direct_recipients_share_dedupe_keys_and_can_be_held(world):
    w = world
    direct = [{"kind": "email", "address": "Lead@Example.test", "user_id": None, "label": "Team lead"},
              {"kind": "email", "address": "ops@example.test", "user_id": None, "label": "Ops"}]
    aid = w.alert()
    n = await dispatch(w, aid, [w.ch.noc], groupable=False, recipients=direct)
    assert n == 2                                         # the channel + the lead (ops was covered)
    assert sorted(tuple(m["to"]) for m in w.messages) == [("lead@example.test",),
                                                          ("noc@example.test", "ops@example.test")]
    keys = w.db.query("SELECT channel_id::text, recipient, state FROM alert_delivery_keys"
                      " WHERE alert_id = $1 AND channel_id = $2", aid, ng.DIRECT_CHANNEL_ID)
    assert keys == [(ng.DIRECT_CHANNEL_ID, "lead@example.test", "accepted")]
    # A retry resends nothing; every route an earlier dispatch reached counts as delivered.
    assert await dispatch(w, aid, [w.ch.noc], groupable=False, recipients=direct) == 3
    assert len(w.messages) == 2
    # Held per person, released to that person only.
    w.settings()
    other = w.alert()
    assert await dispatch(w, other, [], recipients=direct[:1]) == 1
    (row,) = digests(w)
    assert row[3] == ng.DIRECT_CHANNEL_ID and row[4] == "email:lead@example.test"
    await release(w)
    assert w.messages[-1]["to"] == ["lead@example.test"] and "[Digest]" in w.messages[-1]["subject"]


@pytest.mark.asyncio
async def test_settings_are_cleaned_and_clamped():
    s = ng.clean_settings({"enabled": 1, "group_by": ["site", "bogus", "appliance"], "group_wait_seconds": 99999,
                           "max_items": 1, "budget_per_recipient_per_hour": None})
    assert s["enabled"] is True and s["group_by"] == ["appliance", "site"]
    assert s["group_wait_seconds"] == 3600 and s["max_items"] == 5 and s["budget_per_recipient_per_hour"] == 0
    assert ng.event_family("host_memory_pct") == "memory" and ng.event_family("if_errors") == "interface"
    assert ng.event_family(None, service=True) == "service" and ng.event_family(None, device=True) == "reachability"


def test_holds_are_on_the_timeline_and_the_api_manages_digests(world):
    w = world
    w.settings()
    aid = w.alert()
    asyncio.run(dispatch(w, aid))
    with api_client(w.db, role="admin", persist_user=True) as client:
        detail = client.get(f"/api/v1/alerts/{aid}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        holds = body["notification_holds"]
        assert len(holds) == 2 and all(h["status"] == "open" and h["reason"] == "grouping" for h in holds)
        assert all("Released at" in h["release_condition"] for h in holds)
        held = [e for e in body["events"] if e["kind"] == "notification_held"]
        assert len(held) == 2
        assert held[0]["label"] == ng.EVENT_LABELS["notification_held"]
        assert held[0]["reason"].startswith("Grouped with related memory notifications for Riyadh DC")
        assert "Released at" in held[0]["detail"]["release_condition"]
        assert any(d["outcome"] == "held" for d in body["deliveries"])

        cfg = client.get("/api/v1/alerts/notification-grouping").json()
        assert cfg["enabled"] is True and cfg["recommended"]["enabled"] is True
        assert "event_family" in cfg["group_by_choices"]
        put = client.put("/api/v1/alerts/notification-grouping",
                         json={**ng.DEFAULT_SETTINGS, "group_by": ["site"], "group_wait_seconds": 120})
        assert put.status_code == 200 and put.json()["group_wait_seconds"] == 120
        assert put.json()["enabled"] is False
        assert client.put("/api/v1/alerts/notification-grouping",
                          json={"group_by": ["nope"]}).status_code == 422

        listing = client.get("/api/v1/alerts/digests?status=open").json()["items"]
        assert len(listing) == 2 and {d["channel_name"] for d in listing} == {"noc", "hook"}
        did = next(d["id"] for d in listing if d["channel_name"] == "noc")
        one = client.get(f"/api/v1/alerts/digests/{did}").json()
        assert one["item_count"] == 1 and one["items"][0]["link"] == f"/alerts/{aid}"
        assert one["items"][0]["already_recovered"] is False
        rel = client.post(f"/api/v1/alerts/digests/{did}/release")
        assert rel.status_code == 200, rel.text
        assert rel.json()["status"] == "sent" and rel.json()["outcome"] == "sent"
        assert client.post(f"/api/v1/alerts/digests/{did}/release").status_code == 409
        assert client.get("/api/v1/alerts/digests/00000000-0000-0000-0000-000000000000").status_code == 404
    assert len(w.messages) == 1

    # A role without alerts.manage may read but not release; a foreign scope sees nothing.
    with api_client(w.db, role="digest-viewer", permissions=["alerts.view"], scope_tags=("team-b",)) as client:
        assert client.get("/api/v1/alerts/digests").json()["items"] == []
        assert client.get(f"/api/v1/alerts/digests/{did}").status_code == 404
        assert client.post(f"/api/v1/alerts/digests/{did}/release").status_code == 403
        assert client.put("/api/v1/alerts/notification-grouping", json={}).status_code == 403


@pytest.mark.asyncio
async def test_pagerduty_is_never_held_or_budgeted_and_its_recoveries_go_out(world):
    w = world
    w.settings()
    aid = w.alert()
    assert await dispatch(w, aid, [w.ch.pd]) == 1
    assert len(w.hooks) == 1 and digests(w) == []
    # Budget only: a PagerDuty route over budget is still paged.
    w.settings(group_wait_seconds=0, budget_per_recipient_per_hour=1)
    for _ in range(3):
        assert await dispatch(w, w.alert(), [w.ch.pd]) == 1
    assert len(w.hooks) == 4 and digests(w) == []
    # A trigger held on another channel does not swallow the PagerDuty recovery.
    w.settings()
    other = w.alert()
    await dispatch(w, other, [w.ch.noc, w.ch.pd])
    assert len(w.hooks) == 5 and len(digests(w)) == 1
    assert await dispatch(w, other, [w.ch.pd], event="recovery") == 1
    assert len(w.hooks) == 6 and w.hooks[-1]["delivery_event"] == "recovery"


@pytest.mark.asyncio
async def test_a_failed_digest_is_retried_then_marked_failed(world):
    w = world
    w.settings()
    await dispatch(w, w.alert(), [w.ch.noc])
    w.behaviour.fail_email = True
    for attempt in (1, 2):
        counts = await release(w)
        assert counts["retrying"] == 1 and counts["failed"] == 0
        (row,) = digests(w)
        assert row[5] == "open" and row[9].startswith(f"Attempt {attempt} of 3 failed")
        wait = w.db.scalar("SELECT extract(epoch FROM release_at - now()) FROM notification_digests")
        assert 250 < wait <= 300                                     # one group interval later
    counts = await release(w)
    assert counts["failed"] == 1
    (row,) = digests(w)
    assert row[5] == "failed" and row[9].startswith("Attempt 3 of 3 failed")
    assert w.db.scalar("SELECT count(*) FROM alert_events WHERE kind = 'notification_release_failed'") == 3
    # Fixed route: a manual release still sends it.
    w.behaviour.fail_email = False
    async with session(w.db) as s:
        assert await ng.release_now(s, row[0]) == "sent"
    assert len(w.messages) == 1


@pytest.mark.asyncio
async def test_a_retry_merges_into_a_digest_opened_while_it_was_sending(world):
    w = world
    w.settings()
    await dispatch(w, w.alert(), [w.ch.noc])

    async def failing_send(db_, channels, ctx):
        await dispatch(w, w.alert(), [w.ch.noc])                     # a new hold meanwhile
        return 0

    w.db.execute("UPDATE notification_digests SET release_at = now() - interval '1 second'")
    async with session(w.db) as s:
        counts = await ng.release_due(s, dispatch=failing_send)
    assert counts["retrying"] == 1
    rows = {r[5]: r for r in digests(w)}
    assert set(rows) == {"open", "cancelled"}
    assert rows["open"][8] == 2 and "moved to the open summary" in rows["cancelled"][9]
    assert w.db.scalar("SELECT count(*) FROM notification_digest_items WHERE digest_id = $1",
                       rows["open"][0]) == 2


@pytest.mark.asyncio
async def test_a_manual_release_is_not_reclaimed_by_the_sweep_mid_send(world):
    w = world
    w.settings()
    await dispatch(w, w.alert(), [w.ch.noc])
    w.db.execute("UPDATE notification_digests SET status = 'failed', release_at = now() - interval '1 hour'")
    (row,) = digests(w)
    reclaimed = []

    async def slow_send(db_, channels, ctx):
        async with session(w.db) as other:
            reclaimed.append((await ng.release_due(other))["claimed"])
        return await has.dispatch_to_channels(db_, channels, ctx)

    async with session(w.db) as s:
        assert await ng.release_now(s, row[0], dispatch=slow_send) == "sent"
    assert reclaimed == [0] and len(w.messages) == 1


@pytest.mark.asyncio
async def test_direct_pages_use_the_only_enabled_gateway_when_none_is_default(world):
    w = world
    w.db.execute("UPDATE notification_gateways SET is_default = false")
    lead = [{"kind": "email", "address": "lead@example.test", "user_id": None, "label": "Lead"}]
    assert await dispatch(w, w.alert(), [], groupable=False, recipients=lead) == 1
    assert w.messages[-1]["to"] == ["lead@example.test"]
    async with session(w.db) as s:
        assert await has.direct_gateway_kinds(s) == {"email"}
    # Two enabled gateways and no default: ambiguous, so nothing is guessed.
    w.db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
                 " VALUES ('mail-2', 'smtp', '{}'::jsonb, false, true)")
    assert await dispatch(w, w.alert(), [], groupable=False, recipients=lead) == 0
    assert len(w.messages) == 1
    async with session(w.db) as s:
        assert await has.direct_gateway_kinds(s) == set()
