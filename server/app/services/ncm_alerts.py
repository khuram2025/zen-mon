"""NCM alerts through the alert pipeline (alerting plan G-14).

NCM alerts have no alert rule. They are raised by ``ncm_events`` (a backup
failed, the configuration changed) and by this module (a compliance policy
fails, the running configuration is not saved), and notified to the device's
NCM channels (``device_ncm.notify_channels``).

Delivery
    ``ncm_notification_outbox`` stays the durable queue: the row is written
    in the same transaction as the alert, so a crash never loses a
    notification. ``ncm_events.deliver_due`` hands each due row to
    :func:`send`, which sends it through
    ``host_alert_service.dispatch_to_channels`` with the alert id, a delivery
    event ("trigger", "trigger:repeat:<n>", "recovery"), the alert's priority
    and the event family ``configuration``. Delivery keys therefore stop a
    retried row from reaching a recipient twice, warning/info triggers may be
    grouped into digests and alert storms can combine them. NCM alerts are
    not escalated (escalation policies route rule alerts).

Compliance (kind ``compliance``)
    One alert per device + policy while at least one of the policy's rules
    fails on the device's latest validated running configuration. Severity is
    the worst failing rule's: critical -> critical, high/medium -> warning,
    low/info -> info. It resolves (with a recovery notice when the trigger
    was notified) once the policy has no failing rule and at least one rule
    passed or is excepted. Rules that cannot be assessed (``unknown``) are
    missing data and never resolve it. A policy that no longer applies to the
    device (disabled, platform changed) closes its alert without a notice.
    Violations already failing when migrate-201 ran are ``baseline``: their
    alerts open without a notification.

Unsaved configuration (kind ``unsaved_config``)
    For devices whose NCM captures include the startup configuration: the
    latest validated running and startup captures, taken within
    ``PAIR_WINDOW_S`` of each other, differ after comparison normalisation.
    The alert is raised once the difference has persisted for
    ``UNSAVED_HOLD_S`` across captures (an engineer usually saves within
    minutes of a change) and resolves when a capture pair matches again.
    Captures too far apart to compare are missing data.

State lives in ``ncm_alert_state`` (migrate-201).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from app.services import alert_funnel as funnel

logger = logging.getLogger("zenplus.ncm_alerts")

EVENT_FAMILY = "configuration"
COMPLIANCE = "compliance"
UNSAVED = "unsaved_config"
UNSAVED_HOLD_S = 4 * 3600
PAIR_WINDOW_S = 900
LISTED_RULES = 3

_RULE_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
_ALERT_SEVERITY = {"critical": "critical", "high": "warning", "medium": "warning",
                   "low": "info", "info": "info"}


# ─── Delivery through dispatch_to_channels ───────────────────────────────────

def ctx_from_payload(payload: dict) -> dict:
    """The dispatch_to_channels ctx of a queued NCM notification. The original
    payload keys (event_id, event_type, device_id, metadata, timestamp) stay in
    the ctx, so a generic webhook still receives them."""
    from app.services import alert_phrasing as ap
    p = dict(payload or {})
    recovery = str(p.get("status") or "").upper() == "RESOLVED"
    severity = str(p.get("severity") or ("info" if recovery else "warning")).lower()
    subject = p.get("subject") or "NCM event"
    sentence = p.get("message") or p.get("body") or subject
    who = p.get("hostname") or ""
    if p.get("ip_address") and p.get("ip_address") != who:
        who = f"{who} ({p['ip_address']})" if who else p["ip_address"]
    alert_id = p.get("alert_id")
    when = p.get("timestamp") or datetime.now(timezone.utc).isoformat()
    ctx = {
        **{k: p[k] for k in ("event_id", "event_type", "device_id", "metadata", "timestamp") if k in p},
        "subject": subject, "body": p.get("body") or sentence,
        "message": p.get("sms_body") or subject[:300],
        "hostname": p.get("hostname") or "", "ip_address": p.get("ip_address") or "",
        "status": "RESOLVED" if recovery else (p.get("status") or "ACTIVE"),
        "severity": severity, "resolved": recovery, "is_recovery": recovery,
        "rule_name": p.get("rule_name") or "Configuration management",
        "metric": f"ncm:{p.get('event_type') or 'event'}", "event_family": EVENT_FAMILY,
        "details": [("Device", who or None), ("Event", p.get("rule_name") or None)],
        "triggered_at": when,
        **funnel.notification_fields(severity, None, trigger=not recovery),
    }
    if alert_id:
        ctx["alert_id"] = str(alert_id)
        ctx["delivery_event"] = str(p.get("delivery_event") or ("recovery" if recovery else "trigger"))[:64]
        ctx.update(ap.incident_context(alert_id=alert_id, first_seen=p.get("first_seen") or when,
                                       now=when, lifecycle="recovered" if recovery else "new"))
    return ctx


async def send(db, channel_ids, payload) -> int:
    """The outbox sender: how many of ``channel_ids`` handled the notification.

    A channel on which some recipient's delivery definitely failed counts as
    not handled, so the outbox retries it; the delivery keys then skip the
    recipients that already received it. Commits the session."""
    from app.services import alert_delivery_log as dl
    from app.services.host_alert_service import dispatch_to_channels
    ctx = ctx_from_payload(payload)
    handled = int(await dispatch_to_channels(db, list(channel_ids), ctx) or 0)
    if handled and ctx.get("alert_id") and dl.keys_enabled():
        failed = (await db.execute(text(
            "SELECT count(*) FROM alert_delivery_keys WHERE alert_id = CAST(:aid AS uuid) "
            "AND event = :ev AND channel_id::text = ANY(CAST(:ch AS text[])) AND state = 'failed'"),
            {"aid": ctx["alert_id"], "ev": ctx["delivery_event"],
             "ch": [str(c) for c in channel_ids]})).scalar()
        if failed:
            return 0
    return handled


# ─── Shared alert helpers ────────────────────────────────────────────────────

async def _raise(db, device_id, kind: str, severity: str, subject: str, meta: dict) -> str:
    p_col, p_val, p_params = funnel.priority_insert(await funnel.priority_supported(db), None, severity)
    return (await db.execute(text(
        f"INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata{p_col}) "
        f"VALUES (:id, NULL, 'active', :sev, :msg, now(), CAST(:meta AS jsonb){p_val}) RETURNING id"),
        {"id": device_id, "sev": severity, "msg": subject,
         "meta": json.dumps({"ncm": True, "ncm_event": kind, **meta}, default=str), **p_params})).scalar()


async def _open(db, alert_id):
    if not alert_id:
        return None
    return (await db.execute(text(
        "SELECT id, triggered_at, metadata FROM alerts WHERE id = :id AND status IN ('active','acknowledged')"),
        {"id": alert_id})).first()


async def _resolve(db, alert_id, by: str, extra: Optional[dict] = None) -> bool:
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = now(), "
        "metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
        "WHERE id = :id AND status IN ('active','acknowledged')"),
        {"id": alert_id, "m": json.dumps({"resolved_by": by, **(extra or {})}, default=str)})
    return bool(res.rowcount)


def _meta(meta) -> dict:
    if isinstance(meta, dict):
        return meta
    try:
        return json.loads(meta) if meta else {}
    except ValueError:
        return {}


def _notified(meta) -> bool:
    from app.services.alert_notify_state import flag_means_notified
    return flag_means_notified(_meta(meta).get("notified"))


async def _queue(db, device_id, device, key: str, kind: str, summary: str, sentence: str, *,
                 alert_id, delivery_event: str, severity: str, metadata: dict,
                 status: str = "ACTIVE", first_seen=None) -> bool:
    """Queue one notification per NCM channel of the device. True when queued."""
    from app.services import ncm_events
    if not device.notify_channels:
        return False
    payload = ncm_events._payload(device, device_id, key, kind, summary, metadata=metadata,
                                  severity=severity, status=status, sentence=sentence)
    payload.update(alert_id=str(alert_id), delivery_event=delivery_event)
    if first_seen is not None:
        payload["first_seen"] = first_seen.isoformat() if hasattr(first_seen, "isoformat") else str(first_seen)
    await ncm_events._outbox(db, device_id, device, key, payload)
    return True


def _who(device) -> str:
    from app.services import alert_phrasing as ap
    return ap.asset_label(device.hostname, device.ip, ap.asset_type_label(getattr(device, "device_type", None)))


# ─── Compliance ──────────────────────────────────────────────────────────────

def compliance_sentence(who: str, policy: str, failing: list[dict]) -> str:
    n = len(failing)
    listed = ", ".join(f"{f['title']} ({f['severity']})" for f in failing[:LISTED_RULES])
    more = f" and {n - LISTED_RULES} more" if n > LISTED_RULES else ""
    return (f"Configuration of {who} fails {n} check{'s' if n != 1 else ''} of compliance policy "
            f"{policy}: {listed}{more}.")


def compliance_recovery_sentence(who: str, policy: str) -> str:
    return f"Configuration of {who} passes compliance policy {policy} again."


def worst(failing: list[dict]) -> str:
    return max((f["severity"] for f in failing), key=lambda s: _RULE_RANK.get(s, 0), default="medium")


async def _policy_results(db, device_id) -> dict[str, dict]:
    rows = (await db.execute(text(
        "SELECT r.policy_id::text AS policy_id, p.name AS policy, r.status, pr.title, pr.severity "
        "FROM ncm_compliance_results r JOIN ncm_policies p ON p.id = r.policy_id "
        "JOIN ncm_policy_rules pr ON pr.id = r.rule_id WHERE r.device_id = :d "
        "ORDER BY p.name, pr.position"), {"d": device_id})).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        p = out.setdefault(r["policy_id"], {"name": r["policy"], "failing": [], "ok": 0, "unknown": 0})
        if r["status"] == "fail":
            p["failing"].append({"title": r["title"], "severity": r["severity"]})
        elif r["status"] in ("pass", "excepted"):
            p["ok"] += 1
        else:
            p["unknown"] += 1
    for p in out.values():
        p["failing"].sort(key=lambda f: -_RULE_RANK.get(f["severity"], 0))
    return out


async def sync_compliance(db, device_id) -> dict:
    """Open, update or close the device's compliance alerts from its stored
    results (call after evaluate_device). Never commits."""
    from app.services import ncm_events
    counts = {"raised": 0, "resolved": 0}
    device = await ncm_events._device(db, device_id)
    if not device:
        return counts
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"ncm_alert:{device_id}:{COMPLIANCE}"})
    policies = await _policy_results(db, device_id)
    states = {r.subject: r for r in (await db.execute(text(
        "SELECT subject, alert_id, baseline, first_seen_at FROM ncm_alert_state "
        "WHERE device_id = :d AND kind = :k"), {"d": device_id, "k": COMPLIANCE})).all()}
    who = _who(device)
    for pid, p in policies.items():
        if not p["failing"]:
            continue
        state = states.get(pid)
        rule_sev = worst(p["failing"])
        severity = _ALERT_SEVERITY.get(rule_sev, "warning")
        sentence = compliance_sentence(who, p["name"], p["failing"])
        meta = {"policy_id": pid, "policy": p["name"], "failing": p["failing"][:20],
                "failing_count": len(p["failing"]), "worst_rule_severity": rule_sev}
        summary = "Compliance policy failing"
        open_row = await _open(db, state.alert_id if state else None)
        if open_row:
            await db.execute(text(
                "UPDATE alerts SET severity = :sev, metadata = COALESCE(metadata,'{}'::jsonb) || "
                "CAST(:m AS jsonb) WHERE id = :id"),
                {"id": open_row.id, "sev": severity,
                 "m": json.dumps({**meta, "message": sentence, "last_seen": datetime.now(timezone.utc).isoformat()})})
            await db.execute(text("UPDATE ncm_alert_state SET last_seen_at = now(), detail = CAST(:m AS jsonb) "
                                  "WHERE device_id = :d AND kind = :k AND subject = :s"),
                             {"d": device_id, "k": COMPLIANCE, "s": pid, "m": json.dumps(meta)})
            continue
        baseline = bool(state and state.baseline)
        subject = f"NCM: {summary} - {device.hostname}" + (
            f" ({device.ip})" if device.ip and device.ip != device.hostname else "")
        alert_id = await _raise(db, device_id, "compliance.failed", severity, subject,
                                {**meta, "message": sentence, "baseline": baseline})
        queued = False
        if not baseline:
            queued = await _queue(db, device_id, device, f"compliance:{alert_id}:open", "compliance.failed",
                                  summary, sentence, alert_id=alert_id,
                                  delivery_event="trigger", severity=severity, metadata=meta)
        await db.execute(text("UPDATE alerts SET metadata = metadata || CAST(:m AS jsonb) WHERE id = :id"),
                         {"id": alert_id, "m": json.dumps({"notified": queued})})
        await db.execute(text(
            "INSERT INTO ncm_alert_state (device_id, kind, subject, alert_id, detail) "
            "VALUES (:d, :k, :s, :a, CAST(:m AS jsonb)) ON CONFLICT (device_id, kind, subject) DO UPDATE "
            "SET alert_id = EXCLUDED.alert_id, last_seen_at = now(), detail = EXCLUDED.detail"),
            {"d": device_id, "k": COMPLIANCE, "s": pid, "a": alert_id, "m": json.dumps(meta)})
        counts["raised"] += 1
    for pid, state in states.items():
        p = policies.get(pid)
        if p and p["failing"]:
            continue
        if p and not p["ok"]:
            continue                                   # only unknown results: missing data
        open_row = await _open(db, state.alert_id)
        if open_row:
            name = (p or {}).get("name") or _meta(open_row.metadata).get("policy") or "policy"
            if p is None:
                await _resolve(db, open_row.id, "ncm_compliance_not_applicable")
            else:
                sentence = compliance_recovery_sentence(who, name)
                await _resolve(db, open_row.id, "ncm_compliance", {"recovery_message": sentence})
                if _notified(open_row.metadata):
                    await _queue(db, device_id, device, f"compliance:{open_row.id}:closed", "compliance.passed",
                                 "Compliance policy passing", sentence, alert_id=open_row.id,
                                 delivery_event="recovery", severity="info", status="RESOLVED",
                                 metadata={"policy_id": pid, "policy": name}, first_seen=open_row.triggered_at)
            counts["resolved"] += 1
        await db.execute(text("DELETE FROM ncm_alert_state WHERE device_id = :d AND kind = :k AND subject = :s"),
                         {"d": device_id, "k": COMPLIANCE, "s": pid})
    return counts


# ─── Unsaved running configuration ───────────────────────────────────────────

_STARTUP_NOISE = re.compile(
    r"^(?:Using \d+ out of \d+ bytes[^\n]*|! No configuration change since last restart[^\n]*|"
    r"! Startup config saved at[^\n]*|!Command: show (?:running|startup)-config[^\n]*|"
    r"!(?:Running|Startup) configuration last done at[^\n]*|!Time: [^\n]*)$", re.M)


def comparable(content: str) -> list[str]:
    """Comparison lines of a running or startup configuration."""
    from app.services.ncm_content import normalize_config
    text_ = _STARTUP_NOISE.sub("", normalize_config(content or ""))
    return [line.rstrip() for line in text_.splitlines() if line.strip() and line.strip() not in ("!", "end")]


def unsaved_diff(running: str, startup: str) -> int:
    """Lines that differ between the running and the startup configuration.
    CPU-bound on large configurations: async callers run it off the event loop."""
    from app.services.ncm_content import changed_line_count
    return changed_line_count(comparable(startup), comparable(running))


def unsaved_sentence(who: str, lines: int) -> str:
    return (f"Running configuration of {who} has changes not saved to the startup configuration "
            f"({lines} line{'s' if lines != 1 else ''} differ); a reload would lose them.")


def unsaved_recovery_sentence(who: str) -> str:
    return f"Running configuration of {who} is saved to the startup configuration again."


async def _latest(db, device_id, config_type: str):
    return (await db.execute(text(
        "SELECT id, captured_at, content, content_enc, content_hash, norm_hash FROM device_configs "
        "WHERE device_id = :d AND config_type = :t AND validated ORDER BY captured_at DESC, id DESC LIMIT 1"),
        {"d": device_id, "t": config_type})).first()


async def check_unsaved(db, device_id, *, hold_s: int = UNSAVED_HOLD_S) -> Optional[str]:
    """Compare the latest running and startup captures and open / close the
    unsaved-configuration alert. Returns "raised", "resolved", "pending",
    "open", "saved" or None (nothing to compare). Never commits."""
    from app.services import ncm_events
    from app.services.ncm_content import off_loop, read_content
    running, startup = await _latest(db, device_id, "running"), await _latest(db, device_id, "startup")
    if not running or not startup:
        return None
    r_at, s_at = running.captured_at, startup.captured_at
    if abs((r_at - s_at).total_seconds()) > PAIR_WINDOW_S:
        return None
    device = await ncm_events._device(db, device_id)
    if not device:
        return None
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"ncm_alert:{device_id}:{UNSAVED}"})
    seen_at = max(r_at, s_at)
    lines = 0 if running.norm_hash and running.norm_hash == startup.norm_hash else \
        await off_loop(unsaved_diff, read_content(running), read_content(startup))
    state = (await db.execute(text(
        "SELECT alert_id, first_seen_at FROM ncm_alert_state WHERE device_id = :d AND kind = :k AND subject = ''"),
        {"d": device_id, "k": UNSAVED})).first()
    who = _who(device)
    if not lines:
        if not state:
            return "saved"
        await db.execute(text("DELETE FROM ncm_alert_state WHERE device_id = :d AND kind = :k AND subject = ''"),
                         {"d": device_id, "k": UNSAVED})
        open_row = await _open(db, state.alert_id)
        if not open_row:
            return "saved"
        sentence = unsaved_recovery_sentence(who)
        await _resolve(db, open_row.id, "ncm_unsaved_config", {"recovery_message": sentence})
        if _notified(open_row.metadata):
            await _queue(db, device_id, device, f"unsaved:{open_row.id}:closed", "config.saved",
                         "Configuration saved", sentence, alert_id=open_row.id, delivery_event="recovery",
                         severity="info", status="RESOLVED", metadata={"running_version": str(running.id),
                                                                       "startup_version": str(startup.id)},
                         first_seen=open_row.triggered_at)
        return "resolved"
    meta = {"running_version": str(running.id), "startup_version": str(startup.id), "lines_differ": lines}
    if not state:
        await db.execute(text(
            "INSERT INTO ncm_alert_state (device_id, kind, subject, first_seen_at, last_seen_at, detail) "
            "VALUES (:d, :k, '', :at, :at, CAST(:m AS jsonb))"),
            {"d": device_id, "k": UNSAVED, "at": seen_at, "m": json.dumps(meta)})
        state_first, state_alert = seen_at, None
    else:
        await db.execute(text("UPDATE ncm_alert_state SET last_seen_at = :at, detail = CAST(:m AS jsonb) "
                              "WHERE device_id = :d AND kind = :k AND subject = ''"),
                         {"d": device_id, "k": UNSAVED, "at": seen_at, "m": json.dumps(meta)})
        state_first, state_alert = state.first_seen_at, state.alert_id
    sentence = unsaved_sentence(who, lines)
    open_row = await _open(db, state_alert)
    if open_row:
        await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb) "
                              "WHERE id = :id"), {"id": open_row.id, "m": json.dumps({**meta, "message": sentence})})
        return "open"
    if (seen_at - state_first).total_seconds() < hold_s:
        return "pending"
    summary = "Unsaved configuration"
    subject = f"NCM: {summary} - {device.hostname}" + (
        f" ({device.ip})" if device.ip and device.ip != device.hostname else "")
    alert_id = await _raise(db, device_id, "config.unsaved", "warning", subject,
                            {**meta, "message": sentence, "unsaved_since": state_first.isoformat()})
    queued = await _queue(db, device_id, device, f"unsaved:{alert_id}:open", "config.unsaved", summary, sentence,
                          alert_id=alert_id, delivery_event="trigger", severity="warning", metadata=meta)
    await db.execute(text("UPDATE alerts SET metadata = metadata || CAST(:m AS jsonb) WHERE id = :id"),
                     {"id": alert_id, "m": json.dumps({"notified": queued})})
    await db.execute(text("UPDATE ncm_alert_state SET alert_id = :a WHERE device_id = :d AND kind = :k "
                          "AND subject = ''"), {"a": alert_id, "d": device_id, "k": UNSAVED})
    return "raised"
