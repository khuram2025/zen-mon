"""Multi-level alert escalation (SLA tiers).

The trigger notification goes to the rule's own channels the moment the alert
fires. This service owns everything after that moment: if the alert stays
active and *unacknowledged*, each configured escalation level pages its own
set of channels once its ``after_minutes`` deadline passes — level 1 after
15 minutes, level 2 after 30, and so on, exactly like a tiered on-call SLA.

Rules of the game (the ones an on-call engineer expects):

* Acknowledging the alert stops all further escalation — a human owns it now.
* A genuine recovery sends an all-clear to every channel that was escalated
  to, so a tier-2 manager paged at 03:10 also hears it ended at 03:25. Base
  channels are excluded from that all-clear when the rule already sends its
  own recovery notification (they would get it twice). A manual closure sends
  "closed by an operator" instead — it is not evidence the condition cleared —
  and superseded/administrative/legacy-snooze closures send nothing.
* A suppressed (snoozed) condition does not escalate: someone chose to mute
  it. Its incident stays open, so the escalation clock is still honest when
  the snooze ends.
* A level may repeat (``repeat_every_minutes``) until ack/resolve;
  ``alert_rules.max_repeat`` caps the number of repeats (0 = unlimited).
* A trigger that was never announced (quiet hours / flap cooldown recorded
  ``metadata.notified = false``) is not escalated: escalating an incident
  nobody was told about pages tier 2 before tier 1.
* Deadlines run from the moment the trigger was announced: a held trigger
  (min_duration, or a flapping device) stamps ``metadata.notified_at`` when the
  hold sweeper confirms it, and that — not ``triggered_at`` — is the anchor.
  Otherwise a trigger held for 10 minutes would page level 1 seconds after
  its own base page. "Active for" still counts from ``triggered_at``.

Escalation state is kept on the alert row as ``metadata->'escalation'``:
``{"level": n, "last_at": iso, "repeats": n, "channels": [...], "closed": bool}``.

Both uvicorn workers start this loop. A sweep runs in one worker at a time
(``alert_worker_lock``), and each step is claimed before it is sent: the new
state is written with a compare-and-set against the state the sweep read and
committed, so a step another sweep already took is skipped, and the dispatch
is keyed per incident, step ("escalation:L2:r1", "all_clear") and recipient,
so a retry or restart never pages a tier twice for the same step. Repeats of a
level are labelled as reminders with their count ("Reminder 2 of 3").
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap
from app.services import alert_notify_state as ns
from app.services.alert_workflow import closure_reason, record_event, suppressed_sql

logger = logging.getLogger("zenplus.alert_escalation")

EVAL_INTERVAL_S = 30
MAX_LEVELS = 5


# ── Pure planning logic (unit-tested) ────────────────────────────────────────

def plan_escalation(levels: list[dict], state: dict | None, triggered_at,
                    now: datetime, max_repeat: int = 0, notified_at=None):
    """What, if anything, should happen for one active unacknowledged alert.

    Returns ``("escalate", level_no)`` when a new (higher) level is due,
    ``("repeat", level_no)`` when the current level should re-page, or ``None``.
    ``level_no`` is 1-based. When several levels became due at once (sweeper
    downtime, a long outage discovered late) only the highest fires — paging
    three tiers in the same minute tells nobody anything the top tier doesn't.

    Level deadlines count from ``COALESCE(notified_at, triggered_at)``:
    ``notified_at`` (datetime or ISO text) is when a held trigger was
    confirmed and announced.
    """
    anchor = _parse_ts(notified_at) or triggered_at
    if not levels or not anchor:
        return None
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    elapsed_min = (now - anchor).total_seconds() / 60.0
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
    """Whether the trigger notification actually went out (legacy = yes).
    A trigger still "sending" is not escalated until its outcome is known."""
    return ns.flag_means_notified(meta.get("notified"))


# ── Sweep ────────────────────────────────────────────────────────────────────

_SWEEP_SQL = """
    SELECT a.id, a.rule_id, a.device_id, a.server_id, a.service_check_id,
           a.status, a.severity AS alert_severity, a.message,
           a.triggered_at, a.resolved_at, a.acknowledged_at, a.metadata,
           a.metadata->>'notified_at' AS notified_at,
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
            (a.status = 'active' AND NOT """ + suppressed_sql("a") + """)
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


