"""One notification funnel (1.24.17): every alert source sends through
``host_alert_service.dispatch_to_channels``.

Device-status and service-check alerts — the largest share of pages — used to
run their own per-channel send loops: no recipient dedupe, no durable delivery
keys, nothing a grouping gate or a per-recipient budget could ever see. They
now hand the dispatcher one ctx per rule carrying the incident (``alert_id``),
the lifecycle event (``delivery_event``), the priority and whether the send may
be held for a digest (``groupable``), after their own state is committed.

What recipients receive is unchanged: the same subject, plain body, HTML
fields, SMS text and webhook keys as before.

The first half drives the engine against the in-memory alerts table of
test_alert_status_into_up with a recording dispatcher; the second half runs the
real dispatcher and keys on a disposable PostgreSQL.
"""
from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1 import alert_engine
from app.api.v1.alert_engine import (
    ServiceStatusChangeEvent, StatusChangeEvent, evaluate_service_status_change,
    evaluate_status_change,
)
from app.services import alert_delivery_log as dl
from app.services import alert_funnel
from app.services import alert_phrasing as ap
from app.services import email_render, host_alert_service

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)
from test_alert_status_into_up import DEVICE, AlertsDB, device_down_rule

CHANNEL = "44444444-4444-4444-4444-444444444444"
CHECK = "55555555-5555-5555-5555-555555555555"


# ── helpers ─────────────────────────────────────────────────────────────────

