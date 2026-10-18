"""What an alert message tells its reader (NOTIFY 05 and 06).

* Every alert email that carries incident context names the incident, its
  lifecycle stage, when it was first seen and how long it has been active;
  the subject keeps "[SEVERITY]" first, then the lifecycle tag, and ends with
  the incident ref. A recovery says Recovered with the total duration.
* A check that got no reply has no latency: it reads "unavailable (no
  reply)", never "0.0 ms" beside 100% loss, and rtt rules never treat it as
  0 ms. The banner shows the reachability state and the severity as separate
  pills instead of a DOWN banner in warning colours.

The device status path runs against the in-memory alerts table of
test_alert_status_into_up with the SMTP transport replaced by a recorder.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import alert_engine
from app.api.v1.alert_engine import StatusChangeEvent, evaluate_status_change
from app.services import alert_phrasing as ap
from app.services import email_render as er

from test_alert_status_into_up import DEVICE, AlertsDB, device_down_rule

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
ALERT_ID = "3f2a9c1d-0b7e-4c55-9a10-2d6f0e8b1c44"


# ── incident_context ─────────────────────────────────────────────────────────

def test_incident_context_keys_and_labels():
    ctx = ap.incident_context(alert_id=ALERT_ID, first_seen=NOW - timedelta(minutes=12),
                              now=NOW, lifecycle="new")
    assert ctx == {
        "incident_ref": "INC-3f2a9c1d", "incident_id": ALERT_ID,
        "first_seen": "2026-10-18T08:48:00+00:00", "active_for": "12 minutes",
        "lifecycle": "new", "lifecycle_label": "New", "lifecycle_tag": "New",
    }
    assert set(ctx) == set(ap.INCIDENT_KEYS)


@pytest.mark.parametrize("kwargs,label,tag", [
    (dict(lifecycle="reminder", reminder_index=2, reminder_total=3), "Reminder 2 of 3", "Reminder 2/3"),
    (dict(lifecycle="reminder", reminder_index=4), "Reminder 4", "Reminder 4"),
    (dict(lifecycle="reminder"), "Reminder", "Reminder"),
    (dict(lifecycle="escalated", escalation_level=2), "Escalated to level 2", "Escalated L2"),
    (dict(lifecycle="escalation"), "Escalated", "Escalated"),
    (dict(lifecycle="changed"), "Changed", "Changed"),
    (dict(lifecycle="recovered"), "Recovered", "Recovered"),
    (dict(lifecycle="recovery"), "Recovered", "Recovered"),
    (dict(lifecycle="trigger"), "New", "New"),
])
def test_lifecycle_labels(kwargs, label, tag):
    ctx = ap.incident_context(alert_id=ALERT_ID, first_seen=NOW, now=NOW, **kwargs)
    assert ctx["lifecycle_label"] == label and ctx["lifecycle_tag"] == tag
    assert ctx["lifecycle"] in ap.LIFECYCLES


def test_incident_context_tolerates_strings_naive_times_and_missing_ids():
    ctx = ap.incident_context(alert_id=None, first_seen="2026-10-18T06:45:00Z",
                              now=datetime(2026, 10, 18, 9, 0), lifecycle="recovered")
    assert ctx["incident_ref"] == "" and ctx["incident_id"] == ""
    assert ctx["first_seen"] == "2026-10-18T06:45:00+00:00"
    assert ctx["active_for"] == "2 hours 15 minutes"
    unknown = ap.incident_context(alert_id=42, first_seen=None, now=NOW, lifecycle="new")
    assert unknown["incident_ref"] == "INC-42" and unknown["first_seen"] == unknown["active_for"] == ""
    with pytest.raises(ValueError):
        ap.incident_context(alert_id=ALERT_ID, first_seen=NOW, now=NOW, lifecycle="acknowledged")


def test_the_incident_ref_is_stable_across_one_incidents_mails():
    from uuid import UUID
    refs = {ap.incident_ref(ALERT_ID), ap.incident_ref(UUID(ALERT_ID)), ap.incident_ref(ALERT_ID.upper())}
    assert refs == {"INC-3f2a9c1d"}


# ── Subjects ─────────────────────────────────────────────────────────────────

def _ctx(lifecycle, **kw):
    return ap.incident_context(alert_id=ALERT_ID, first_seen=NOW - timedelta(hours=2, minutes=14),
                               now=NOW, lifecycle=lifecycle, **kw)


@pytest.mark.parametrize("subject,lifecycle,kw,expected", [
    ("[WARNING] ALERT: Memory high — srv-a", "new", {},
     "[WARNING] [New] Memory high — srv-a [INC-3f2a9c1d]"),
    ("[WARNING] Resolved: Memory high — srv-a", "recovered", {},
     "[WARNING] [Recovered] Memory high — srv-a [INC-3f2a9c1d]"),
    ("[CRITICAL] DOWN: Device Down — core-1 (10.0.0.1)", "new", {},
     "[CRITICAL] [New] DOWN: Device Down — core-1 (10.0.0.1) [INC-3f2a9c1d]"),
    ("[WARNING] ALERT: Memory high — srv-a", "reminder", dict(reminder_index=2, reminder_total=3),
     "[WARNING] [Reminder 2/3] Memory high — srv-a [INC-3f2a9c1d]"),
    ("[CRITICAL] ALERT: CPU — srv-b", "escalated", dict(escalation_level=2),
     "[CRITICAL] [Escalated L2] CPU — srv-b [INC-3f2a9c1d]"),
    # A subject the rule's author wrote keeps every word.
    ("PING FAIL core-1", "new", {}, "[New] PING FAIL core-1 [INC-3f2a9c1d]"),
])
def test_subject_keeps_severity_first_then_lifecycle_and_ref(subject, lifecycle, kw, expected):
    ctx = _ctx(lifecycle, **kw)
    assert er.alert_subject(subject, ctx) == expected
    assert er.alert_subject(expected, ctx) == expected          # idempotent


def test_subject_without_incident_context_is_unchanged():
    assert er.alert_subject("[WARNING] ALERT: x", {}) == "[WARNING] ALERT: x"
    assert er.alert_subject("[WARNING] ALERT: x", None) == "[WARNING] ALERT: x"
    assert er.alert_subject("[WARNING] ALERT: x", {"severity": "warning"}) == "[WARNING] ALERT: x"


# ── Email bodies ─────────────────────────────────────────────────────────────

BASE = {"severity": "warning", "status": "ALERT", "title": "Memory high", "hostname": "srv-a",
        "message": "Host memory on srv-a rose above 90.0% (currently 90.3%).",
        "details": [("Alert rule", "Memory high")], "timestamp": NOW.isoformat()}


def test_every_alert_email_with_incident_keys_shows_the_incident():
    ctx = {**BASE, **_ctx("new")}
    html_ = er.build_alert_email_html(ctx)
    text_ = er.build_alert_email_text(ctx)
    for body in (html_, text_):
        assert "Incident INC-3f2a9c1d · New" in body
        assert "First seen" in body and "Oct 18, 2026 · 06:46 UTC" in body
        assert "Active for" in body and "2 hours 14 minutes" in body


def test_a_recovery_says_recovered_with_the_total_duration():
    ctx = {**BASE, "resolved": True, "status": "RESOLVED", **_ctx("recovered")}
    for body in (er.build_alert_email_html(ctx), er.build_alert_email_text(ctx)):
        assert "Incident INC-3f2a9c1d · Recovered after 2 hours 14 minutes" in body
        assert "Total duration" in body
    # A sender that already lists "Active for" does not get it twice.
    dup = {**ctx, "details": [("Active for", "2 hours 14 minutes")]}
    text_ = er.build_alert_email_text(dup)
    assert text_.count("2 hours 14 minutes") == 2          # summary line + its own row
    assert "Total duration" not in text_


def test_reminder_and_escalation_labels_reach_the_body():
    for kw, label in ((dict(lifecycle="reminder", reminder_index=2, reminder_total=3), "Reminder 2 of 3"),
                      (dict(lifecycle="escalated", escalation_level=2), "Escalated to level 2")):
        ctx = {**BASE, **ap.incident_context(alert_id=ALERT_ID, first_seen=NOW, now=NOW, **kw)}
        assert f"Incident INC-3f2a9c1d · {label}" in er.build_alert_email_html(ctx)


def test_without_incident_keys_the_email_is_as_before():
    html_ = er.build_alert_email_html(BASE)
    assert "Incident " not in html_ and "First seen" not in html_


def _chips(html_):
    return re.findall(r'border-radius:999px;">([^<]*)</span>', html_)


def test_status_and_severity_are_separate_pills():
    html_ = er.build_alert_email_html({**BASE, "status": "DOWN", "severity": "warning"})
    assert _chips(html_) == ["DOWN", "WARNING"]
    status_chip = re.search(r'background:(#[0-9A-Fa-f]{6});[^>]*>DOWN</span>', html_).group(1)
    severity_chip = re.search(r'background:(#[0-9A-Fa-f]{6});[^>]*>WARNING</span>', html_).group(1)
    assert status_chip.upper() != er.severity_hex("warning").upper()     # no DOWN in amber
    assert severity_chip.upper() == er.severity_hex("warning").upper()
    resolved = er.build_alert_email_html({**BASE, "resolved": True})
    assert _chips(resolved) == ["RESOLVED"]


def test_headline_detail_line_is_rendered():
    html_ = er.build_alert_email_html({**BASE, "headline_metric": ap.headline_metric(
        "if_errors", 152, ">", 100, context={"interval_s": 60, "in": 150, "out": 2})})
    assert "152 errors" in html_ and "In the last 60 s (150 in, 2 out)" in html_
    assert "100 errors" in html_


# ── Device status path: no reply means no latency (NOTIFY 06) ────────────────

CHANNEL = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def mail(monkeypatch):
    """Run evaluate_status_change against AlertsDB; record every email."""
    monkeypatch.setattr(alert_engine, "_device_in_maintenance", AsyncMock(return_value=False))
    monkeypatch.setattr(alert_engine, "_offline_sensor_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "_find_suppressing_dependency", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "get_configured_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", True)
    monkeypatch.setattr(alert_engine, "_smtp_gateway", AsyncMock(return_value={"host": "smtp.invalid"}))
    monkeypatch.setattr(alert_engine, "_dashboard_url", AsyncMock(return_value=""))
    monkeypatch.setattr(alert_engine, "decrypt_config", lambda cfg: cfg)
    monkeypatch.setattr(alert_engine.dl, "record", AsyncMock())
    sent: list[dict] = []
    contexts: list[dict] = []
    real_build = alert_engine.build_alert_email_html

    def build(ctx):
        contexts.append(ctx)
        return real_build(ctx)

    async def send(gw, recipients, subject, body, html_body=None):
        sent.append({"subject": subject, "body": body, "html": html_body, "ctx": contexts[-1]})
        return True

    monkeypatch.setattr(alert_engine, "build_alert_email_html", build)
    monkeypatch.setattr(alert_engine, "_send_email", send)

    def world(*rules):
        db = AlertsDB(list(rules))
        db.channels[CHANNEL] = NS(name="noc", type="email", config={"recipients": "noc@example.invalid"},
                                  gateway_id=None, enabled=True)
        return db

    async def run(db, old, new, **kw):
        return await evaluate_status_change(StatusChangeEvent(
            device_id=DEVICE, hostname="edge-1", ip_address="10.0.0.1",
            old_status=old, new_status=new, **kw), db=db)

    return NS(sent=sent, world=world, run=run)


def _loss_rule(**kw):
    return device_down_rule(name="Packet Loss", metric="packet_loss", operator="gt", threshold=5,
                            severity="warning", notify_channels=[CHANNEL], **kw)


def test_the_event_accepts_a_null_rtt():
    event = StatusChangeEvent(device_id=DEVICE, hostname="a", ip_address="10.0.0.1",
                              old_status="up", new_status="down", rtt_ms=None, packet_loss=1.0)
    assert event.rtt_ms is None
    assert StatusChangeEvent(device_id=DEVICE, hostname="a", ip_address="10.0.0.1",
                             old_status="up", new_status="down").rtt_ms is None


@pytest.mark.parametrize("old,new,rtt,loss,expected", [
    ("up", "down", None, 1.0, None),
    ("up", "down", 0.0, 1.0, None),          # an older poller's 0 for "no reply"
    ("up", "down", 3.2, 0.5, None),          # a move into down was an unanswered check
    ("up", "degraded", 0.0, 1.0, None),      # nothing came back
    ("up", "degraded", 412.5, 0.25, 412.5),
    ("down", "up", 1.5, 0.0, 1.5),
])
def test_reply_rtt(old, new, rtt, loss, expected):
    event = StatusChangeEvent(device_id=DEVICE, hostname="a", ip_address="10.0.0.1",
                              old_status=old, new_status=new, rtt_ms=rtt, packet_loss=loss)
    assert alert_engine._reply_rtt(event) == expected


def test_an_rtt_rule_never_reads_no_reply_as_zero_ms():
    below = device_down_rule(metric="rtt", operator="lt", threshold=1)
    above = device_down_rule(metric="rtt", operator="gt", threshold=100)
    for rule in (below, above):
        assert not alert_engine._conditions_match(rule, alert_engine._INTO_DOWN_READINGS)
        assert not alert_engine._could_fire_into(rule, "down")
    assert alert_engine._INTO_DOWN_READINGS["rtt"] is None


@pytest.mark.asyncio
async def test_complete_loss_never_shows_as_excellent_latency(mail):
    db = mail.world(_loss_rule())
    out = await mail.run(db, "up", "down", rtt_ms=0.0, packet_loss=1.0)
    assert out["notifications_sent"] == 1
    [m] = mail.sent
    ctx = m["ctx"]
    # The headline leads with reachability evidence: 100% loss, no latency.
    assert ctx["headline_metric"]["label"] == "Packet loss"
    assert ctx["headline_metric"]["value"] == "100%"
    assert ctx["headline_metric"]["secondary_label"] == "Round-trip time"
    assert ctx["headline_metric"]["secondary_value"] == "unavailable (no reply)"
    assert ctx["headline_metric"]["detail"] == "Threshold 5%"
    details = dict(ctx["details"])
    assert details["Reachability"] == "Down (no reply)"
    assert details["Round-trip time"] == "unavailable (no reply)"
    for text_ in (m["html"], m["body"], m["subject"]):
        assert "0.0ms" not in text_ and "0.0 ms" not in text_ and "0 ms" not in text_
    assert m["body"] == "Packet loss on edge-1 (10.0.0.1) rose above 5% (currently 100%)."
    # State and severity as separate pills: "DOWN" neutral, "WARNING" amber.
    assert _chips(m["html"]) == ["DOWN", "WARNING"]
    # Lifecycle and incident in subject and body.
    [row] = db.alerts
    ref = ap.incident_ref(row["id"])
    assert m["subject"] == f"[WARNING] [New] DOWN: Packet Loss — edge-1 (10.0.0.1) [{ref}]"
    assert f"Incident {ref} · New" in m["html"]
    assert ctx["incident_ref"] == ref and ctx["lifecycle"] == "new"


@pytest.mark.asyncio
async def test_a_degraded_latency_alert_reports_the_reply(mail):
    rule = device_down_rule(name="High latency", metric="rtt", operator="gt", threshold=100,
                            severity="warning", notify_channels=[CHANNEL])
    db = mail.world(rule)
    await mail.run(db, "up", "degraded", rtt_ms=100.04, packet_loss=0.0)
    [m] = mail.sent
    head = m["ctx"]["headline_metric"]
    assert head["label"] == "Round-trip time" and head["value"] == "100.04 ms"
    assert head["detail"] == "Threshold 100.00 ms"
    assert head["secondary_label"] == "Packet loss" and head["secondary_value"] == "0%"
    assert m["body"] == ("Round-trip time on edge-1 (10.0.0.1) rose above 100.00 ms "
                         "(currently 100.04 ms).")
    assert dict(m["ctx"]["details"])["Reachability"] == "Degraded (responding)"


@pytest.mark.asyncio
async def test_an_rtt_rule_does_not_fire_when_nothing_answered(mail):
    rule = device_down_rule(name="Very low latency", metric="rtt", operator="lt", threshold=1,
                            notify_channels=[CHANNEL])
    db = mail.world(rule)
    await mail.run(db, "up", "down", rtt_ms=0.0, packet_loss=1.0)
    assert mail.sent == [] and db.alerts == []


@pytest.mark.asyncio
async def test_device_recovery_mail_is_labelled_recovered_with_total_duration(mail):
    rule = device_down_rule(severity="critical", notify_channels=[CHANNEL])
    db = mail.world(rule)
    opened = db.seed(rule, minutes_ago=134, notified=True)
    await mail.run(db, "down", "up", rtt_ms=1.5, packet_loss=0.0)
    [m] = mail.sent
    ref = ap.incident_ref(opened["id"])
    assert m["subject"] == f"[CRITICAL] [Recovered] Device Down — edge-1 (10.0.0.1) [{ref}]"
    assert f"Incident {ref} · Recovered after 2 hours 14 minutes" in m["html"]
    assert "Total duration" in m["html"]
    ctx = m["ctx"]
    assert ctx["lifecycle"] == "recovered" and ctx["active_for"] == "2 hours 14 minutes"
    assert ctx["first_seen"] == opened["triggered_at"].isoformat()
    assert ctx["headline_metric"]["label"] == "Round-trip time"
    assert ctx["headline_metric"]["value"] == "1.5 ms"
    assert _chips(m["html"]) == ["RESOLVED"]


@pytest.mark.asyncio
async def test_template_variables_render_no_reply_and_the_incident(mail):
    rule = _loss_rule(email_body="{hostname}: rtt {rtt}, loss {packet_loss}, {incident_ref} {lifecycle_label}")
    db = mail.world(rule)
    await mail.run(db, "up", "down", rtt_ms=None, packet_loss=1.0)
    [m] = mail.sent
    [row] = db.alerts
    assert m["body"] == f"edge-1: rtt unavailable (no reply), loss 100%, {ap.incident_ref(row['id'])} New"