async def _store_state(db: AsyncSession, alert_id, state: dict, expected=None) -> bool:
    """Write the escalation state if it is still ``expected`` (the state this
    sweep read, None = none yet). False when another sweep got there first."""
    row = (await db.execute(
        text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || "
             "jsonb_build_object('escalation', CAST(:e AS jsonb)) WHERE id = :id "
             "AND COALESCE(metadata->'escalation', 'null'::jsonb) = CAST(:old AS jsonb) "
             "RETURNING id"),
        {"id": alert_id, "e": json.dumps(state), "old": json.dumps(expected)},
    )).first()
    return row is not None


def _incident(row, now, lifecycle: str, **extra) -> dict:
    return ap.incident_context(alert_id=row.id, first_seen=row.triggered_at, now=now,
                               lifecycle=lifecycle, **extra)


async def _dispatch_escalation(db: AsyncSession, row, level_no: int,
                               level: dict, *, is_repeat: bool,
                               action_url: str, repeats: int = 0) -> int:
    """Send one escalation (or repeat) notification to the level's channels.

    A repeat is a reminder of the same incident and says which one it is
    ("Reminder 2 of 3"; the total is the rule's max_repeat, open-ended when 0).
    """
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
    total = int(row.max_repeat or 0) or None
    if is_repeat:
        incident = _incident(row, now, "reminder", reminder_index=repeats or None,
                             reminder_total=total)
        reminder = incident.get("lifecycle_label") or "Reminder"
        body = (f"{row.message} This alert has been active for {duration} without "
                f"being acknowledged. {reminder} at escalation level {level_no}.")
    else:
        incident = _incident(row, now, "escalated", escalation_level=level_no)
        # "Escalation L2:" already leads the subject; the tag would repeat it.
        incident["lifecycle_tag"] = ""
        body = (f"{row.message} This alert has been active for {duration} without "
                f"being acknowledged and has been escalated to escalation level {level_no}.")
    sms = (f"ZenPlus ESCALATION L{level_no} {sev.upper()} — "
           f"{row.rule_name}: {row.message} Active {duration}, unacknowledged."
           + (f" {incident.get('lifecycle_label')}." if is_repeat else ""))
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
                           + (f" · {incident.get('lifecycle_label', 'reminder').lower()}"
                              if is_repeat else "")),
            ("Active for", duration),
            ("Acknowledged", "No"),
        ],
        "triggered_at": now.isoformat(),
        "action_url": action_url,
        "rule_id": str(row.rule_id),
        "is_recovery": False,
        "escalation_level": level_no,
        "alert_id": str(row.id),
        "delivery_event": f"escalation:L{level_no}:r{repeats if is_repeat else 0}",
        **incident,
    })
    return sent