class CommitLog(AlertsDB):
    """AlertsDB that records commits in the statement log."""

    async def commit(self):
        self.statements.append(("COMMIT", {}))


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(alert_engine, "_device_in_maintenance", AsyncMock(return_value=False))
    monkeypatch.setattr(alert_engine, "_offline_sensor_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "_find_suppressing_dependency", AsyncMock(return_value=None))
    monkeypatch.setattr(alert_engine, "get_configured_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(alert_engine, "_dashboard_url", AsyncMock(side_effect=lambda db, p="/alerts": f"https://zp.example{p}"))
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", True)
    alert_funnel.reset_for_tests()
    calls: list[tuple[list, dict, list]] = []

    async def dispatch(db, channel_ids, ctx):
        # What the database had seen when the send started.
        calls.append((list(channel_ids), dict(ctx), list(db.statements)))
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)

    async def run(db, old, new, **kw):
        return await evaluate_status_change(StatusChangeEvent(
            device_id=DEVICE, hostname="edge-1", ip_address="10.0.0.1",
            old_status=old, new_status=new, **kw), db=db)

    async def run_service(db, old, new, **kw):
        return await evaluate_service_status_change(ServiceStatusChangeEvent(
            service_check_id=CHECK, check_name="Portal HTTPS", check_type="http",
            old_status=old, new_status=new, target="https://portal.example", **kw), db=db)

    return NS(calls=calls, run=run, run_service=run_service)


def _rule(**kw):
    return device_down_rule(notify_channels=[CHANNEL], **kw)


# ── device status ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_device_trigger_is_sent_through_the_dispatcher_with_its_keys(engine):
    db = CommitLog([_rule()])
    out = await engine.run(db, "up", "down")
    [row] = db.alerts
    [(channels, ctx, seen)] = engine.calls
    assert channels == [CHANNEL]
    assert ctx["alert_id"] == str(row["id"]) and ctx["delivery_event"] == "trigger"
    # Critical derives P2: never held.
    assert ctx["priority"] == "P2" and ctx["groupable"] is False
    # The incident row and its notified stamp were committed before the send.
    assert seen[-1][0] == "COMMIT"
    assert row["metadata"]["notified"] is True
    assert out["notifications_sent"] == 1


@pytest.mark.asyncio
async def test_a_warning_trigger_may_be_grouped_but_an_explicit_p1_never(engine):
    db = AlertsDB([_rule(severity="warning"), _rule(name="Core down", severity="warning", priority="P1")])
    await engine.run(db, "up", "down")
    by_rule = {ctx["rule_name"]: ctx for _, ctx, _ in engine.calls}
    assert by_rule["Device Down"]["priority"] == "P3" and by_rule["Device Down"]["groupable"] is True
    assert by_rule["Core down"]["priority"] == "P1" and by_rule["Core down"]["groupable"] is False


@pytest.mark.asyncio
async def test_a_device_recovery_is_keyed_on_the_incident_it_closes(engine):
    rule = _rule(severity="warning")
    db = AlertsDB([rule])
    opened = db.seed(rule, notified=True)
    await engine.run(db, "down", "up", rtt_ms=1.5)
    [(_, ctx, _)] = engine.calls
    assert ctx["alert_id"] == str(opened["id"]) and ctx["delivery_event"] == "recovery"
    assert ctx["is_recovery"] is True and ctx["resolved"] is True
    # Recoveries always go out at once.
    assert ctx["groupable"] is False
    assert ctx["lifecycle"] == "recovered" and ctx["incident_ref"] == ap.incident_ref(opened["id"])


@pytest.mark.asyncio
async def test_an_unannounced_trigger_still_gets_no_recovery(engine):
    rule = _rule()
    db = AlertsDB([rule])
    db.seed(rule, notified=False)            # quiet hours swallowed the trigger
    await engine.run(db, "down", "up")
    assert engine.calls == []


@pytest.mark.asyncio
async def test_an_up_rule_announces_against_its_own_row(engine):
    rule = _rule(name="Device came up", trigger_on="up", severity="info")
    db = AlertsDB([rule])
    await engine.run(db, "down", "up")
    [row] = db.alerts
    [(_, ctx, _)] = engine.calls
    assert ctx["alert_id"] == str(row["id"]) and ctx["delivery_event"] == "trigger"


@pytest.mark.asyncio
async def test_quiet_hours_cooldown_and_flap_holds_still_send_nothing(engine, monkeypatch):
    db = AlertsDB([_rule()])
    await engine.run(db, "up", "down", flapping=True, is_flapping=True)
    assert engine.calls == [] and db.alerts[0]["status"] == "pending"

    monkeypatch.setattr(alert_engine, "notifications_allowed", lambda *a: False)
    db = AlertsDB([_rule()])
    await engine.run(db, "up", "down")
    assert engine.calls == [] and db.alerts[0]["metadata"]["notified"] is False


@pytest.mark.asyncio
async def test_the_device_ctx_carries_the_mail_and_webhook_fields_it_always_did(engine):
    rule = _rule(name="Packet Loss", metric="packet_loss", operator="gt", threshold=5,
                 severity="warning")
    db = AlertsDB([rule])
    await engine.run(db, "up", "down", rtt_ms=0.0, packet_loss=1.0)
    [(_, ctx, _)] = engine.calls
    [row] = db.alerts
    # Webhook family: the keys the per-channel loop posted (PagerDuty's dedup
    # key is still zenplus:<rule>:<device>).
    assert {k: ctx[k] for k in ("hostname", "ip_address", "status", "severity", "is_recovery",
                                "device_id", "rule_id", "rule_name", "event_type")} == {
        "hostname": "edge-1", "ip_address": "10.0.0.1", "status": "DOWN", "severity": "warning",
        "is_recovery": False, "device_id": DEVICE, "rule_id": str(rule.id),
        "rule_name": "Packet Loss", "event_type": "trigger"}
    assert ctx["message"] == ("ZenPlus WARNING — Packet Loss: Packet loss on edge-1 (10.0.0.1) "
                              "rose above 5% (currently 100%).")
    # Mail: subject, prose, headline, details and the dashboard link.
    assert ctx["subject"] == "[WARNING] DOWN: Packet Loss — edge-1 (10.0.0.1)"
    assert ctx["body"] == "Packet loss on edge-1 (10.0.0.1) rose above 5% (currently 100%)."
    assert ctx["headline_metric"]["label"] == "Packet loss"
    assert ctx["headline_metric"]["detail"] == "Threshold 5%"
    assert dict(ctx["details"])["Reachability"] == "Down (no reply)"
    assert ctx["action_url"] == "https://zp.example/alerts"
    assert ctx["incident_ref"] == ap.incident_ref(row["id"])
    # The SMS gateway's device placeholders.
    assert ctx["sms_template_values"] == {"hostname": "edge-1", "ip_address": "10.0.0.1",
                                          "status": "DOWN"}
    json.dumps(ctx)                           # a webhook posts it as JSON


# ── service checks ──────────────────────────────────────────────────────────

class ServiceDB(AlertsDB):
    """AlertsDB for the service path: rows carry the check id."""

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if sql.startswith("INSERT INTO alerts") and params.get("service_check_id"):
            result = await super().execute(statement, params)
            self.alerts[-1]["service_check_id"] = params["service_check_id"]
            return result
        if sql.startswith("UPDATE alerts SET status = 'resolved'") and "service_check_id = :service_check_id" in sql:
            hits = [a for a in self.alerts if a["status"] in ("pending", "active", "acknowledged")
                    and a["service_check_id"] == params["service_check_id"]
                    and not a["metadata"].get("is_recovery")]
            for a in hits:
                a["status"] = "resolved"
            self.statements.append((sql, dict(params)))
            return _rows([NS(id=a["id"], rule_id=a["rule_id"], triggered_at=a["triggered_at"],
                             notified=a["metadata"].get("notified"),
                             new_status=a["metadata"].get("new_status")) for a in hits])
        return await super().execute(statement, params)


def _rows(rows):
    from test_alert_status_into_up import _Result
    return _Result(rows, rowcount=len(rows))


def _service_rule(**kw):
    return device_down_rule(name="Portal down", trigger_on="down", service_check_id=CHECK,
                            service_check_group_id=None, metric="service_status",
                            notify_channels=[CHANNEL], **kw)


@pytest.mark.asyncio
async def test_a_service_trigger_and_recovery_use_the_dispatcher(engine):
    rule = _service_rule(severity="warning")
    db = ServiceDB([rule])
    await engine.run_service(db, "up", "down", error="HTTP 503")
    [(_, trig, _)] = engine.calls
    [row] = db.alerts
    assert trig["alert_id"] == str(row["id"]) and trig["delivery_event"] == "trigger"
    assert trig["priority"] == "P3" and trig["groupable"] is True
    # Webhooks: the service keys, never device_id (PagerDuty keys on the check).
    assert trig["service_check_id"] == CHECK and "device_id" not in trig
    assert trig["hostname"] == "Portal HTTPS" and trig["ip_address"] == "https://portal.example"
    # Mail: the check headline, no IP line, the /services link.
    assert trig["email_overrides"] == {"ip_address": "", "title": "Portal down"}
    assert trig["headline_metric"] == {"label": "HTTP", "value": "DOWN",
                                       "secondary_label": "Target",
                                       "secondary_value": "https://portal.example"}
    assert dict(trig["details"])["Error"] == "HTTP 503"
    assert trig["action_url"] == "https://zp.example/services"

    row["metadata"]["notified"] = True
    await engine.run_service(db, "down", "up")
    rec = engine.calls[-1][1]
    assert rec["alert_id"] == str(row["id"]) and rec["delivery_event"] == "recovery"
    assert rec["groupable"] is False and rec["is_recovery"] is True


# ── traps ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_trap_alert_is_keyed_on_its_row(engine):
    from test_network_trap_contract import DB
    from uuid import uuid4
    rule = NS(id=uuid4(), name="Link down", metric="trap", operator="==", threshold=0,
              device_id=None, group_id=None, scope_tag=None, trap_oid="1.3.6.1.6.3.1.1.5.3",
              severity="warning", notify_channels=[CHANNEL], cooldown=60,
              schedule_start=None, schedule_end=None, schedule_days=None,
              email_subject=None, email_body=None, sms_template=None, conditions=None,
              condition_logic="AND")
    db = DB(rule)
    db.statements = []
    await alert_engine.evaluate_trap(alert_engine.TrapEvent(
        event_id=uuid4(), source_ip="192.0.2.9", trap_oid=rule.trap_oid), db)
    [(_, ctx, _)] = engine.calls
    assert ctx["alert_id"] and ctx["delivery_event"] == "trigger"
    assert ctx["priority"] == "P3" and ctx["groupable"] is True


# ── priority fields ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("severity,rule_priority,trigger,expected", [
    ("critical", None, True, {"priority": "P2", "groupable": False}),
    ("warning", None, True, {"priority": "P3", "groupable": True}),
    ("info", None, True, {"priority": "P4", "groupable": True}),
    ("warning", "P1", True, {"priority": "P1", "groupable": False}),
    ("warning", "P2", True, {"priority": "P2", "groupable": False}),
    ("critical", "P3", True, {"priority": "P3", "groupable": False}),   # critical never waits
    ("warning", None, False, {"priority": "P3", "groupable": False}),   # recoveries never wait
])
def test_notification_fields(severity, rule_priority, trigger, expected):
    assert alert_funnel.notification_fields(severity, rule_priority, trigger=trigger) == expected


