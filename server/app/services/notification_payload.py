"""Chat, incident and webhook payloads of an alert notification.

Slack, Teams, Discord and PagerDuty lead with the same sentence the mail
and SMS use ("Router core-sw-01 (10.0.0.1) went down at 14:44: it does not
answer ping."), never "core-sw-01 is ALERT", and carry the asset, site, value,
the time in the organisation's regional settings, how long it lasted (on a
recovery) and the incident link. ``alert_engine``'s senders build their
payloads from :func:`chat_view`; ``alert_delivery_log.webhook_sent`` records
the same title and text, so the two are one function.

A generic webhook (and PagerDuty's ``custom_details``) gets
:func:`webhook_fields`: an explicit, documented field set
(docs/alerts-api-contract.md "Webhook payload"). It never carries recipient
addresses, the HTML mail or anything else the dispatch ctx holds internally.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from app.services import alert_phrasing as ap
from app.services.notification_context import local_time

TITLE_CHARS = 250

# The documented generic-webhook / PagerDuty custom_details fields, in order.
WEBHOOK_FIELDS = (
    "event", "event_type", "title", "subject", "message", "body",
    "severity", "priority", "status", "is_recovery", "resolved",
    "rule_id", "rule_name", "alert_id", "incident_ref", "incident_url",
    "hostname", "ip_address", "device_id", "server_id", "service_check_id",
    "site", "group", "metric", "value", "threshold",
    "triggered_at", "local_time", "first_seen", "active_for", "duration",
    "lifecycle", "escalation_level", "details",
)


def _flat(value) -> str:
    return " ".join(str(value or "").split())


def _first_paragraph(value) -> str:
    text_ = str(value or "").strip()
    return _flat(text_.split("\n\n", 1)[0]) if text_ else ""


def title(ctx: dict) -> str:
    """The event sentence: what happened, to which asset."""
    for candidate in (ctx.get("event_sentence"), _first_paragraph(ctx.get("body")),
                      ctx.get("message")):
        flat = _flat(candidate)
        if flat:
            return ap.brief(flat, TITLE_CHARS)
    who = ap.host_label(ctx.get("hostname"), ctx.get("ip_address")) or "An asset"
    return f"{who}: {str(ctx.get('status') or 'alert').lower()}."


def _headline(ctx: dict) -> dict:
    h = ctx.get("headline_metric")
    return h if isinstance(h, dict) else {}


def value_text(ctx: dict) -> str:
    """"97% (threshold 90%)", "DOWN (target https://...)": the headline value."""
    h = _headline(ctx)
    value = _flat(h.get("value"))
    if not value:
        return ""
    second = _flat(h.get("secondary_value"))
    label = _flat(h.get("secondary_label")).lower()
    return f"{value} ({label} {second})" if second and label else value


def duration_text(ctx: dict) -> str:
    """How long the incident lasted, on a recovery ("" otherwise)."""
    if not (ctx.get("is_recovery") or ctx.get("resolved")):
        return ""
    return _flat(ctx.get("active_for") or ctx.get("duration"))


def incident_url(ctx: dict) -> str:
    return ap.incident_links(ctx)[0]


def event_name(ctx: dict) -> str:
    """trigger, recovery, escalation, reminder, all_clear, closure, repeat ..."""
    ev = str(ctx.get("delivery_event") or "").strip()
    if ev:
        head = ev.split(":", 1)[0]
        return "reminder" if ctx.get("lifecycle") == "reminder" and head == "escalation" else head
    if ctx.get("escalation_level"):
        return "escalation"
    return "recovery" if (ctx.get("is_recovery") or ctx.get("resolved")) else "trigger"


def _severity_word(ctx: dict) -> str:
    sev = str(ctx.get("severity") or "warning").strip().lower() or "warning"
    prio = str(ctx.get("priority") or "").strip().upper()
    return f"{sev.capitalize()} ({prio})" if prio else sev.capitalize()


def chat_view(ctx: dict) -> dict:
    """{title, facts, text, url, recovery, severity} for a chat card.

    ``facts`` are (label, value) rows, only the known ones; ``text`` is the
    facts as "Label: value" lines (the delivery log's content)."""
    c = ctx or {}
    recovery = bool(c.get("is_recovery") or c.get("resolved"))
    asset = ap.host_label(c.get("hostname"), c.get("ip_address"))
    ref = _flat(c.get("incident_ref"))
    facts = [
        ("Asset", asset),
        ("Site", _flat(c.get("site"))),
        ("Severity", "Resolved" if recovery else _severity_word(c)),
        ("Value", value_text(c)),
        ("Time", local_time(c)),
        ("Duration", duration_text(c)),
        ("Rule", _flat(c.get("rule_name"))),
        ("Incident", ref),
    ]
    facts = [(label, value) for label, value in facts if value]
    url = incident_url(c)
    lines = [f"{label}: {value}" for label, value in facts]
    if url:
        lines.append(f"Link: {url}")
    return {
        "title": title(c),
        "facts": facts,
        "text": "\n".join(lines),
        "url": url,
        "recovery": recovery,
        "severity": str(c.get("severity") or "warning").lower(),
    }


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def webhook_fields(ctx: dict) -> dict:
    """The generic webhook payload (see WEBHOOK_FIELDS and the API contract)."""
    c = ctx or {}
    h = _headline(c)
    recovery = bool(c.get("is_recovery") or c.get("resolved"))
    details = []
    for row in c.get("details") or []:
        if isinstance(row, (list, tuple)) and len(row) >= 2 and row[1] not in (None, ""):
            details.append({"label": str(row[0]), "value": _jsonable(row[1])})
    out = {
        "event": event_name(c),
        # trigger / recovery, as webhook payloads always carried it.
        "event_type": c.get("event_type") or ("recovery" if recovery else "trigger"),
        "title": title(c),
        "subject": c.get("subject") or "",
        "message": c.get("message") or "",
        # The plain-text mail body; the HTML mail is never sent to a webhook.
        "body": c.get("body") or "",
        "severity": str(c.get("severity") or "warning").lower(),
        "priority": c.get("priority"),
        "status": c.get("status") or "",
        "is_recovery": recovery,
        "resolved": recovery,
        "rule_id": c.get("rule_id"),
        "rule_name": c.get("rule_name") or "",
        "alert_id": c.get("alert_id") or c.get("incident_id") or None,
        "incident_ref": c.get("incident_ref") or (ap.incident_ref(c.get("alert_id")) or None),
        "incident_url": incident_url(c) or None,
        "hostname": c.get("hostname") or "",
        "ip_address": c.get("ip_address") or "",
        "device_id": c.get("device_id"),
        "server_id": c.get("server_id"),
        "service_check_id": c.get("service_check_id"),
        "site": c.get("site") or "",
        "group": c.get("group") or "",
        "metric": h.get("label"),
        "value": h.get("value"),
        "threshold": h.get("secondary_value") if str(h.get("secondary_label") or "").lower()
        in ("threshold", "limit") else None,
        "triggered_at": c.get("triggered_at"),
        "local_time": local_time(c) or None,
        "first_seen": c.get("first_seen") or None,
        "active_for": c.get("active_for") or None,
        "duration": duration_text(c) or None,
        "lifecycle": c.get("lifecycle") or None,
        "escalation_level": c.get("escalation_level"),
        "details": details,
    }
    return {k: _jsonable(out[k]) for k in WEBHOOK_FIELDS}


def template_values(ctx: dict) -> dict:
    """The webhook fields as text, for a channel's own body template."""
    out = {}
    for key, value in webhook_fields(ctx).items():
        if value is None:
            out[key] = ""
        elif isinstance(value, (dict, list)):
            out[key] = json.dumps(value)
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out
