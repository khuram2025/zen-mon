"""Event alerts: one open row per condition.

Event alerts are raised by something that *happened* — an SNMP trap, a syslog
message, a Device Tracker sighting, an NCM configuration change — not by a
level the evaluators keep measuring, so nothing ever sees them recover. They
used to insert one active row per event: a trap storm left hundreds of open
rows, and a rogue endpoint seen every few minutes stacked a fresh row after
every cooldown (alerting audit G-7).

Now a repeat of the same condition while its row is still open updates that
row instead of inserting another one. The condition is:

* SNMP trap: rule + device + trap OID (rule + source address + trap OID for a
  trap from an unknown device);
* syslog: rule + device (rule + source address for an unknown device);
* Device Tracker event: rule + event kind + endpoint MAC (``udt_dedupe``).

The row keeps its history in metadata: ``occurrences`` (1 for a new row),
``last_seen`` (ISO time of the newest occurrence), ``last_message`` and
``recent_occurrences`` (the newest :data:`RECENT_MAX` as ``{at, message,
event_id}``). ``message`` and ``triggered_at`` stay those of the first event.

Paging is unchanged: the rule's cooldown dedupes pages per condition. A repeat
pages again once the cooldown has passed since the last page
(``metadata.last_notified_at``, else ``triggered_at`` when the trigger went
out), keyed with delivery event ``trigger:repeat:<occurrence>`` so it is not mistaken
for the row's original trigger.

Open event rows that see no new occurrence for the configured window are
closed by :mod:`app.services.alert_expiry`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

RECENT_MAX = 10
MESSAGE_MAX = 1000
OPEN_STATUSES_SQL = "('active', 'acknowledged')"
# Identity, history and workflow keys an occurrence's own details never overwrite.
HISTORY_KEYS = frozenset({
    "occurrences", "last_seen", "last_message", "recent_occurrences", "notified",
    "last_notified_at", "sending_since", "udt_dedupe", "event_type", "mac", "endpoint_id",
    "metric", "trap", "trap_oid", "syslog_source", "ncm", "ncm_event", "resolved_by",
    "escalation", "suppression", "rule_id", "dedupe",
})

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def parse_ts(value: Any) -> datetime | None:
    """A stored ISO timestamp (or datetime) as an aware datetime, else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not _ISO_RE.match(value):
        return None
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def occurrences(meta: dict | None) -> int:
    """How many events the row stands for (1 for a row raised before this)."""
    raw = (meta or {}).get("occurrences")
    if isinstance(raw, bool):
        return 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def first_metadata(at: datetime, message: str | None, event_id: Any = None) -> dict:
    """Occurrence fields for a newly raised event row."""
    entry = {"at": iso(at), "message": (message or "")[:MESSAGE_MAX]}
    if event_id:
        entry["event_id"] = str(event_id)
    return {"occurrences": 1, "last_seen": iso(at), "last_message": entry["message"],
            "recent_occurrences": [entry]}


def occurrence_patch(meta: dict | None, at: datetime, message: str | None, *,
                     event_id: Any = None, extra: dict | None = None) -> dict:
    """The metadata keys to merge into an open row for one more occurrence."""
    meta = meta or {}
    entry = {"at": iso(at), "message": (message or "")[:MESSAGE_MAX]}
    if event_id:
        entry["event_id"] = str(event_id)
    recent = meta.get("recent_occurrences")
    recent = [r for r in recent if isinstance(r, dict)] if isinstance(recent, list) else []
    recent = (recent + [entry])[-RECENT_MAX:]
    patch = {k: v for k, v in (extra or {}).items() if k not in HISTORY_KEYS}
    patch.update({"occurrences": occurrences(meta) + 1, "last_seen": entry["at"],
                  "last_message": entry["message"], "recent_occurrences": recent})
    return patch


def last_page_at(meta: dict | None, triggered_at: datetime | None) -> datetime | None:
    """When this row last paged: ``last_notified_at``, else the trigger time
    unless the trigger was suppressed (``notified`` false)."""
    meta = meta or {}
    stamped = parse_ts(meta.get("last_notified_at"))
    if stamped is not None:
        return stamped
    if str(meta.get("notified", "true")).lower() == "false":
        return None
    return parse_ts(triggered_at)


def repeat_page_due(meta: dict | None, triggered_at: datetime | None, now: datetime,
                    cooldown_s: int) -> bool:
    """A repeat of an open condition pages again once the rule's cooldown has
    passed since the row last paged (or at once if it never paged)."""
    last = last_page_at(meta, triggered_at)
    if last is None:
        return True
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= max(0, int(cooldown_s or 0))


def repeat_event(patch: dict) -> str:
    """Delivery-key event of a repeat page (distinct per occurrence)."""
    return f"trigger:repeat:{int(patch.get('occurrences') or 2)}"


def last_page_sql(a: str = "alerts") -> str:
    """SQL twin of :func:`last_page_at` for rows whose notified flag is true."""
    return (f"COALESCE(CASE WHEN {a}.metadata->>'last_notified_at' ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'"
            f" THEN ({a}.metadata->>'last_notified_at')::timestamptz END, {a}.triggered_at)")