# ══ The real dispatcher and keys, on a disposable PostgreSQL ═════════════════

@contextlib.asynccontextmanager
async def session(db):
    eng = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with async_sessionmaker(eng, expire_on_commit=False)() as s:
            yield s
    finally:
        await eng.dispose()


@pytest.fixture
def world(pg_db, monkeypatch):
    """A device, a warning Device Down rule on two email channels that share a
    recipient, a webhook channel, and a default SMTP gateway."""
    dl.reset_for_tests()
    alert_funnel.reset_for_tests()
    monkeypatch.setattr(alert_engine, "_NOTIFY_WHILE_FLAPPING_SUPPORTED", False)
    monkeypatch.setattr(alert_engine, "_PENDING_STATUS_SUPPORTED", None)
    monkeypatch.setattr(alert_engine, "_dashboard_url", AsyncMock(return_value="https://zp.example/alerts"))
    db = pg_db
    db.execute("DELETE FROM alerts; DELETE FROM alert_rules; DELETE FROM notification_gateways")
    db.execute("INSERT INTO notification_gateways (name, type, config, is_default, enabled)"
               " VALUES ('mail', 'smtp', $1::jsonb, true, true)",
               {"host": "smtp.invalid", "from_email": "zenplus@example.invalid"})

    def channel(name, kind, config):
        return str(db.scalar("INSERT INTO notification_channels (name, type, config)"
                             " VALUES ($1, $2, $3::jsonb) RETURNING id", name, kind, config))

    noc = channel("noc", "email", {"recipients": "ops@example.test, noc@example.test"})
    mgmt = channel("mgmt", "email", {"recipients": "OPS@example.test; mgr@example.test"})
    hook = channel("hook", "webhook", {"url": "https://hooks.example.invalid/zenplus"})
    device = str(db.scalar("INSERT INTO devices (hostname, ip_address, status)"
                           " VALUES ('edge-1', '10.88.0.1', 'up') RETURNING id"))
    rule = str(db.scalar(
        "INSERT INTO alert_rules (name, metric, operator, threshold, trigger_on, recovery_alert,"
        " cooldown, min_duration, severity, notify_channels)"
        " VALUES ('Device Down', 'ping_status', '==', 0, 'down', true, 0, 0, 'warning', $1::jsonb)"
        " RETURNING id", [noc, mgmt, hook]))

    mails: list[dict] = []
    hooks: list[dict] = []
    rendered: list[dict] = []
    real_build = email_render.build_alert_email_html

    def build(ctx):
        rendered.append(ctx)
        return real_build(ctx)

    async def send_email(gw, recipients, subject, body, html_body=None):
        mails.append({"to": sorted(r.strip().lower() for r in recipients.split(",")),
                      "subject": subject, "body": body, "html": html_body})
        return True

    async def send_hook(kind, cfg, ctx):
        hooks.append(ctx)
        return True, None

    monkeypatch.setattr(email_render, "build_alert_email_html", build)
    monkeypatch.setattr(alert_engine, "_send_email", send_email)
    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", send_hook)
    return NS(db=db, device=device, rule=rule, mails=mails, hooks=hooks, rendered=rendered)