async def _dispatch_all_clear(db: AsyncSession, row, channels: list[str],
                              *, action_url: str, manual: bool = False) -> int:
    """Resolution notice to the channels that were escalated to. ``manual``
    closures are announced as closed by an operator, never as all clear."""
    from app.services.host_alert_service import dispatch_to_channels

    hostname, ip = _subject_noun(row)
    sev = (row.rule_severity or row.alert_severity or "warning")
    duration = ap.duration_between(row.triggered_at, row.resolved_at)
    meta = dict(row.metadata or {})
    incident = _incident(row, row.resolved_at or datetime.now(timezone.utc),
                         "changed" if manual else "recovered")
    if manual:
        # Not a recovery: name it for what it is, and let the subject's own
        # "Closed by operator:" carry it.
        incident.update({"lifecycle_label": "Closed by operator", "lifecycle_tag": ""})
    if manual:
        who = meta.get("resolved_by_name") or "an operator"
        note = meta.get("resolution_note")
        body = (f"The escalated alert was closed by {who}. This is a manual closure, "
                f"not a confirmed recovery; the monitored condition may still be present."
                + (f" Note: {note}" if note else ""))
        sms = (f"ZenPlus closed — {row.rule_name}: {hostname} closed by {who} "
               f"(manual, not a confirmed recovery).")
        subject = f"[{sev.upper()}] Closed by operator: {row.rule_name} — {hostname}"
    else:
        body = (f"The escalated alert has been resolved."
                + (f" It was active for {duration}." if duration else ""))
        sms = (f"ZenPlus resolved — {row.rule_name}: {hostname} all clear."
               + (f" Active for {duration}." if duration else ""))
        subject = f"[{sev.upper()}] Resolved: {row.rule_name} — {hostname}"
    return await dispatch_to_channels(db, channels, {
        "subject": subject,
        "body": body,
        "message": sms,
        "hostname": hostname, "ip_address": ip,
        "status": "CLOSED" if manual else "RESOLVED",
        "severity": sev, "resolved": True,
        "rule_name": row.rule_name,
        "details": [
            ("Alert rule", row.rule_name),
            ("Active for", duration),
            ("Closure", "Manual (operator)" if manual else "Recovered"),
        ],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "action_url": action_url,
        "rule_id": str(row.rule_id),
        "is_recovery": True,
        "alert_id": str(row.id),
        "delivery_kind": "closure" if manual else "all_clear",
        "delivery_event": "closure" if manual else "all_clear",
        **incident,
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
            stored = meta.get("escalation")      # exactly as read: the claim's expected value
            state = stored if isinstance(stored, dict) else None

            if row.status == "resolved":
                # All-clear for the tiers that were paged. Base channels are
                # left out when the rule sends its own recovery notification.
                # Only a genuine recovery is "all clear"; a manual closure is
                # announced as such, and superseded / legacy-snooze /
                # administrative closures are not news to anyone.
                reason = closure_reason("resolved", meta)
                chans = [str(c) for c in (state or {}).get("channels") or []]
                if row.recovery_alert and reason == "recovered":
                    base = {str(c) for c in (row.notify_channels or [])}
                    chans = [c for c in chans if c not in base]
                new_state = dict(state or {})
                new_state["closed"] = True
                new_state["closed_at"] = now.isoformat()
                # Claim the closure first: of two sweeps that read the same
                # open state, only the one whose write lands sends the notice.
                if not await _store_state(db, row.id, new_state, expected=stored):
                    await db.rollback()
                    continue
                await db.commit()
                if chans and reason in ("recovered", "manual"):
                    await _dispatch_all_clear(db, row, chans, action_url=action_url,
                                              manual=(reason == "manual"))
                    await db.commit()
                closed += 1
                continue

            # Active alerts only escalate once the trigger was actually
            # announced, and only while nobody has acknowledged them.
            if row.acknowledged_at is not None or not _was_notified(meta):
                continue

            action = plan_escalation(levels, state, row.triggered_at, now,
                                     int(row.max_repeat or 0),
                                     notified_at=row.notified_at)
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
            # Claim the step first (compare-and-set against the state read
            # above), then dispatch: a step another sweep already took is
            # skipped, and a crashed dispatch retries next sweep via the
            # repeat cadence rather than double-paging every tier.
            if not await _store_state(db, row.id, new_state, expected=stored):
                await db.rollback()
                continue
            ch_ids = [str(c) for c in (level.get("notify_channels") or [])]
            ch_names = [r[0] for r in (await db.execute(text(
                "SELECT name FROM notification_channels WHERE id::text = ANY(CAST(:ids AS text[]))"
                " ORDER BY name"), {"ids": ch_ids})).fetchall()] if ch_ids else []
            await record_event(db, row.id, "escalated", source="system",
                               old_status=row.status, new_status=row.status,
                               detail={"level": level_no, "repeat": kind == "repeat",
                                       "channels": ch_ids, "channel_names": ch_names,
                                       "of_levels": len(levels)})
            await db.commit()
            await _dispatch_escalation(db, row, level_no, level,
                                       is_repeat=(kind == "repeat"),
                                       action_url=action_url,
                                       repeats=new_state["repeats"])
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


async def run_escalation_pass(session_factory=None):
    """One escalation sweep, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import ESCALATION_PASS, run_pass
    return await run_pass(ESCALATION_PASS, sweep_escalations, session_factory)


async def escalation_sweeper_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(30)  # let the app boot
    while True:
        try:
            await run_escalation_pass()
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
        # Device flap start/stop notices: the status writers only record the
        # episodes, delivery happens here so a notice survives an API restart.
        try:
            from app.services.device_flap_notice_service import deliver_pending
            async with AsyncSessionLocal() as db:
                await deliver_pending(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device flap notice sweep failed")
        # Snoozes that ended while their condition is still open: clear the
        # suppression stamp and remind the rule's channels.
        try:
            from app.services.alert_workflow import sweep_suppression_expiry
            async with AsyncSessionLocal() as db:
                await sweep_suppression_expiry(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("suppression expiry sweep failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
