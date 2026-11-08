"""Alert storm (correlation) policy, on a disposable PostgreSQL.

More than N assets with the same kind of event inside the window become ONE
combined notification per channel and recipient instead of one per asset;
the first N still go out on their own (until then it is not a storm). While
the storm runs, held incidents' recoveries and escalations are folded into
its updates; once it goes quiet a closing summary goes to every route that
heard about it, as a PagerDuty resolve when everything recovered. Off by
default; other event types and other scopes are untouched.

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
from app.services import alert_storm as storm
from app.services import host_alert_service as has
from app.services import notification_grouping as ng

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
    storm.reset_for_tests()
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM notification_gateways")
    db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
               " VALUES ('mail', 'smtp', $1::jsonb, true, true)",
               {"host": "smtp.invalid", "from_email": "zenplus@example.invalid"})
    db.execute("ALTER TABLE notification_channels DROP CONSTRAINT IF EXISTS notification_channels_type_check")

    def channel(name, kind, config):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config)"
                             " VALUES ($1, $2, $3::jsonb) RETURNING id", name, kind, config))

    ch = NS(noc=channel("noc", "email", {"recipients": "ops@example.test, noc@example.test"}),
            hook=channel("hook", "webhook", {"url": "https://hooks.example.invalid/zenplus"}),
            pd=channel("pager", "pagerduty", {"routing_key": "R0UT1NGKEY"}))
    sensors = {}
    for site in ("Riyadh DC", "Jeddah branch"):
        sid = db.scalar("INSERT INTO sites (name) VALUES ($1) RETURNING id", site)
        sensors[site] = db.scalar("INSERT INTO sensors (name, site_id, status) VALUES ($1, $2, 'online')"
                                  " RETURNING id", f"sensor-{site}", sid)
    rules = {
        "down": str(db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, severity)"
                              " VALUES ('Device down', 'ping_status', '==', 0, 'critical') RETURNING id")),
        "memory": str(db.scalar("INSERT INTO alert_rules (name, metric, operator, threshold, severity)"
                                " VALUES ('Memory high', 'memory', '>', 90, 'warning') RETURNING id")),
    }
    counter = {"n": 0}

    def alert(site="Riyadh DC", kind="down"):
        counter["n"] += 1
        n = counter["n"]
        dev = db.scalar("INSERT INTO devices (hostname, ip_address, default_sensor_id)"
                        " VALUES ($1, $2::inet, $3) RETURNING id",
                        f"sw-{n:02d}", f"10.251.0.{n}", sensors[site])
        sev = "critical" if kind == "down" else "warning"
        return str(db.scalar(
            "INSERT INTO alerts (device_id, rule_id, severity, message, status, metadata)"
            " VALUES ($1, $2, $3, $4, 'active', '{}'::jsonb) RETURNING id",
            dev, rules[kind], sev, "Device is down (no response to ping)" if kind == "down" else "Memory high"))

    mails: list[dict] = []
    hooks: list[tuple[str, dict]] = []

    async def send_email(gw, recipients, subject, body, html_body=None):
        mails.append({"to": sorted(r.strip().lower() for r in recipients.split(",")),
                      "subject": subject, "body": body, "html": html_body or ""})
        return True

    async def send_hook(kind, cfg, ctx):
        hooks.append((kind, ctx))
        return True, None

    async def dashboard_url(db_, path="/alerts"):
        return f"https://zen.example.test{path}"

    monkeypatch.setattr(alert_engine, "_send_email", send_email)
    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", send_hook)
    monkeypatch.setattr(alert_engine, "_dashboard_url", dashboard_url)

    def settings(**kw):
        db.execute("INSERT INTO system_settings (key, value) VALUES ('alert_storm', $1::jsonb)"
                   " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                   {**storm.DEFAULT_SETTINGS, "enabled": True, "threshold": 3, **kw})

    return NS(db=db, ch=ch, alert=alert, mails=mails, hooks=hooks, settings=settings)


def ctx(alert_id, *, event="trigger", severity="critical", priority="P2", **extra):
    recovery = event == "recovery"
    return {"subject": f"[{severity.upper()}] Device down — {alert_id[:8]}", "body": "Device down.",
            "message": "Device down", "rule_name": "Device down", "hostname": f"host-{alert_id[:4]}",
            "severity": severity, "status": "RESOLVED" if recovery else "DOWN", "is_recovery": recovery,
            "alert_id": alert_id, "delivery_event": event, "priority": priority, **extra}


async def dispatch(w, alert_id, channels=None, **kw):
    async with session(w.db) as s:
        return await has.dispatch_to_channels(
            s, channels if channels is not None else [w.ch.noc, w.ch.hook, w.ch.pd], ctx(alert_id, **kw))


async def release(w):
    w.db.execute("UPDATE notification_digests SET release_at = now() - interval '1 second' WHERE status = 'open'")
    async with session(w.db) as s:
        return await ng.release_due(s)


def pd(w):
    return [c for k, c in w.hooks if k == "pagerduty"]


def webhooks(w):
    return [c for k, c in w.hooks if k == "webhook"]


@pytest.mark.asyncio
async def test_off_by_default_every_asset_is_notified_on_its_own(world):
    w = world
    for _ in range(8):
        assert await dispatch(w, w.alert()) == 3
    assert len(w.mails) == 8 and len(webhooks(w)) == 8 and len(pd(w)) == 8
    assert w.db.scalar("SELECT count(*) FROM alert_storms") == 0


@pytest.mark.asyncio
async def test_more_than_threshold_down_becomes_one_combined_notification_per_channel(world):
    w = world
    w.settings()
    ids = [w.alert() for _ in range(12)]
    for aid in ids:
        assert await dispatch(w, aid) == 3          # held counts as handled
    # The first three are not a storm yet: sent on their own.
    assert len(w.mails) == 3 and len(webhooks(w)) == 3 and len(pd(w)) == 3
    s = w.db.query("SELECT id, family, status, threshold FROM alert_storms")
    assert len(s) == 1 and s[0][1:] == ("reachability", "active", 3)
    members = dict(w.db.query("SELECT alert_id::text, held FROM alert_storm_members"))
    assert len(members) == 12 and sum(members.values()) == 9
    digests = w.db.query("SELECT channel_id::text, status, reason, item_count FROM notification_digests")
    assert {d[0] for d in digests} == {w.ch.noc, w.ch.hook, w.ch.pd}
    assert all(d[1:] == ("open", "storm", 9) for d in digests)
    assert w.db.scalar("SELECT count(*) FROM alert_deliveries WHERE outcome = 'held'") == 27

    counts = await release(w)
    assert counts["sent"] == 3
    assert len(w.mails) == 4 and len(webhooks(w)) == 4 and len(pd(w)) == 4
    mail = w.mails[-1]
    assert mail["subject"].startswith("[CRITICAL] [P2] Alert storm: 12 devices went down or became unreachable since")
    for i in range(1, 13):
        assert f"sw-{i:02d}" in mail["body"]
    assert "notified individually" in mail["html"]
    hook = webhooks(w)[-1]
    assert hook["type"] == "storm" and hook["storm_summary"]["total"] == 12
    assert hook["storm_summary"]["phase"] == "first"
    assert pd(w)[-1]["dedup_key"] == f"zenplus:storm:{s[0][0]}" and not pd(w)[-1]["is_recovery"]
    # More devices go down while it runs: held into the next update, nothing sent now.
    late = w.alert()
    await dispatch(w, late)
    assert len(w.mails) == 4
    keys = w.db.query("SELECT state FROM alert_delivery_keys WHERE event LIKE 'storm:%'")
    assert {k[0] for k in keys} == {"accepted"} and len(keys) == 4      # 2 mail + hook + pager


@pytest.mark.asyncio
async def test_recoveries_escalations_and_closing_summary(world):
    w = world
    w.settings()
    ids = [w.alert() for _ in range(6)]
    for aid in ids:
        await dispatch(w, aid)
    await release(w)
    sent = len(w.mails)                                                 # 3 individual + 1 storm
    assert sent == 4
    # An individually notified member keeps its own recovery (its pager page resolves).
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", ids[0])
    await dispatch(w, ids[0], event="recovery", severity="info")
    assert len(w.mails) == sent + 1 and pd(w)[-1]["is_recovery"]
    # A held member's recovery and another's escalation are folded into the storm.
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = $1", ids[5])
    await dispatch(w, ids[5], event="recovery", severity="info")
    await dispatch(w, ids[4], event="escalation:L2", escalation_level=2)
    assert len(w.mails) == sent + 1
    held = w.db.query("SELECT i.event FROM notification_digest_items i JOIN notification_digests d"
                      " ON d.id = i.digest_id WHERE d.status = 'open' AND d.channel_id = $1::uuid", w.ch.noc)
    assert sorted(h[0] for h in held) == ["escalation:L2", "recovery"]

    # Quiet for the whole window: the storm ends and every route gets a closing summary.
    w.db.execute("UPDATE alert_storms SET last_event_at = now() - interval '6 minutes'")
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now()")
    counts = await release(w)
    assert counts["sent"] == 3
    assert w.db.scalar("SELECT status FROM alert_storms") == "ended"
    final = w.mails[-1]
    assert final["subject"].startswith("[RESOLVED]") and "Alert storm over" in final["subject"]
    assert "all 6 recovered" in final["subject"]
    assert pd(w)[-1]["is_recovery"] and pd(w)[-1]["dedup_key"].startswith("zenplus:storm:")
    # Nothing more is sent for the ended storm.
    assert (await release(w))["claimed"] == 0


@pytest.mark.asyncio
async def test_closing_summary_lists_assets_still_down(world):
    w = world
    w.settings()
    ids = [w.alert() for _ in range(5)]
    for aid in ids:
        await dispatch(w, aid)
    await release(w)
    w.db.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE id = ANY($1::uuid[])", ids[:3])
    w.db.execute("UPDATE alert_storms SET last_event_at = now() - interval '6 minutes'")
    await release(w)
    final = w.mails[-1]
    assert "3 recovered, 2 still affected" in final["subject"] and not final["subject"].startswith("[RESOLVED]")
    assert "sw-04" in final["body"].split("Recovered")[0] and "sw-05" in final["body"].split("Recovered")[0]
    assert not pd(w)[-1]["is_recovery"]


@pytest.mark.asyncio
async def test_other_event_types_and_scopes_are_untouched(world):
    w = world
    w.settings(scope="site")
    # Memory warnings are not a selected event type.
    for _ in range(6):
        await dispatch(w, w.alert(kind="memory"), severity="warning", priority="P3")
    # Three devices down on each of two sites: at most three per site, no storm.
    for site in ("Riyadh DC", "Jeddah branch"):
        for _ in range(3):
            await dispatch(w, w.alert(site=site))
    assert len(w.mails) == 12 and w.db.scalar("SELECT count(*) FROM alert_storms") == 0
    # A fourth device on one site starts a storm for that site only.
    await dispatch(w, w.alert(site="Jeddah branch"))
    assert w.db.query("SELECT scope_label FROM alert_storms") == [("Jeddah branch",)]
    assert len(w.mails) == 12                      # held for the Jeddah storm summary
    # Each asset counts once however often its trigger is dispatched.
    again = w.alert(site="Riyadh DC")
    await dispatch(w, again)
    await dispatch(w, again)
    assert w.db.scalar("SELECT count(*) FROM alert_storm_events WHERE alert_id = $1", again) == 1


@pytest.mark.asyncio
async def test_window_counts_only_recent_events(world):
    w = world
    w.settings(window_minutes=5)
    for _ in range(3):
        await dispatch(w, w.alert())
    w.db.execute("UPDATE alert_storm_events SET at = now() - interval '10 minutes'")
    await dispatch(w, w.alert())
    assert w.db.scalar("SELECT count(*) FROM alert_storms") == 0 and len(w.mails) == 4


def test_settings_are_cleaned_and_described():
    s = storm.clean_settings({"enabled": True, "threshold": 1, "window_minutes": 999, "families": ["reboot", "bogus"],
                              "scope": "nowhere", "update_minutes": 1})
    assert s["threshold"] == 2 and s["window_minutes"] == 120 and s["update_minutes"] == 5
    assert s["families"] == ["reboot"] and s["scope"] == "all"
    assert storm.clean_settings(None)["enabled"] is False
    text = storm.policy_sentence({**storm.DEFAULT_SETTINGS, "enabled": True})
    assert "more than 5 assets" in text and "devices down" in text and "one combined notification" in text
    assert storm.policy_sentence(storm.DEFAULT_SETTINGS).startswith("Off")


def test_reboot_and_restart_metrics_share_the_reboot_family():
    assert ng.event_family("uptime_reset") == ng.event_family("host_restart") == "reboot"