async def _evaluate(w, old, new, **kw):
    async with session(w.db) as s:
        return await evaluate_status_change(StatusChangeEvent(
            device_id=w.device, hostname="edge-1", ip_address="10.88.0.1",
            old_status=old, new_status=new, **kw), db=s)


def _keys(w, alert_id):
    return w.db.query("SELECT event, recipient, state FROM alert_delivery_keys"
                      " WHERE alert_id = $1 ORDER BY event, recipient", alert_id)


def _open_alert(w):
    return w.db.query("SELECT id, priority, metadata FROM alerts WHERE device_id = $1"
                      " AND status = 'active'", w.device)


@pytest.mark.asyncio
async def test_postgres_device_status_pages_once_per_recipient_with_durable_keys(world):
    w = world
    out = await _evaluate(w, "up", "down")
    [(alert_id, priority, meta)] = _open_alert(w)
    assert priority == "P3"                  # stamped at raise time
    assert meta["notified"] is True
    # ops@ is on both email channels: one mail, not two (the old loop sent two).
    recipients = sorted(r for m in w.mails for r in m["to"])
    assert recipients == ["mgr@example.test", "noc@example.test", "ops@example.test"]
    assert len(w.hooks) == 1 and w.hooks[0]["alert_id"] == str(alert_id)
    assert {(e, s) for e, _, s in _keys(w, alert_id)} == {("trigger", "accepted")}
    assert out["notifications_sent"] >= 2

    # The rendered mail is the one the per-channel loop built.
    [first, *_] = w.mails
    ref = ap.incident_ref(alert_id)
    assert first["subject"].startswith("[WARNING]") and f"[{ref}]" in first["subject"]
    assert "[New]" in first["subject"]
    assert first["body"] == w.rendered[0]["message"]
    ctx = w.rendered[0]
    assert ctx["title"] == "Device Down" and ctx["hostname"] == "edge-1"
    assert ctx["ip_address"] == "10.88.0.1" and ctx["status"] == "DOWN"
    assert ctx["severity"] == "warning" and ctx["resolved"] is False
    assert dict(ctx["details"])["Reachability"] == "Down (no reply)"
    assert ctx["headline_metric"]["label"] == "Packet loss"
    assert ctx["action_url"] == "https://zp.example/alerts"
    assert ctx["incident_ref"] == ref and ctx["lifecycle"] == "new"


