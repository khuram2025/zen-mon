"""Multi-level alert escalation (SLA tiers).

The trigger notification goes to the rule's own channels the moment the alert
fires. This service owns everything after that moment: if the alert stays
active and *unacknowledged*, each configured escalation level pages its own
set of channels once its ``after_minutes`` deadline passes — level 1 after
15 minutes, level 2 after 30, and so on, exactly like a tiered on-call SLA.

Rules of the game (the ones an on-call engineer expects):

* Acknowledging the alert stops all further escalation — a human owns it now.
* Resolving the alert sends an all-clear to every channel that was escalated
  to, so a tier-2 manager paged at 03:10 also hears it ended at 03:25. Base
  channels are excluded from that all-clear when the rule already sends its
  own recovery notification (they would get it twice).
* A level may repeat (``repeat_every_minutes``) until ack/resolve;
  ``alert_rules.max_repeat`` caps the number of repeats (0 = unlimited).
* A trigger that was never announced (quiet hours / flap cooldown recorded
  ``metadata.notified = false``) is not escalated: escalating an incident
  nobody was told about pages tier 2 before tier 1.

Escalation state is kept on the alert row as ``metadata->'escalation'``:
``{"level": n, "last_at": iso, "repeats": n, "channels": [...], "closed": bool}``.
The sweeper is idempotent per sweep and safe to run in both uvicorn workers:
the state row is updated in the same transaction as the dispatch attempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap

logger = logging.getLogger("zenplus.alert_escalation")

EVAL_INTERVAL_S = 30
MAX_LEVELS = 5


# ── Pure planning logic (unit-tested) ────────────────────────────────────────

def plan_escalation(levels: list[dict], state: dict | None, triggered_at,
                    now: datetime, max_repeat: int = 0):
    """What, if anything, should happen for one active unacknowledged alert.

    Returns ``("escalate", level_no)`` when a new (higher) level is due,
    ``("repeat", level_no)`` when the current level should re-page, or ``None``.
    ``level_no`` is 1-based. When several levels became due at once (sweeper
    downtime, a long outage discovered late) only the highest fires — paging
    three tiers in the same minute tells nobody anything the top tier doesn't.
    """
    if not levels or not triggered_at:
        return None
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    elapsed_min = (now - triggered_at).total_seconds() / 60.0
    if elapsed_min < 0:
        return None

    current = int((state or {}).get("level") or 0)
    due = [
        i + 1
        for i, lv in enumerate(levels[:MAX_LEVELS])
        if float(lv.get("after_minutes") or 0) <= elapsed_min
    ]
    highest_due = max(due) if due else 0
    if highest_due > current:
        return ("escalate", highest_due)

    if current >= 1 and current <= len(levels):
        lv = levels[current - 1]
        every = lv.get("repeat_every_minutes")
        if every:
            repeats = int((state or {}).get("repeats") or 0)
            if max_repeat and repeats >= max_repeat:
                return None
            last_at = _parse_ts((state or {}).get("last_at"))
            if last_at is None:
                return None
            if (now - last_at).total_seconds() / 60.0 >= float(every):
                return ("repeat", current)
    return None


def _parse_ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        ts = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def escalated_channel_ids(levels: list[dict], upto_level: int) -> list[str]:
    """Distinct channel ids paged by levels 1..upto_level, in level order."""
    seen: list[str] = []
    for lv in levels[:upto_level]:
        for ch in lv.get("notify_channels") or []:
            cid = str(ch)
            if cid not in seen:
                seen.append(cid)
    return seen


def _was_notified(meta: dict) -> bool:
    """Whether the trigger notification actually went out (legacy = yes)."""
    return str(meta.get("notified", True)).lower() != "false"


# ── Sweep ────────────────────────────────────────────────────────────────────

_SWEEP_SQL = """
    SELECT a.id, a.rule_id, a.device_id, a.server_id, a.service_check_id,
           a.status, a.severity AS alert_severity, a.message,
           a.triggered_at, a.resolved_at, a.acknowledged_at, a.metadata,
           r.name AS rule_name, r.metric, r.operator, r.threshold,
           r.conditions, r.condition_logic, r.severity AS rule_severity,
           r.notify_channels, r.escalation_levels, r.max_repeat,
           r.recovery_alert,
           d.hostname AS device_hostname, host(d.ip_address) AS device_ip,
           s.hostname AS server_hostname,
           sc.name AS check_name
    FROM alerts a
    JOIN alert_rules r ON r.id = a.rule_id
    LEFT JOIN devices d ON d.id = a.device_id
    LEFT JOIN servers s ON s.id = a.server_id
    LEFT JOIN service_checks sc ON sc.id = a.service_check_id
    WHERE r.enabled = true
      AND jsonb_typeof(r.escalation_levels) = 'array'
      AND jsonb_array_length(r.escalation_levels) > 0
      AND (
            a.status = 'active'
         OR (a.status = 'resolved'
             AND jsonb_exists(COALESCE(a.metadata, '{}'::jsonb), 'escalation')
             AND COALESCE(a.metadata->'escalation'->>'closed', 'false') <> 'true'
             AND a.resolved_at > now() - interval '1 day')
      )
