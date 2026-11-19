"""Alerting text phase 2 (plan 2.6 and the text-review leftovers).

* {site} / {group} / {site_suffix} for every family with an asset, the site
  in the mail details and as a short SMS suffix ("... at Site Riyadh-HQ").
* Chat and incident channels lead with the event sentence; generic webhooks
  and PagerDuty custom_details get an explicit field set without recipient
  addresses or HTML.
* Unknown {placeholders} are refused when a rule is saved; NetPath, Device
  Tracker and APM rules send their own templates.
* Reboot/restart sentences without dashes; agent-offline text in regional time.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import alert_phrasing as ap
from app.services import notification_context as nc
from app.services import notification_payload as npl
from app.services.branding import Branding
from app.services.sms_text import gsm7_length, to_gsm7

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_alert_delivery_content import ctx as delivery_ctx, deliveries, dispatch, world  # noqa: F401

RIYADH = Branding(timezone="Asia/Riyadh")
NOW = datetime(2026, 11, 19, 11, 44, tzinfo=timezone.utc)


# ── Site and group placeholders ──────────────────────────────────────────────

def test_site_suffix_is_short_and_only_when_known():
    assert ap.place_variables("Riyadh-HQ", "Core") == {
        "site": "Riyadh-HQ", "group": "Core", "site_suffix": " at Site Riyadh-HQ"}
    assert ap.place_variables() == {"site": "", "group": "", "site_suffix": ""}
    assert ap.site_suffix("Site B") == " at Site B"
    long_name = ap.site_suffix("Riyadh Head Office Data Centre Hall 3 Row 7")
    assert long_name.endswith("...") and len(long_name) <= len(" at Site ") + ap.SMS_SITE_CHARS


@pytest.mark.parametrize("kind", ["device", "service", "trap", "syslog"])
def test_every_default_sms_carries_the_site_suffix(kind):
    t = ap.default_templates(kind)
    for field in ("sms_template", "recovery_sms_template"):
        if t.get(field):
            assert "{rule_name}{site_suffix}:" in t[field], (kind, field)
    for field in ("sms_template", "recovery_sms_template", "email_subject", "email_body"):
        if t.get(field):
            assert not ap.unknown_placeholders(t[field], kind), (kind, field)


def test_realistic_sms_stays_in_one_gsm7_segment():
    v = ap.phrasing_variables(metric="ping_status", operator="==", threshold=0,
                              subject_noun=ap.host_label("RUH-CORE-SW-01", "10.120.30.40"),
                              asset_type="Switch", device_status="down", event_time=NOW,
                              branding=RIYADH, now=NOW)
    v.update(severity="CRITICAL", rule_name="Device Down", **ap.place_variables("Riyadh-HQ"))
    sms = ap.sms_with_timestamp(ap.DEFAULT_SMS.format_map(v), NOW, RIYADH)
    assert sms == ("ZenPlus CRITICAL - Device Down at Site Riyadh-HQ: Switch RUH-CORE-SW-01 "
                   "(10.120.30.40) went down at 14:44: it does not answer ping.")
    assert sms == to_gsm7(sms) and gsm7_length(sms) <= 160

    v = ap.phrasing_variables(metric="host_memory_pct", operator=">", threshold=90, value=97.2,
                              subject_noun=ap.host_label("RUH-APP-SRV-01", "10.120.31.15"),
                              asset_type="Server")
    v.update(severity="WARNING", rule_name="High memory", **ap.place_variables("Riyadh-HQ"))
    sms = ap.sms_with_timestamp(ap.DEFAULT_SMS.format_map(v), NOW, RIYADH)
    assert "High memory at Site Riyadh-HQ: Host memory on Server RUH-APP-SRV-01" in sms
    assert gsm7_length(sms) <= 160


def test_fill_place_fills_placeholders_and_adds_detail_rows():
    ctx = {"subject": "[WARNING] {site}", "message": "ZenPlus - r{site_suffix}: x", "body": "{group}",
           "details": [("Alert rule", "r"), ("Condition", "c")]}
    out = nc.fill_place(ctx, ap.place_variables("Riyadh-HQ", "Core"))
    assert (out["subject"], out["message"], out["body"]) == (
        "[WARNING] Riyadh-HQ", "ZenPlus - r at Site Riyadh-HQ: x", "Core")
    assert out["details"] == [("Alert rule", "r"), ("Site", "Riyadh-HQ"), ("Group", "Core"),
                              ("Condition", "c")]
    assert (out["site"], out["group"]) == ("Riyadh-HQ", "Core")
    # A site that is the device's location text is not shown twice; a known
    # group row is kept; an unknown site leaves no placeholder behind.
    out = nc.fill_place({"details": [("Group", "Core"), ("Location", "Riyadh-HQ")]},
                        ap.place_variables("Riyadh-HQ", "Core"))
    assert out["details"] == [("Group", "Core"), ("Location", "Riyadh-HQ")]
    out = nc.fill_place({"message": "a{site_suffix}: b"}, ap.place_variables())
    assert out["message"] == "a: b" and "details" not in out


@contextlib.asynccontextmanager
async def session(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            yield s
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_asset_place_follows_the_grouping_site_definition(pg_db):
    db = pg_db
    site = db.scalar("INSERT INTO sites (name) VALUES ('Riyadh-HQ') RETURNING id")
    dr = db.scalar("INSERT INTO sites (name) VALUES ('Jeddah-DR') RETURNING id")
    sensor = db.scalar("INSERT INTO sensors (name, status, site_id) VALUES ('ruh-sensor', 'online', $1)"
                       " RETURNING id", site)
    group = db.scalar("INSERT INTO device_groups (name) VALUES ('Core switches') RETURNING id")
    polled = db.scalar("INSERT INTO devices (hostname, ip_address, location, group_id, default_sensor_id)"
                       " VALUES ('sw-1', '192.0.2.10', 'Rack 4', $1, $2) RETURNING id", group, sensor)
    located = db.scalar("INSERT INTO devices (hostname, ip_address, location)"
                        " VALUES ('sw-2', '192.0.2.11', 'Dammam branch') RETURNING id")
    bare = db.scalar("INSERT INTO devices (hostname, ip_address) VALUES ('sw-3', '192.0.2.12') RETURNING id")
    server = db.scalar("INSERT INTO servers (display_name, hostname, site_id, device_id)"
                       " VALUES ('app', 'app', $1, $2) RETURNING id", dr, polled)
    check = db.scalar("INSERT INTO service_checks (name, check_type, target_host, device_id)"
                      " VALUES ('web', 'http', '192.0.2.11', $1) RETURNING id", located)
    alert = db.scalar("INSERT INTO alerts (device_id, severity, message, status)"
                      " VALUES ($1, 'warning', 'm', 'active') RETURNING id", polled)
    async with session(db) as s:
        place = lambda **kw: nc.asset_place(s, kw)  # noqa: E731
        # The polling sensor's site wins over the location text.
        assert await place(device_id=str(polled)) == ap.place_variables("Riyadh-HQ", "Core switches")
        assert (await place(device_id=str(located)))["site"] == "Dammam branch"
        assert await place(device_id=str(bare)) == ap.place_variables()
        # A server's own site; its group is its linked device's.
        assert await place(server_id=str(server)) == ap.place_variables("Jeddah-DR", "Core switches")
        # A service check through its linked device.
        assert (await place(service_check_id=str(check)))["site"] == "Dammam branch"
        # Only the alert id: its row names the asset.
        assert (await place(alert_id=str(alert)))["site"] == "Riyadh-HQ"
        assert await place(device_id="not-a-uuid") == ap.place_variables()
        assert await place() == ap.place_variables()


@pytest.mark.asyncio
async def test_dispatch_fills_the_site_for_a_sender_that_did_not(world):
    w = world
    site = w.db.scalar("INSERT INTO sites (name) VALUES ('Riyadh-HQ') RETURNING id")
    w.db.execute("UPDATE servers SET site_id = $1", site)
    alert_id = w.alert()
    c = delivery_ctx(alert_id, message="ZenPlus WARNING - Memory high{site_suffix}: srv",
                     details=[("Alert rule", "Memory high")])
    assert await dispatch(w, [w.ch.mail, w.ch.sms], c) == 2
    assert w.texts[0][1].startswith("ZenPlus WARNING - Memory high at Site Riyadh-HQ: srv")
    [(_, _, body)] = w.mails
    assert "Site" in body and "Riyadh-HQ" in body


# ── Chat and webhook payloads ────────────────────────────────────────────────

def _ctx(**extra):
    return {"subject": "[CRITICAL] DOWN: Core down — sw-1 (10.0.0.1)",
            "body": "Switch sw-1 (10.0.0.1) went down at 14:44: it does not answer ping.\n\nMore.",
            "message": "ZenPlus CRITICAL - Core down: Switch sw-1 went down", "hostname": "sw-1",
            "ip_address": "10.0.0.1", "status": "DOWN", "severity": "critical", "priority": "P2",
            "rule_id": "r1", "rule_name": "Core down", "triggered_at": "2026-11-19T11:44:00+00:00",
            "alert_id": "3f2a9c1d-0000-4000-8000-000000000001", "delivery_event": "trigger",
            "action_url": "https://zp.example/alerts", "device_id": "d1",
            "headline_metric": {"label": "Packet loss", "value": "100%",
                                "secondary_label": "Threshold", "secondary_value": "10%"},
            "details": [("Alert rule", "Core down"), ("Group", None)],
            "recipients": [{"kind": "sms", "address": "+966500000009"}],
            "html_body": "<html>mail</html>", "email_overrides": {"x": 1}, **extra}


def test_chat_title_is_the_event_sentence_not_host_is_status():
    v = npl.chat_view(_ctx())
    assert v["title"] == "Switch sw-1 (10.0.0.1) went down at 14:44: it does not answer ping."
    facts = dict(v["facts"])
    assert facts["Severity"] == "Critical (P2)" and facts["Value"] == "100% (threshold 10%)"
    assert facts["Time"].startswith("2026-11-19 11:44") and "Duration" not in facts
    assert v["url"] == "https://zp.example/alerts/3f2a9c1d-0000-4000-8000-000000000001"
    # An explicit event sentence wins over the mail body; a recovery says how long.
    v = npl.chat_view(_ctx(event_sentence="Switch sw-1 came back up.", is_recovery=True,
                           active_for="6 minutes", local_time="2026-11-19 14:50 GMT+3"))
    assert v["title"] == "Switch sw-1 came back up."
    assert dict(v["facts"])["Duration"] == "6 minutes"
    assert dict(v["facts"])["Time"] == "2026-11-19 14:50 GMT+3"
    assert dict(v["facts"])["Severity"] == "Resolved"


def test_escalation_chat_title_no_longer_reads_is_escalation():
    from app.api.v1 import alert_engine
    c = _ctx(status="ESCALATION L2", escalation_level=2, delivery_event="escalation:L2:r0",
             event_sentence="Escalated to level 2: Host memory on Server srv-01 opened at 97%.")
    assert alert_engine.slack_payload(c)["attachments"][0]["title"].startswith("Escalated to level 2:")
    assert " is ESCALATION" not in str(alert_engine.teams_payload(c))
    assert npl.webhook_fields(c)["event"] == "escalation"


def test_webhook_fields_are_the_documented_set_without_addresses_or_html():
    fields = npl.webhook_fields(_ctx(site="Riyadh-HQ", group="Core"))
    assert tuple(fields) == npl.WEBHOOK_FIELDS
    assert fields["title"].startswith("Switch sw-1 (10.0.0.1) went down")
    assert (fields["event"], fields["site"], fields["value"], fields["threshold"]) == (
        "trigger", "Riyadh-HQ", "100%", "10%")
    assert fields["incident_ref"] == "INC-3f2a9c1d"
    assert fields["details"] == [{"label": "Alert rule", "value": "Core down"}]
    text_ = str(fields)
    assert "+966500000009" not in text_ and "<html>" not in text_ and "email_overrides" not in text_
    import json
    json.dumps(fields)
    # A channel's own body template gets the same fields as text.
    values = npl.template_values(_ctx())
    assert values["is_recovery"] == "false" and values["site"] == "" and "recipients" not in values


@pytest.mark.asyncio
async def test_pagerduty_custom_details_are_the_webhook_fields(monkeypatch):
    from app.api.v1 import alert_engine
    payload = alert_engine.pagerduty_payload(_ctx(site="Riyadh-HQ"), "rk")
    assert payload["payload"]["summary"].startswith("Switch sw-1 (10.0.0.1) went down")
    assert payload["payload"]["custom_details"] == npl.webhook_fields(_ctx(site="Riyadh-HQ"))
    assert payload["payload"]["group"] == "Riyadh-HQ"
    # The dedup key is unchanged: rule + device.
    assert payload["dedup_key"] == "zenplus:r1:d1"


# ── Template validation ──────────────────────────────────────────────────────

class _NoDB:
    def begin_nested(self):
        raise RuntimeError("no database here")


@pytest.mark.asyncio
async def test_unknown_placeholders_are_refused_with_the_valid_names():
    from app.api.v1.alert_rules import _validate_template_placeholders as check
    rule = {"metric": "cpu", "operator": ">", "threshold": 90}
    await check(_NoDB(), {"email_subject": "{rule_name} at {site} ({group})",
                          "sms_template": "{severity} {custom.rack} {incident_ref}"}, rule)
    with pytest.raises(HTTPException) as exc:
        await check(_NoDB(), {"email_subject": "{hostnmae} is {status}",
                              "sms_template": "{site_name}{site_suffix}"}, rule)
    detail = exc.value.detail
    assert exc.value.status_code == 422 and detail["code"] == "unknown_template_placeholder"
    assert detail["unknown"] == ["hostnmae", "site_name"]
    assert detail["fields"] == {"email_subject": ["hostnmae"], "sms_template": ["site_name"]}
    assert "site" in detail["valid"] and "hostname" in detail["valid"]
    assert "{hostnmae}" in detail["message"] and "{hostname}" in detail["message"]
    # Each kind has its own names: a syslog rule has no incident lifecycle,
    # a service rule has the check's.
    with pytest.raises(HTTPException):
        await check(_NoDB(), {"email_body": "{incident_ref}"}, {"metric": "syslog"})
    await check(_NoDB(), {"email_body": "{check_name} {error_brief}"}, {"metric": "service_status"})
    with pytest.raises(HTTPException):
        await check(_NoDB(), {"email_body": "{check_result}"}, {"metric": "cpu"})
    # Legacy pre-filled defaults and server-agent rules are not checked.
    await check(_NoDB(), {"email_body": "An alert has been triggered:"}, rule)
    await check(_NoDB(), {"email_body": "{whatever}"}, {"metric": "host_cpu_pct"})


def test_rule_api_refuses_unknown_placeholders_on_create_and_update(pg_db):
    db = pg_db
    db.execute("INSERT INTO custom_field_defs (key, label, field_type, asset_kinds)"
               " VALUES ('rack', 'Rack', 'text', ARRAY['device'])")
    base = {"name": "CPU high", "metric": "cpu", "operator": ">", "threshold": 90,
            "severity": "warning", "notify_channels": []}
    with api_client(db, role="admin", persist_user=True, username="admin-1") as c:
        r = c.post("/api/v1/alert-rules", json={**base, "sms_template": "{hostname}{site_suffix} {custom.rack}"})
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        bad = c.post("/api/v1/alert-rules", json={**base, "email_subject": "{hostnmae}"})
        assert bad.status_code == 422 and bad.json()["detail"]["unknown"] == ["hostnmae"]
        bad = c.post("/api/v1/alert-rules", json={**base, "sms_template": "{custom.nope}"})
        assert bad.status_code == 422 and bad.json()["detail"]["unknown"] == ["custom.nope"]
        r = c.put(f"/api/v1/alert-rules/{rid}", json={"recovery_email_body": "{event_sentence} {sitee}"})
        assert r.status_code == 422 and r.json()["detail"]["fields"] == {"recovery_email_body": ["sitee"]}
        assert c.put(f"/api/v1/alert-rules/{rid}", json={"recovery_email_body": "{event_sentence} {site}"}
                     ).status_code == 200


# ── NetPath, Device Tracker and APM send their own templates ─────────────────

def test_netpath_renders_the_rules_own_template():
    from app.services import netpath_alert_service as nps
    rule = {"id": "r", "name": "WAN path", "metric": "netpath_unreachable", "operator": "==",
            "threshold": 1, "severity": "critical",
            "sms_template": "NETPATH {rule_name}: {event_sentence} [{hostname}]{site_suffix}"}
    probe = {"name": "Office-365", "target_ip": "40.97.1.1"}
    v = nps.render_variables(rule, probe, "Path to Office-365 (40.97.1.1) can no longer reach the target")
    texts = nps.render_templates(rule, v)
    assert texts["sms_template"] == ("NETPATH WAN path: Path to Office-365 (40.97.1.1) can no longer "
                                     "reach the target. [Office-365]")
    # No template of its own: the device wording, GSM-7.
    assert texts["email_subject"] == "[CRITICAL] ALERT: WAN path — Path to Office-365 (40.97.1.1)"
    rec = nps.render_templates({**rule, "sms_template": None}, nps.render_variables(
        rule, probe, "Path to Office-365 (40.97.1.1) can reach the target again", is_recovery=True),
        is_recovery=True)
    assert rec["sms_template"].startswith("ZenPlus resolved - WAN path: Path to Office-365")


def test_device_tracker_renders_the_rules_own_template():
    from app.services import udt_alert_service as uds
    rule = {"id": "r", "name": "Rogue", "metric": "udt_rogue_endpoint", "operator": "==", "threshold": 1,
            "severity": "warning", "email_subject": "Rogue on {hostname}{site_suffix}"}
    v = uds.render_variables(rule, "Endpoint laptop-7 is not authorised and was seen on Switch sw-1",
                             "sw-1", "10.0.0.2", alert_id="3f2a9c1d-0000-4000-8000-000000000001")
    v.update(ap.place_variables("Riyadh-HQ"))
    texts = uds.render_templates(rule, v)
    assert texts["email_subject"] == "Rogue on sw-1 at Site Riyadh-HQ"
    assert texts["sms_template"] == ("ZenPlus WARNING - Rogue at Site Riyadh-HQ: Endpoint laptop-7 is "
                                     "not authorised and was seen on Switch sw-1.")
    assert "—" not in texts["sms_template"] and v["incident_ref"] == "INC-3f2a9c1d"


@pytest.mark.asyncio
async def test_apm_threshold_rule_sends_its_own_template(monkeypatch):
    from app.services import apm_alert_service as apm
    sent = []

    async def capture(db, channels, ctx):
        sent.append(ctx)
        return 1

    async def tz(db):
        return "UTC"

    monkeypatch.setattr(apm, "dispatch_to_channels", capture)
    monkeypatch.setattr("app.services.alert_schedule.get_configured_timezone", tz)
    rule = NS(id="r", name="Checkout latency", metric="apm_latency_p95", operator=">", threshold=500,
              severity="warning", notify_channels=["c"], schedule_start=None, schedule_end=None,
              schedule_days=None, conditions=None, condition_logic="AND", priority=None,
              email_subject="APM {hostname}: {reading}", email_body=None, sms_template=None,
              recovery_email_subject=None, recovery_email_body=None, recovery_sms_template=None)
    assert await apm._notify(None, rule, "checkout-api", 812.0, alert_id="3f2a9c1d-0000-4000-8000-000000000001")
    [ctx] = sent
    assert ctx["subject"] == "APM checkout-api: 812 ms"
    assert ctx["message"].startswith("ZenPlus WARNING - Checkout latency: p95 latency on checkout-api")
    assert ctx["incident_ref"] == "INC-3f2a9c1d"


# ── Leftovers ────────────────────────────────────────────────────────────────

def test_event_sentences_have_no_dashes():
    now = NOW.timestamp()
    for metric, c in (("uptime_reset", {"rebooted_at": now - 180, "uptime_s": 180,
                                        "previous_uptime_s": 86400 * 41, "reboot_count": 1}),
                      ("uptime_reset", {"rebooted_at": now - 60, "uptime_s": 60, "reboot_count": 2,
                                        "first_rebooted_at": now - 600}),
                      ("host_restart", {"boot_at": now - 120, "restart_count": 1, "uptime_s": 120}),
                      ("host_restart", {"boot_at": now - 780, "agent_reporting": False,
                                        "heartbeat_at": now - 700})):
        for recovery in (False, True):
            sentence = ap.event_sentence(metric, "Switch sw-1", c, recovery=recovery,
                                         branding=RIYADH, now=now)
            assert "—" not in sentence and " - " not in sentence, sentence


def test_agent_offline_text_uses_the_regional_time():
    from app.services.server_health_service import agent_offline_message
    at = datetime(2026, 11, 19, 11, 44, tzinfo=timezone.utc)
    msg = agent_offline_message("srv-01", at, RIYADH)
    assert msg.startswith("Agent on srv-01 is offline. Last heartbeat 2026-11-19 14:44")
    assert "UTC" not in msg and "—" not in msg
    assert agent_offline_message(None, None) == "Agent on server is offline."
