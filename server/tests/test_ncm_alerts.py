"""NCM alerts through the alert pipeline (alerting plan G-14, migrate-201).

* Queued NCM notifications (ncm_notification_outbox) are sent through
  host_alert_service.dispatch_to_channels with the alert id, a delivery
  event, the priority and the "configuration" event family.
* A failing compliance policy raises one alert per device + policy, closes
  when the policy passes again, keeps it open on unknown results, closes it
  silently when the policy no longer applies, and opens pre-upgrade
  (baseline) violations without paging.
* Running configuration not saved to startup raises an alert after a hold.

Pure parts run without a database; the rest on a disposable PostgreSQL
(pg_fixture). dispatch_to_channels is replaced where the transport is not
the subject.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import host_alert_service, ncm_alerts, ncm_compliance, ncm_events

from pg_fixture import pg_cluster, pg_db  # noqa: F401  (fixtures are used by name)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
CHANNEL_CONFIG = {"url": "https://hooks.example.invalid/ncm"}


# ── Pure parts ──────────────────────────────────────────────────────────────

def test_ctx_from_payload_keys_family_and_priority():
    payload = {"event_id": "v1", "event_type": "config.changed", "device_id": "d1", "hostname": "sw-01",
               "ip_address": "10.0.0.2", "rule_name": "Running configuration changed",
               "subject": "NCM: Running configuration changed - sw-01 (192.0.2.42)",
               "message": "The running configuration of Switch sw-01 (192.0.2.42) changed.",
               "body": "The running configuration of Switch sw-01 (192.0.2.42) changed.",
               "sms_body": "ZenPlus WARNING — The running configuration ...", "severity": "warning",
               "status": "ACTIVE", "timestamp": T0.isoformat(), "metadata": {"added": 1},
               "alert_id": "8c6f0b4e-5d0c-4c39-9a0e-0d6f3d1d2a11", "delivery_event": "trigger:repeat:2"}
    ctx = ncm_alerts.ctx_from_payload(payload)
    assert ctx["alert_id"] == payload["alert_id"] and ctx["delivery_event"] == "trigger:repeat:2"
    assert ctx["event_family"] == "configuration" and ctx["priority"] == "P3" and ctx["groupable"] is True
    assert ctx["event_id"] == "v1" and ctx["metadata"] == {"added": 1}      # webhook receivers keep these
    assert ctx["body"] == payload["body"] and ctx["incident_ref"] == "INC-8c6f0b4e"
    assert ctx["resolved"] is False and ("Device", "sw-01 (10.0.0.2)") in ctx["details"]
    rec = ncm_alerts.ctx_from_payload({**payload, "status": "RESOLVED", "severity": "info",
                                       "delivery_event": "recovery"})
    assert rec["is_recovery"] is True and rec["groupable"] is False and rec["delivery_event"] == "recovery"
    legacy = ncm_alerts.ctx_from_payload({"subject": "NCM test"})        # queued before the upgrade
    assert "alert_id" not in legacy and "delivery_event" not in legacy


def test_compliance_sentences_and_severity():
    failing = [{"title": "No default SNMP communities", "severity": "critical"},
               {"title": "VTY lines do not accept Telnet", "severity": "high"},
               {"title": "NTP server configured", "severity": "medium"},
               {"title": "Login banner present", "severity": "low"}]
    assert ncm_alerts.worst(failing) == "critical"
    assert ncm_alerts.compliance_sentence("Switch sw-01 (192.0.2.42)", "Cisco IOS hardening", failing) == (
        "Configuration of Switch sw-01 (192.0.2.42) fails 4 checks of compliance policy Cisco IOS hardening: "
        "No default SNMP communities (critical), VTY lines do not accept Telnet (high), "
        "NTP server configured (medium) and 1 more.")
    assert ncm_alerts.compliance_recovery_sentence("Switch sw-01", "P") == \
        "Configuration of Switch sw-01 passes compliance policy P again."


def test_unsaved_comparison_ignores_headers():
    running = "Building configuration...\n\nCurrent configuration : 1234 bytes\n!\nhostname sw-01\nntp server 1.1.1.1\nend\n"
    startup = "Using 1180 out of 65536 bytes\n!\nhostname sw-01\nntp server 1.1.1.1\nend\n"
    assert ncm_alerts.unsaved_diff(running, startup) == 0
    assert ncm_alerts.unsaved_diff(running + "logging host 10.0.0.9\n", startup) == 1
    assert ncm_alerts.unsaved_sentence("Switch sw-01 (192.0.2.42)", 3) == (
        "Running configuration of Switch sw-01 (192.0.2.42) has changes not saved to the startup "
        "configuration (3 lines differ); a reload would lose them.")


# ── On PostgreSQL ───────────────────────────────────────────────────────────

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
    db.execute("DELETE FROM alerts")
    channel = str(db.scalar("INSERT INTO notification_channels (name, type, config) VALUES ('ncm-hook', 'webhook', "
                            "$1::jsonb) RETURNING id", CHANNEL_CONFIG))
    device = str(db.scalar("INSERT INTO devices (hostname, ip_address, device_type) VALUES ('sw-01', '192.0.2.42', "
                           "'switch') RETURNING id"))
    db.execute("INSERT INTO device_ncm (device_id, platform, notify_channels) VALUES ($1, 'cisco_ios', "
               "ARRAY[$2]::uuid[])", device, channel)
    sent = []

    async def dispatch(s, channel_ids, ctx):
        sent.append((list(channel_ids), ctx))
        return len(channel_ids)

    monkeypatch.setattr(host_alert_service, "dispatch_to_channels", dispatch)
    return db, device, channel, sent


def ncm_alert_rows(db, device, kind=None):
    rows = db.query("SELECT id::text, status, severity, message, metadata, priority FROM alerts "
                    "WHERE device_id = $1 ORDER BY triggered_at", device)
    out = [(i, s, sev, m, md if isinstance(md, dict) else json.loads(md), p) for i, s, sev, m, md, p in rows]
    return [r for r in out if kind is None or r[4].get("ncm_event") == kind]


def outbox(db):
    rows = db.query("SELECT event_key, status, payload FROM ncm_notification_outbox ORDER BY created_at, event_key")
    return [(k, s, p if isinstance(p, dict) else json.loads(p)) for k, s, p in rows]


@pytest.mark.asyncio
async def test_ncm_events_are_delivered_through_dispatch_to_channels(world):
    db, device, channel, sent = world
    async with session(db) as s:
        first = await ncm_events.event(s, device, "config.changed", "v1", metadata={"added": 2, "removed": 1})
        await ncm_events.event(s, device, "config.changed", "v2", metadata={"added": 1})
        incident, _created = await ncm_events.open_incident(s, device, category="timeout", message="timeout")
        await s.commit()
        assert (await ncm_events.deliver_due(s)) == {"processed": 3, "delivered": 3}
    events = {ctx["event_id"]: ctx for _ch, ctx in sent}
    assert events["v1"]["alert_id"] == str(first) and events["v1"]["delivery_event"] == "trigger"
    assert events["v2"]["alert_id"] == str(first) and events["v2"]["delivery_event"] == "trigger:repeat:2"
    failed = events[f"incident:{incident}:open"]
    assert failed["delivery_event"] == "trigger" and failed["event_family"] == "configuration"
    assert failed["body"].startswith("Configuration backup of Switch sw-01 (192.0.2.42) failed")
    assert all([str(c) for c in ch] == [channel] for ch, _ in sent)
    assert {s for _k, s, _p in outbox(db)} == {"delivered"}
    # The next successful capture closes the incident: the recovery is keyed to the same alert.
    async with session(db) as s:
        await ncm_events.resolve_incident(s, device)
        await s.commit()
        await ncm_events.deliver_due(s)
    rec = sent[-1][1]
    assert rec["delivery_event"] == "recovery" and rec["alert_id"] == failed["alert_id"] and rec["is_recovery"]


@pytest.mark.asyncio
async def test_real_dispatch_keys_make_a_retried_row_send_once(world, monkeypatch):
    """With the real dispatcher, a row delivered but recorded as pending (crash
    after the send) is not sent a second time on the retry."""
    from app.api.v1 import alert_engine
    from app.services import alert_delivery_log as dl
    db, device, channel, _sent = world
    monkeypatch.undo()
    dl.reset_for_tests()
    hooks = []

    async def send_hook(kind, cfg, ctx):
        hooks.append(ctx)
        return True, None
    monkeypatch.setattr(alert_engine, "_dispatch_channel_result", send_hook)
    async with session(db) as s:
        await ncm_events.event(s, device, "config.changed", "v1", metadata={"added": 1})
        await s.commit()
        assert (await ncm_events.deliver_due(s))["delivered"] == 1
    db.execute("UPDATE ncm_notification_outbox SET status = 'pending', next_attempt_at = now()")
    async with session(db) as s:
        assert (await ncm_events.deliver_due(s))["delivered"] == 1   # already delivered: counted
    assert len(hooks) == 1 and hooks[0]["event_family"] == "configuration"
    [(state,)] = db.query("SELECT state FROM alert_delivery_keys")
    assert state == "accepted"


def add_policy(db, name="Cisco IOS hardening", rules=(("ssh-v2", "SSH version 2 only", "medium"),
                                                      ("snmp", "No default SNMP communities", "critical"))):
    pid = str(db.scalar("INSERT INTO ncm_policies (name, platforms) VALUES ($1, ARRAY['cisco_ios']) RETURNING id",
                        name))
    ids = {}
    for pos, (key, title, sev) in enumerate(rules):
        ids[key] = str(db.scalar(
            "INSERT INTO ncm_policy_rules (policy_id, rule_key, title, severity, check_type, pattern, position) "
            "VALUES ($1, $2, $3, $4, 'must_match', 'x', $5) RETURNING id", pid, key, title, sev, pos))
    return pid, ids


def set_results(db, device, pid, statuses: dict):
    db.execute("DELETE FROM ncm_compliance_results WHERE device_id = $1 AND policy_id = $2", device, pid)
    for rule_id, status in statuses.items():
        db.execute("INSERT INTO ncm_compliance_results (device_id, rule_id, policy_id, status) VALUES ($1, $2, $3, $4)",
                   device, rule_id, pid, status)


async def sync(db, device):
    async with session(db) as s:
        out = await ncm_alerts.sync_compliance(s, device)
        await s.commit()
        return out


@pytest.mark.asyncio
async def test_compliance_alert_per_device_and_policy_opens_and_closes(world):
    db, device, channel, _sent = world
    pid, rules = add_policy(db)
    other, other_rules = add_policy(db, "NTP policy", (("ntp", "NTP server configured", "low"),))
    set_results(db, device, pid, {rules["ssh-v2"]: "fail", rules["snmp"]: "pass"})
    set_results(db, device, other, {other_rules["ntp"]: "fail"})
    assert (await sync(db, device))["raised"] == 2
    rows = ncm_alert_rows(db, device, "compliance.failed")
    by_policy = {r[4]["policy"]: r for r in rows}
    aid, status, severity, message, meta, priority = by_policy["Cisco IOS hardening"]
    assert status == "active" and severity == "warning" and priority == "P3"
    assert message == "NCM: Compliance policy failing - sw-01 (192.0.2.42)"
    assert meta["message"] == ("Configuration of Switch sw-01 (192.0.2.42) fails 1 check of compliance policy "
                               "Cisco IOS hardening: SSH version 2 only (medium).")
    assert meta["notified"] is True and meta["failing_count"] == 1
    assert by_policy["NTP policy"][2] == "info"
    [(key, st, payload)] = [o for o in outbox(db) if o[0] == f"compliance:{aid}:open"]
    assert st == "pending" and payload["alert_id"] == aid and payload["delivery_event"] == "trigger"

    # Worse: the critical check fails too. Same incident, severity follows, no second notification.
    set_results(db, device, pid, {rules["ssh-v2"]: "fail", rules["snmp"]: "fail"})
    assert (await sync(db, device))["raised"] == 0
    rows = {r[4]["policy"]: r for r in ncm_alert_rows(db, device, "compliance.failed")}
    assert rows["Cisco IOS hardening"][0] == aid and rows["Cisco IOS hardening"][2] == "critical"
    assert len(outbox(db)) == 2

    # Cannot be assessed (no validated configuration): missing data, stays open.
    set_results(db, device, pid, {rules["ssh-v2"]: "unknown", rules["snmp"]: "unknown"})
    await sync(db, device)
    assert {r[4]["policy"]: r[1] for r in ncm_alert_rows(db, device)}["Cisco IOS hardening"] == "active"

    # Compliant again (an excepted rule counts as compliant): resolved with a recovery.
    set_results(db, device, pid, {rules["ssh-v2"]: "pass", rules["snmp"]: "excepted"})
    assert (await sync(db, device))["resolved"] == 1
    rows = {r[4]["policy"]: r for r in ncm_alert_rows(db, device)}
    assert rows["Cisco IOS hardening"][1] == "resolved"
    [(_k, _st, rec)] = [o for o in outbox(db) if o[0] == f"compliance:{aid}:closed"]
    assert rec["delivery_event"] == "recovery" and rec["status"] == "RESOLVED"
    assert rec["message"] == "Configuration of Switch sw-01 (192.0.2.42) passes compliance policy Cisco IOS hardening again."
    assert db.scalar("SELECT count(*) FROM ncm_alert_state WHERE subject = $1", pid) == 0

    # The NTP policy stops applying (disabled): closed without a notice.
    db.execute("DELETE FROM ncm_compliance_results WHERE policy_id = $1", other)
    await sync(db, device)
    rows = {r[4]["policy"]: r for r in ncm_alert_rows(db, device)}
    assert rows["NTP policy"][1] == "resolved"
    assert rows["NTP policy"][4]["resolved_by"] == "ncm_compliance_not_applicable"
    assert not [o for o in outbox(db) if o[0].endswith(":closed") and o[2]["alert_id"] == rows["NTP policy"][0]]


@pytest.mark.asyncio
async def test_baseline_violation_opens_without_paging(world):
    db, device, _channel, _sent = world
    pid, rules = add_policy(db)
    set_results(db, device, pid, {rules["ssh-v2"]: "fail"})
    # What migrate-201 seeds for violations that existed before the upgrade.
    db.execute("INSERT INTO ncm_alert_state (device_id, kind, subject, baseline) VALUES ($1, 'compliance', $2, true)",
               device, pid)
    await sync(db, device)
    [(aid, status, _sev, _m, meta, _p)] = ncm_alert_rows(db, device, "compliance.failed")
    assert status == "active" and meta["notified"] is False and meta["baseline"] is True
    assert outbox(db) == []
    set_results(db, device, pid, {rules["ssh-v2"]: "pass"})
    await sync(db, device)
    assert ncm_alert_rows(db, device)[0][1] == "resolved" and outbox(db) == []     # no recovery either


def test_migration_seeds_baseline_from_failing_results(world):
    from pathlib import Path
    db, device, _channel, _sent = world
    pid, rules = add_policy(db)
    set_results(db, device, pid, {rules["ssh-v2"]: "fail", rules["snmp"]: "pass"})
    db.execute("DELETE FROM ncm_alert_state")
    db.psql_file(Path(__file__).resolve().parents[2] / "scripts" / "migrate-201-virtualization-ncm-alerts.sql")
    assert db.query("SELECT kind, subject, baseline FROM ncm_alert_state WHERE device_id = $1", device) == [
        ("compliance", pid, True)]


@pytest.mark.asyncio
async def test_evaluate_device_raises_compliance_alerts(world):
    db, device, _channel, _sent = world
    content = "hostname sw-01\nsnmp-server community public RO\nline vty 0 4\n transport input telnet\n"
    async with session(db) as s:
        counts = await ncm_compliance.evaluate_device(s, device, content=content, platform="cisco_ios")
        await s.commit()
    assert counts["fail"] >= 2
    [(_id, status, severity, _m, meta, _p)] = ncm_alert_rows(db, device, "compliance.failed")
    assert status == "active" and severity == "critical" and meta["policy"] == "Cisco IOS / IOS-XE hardening"
    assert meta["worst_rule_severity"] == "critical"


def add_config(db, device, ctype, content, at):
    return str(db.scalar(
        "INSERT INTO device_configs (device_id, config_type, content, content_hash, norm_hash, validated, captured_at) "
        "VALUES ($1, $2, $3, $4, $5, true, $6) RETURNING id", device, ctype, content,
        hashlib.sha256(content.encode()).hexdigest(), hashlib.sha256(content.encode()).hexdigest(), at))


async def unsaved(db, device):
    async with session(db) as s:
        out = await ncm_alerts.check_unsaved(s, device)
        await s.commit()
        return out


@pytest.mark.asyncio
async def test_unsaved_configuration_hold_raise_and_save(world):
    db, device, _channel, _sent = world
    base = "!\nhostname sw-01\nntp server 1.1.1.1\nend\n"
    changed = base.replace("end\n", "logging host 10.0.0.9\nend\n")
    assert await unsaved(db, device) is None                         # nothing captured
    add_config(db, device, "running", "Building configuration...\n" + base, T0)
    add_config(db, device, "startup", "Using 1 out of 65536 bytes\n" + base, T0 + timedelta(seconds=20))
    assert await unsaved(db, device) == "saved"
    # A change is captured before it is saved: pending, not yet an alert.
    add_config(db, device, "running", changed, T0 + timedelta(hours=1))
    assert await unsaved(db, device) is None                         # captures too far apart to compare
    add_config(db, device, "startup", base, T0 + timedelta(hours=1, seconds=30))
    assert await unsaved(db, device) == "pending"
    assert ncm_alert_rows(db, device) == []
    # Still unsaved at the next backup, 4 hours later: one alert, one notification.
    add_config(db, device, "running", changed + "!\n", T0 + timedelta(hours=5, seconds=10))
    add_config(db, device, "startup", base + "!\n", T0 + timedelta(hours=5, seconds=40))
    assert await unsaved(db, device) == "raised"
    [(aid, status, severity, message, meta, _p)] = ncm_alert_rows(db, device, "config.unsaved")
    assert status == "active" and severity == "warning"
    assert meta["message"] == ("Running configuration of Switch sw-01 (192.0.2.42) has changes not saved to the "
                               "startup configuration (1 line differ); a reload would lose them.")
    assert [o[0] for o in outbox(db)] == [f"unsaved:{aid}:open"]
    assert await unsaved(db, device) == "open"
    # Saved: the next pair matches, the alert resolves with a recovery.
    add_config(db, device, "startup", changed, T0 + timedelta(hours=6))
    add_config(db, device, "running", changed, T0 + timedelta(hours=6, seconds=5))
    assert await unsaved(db, device) == "resolved"
    assert ncm_alert_rows(db, device)[0][1] == "resolved"
    assert outbox(db)[-1][0] == f"unsaved:{aid}:closed"