"""


def _subject_noun(row) -> tuple[str, str]:
    """(display name, ip) for the thing the alert is about."""
    name = row.device_hostname or row.server_hostname or row.check_name or "monitored object"
    return name, (row.device_ip or "")


async def _store_state(db: AsyncSession, alert_id, state: dict) -> None:
    await db.execute(
        text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || "
             "jsonb_build_object('escalation', CAST(:e AS jsonb)) WHERE id = :id"),
        {"id": alert_id, "e": json.dumps(state)},
    )


async def _dispatch_escalation(db: AsyncSession, row, level_no: int,
                               level: dict, *, is_repeat: bool,
                               action_url: str) -> int:
    """Send one escalation (or repeat) notification to the level's channels."""
    from app.services.host_alert_service import dispatch_to_channels

    now = datetime.now(timezone.utc)
    hostname, ip = _subject_noun(row)
    sev = (row.rule_severity or row.alert_severity or "warning")
    duration = ap.duration_between(row.triggered_at, now)
    levels = row.escalation_levels or []
    condition = ap.conditions_label(
        row.conditions, row.condition_logic or "AND",
        metric=row.metric, operator=row.operator, threshold=row.threshold,
    )
    verb = "is repeating at" if is_repeat else "has been escalated to"
    body = (f"{row.message} This alert has been active for {duration} without "
            f"being acknowledged and {verb} escalation level {level_no}.")
    sms = (f"ZenPlus ESCALATION L{level_no} {sev.upper()} — "
           f"{row.rule_name}: {row.message} Active {duration}, unacknowledged.")
    sent = await dispatch_to_channels(db, level.get("notify_channels") or [], {
        "subject": f"[{sev.upper()}] Escalation L{level_no}: {row.rule_name} — {hostname}",
        "body": body,
        "message": sms,
        "hostname": hostname, "ip_address": ip,
        "status": f"ESCALATION L{level_no}",
        "severity": sev, "resolved": False,
        "rule_name": row.rule_name,
        "notice": (
            f"Escalation policy: this alert was not acknowledged within "
            f"{level.get('after_minutes')} minutes, so it has been escalated to "
            f"the level-{level_no} contacts."
        ),
        "details": [
            ("Alert rule", row.rule_name),
            ("Condition", condition),
            ("Escalation", f"Level {level_no} of {len(levels)}"
                           + (" · repeat" if is_repeat else "")),
            ("Active for", duration),
            ("Acknowledged", "No"),
        ],
        "triggered_at": now.isoformat(),
        "action_url": action_url,
        "rule_id": str(row.rule_id),
        "is_recovery": False,
        "escalation_level": level_no,
    })
    return sent


async def _dispatch_all_clear(db: AsyncSession, row, channels: list[str],
                              *, action_url: str) -> int:
    """Resolution notice to the channels that were escalated to."""
    from app.services.host_alert_service import dispatch_to_channels

    hostname, ip = _subject_noun(row)
    sev = (row.rule_severity or row.alert_severity or "warning")
    duration = ap.duration_between(row.triggered_at, row.resolved_at)
    body = (f"The escalated alert has been resolved."
            + (f" It was active for {duration}." if duration else ""))
    sms = (f"ZenPlus resolved — {row.rule_name}: {hostname} all clear."
           + (f" Active for {duration}." if duration else ""))
    return await dispatch_to_channels(db, channels, {
        "subject": f"[{sev.upper()}] Resolved: {row.rule_name} — {hostname}",
        "body": body,
        "message": sms,
        "hostname": hostname, "ip_address": ip,
        "status": "RESOLVED",
        "severity": sev, "resolved": True,
        "rule_name": row.rule_name,
        "details": [
            ("Alert rule", row.rule_name),
            ("Active for", duration),
        ],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "action_url": action_url,
        "rule_id": str(row.rule_id),
        "is_recovery": True,
    })