@pytest.mark.asyncio
async def test_postgres_a_replayed_recovery_of_the_same_incident_is_not_resent(world):
    w = world
    await _evaluate(w, "up", "down")
    [(alert_id, _, _)] = _open_alert(w)
    w.mails.clear(); w.hooks.clear()

    await _evaluate(w, "down", "up", rtt_ms=1.2)
    assert sorted(r for m in w.mails for r in m["to"]) == [
        "mgr@example.test", "noc@example.test", "ops@example.test"]
    assert len(w.hooks) == 1 and w.hooks[0]["is_recovery"] is True
    assert "[Recovered]" in w.mails[0]["subject"]
    assert {e for e, _, _ in _keys(w, alert_id)} == {"trigger", "recovery"}

    # A replay finds the same incident open again (an evaluation retried
    # after its close was lost): its recovery keys are already accepted, so
    # nobody is told twice — and the send still counts as delivered.
    w.db.execute("UPDATE alerts SET status = 'active', resolved_at = NULL WHERE id = $1", alert_id)
    w.mails.clear(); w.hooks.clear()
    out = await _evaluate(w, "down", "up", rtt_ms=1.2)
    assert w.mails == [] and w.hooks == []
    assert out["notifications_sent"] >= 1


@pytest.mark.asyncio
async def test_postgres_a_second_dispatch_of_the_same_trigger_sends_nothing(world):
    w = world
    await _evaluate(w, "up", "down")
    [(alert_id, _, _)] = _open_alert(w)
    sent = len(w.mails)
    channels = w.db.scalar("SELECT notify_channels FROM alert_rules WHERE id = $1", w.rule)
    async with session(w.db) as s:
        n = await alert_engine._dispatch_planned(s, channels, {
            "subject": "x", "body": "x", "message": "x", "hostname": "edge-1",
            "severity": "warning", "status": "DOWN", "rule_name": "Device Down",
            "alert_id": str(alert_id), "delivery_event": "trigger"})
    assert len(w.mails) == sent and len(w.hooks) == 1
    assert n >= 1