_warned_missing_column = False


async def sweep_escalations(db: AsyncSession) -> dict[str, int]:
    global _warned_missing_column
    try:
        rows = (await db.execute(text(_SWEEP_SQL))).fetchall()
    except Exception as exc:
        # Rolling upgrade: code deployed before migrate-100 ran. Degrade to a
        # no-op with one warning instead of a stack trace every 30 seconds.
        if "escalation_levels" in str(exc):
            await db.rollback()
            if not _warned_missing_column:
                logger.warning("alert escalation disabled: alert_rules.escalation_levels "
                               "missing — run migrate-100-alert-escalations.sql")
                _warned_missing_column = True
            return {"checked": 0, "escalated": 0, "repeated": 0, "closed": 0}
        raise
    if not rows:
        return {"checked": 0, "escalated": 0, "repeated": 0, "closed": 0}

    from app.api.v1.alert_engine import _dashboard_url
    action_url = await _dashboard_url(db, "/alerts")

    now = datetime.now(timezone.utc)
    escalated = repeated = closed = 0
    for row in rows:
        try:
            meta = dict(row.metadata or {})
            levels = [lv for lv in (row.escalation_levels or []) if isinstance(lv, dict)]
            state = meta.get("escalation") if isinstance(meta.get("escalation"), dict) else None

            if row.status == "resolved":
                # All-clear for the tiers that were paged. Base channels are
                # left out when the rule sends its own recovery notification.
                chans = [str(c) for c in (state or {}).get("channels") or []]
                if row.recovery_alert:
                    base = {str(c) for c in (row.notify_channels or [])}
                    chans = [c for c in chans if c not in base]
                if chans:
                    await _dispatch_all_clear(db, row, chans, action_url=action_url)
                new_state = dict(state or {})
                new_state["closed"] = True
                new_state["closed_at"] = now.isoformat()
                await _store_state(db, row.id, new_state)
                await db.commit()
                closed += 1
                continue

            # Active alerts only escalate once the trigger was actually
            # announced, and only while nobody has acknowledged them.
            if row.acknowledged_at is not None or not _was_notified(meta):
                continue

            action = plan_escalation(levels, state, row.triggered_at, now,
                                     int(row.max_repeat or 0))
            if action is None:
                continue
            kind, level_no = action
            level = levels[level_no - 1]

            new_state = {
                "level": level_no,
                "last_at": now.isoformat(),
                "repeats": (int((state or {}).get("repeats") or 0) + 1)
                           if kind == "repeat" else 0,
                "channels": escalated_channel_ids(levels, level_no),
            }
            # State first, then dispatch, one transaction per alert: a crashed
            # dispatch retries next sweep via repeat cadence rather than
            # double-paging every tier.
            await _store_state(db, row.id, new_state)
            await _dispatch_escalation(db, row, level_no, level,
                                       is_repeat=(kind == "repeat"),
                                       action_url=action_url)
            await db.commit()
            if kind == "repeat":
                repeated += 1
            else:
                escalated += 1
                logger.info("alert %s escalated to level %d (rule %s)",
                            row.id, level_no, row.rule_name)
        except Exception:
            await db.rollback()
            logger.exception("escalation sweep failed for alert %s", row.id)

    return {"checked": len(rows), "escalated": escalated,
            "repeated": repeated, "closed": closed}


async def escalation_sweeper_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(30)  # let the app boot
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await sweep_escalations(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("alert escalation sweep failed")
        # min_duration holds ride the same cadence: pending status alerts are
        # confirmed (still breaching -> dispatch) or cancelled (blip -> silent)
        # once their hold expires. Separate session so a failure in either
        # sweep never poisons the other's transaction.
        try:
            from app.services.alert_hold_service import sweep_pending_holds
            async with AsyncSessionLocal() as db:
                await sweep_pending_holds(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("alert hold sweep failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
