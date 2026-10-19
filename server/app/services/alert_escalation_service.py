"""Alert escalation: escalation policies (1.24.17) and legacy SLA tiers.

Two routes share this sweep (precedence in escalation_policy_service):

* **Escalation policies** — the rule's own policy, else the enabled policy
  that is the default for the incident's priority. Steps reach on-call
  schedules, users and channels; acknowledgement stops step escalation but
  not the unresolved reviews; an expired acknowledgement re-opens escalation;
  an empty or failed step uses the fallback route (``coverage_gap`` /
  ``route_failed`` on the timeline). See :func:`sweep_policy_escalations`.
* **Inline ``escalation_levels``** on a rule with no policy route — the
  behaviour described below, unchanged.

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
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_phrasing as ap
from app.services import alert_notify_state as ns
from app.services import alert_priority as prio
from app.services import builtin_alert_sources as bas
from app.services import escalation_policy_service as eps
from app.services import oncall_service as oc
from app.services.alert_workflow import (
    ack_anchor, closure_reason, effective_rule_sql, expire_acknowledgement, notice_sql,
    record_event, suppressed_sql,
)

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
            (a.status = 'active' AND NOT """ + suppressed_sql("a") + """ {policy_free})
         OR (a.status = 'resolved'
             AND jsonb_exists(COALESCE(a.metadata, '{{}}'::jsonb), 'escalation')
             AND COALESCE(a.metadata->'escalation'->>'closed', 'false') <> 'true'
             AND a.resolved_at > now() - interval '1 day' {legacy_state})
      )
"""


def _legacy_sweep_sql(policies: bool) -> str:
    """The legacy (inline escalation_levels) sweep. Once migration 164 has
    run, an alert routed by an escalation policy (the rule's own, or the
    default for its priority) belongs to the policy sweep instead, and a
    policy-owned escalation state is never closed here."""
    if not policies:
        return _SWEEP_SQL.format(policy_free="", legacy_state="")
    return _SWEEP_SQL.format(
        policy_free=f"AND {eps.route_policy_sql('a', 'r')} IS NULL",
        legacy_state="AND COALESCE(a.metadata->'escalation'->>'mode', '') <> 'policy'")


def _subject_noun(row) -> tuple[str, str]:
    """(display name, ip) for the thing the alert is about."""
    name = (row.device_hostname or row.server_hostname or row.check_name
            or getattr(row, "sensor_name", None) or "monitored object")
    return name, (row.device_ip or "")


def _rule_id_text(rule_id) -> str | None:
    """A rule id for the notification context; None (never "None") for a
    built-in incident, which has no rule."""
    return str(rule_id) if rule_id else None


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
                              *, action_url: str, manual: bool = False,
                              recipients: list[dict] | None = None,
                              priority: str | None = None) -> int:
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
        "rule_id": _rule_id_text(row.rule_id),
        "is_recovery": True,
        "alert_id": str(row.id),
        "delivery_kind": "closure" if manual else "all_clear",
        "delivery_event": "closure" if manual else "all_clear",
        **({"recipients": [{"kind": r["kind"], "address": r["address"],
                            "user_id": r.get("user_id"), "label": r.get("label")}
                           for r in recipients]} if recipients else {}),
        **({"priority": priority} if priority else {}),
        **incident,
    })


_warned_missing_column = False


async def sweep_legacy_escalations(db: AsyncSession) -> dict[str, int]:
    """Inline ``escalation_levels`` tiers (pre-1.24.17 behaviour, unchanged)."""
    global _warned_missing_column
    try:
        rows = (await db.execute(text(_legacy_sweep_sql(await eps.supported(db))))).fetchall()
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


# ── Escalation policies (migration 164) ──────────────────────────────────────
#
# Alerts routed by an escalation policy — the rule's own policy, else the
# enabled policy that is the default for the alert's priority — escalate here;
# everything else keeps the inline levels above. The policy state lives in the
# same ``metadata->'escalation'`` with ``"mode": "policy"`` and is claimed with
# the same compare-and-set, so two sweeps (two workers, or a retry) page a step
# once. Sends name people directly (``ctx["recipients"]``) and channels; the
# delivery keys are "trigger:S0" (the step-0 page at delay 0, groupable for a
# P3/P4 warning like any trigger, minus the rule's own already-paged
# channels), "escalation:S<step>:r<repeat>" (":x<n>" after the n-th expired
# acknowledgement), "review:<n>", "ack_expired:<n>" and
# "fallback:S<step>[:r<repeat>][:x<n>]". The policy takes an incident over
# with a state anchored at that moment, so step 0 is always next.
#
# Built-in incidents (builtin_alert_sources: agent offline, sensor offline,
# NCM backup failures, ...) have no rule: they are routed by the priority
# default policy alone, and only while their family is enabled in the
# builtin_alert_routing setting (health thresholds additionally once open for
# 5 minutes). Their "rule name" is the family label, and channels the source
# already notified itself (an NCM event's device channels) count as the
# rule's own channels: step 0 does not page them again.

POLICY_SWEEP_BATCH = 1000


def _policy_sweep_sql(*, ids_only: bool = False, builtin_families=(), builtin_since=None) -> str:
    """The policy sweep's candidates. ``ids_only``: every candidate id, most
    urgent work first — closures of resolved incidents (their all-clear),
    then by priority (P1 first), then the newest incident — so a backlog
    never starves today's P1 behind old warnings. Otherwise: the full rows
    of the ids in ``:ids`` that are still candidates.

    An open incident is a candidate when its rule is enabled, or — with no
    rule — when it is a built-in incident of an enabled family in
    ``builtin_families`` (route_policy_sql then resolves the priority
    default, the only route a rule-less incident has)."""
    route = eps.route_policy_sql("a", "r")
    builtin = bas.routable_sql("a", builtin_families, builtin_since)
    where = f"""
    FROM alerts a
    LEFT JOIN alert_rules r ON r.id = {effective_rule_sql('a')}
    LEFT JOIN devices d ON d.id = a.device_id
    LEFT JOIN servers s ON s.id = a.server_id
    LEFT JOIN service_checks sc ON sc.id = a.service_check_id
    LEFT JOIN sensors sen ON sen.id = a.sensor_id
    WHERE NOT {notice_sql('a')}
      AND (
            (a.status IN ('active', 'acknowledged')
             AND (r.enabled = true OR (r.id IS NULL AND {builtin}))
             AND NOT {suppressed_sql('a')} AND {route} IS NOT NULL)
         OR (a.status = 'resolved'
             AND a.metadata->'escalation'->>'mode' = 'policy'
             AND COALESCE(a.metadata->'escalation'->>'closed', 'false') <> 'true'
             AND a.resolved_at > now() - interval '1 day')
      )"""
    if ids_only:
        rank = (f"CASE {prio.priority_sql('a', 'r')} WHEN 'P1' THEN 1 WHEN 'P2' THEN 2"
                f" WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END")
        return (f"SELECT a.id {where}"
                f" ORDER BY CASE WHEN a.status = 'resolved' THEN 0 ELSE 1 END, {rank},"
                f" a.triggered_at DESC, a.id")
    return f"""
    SELECT a.id, a.rule_id, a.device_id, a.server_id, a.service_check_id,
           a.status, a.severity AS alert_severity, a.priority AS alert_priority, a.message,
           a.triggered_at, a.resolved_at, a.acknowledged_at, a.acknowledged_by, a.assigned_to,
           a.metadata, {suppressed_sql('a')} AS suppressed,
           r.id AS eff_rule_id, COALESCE(r.name, {bas.label_sql('a')}) AS rule_name,
           r.metric, r.operator, r.threshold,
           r.conditions, r.condition_logic, r.severity AS rule_severity, r.priority AS rule_priority,
           COALESCE(r.notify_channels, {bas.base_channels_sql('a')}) AS notify_channels,
           COALESCE(r.recovery_alert, {bas.notifies_own_recovery_sql('a')}) AS recovery_alert,
           r.escalation_policy_id,
           CASE WHEN r.id IS NULL THEN {bas.family_sql('a')} END AS builtin_key,
           d.hostname AS device_hostname, host(d.ip_address) AS device_ip,
           s.hostname AS server_hostname, sc.name AS check_name, sen.name AS sensor_name,
           {route} AS route_policy_id
    {where}
      AND a.id = ANY(CAST(:ids AS uuid[]))
"""


def _rule_row_view(row):
    """The legacy notification helpers read ``rule_id``; a server alert keeps
    its rule only in metadata, so hand them the effective rule id."""
    class _View:
        def __getattr__(self, name):
            if name == "rule_id":
                return row.eff_rule_id
            return getattr(row, name)
    return _View()


def _priority_of(row) -> str:
    return prio.effective_priority(row.alert_priority, row.rule_priority,
                                   row.alert_severity or row.rule_severity)


def _step_label(policy: eps.Policy, index: int) -> str:
    st = policy.step(index)
    name = (st.name if st and st.name else None)
    return f"Step {index + 1} of {len(policy.steps)}" + (f" ({name})" if name else "")


def _person_keys(people: list[dict]) -> list[dict]:
    """Compact recipient records kept in the state (for the all-clear and
    the reviews): kind, address, user id."""
    return [{"kind": r.get("kind"), "address": r.get("address"), "user_id": r.get("user_id"),
             "label": r.get("user_name") or r.get("label")} for r in people]


def _merge_people(a: list[dict], b: list[dict]) -> list[dict]:
    return oc.dedupe_recipients(list(a or []) + list(b or []))


def _merge_ids(a, b) -> list[str]:
    return list(dict.fromkeys([str(x) for x in (a or [])] + [str(x) for x in (b or [])]))


def incident_links(base: str, alert_id) -> tuple[str, str]:
    """("View incident", "Acknowledge") links for an alert; ("", "") without a
    known dashboard URL. The acknowledge link opens a confirmation page —
    a GET never changes the incident (mail scanners follow links)."""
    if not base:
        return "", ""
    view = f"{base.rstrip('/')}/alerts/{alert_id}"
    return view, f"{view}?ack=1"


async def _dispatch_policy(db: AsyncSession, row, plan: eps.Plan, policy: eps.Policy, *,
                           channels: list[str], people: list[dict], event: str,
                           links: tuple[str, str], fallback: bool = False,
                           owner_name: str | None = None, groupable: bool = False) -> int:
    """One policy notification (step page, repeat, review or acknowledgement
    expiry) to ``channels`` and ``people``. Returns deliveries (held counts)."""
    from app.services.host_alert_service import dispatch_to_channels

    now = datetime.now(timezone.utc)
    hostname, ip = _subject_noun(row)
    sev = (row.rule_severity or row.alert_severity or "warning")
    priority = _priority_of(row)
    duration = ap.duration_between(row.triggered_at, now)
    condition = ap.conditions_label(row.conditions, row.condition_logic or "AND",
                                    metric=row.metric, operator=row.operator, threshold=row.threshold)
    builtin = bas.source(getattr(row, "builtin_key", None))
    if builtin is not None and not condition:
        condition = builtin.description
    rule_name = row.rule_name or (builtin.label if builtin else None) or "Alert"
    step_txt = _step_label(policy, plan.step) if plan.step >= 0 else ""
    acked = row.status == "acknowledged" and plan.kind != "ack_expired"
    if plan.kind == "review":
        incident = _incident(row, now, "reminder", reminder_index=plan.review)
        incident.update(lifecycle_label=f"Unresolved update {plan.review}",
                        lifecycle_tag=f"Update {plan.review}")
        head = f"Unresolved update {plan.review}"
        body = (f"{row.message} This incident is still open after {duration}"
                + (f" and is owned by {owner_name}." if owner_name else
                   (" and has been acknowledged." if acked else " and has not been acknowledged."))
                + " Please review its impact and progress.")
        notice = (f"Escalation policy {policy.name}: an update is sent every "
                  f"{policy.review_minutes} minutes until the incident is resolved.")
    elif plan.kind == "ack_expired":
        incident = _incident(row, now, "reminder")
        incident.update(lifecycle_label="Acknowledgement expired", lifecycle_tag="Ack expired")
        head = "Acknowledgement expired"
        body = (f"{row.message} The acknowledgement is older than {policy.ack_expiry_minutes} "
                f"minutes and the incident is still open (active for {duration}), so escalation "
                f"has re-opened at {step_txt.lower() or 'the first step'}.")
        notice = f"Escalation policy {policy.name}: acknowledgements expire after {policy.ack_expiry_minutes} minutes."
    elif plan.kind == "repeat":
        total = int(policy.max_repeats or 0) or None
        incident = _incident(row, now, "reminder", reminder_index=plan.repeat, reminder_total=total)
        head = f"Escalation S{plan.step}"
        body = (f"{row.message} This alert has been active for {duration} without being "
                f"acknowledged. {incident.get('lifecycle_label') or 'Reminder'} at {step_txt.lower()}.")
        notice = (f"Escalation policy {policy.name}: the current step is re-notified every "
                  f"{policy.repeat_minutes} minutes until someone acknowledges.")
    elif plan.step <= 0:
        incident = _incident(row, now, "new")
        head = "ALERT"
        body = f"{row.message} You are notified by escalation policy {policy.name}."
        notice = f"Escalation policy {policy.name}: initial notification ({step_txt.lower()})."
    else:
        incident = _incident(row, now, "escalated", escalation_level=plan.step)
        incident["lifecycle_tag"] = ""
        delay = policy.steps[plan.step].delay_minutes
        head = f"Escalation S{plan.step}"
        body = (f"{row.message} This alert has been active for {duration} without being "
                f"acknowledged and has been escalated to {step_txt.lower()}.")
        notice = (f"Escalation policy {policy.name}: this alert was not acknowledged within "
                  f"{delay} minutes, so it has been escalated.")
    if fallback:
        notice += " The step's own route reached nobody, so the policy's fallback route is notified."
    sms = (f"ZenPlus {priority} {head.upper()} {sev.upper()} — {rule_name}: {row.message} "
           f"Active {duration}" + (", acknowledged." if acked else ", unacknowledged.")
           + (f" {incident.get('incident_ref')}" if incident.get("incident_ref") else ""))
    details = [(("Built-in event", rule_name) if builtin is not None else ("Alert rule", rule_name)),
               ("Condition", condition),
               ("Priority", prio.priority_label(priority)),
               ("Escalation policy", policy.name)]
    if step_txt:
        details.append(("Escalation", step_txt + (" · fallback route" if fallback else "")))
    details += [("Active for", duration),
                ("Acknowledged", f"Yes{' — owner ' + owner_name if owner_name else ''}" if acked else "No")]
    view_url, ack_url = links
    return await dispatch_to_channels(db, channels, {
        "subject": f"[{sev.upper()}] {head}: {rule_name} — {hostname}",
        "body": body, "message": sms,
        "hostname": hostname, "ip_address": ip,
        "status": head.upper() if plan.kind != "escalate" or plan.step > 0 else "ALERT",
        "severity": sev, "priority": priority, "resolved": False,
        "rule_name": rule_name, "notice": notice, "details": details,
        "triggered_at": now.isoformat(),
        "action_url": view_url, "incident_url": view_url, "ack_url": ack_url,
        "rule_id": _rule_id_text(row.eff_rule_id), "is_recovery": False,
        "escalation_level": plan.step if plan.step >= 0 else None,
        "alert_id": str(row.id), "delivery_event": event,
        "recipients": [{"kind": r["kind"], "address": r["address"], "user_id": r.get("user_id"),
                        "label": r.get("label") or r.get("user_name")} for r in people],
        # Only the step-0 page of a P3/P4 warning may be held for a digest;
        # escalations, repeats, reviews and expiries always go out at once.
        "groupable": bool(groupable),
        "email_overrides": {"incident_url": view_url, "ack_url": ack_url, "priority": priority},
        **incident,
    })


async def _owner(directory: eps.Directory, row) -> tuple[list[dict], str | None]:
    uid = row.assigned_to or row.acknowledged_by
    if not uid:
        return [], None
    await directory.prime_people([uid])
    person = directory.people.get(str(uid))
    if person is None:
        return [], None
    return _person_keys(person.recipients("owner")), person.name


async def _close_policy(db: AsyncSession, row, state: dict, stored, now, links) -> bool:
    """Resolution: cancel pending steps and send the all-clear (or the manual
    closure notice) to everyone the policy notified."""
    meta = dict(row.metadata or {})
    reason = closure_reason("resolved", meta)
    chans = [str(c) for c in state.get("channels") or []]
    if row.recovery_alert and reason == "recovered":
        base = {str(c) for c in (row.notify_channels or [])}
        chans = [c for c in chans if c not in base]
    people = list(state.get("people") or [])
    new_state = dict(state, closed=True, closed_at=now.isoformat())
    if not await _store_state(db, row.id, new_state, expected=stored):
        await db.rollback()
        return False
    await db.commit()
    if (chans or people) and reason in ("recovered", "manual"):
        await _dispatch_all_clear(db, _rule_row_view(row), chans, action_url=links[0],
                                  manual=(reason == "manual"), recipients=people,
                                  priority=_priority_of(row))
        await db.commit()
    return True


async def sweep_policy_escalations(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """One pass over policy-routed incidents. ``now`` injects the clock."""
    counts = {"checked": 0, "escalated": 0, "repeated": 0, "reviews": 0,
              "ack_expired": 0, "fallbacks": 0, "closed": 0}
    if not await eps.supported(db):
        return counts
    families, since = await bas.routing_state(db)
    ids = [str(r[0]) for r in (await db.execute(text(
        _policy_sweep_sql(ids_only=True, builtin_families=families, builtin_since=since)))).fetchall()]
    await db.commit()
    directory = eps.Directory(db)
    from app.api.v1.alert_engine import _dashboard_url
    base_url = await _dashboard_url(db, "") if ids else ""
    for start in range(0, len(ids), POLICY_SWEEP_BATCH):
        chunk = ids[start:start + POLICY_SWEEP_BATCH]
        found = {str(r.id): r for r in (await db.execute(
            text(_policy_sweep_sql(builtin_families=families, builtin_since=since)), {"ids": chunk})).fetchall()}
        rows = [found[i] for i in chunk if i in found]
        counts["checked"] += len(rows)
        await _sweep_policy_rows(db, rows, now, base_url, directory, counts)
    return counts


async def _sweep_policy_rows(db: AsyncSession, rows, now, base_url, directory, counts) -> None:
    """One batch of the policy sweep, in the order given."""
    if not rows:
        return
    policy_ids = {str(r.route_policy_id) for r in rows if r.route_policy_id}
    policy_ids |= {str((r.metadata or {}).get("escalation", {}).get("policy_id"))
                   for r in rows if eps.is_policy_state((r.metadata or {}).get("escalation"))}
    policies = await eps.load_policies(db, [p for p in policy_ids if p and p != "None"])
    for row in rows:
        try:
            clock = now or datetime.now(timezone.utc)
            meta = dict(row.metadata or {})
            stored = meta.get("escalation")
            state = stored if eps.is_policy_state(stored) else None
            links = incident_links(base_url, row.id)
            if row.status == "resolved":
                if state is not None and await _close_policy(db, row, state, stored, clock, links):
                    counts["closed"] += 1
                continue
            policy = policies.get(str(row.route_policy_id))
            if policy is None:
                continue
            # Escalate only once the trigger was announced (quiet hours or a
            # flap cooldown leave it unannounced: paging step 1 first would
            # page the backup before the primary).
            if not _was_notified(meta):
                continue
            if state is None or state.get("policy_id") != policy.id:
                # Takeover: the policy owns this incident from now on (a new
                # incident, one a legacy route or another policy escalated, or
                # an old incident whose rule / priority default just changed).
                # Its steps and reviews count from this moment, so step 0 is
                # next — never the highest step at once. Who was paged before
                # still gets the all-clear. Claimed before anything is planned.
                fresh = eps.new_state(policy.id, at=clock,
                                      carry=stored if isinstance(stored, dict) else None)
                if state is not None:
                    fresh["people"] = list(state.get("people") or [])
                if not await _store_state(db, row.id, fresh, expected=stored):
                    await db.rollback()
                    continue
                await db.commit()
                state = stored = fresh
            anchor = ack_anchor(row.triggered_at, meta)
            plan = eps.plan_policy(policy, state, status=row.status,
                                   anchor=anchor, acknowledged_at=row.acknowledged_at, now=clock,
                                   suppressed=bool(row.suppressed))
            if plan is None or plan.kind == "close":
                continue
            await _run_plan(db, row, policy, plan, state, stored, clock, links, directory, counts)
        except Exception:
            await db.rollback()
            logger.exception("policy escalation sweep failed for alert %s", row.id)


def _initial_groupable(row) -> bool:
    """The step-0 page may be grouped like any trigger: a warning/info
    incident of priority P3/P4 (alert_funnel.notification_fields)."""
    sev = str(row.alert_severity or row.rule_severity or "warning").strip().lower()
    return sev in ("warning", "info") and not prio.is_urgent(_priority_of(row))


async def _run_plan(db, row, policy, plan, state, stored, clock, links, directory, counts) -> None:
    new_state = eps.apply_plan(state if state is not None else stored, plan, policy, clock)
    initial = eps.is_initial_page(policy, plan)
    if plan.kind == "review" and plan.covered:
        # A step page went out at the review's due time: it was the update.
        if await _store_state(db, row.id, new_state, expected=stored):
            await db.commit()
        else:
            await db.rollback()
        return

    # Who this action reaches.
    owner_people, owner_name = await _owner(directory, row)
    fallback_used = False
    gap_reasons: list[str] = []
    if plan.kind == "review":
        people = _merge_people(owner_people, new_state.get("last_people") or [])
        channels = [str(c) for c in new_state.get("last_channels") or []]
        if not (people or channels):
            # Nobody owns it and no step has paged yet: the rule's own
            # channels (its base notification) get the update.
            channels = [str(c) for c in (row.notify_channels or [])]
            await directory.prime([{"type": "channel", "id": c} for c in channels], clock)
    elif plan.kind == "ack_expired" and policy.step(plan.step) is None:
        # A policy without steps: the owner and the rule's own channels hear
        # that the acknowledgement lapsed.
        people = owner_people
        channels = [str(c) for c in (row.notify_channels or [])]
        await directory.prime([{"type": "channel", "id": c} for c in channels], clock)
    else:
        step = policy.step(plan.step)
        targets = list(step.targets) if step else []
        await directory.prime(targets + list(policy.fallback_targets), clock)
        res = directory.resolve(targets, clock)
        people, channels = _person_keys(res.recipients), res.channel_ids
        gap_reasons = list(res.gaps)
        if eps.coverage_problem(people, channels):
            fb = directory.resolve(policy.fallback_targets, clock)
            people, channels = _person_keys(fb.recipients), fb.channel_ids
            gap_reasons += fb.gaps
            fallback_used = True
        if plan.kind == "ack_expired":
            people = _merge_people(owner_people, people)
        new_state["last_people"], new_state["last_channels"] = people, channels
    new_state["people"] = _merge_people(new_state.get("people") or [], people)
    new_state["channels"] = _merge_ids(new_state.get("channels") or [], channels)
    send_channels = channels
    if initial:
        # The base trigger already paged the rule's own channels: step 0 does
        # not page them twice (they stay recorded for the all-clear).
        base = {str(c) for c in (row.notify_channels or [])}
        send_channels = [c for c in channels if c not in base]

    # Claim (compare-and-set), then record, commit, and only then send.
    if plan.kind == "ack_expired":
        if not await expire_acknowledgement(db, row.id, acknowledged_at=row.acknowledged_at,
                                            expiry_minutes=policy.ack_expiry_minutes):
            await db.rollback()
            return
    if not await _store_state(db, row.id, new_state, expected=stored):
        await db.rollback()
        return
    names = [directory.channels[c].name for c in channels if c in directory.channels]
    detail = {"policy_id": policy.id, "policy_name": policy.name, "step": plan.step,
              "of_steps": len(policy.steps), "channels": channels, "channel_names": names,
              "recipients": [{"kind": p["kind"], "user_id": p.get("user_id"), "label": p.get("label")}
                             for p in people],
              **({"gaps": gap_reasons} if gap_reasons else {})}
    if plan.kind in ("escalate", "repeat"):
        await record_event(db, row.id, "escalated", source="system", old_status=row.status,
                           new_status=row.status,
                           detail={**detail, "repeat": plan.kind == "repeat",
                                   "repeat_no": plan.repeat, "skipped_steps": plan.skipped,
                                   # Legacy readers of the event show "level".
                                   "level": plan.step})
    elif plan.kind == "review":
        await record_event(db, row.id, "review", source="system", old_status=row.status,
                           new_status=row.status, detail={**detail, "review": plan.review})
    if fallback_used:
        await record_event(db, row.id, "coverage_gap", source="system", old_status=row.status,
                           new_status=row.status,
                           reason=f"{_step_label(policy, plan.step)} reached nobody; fallback route used.",
                           detail={**detail, "gaps": gap_reasons, "fallback": True,
                                   "fallback_empty": not (people or channels)})
    await db.commit()

    if not (people or channels):
        return
    count_key = {"escalate": "escalated", "repeat": "repeated", "review": "reviews",
                 "ack_expired": "ack_expired"}[plan.kind]
    if not (people or send_channels):
        counts[count_key] += 1          # step 0 reached only the rule's own (already paged) channels
        return
    expiries = int(new_state.get("expiries") or 0)
    step_event = eps.delivery_event(plan, expiries=expiries, initial=initial)
    fb_event = eps.fallback_event(plan.step, repeat=plan.repeat, expiries=expiries)
    event = fb_event if fallback_used and plan.step >= 0 else step_event
    sent = await _dispatch_policy(db, row, plan, policy, channels=send_channels, people=people,
                                  event=event, links=links, fallback=fallback_used,
                                  owner_name=owner_name,
                                  groupable=initial and _initial_groupable(row))
    await db.commit()
    counts[count_key] += 1
    if fallback_used:
        counts["fallbacks"] += 1
    problem = eps.delivery_problem(sent, True)
    if (problem and not fallback_used and plan.kind != "review" and plan.step >= 0
            and policy.fallback_targets):
        await _route_failed(db, row, policy, plan, new_state, directory, clock, links,
                            owner_name, detail, counts, step_event=step_event, fb_event=fb_event)
    elif problem:
        await record_event(db, row.id, "route_failed", source="system", old_status=row.status,
                           new_status=row.status, reason="No delivery of this notification succeeded.",
                           detail={**detail, "event": event, "fallback": False})
        await db.commit()


async def _route_failed(db, row, policy, plan, state, directory, clock, links, owner_name,
                        detail, counts, *, step_event: str, fb_event: str) -> None:
    """Every delivery of a step failed: record it and use the fallback route."""
    fb = directory.resolve(policy.fallback_targets, clock)
    people, channels = _person_keys(fb.recipients), fb.channel_ids
    await record_event(db, row.id, "route_failed", source="system", old_status=row.status,
                       new_status=row.status,
                       reason=f"No delivery for {_step_label(policy, plan.step).lower()} succeeded;"
                              f" fallback route used.",
                       detail={**detail, "event": step_event, "fallback": True,
                               "fallback_channels": channels, "gaps": fb.gaps})
    expected = state
    new_state = dict(state)
    new_state["people"] = _merge_people(state.get("people") or [], people)
    new_state["channels"] = _merge_ids(state.get("channels") or [], channels)
    new_state["last_people"] = _merge_people(state.get("last_people") or [], people)
    new_state["last_channels"] = _merge_ids(state.get("last_channels") or [], channels)
    if not await _store_state(db, row.id, new_state, expected=expected):
        await db.commit()       # keep the event; another sweep owns the state now
        return
    await db.commit()
    if people or channels:
        await _dispatch_policy(db, row, plan, policy, channels=channels, people=people,
                               event=fb_event, links=links, fallback=True,
                               owner_name=owner_name)
        await db.commit()
        counts["fallbacks"] += 1


async def sweep_escalations(db: AsyncSession) -> dict[str, int]:
    """Legacy inline tiers, then escalation policies (one pass, one worker)."""
    out = await sweep_legacy_escalations(db)
    try:
        policy = await sweep_policy_escalations(db)
    except Exception:
        await db.rollback()
        logger.exception("policy escalation sweep failed")
        policy = {}
    for k, v in policy.items():
        if k != "checked":
            out[k] = out.get(k, 0) + v
    out["policy_checked"] = policy.get("checked", 0)
    return out


# ── Route explanation (GET /alerts/{id}/route) ───────────────────────────────

async def explain_route(db: AsyncSession, alert_id, *, now: datetime | None = None,
                        reveal: bool = False) -> dict | None:
    """Why and where this incident escalates, as of ``now``. Never sends."""
    now = now or datetime.now(timezone.utc)
    policies_ok = await eps.supported(db)
    cols = ("a.priority AS alert_priority, r.priority AS rule_priority, r.escalation_policy_id,"
            if policies_ok else "NULL AS alert_priority, NULL AS rule_priority, NULL AS escalation_policy_id,")
    row = (await db.execute(text(
        f"SELECT a.id, a.status, a.severity, a.triggered_at, a.acknowledged_at, a.resolved_at,"
        f" a.metadata, {suppressed_sql('a')} AS suppressed, {cols}"
        f" r.id AS eff_rule_id, r.name AS rule_name, r.enabled AS rule_enabled,"
        f" r.escalation_levels, r.max_repeat,"
        f" CASE WHEN r.id IS NULL THEN {bas.family_sql('a')} END AS builtin_key"
        f" FROM alerts a LEFT JOIN alert_rules r ON r.id = {effective_rule_sql('a')} WHERE a.id = :id"
    ), {"id": alert_id})).first()
    if not row:
        return None
    meta = dict(row.metadata or {})
    priority = prio.effective_priority(row.alert_priority, row.rule_priority, row.severity)
    stored = meta.get("escalation") if isinstance(meta.get("escalation"), dict) else None
    anchor = ack_anchor(row.triggered_at, meta)
    rule_policy = None
    defaults: dict = {}
    if policies_ok:
        defaults = await eps.default_policies(db)
        if row.escalation_policy_id:
            rp = (await db.execute(text("SELECT id, name, enabled FROM escalation_policies WHERE id = :id"),
                                   {"id": row.escalation_policy_id})).first()
            if rp:
                rule_policy = {"id": str(rp.id), "name": rp.name, "enabled": rp.enabled}
    legacy = row.escalation_levels if row.eff_rule_id else None
    builtin = bas.source(row.builtin_key) if row.eff_rule_id is None else None
    builtin_info = None
    if builtin is not None:
        enabled = builtin.key in await bas.enabled_families(db)
        # When routing for this family started (stamped by the escalation sweep
        # or the settings PUT); read-only here, explain never stamps it.
        since = bas._iso_or_none((await bas._stored_since(db)).get(builtin.key)) if enabled else None
        builtin_info = {"key": builtin.key, "label": builtin.label, "enabled": enabled,
                        "enabled_since": since,
                        "min_age_minutes": builtin.min_age_s // 60 if builtin.min_age_s else 0}
        if not enabled:
            route = eps.Route("none", notes=[
                f"Built-in event {builtin.label}: routing is disabled in settings,"
                f" so it does not escalate."])
        elif since is None:
            # Enabled by default but not yet stamped: the next sweep records
            # "now" as the start, so every incident that already exists
            # (including this one) stays unescalated.
            route = eps.Route("none", notes=[
                f"Built-in event {builtin.label}: routing is enabled and starts with the next escalation"
                f" sweep; only incidents raised after that sweep are escalated, so this one is not."])
        elif _parse_ts(row.triggered_at) and _parse_ts(row.triggered_at) < _parse_ts(since):
            route = eps.Route("none", notes=[
                f"Built-in event {builtin.label}: raised before routing for this event type was"
                f" enabled, so it is not escalated (only new incidents are)."])
        else:
            # No rule: the priority default policy is the only route.
            route = eps.resolve_route(priority=priority, rule_policy=None, defaults=defaults,
                                      legacy_levels=None)
            if route.source == "none":
                route.notes.append(f"Built-in event {builtin.label}: no escalation policy is the default"
                                   f" for {prio.priority_label(priority)}, so it does not escalate.")
            else:
                route.notes.append(f"Built-in event {builtin.label} (no alert rule): routed by the"
                                   f" {priority} default policy.")
                trig = _parse_ts(row.triggered_at)
                if (builtin.min_age_s and row.status in ("active", "acknowledged") and trig
                        and (now - trig).total_seconds() < builtin.min_age_s):
                    route.notes.append(f"It is escalated once it has been open for"
                                       f" {builtin.min_age_s // 60} minutes.")
    elif row.eff_rule_id is None:
        route = eps.Route("none", notes=["This incident has no alert rule, so it does not escalate."])
    else:
        route = eps.resolve_route(priority=priority, rule_policy=rule_policy, defaults=defaults,
                                  legacy_levels=legacy)
    if row.eff_rule_id is not None and not row.rule_enabled:
        route.notes.append("The alert rule is disabled; nothing escalates while it is off.")
    # An incident already escalating under a policy that is no longer its
    # route still closes through that policy's state.
    if eps.is_policy_state(stored) and route.source in ("legacy_levels", "none"):
        route.notes.append("This incident was escalated by a policy that no longer routes it;"
                           " it stops at its current step and still gets the all-clear.")
    out = {
        "alert_id": str(row.id), "priority": priority, "priority_label": prio.priority_label(priority),
        "status": row.status, "at": now,
        "policy": {"id": route.policy_id, "name": route.policy_name, "source": route.source},
        "notes": route.notes, "state": None, "steps": [],
        "builtin": builtin_info,
    }
    acknowledged = row.status == "acknowledged"
    if route.source in ("rule", "priority_default"):
        policy = (await eps.load_policies(db, [route.policy_id])).get(route.policy_id)
        if policy is None:
            return out
        state = stored if eps.is_policy_state(stored) and stored.get("policy_id") == policy.id else None
        # Not taken over yet: the sweep will anchor its steps at takeover.
        st = state or eps.new_state(policy.id, at=now)
        next_at, next_what = eps.next_action(policy, st, status=row.status, anchor=anchor,
                                             acknowledged_at=row.acknowledged_at, now=now,
                                             suppressed=bool(row.suppressed))
        esc_anchor = eps.escalation_anchor(st, anchor) or now
        preview = await eps.preview(db, policy, priority=priority, at=esc_anchor, reveal=reveal)
        history = st.get("history") or {}
        for item in preview["steps"]:
            item["notified_at"] = history.get(str(item["position"]))
            item["current"] = item["position"] == int(st.get("step", -1))
        out.update(
            state={"step": int(st.get("step", -1)), "repeats": int(st.get("repeats") or 0),
                   "reviews": int(st.get("reviews") or 0), "expiries": int(st.get("expiries") or 0),
                   "acknowledged": acknowledged, "suppressed": bool(row.suppressed),
                   "closed": bool(st.get("closed")), "last_at": st.get("last_at"),
                   "next_at": next_at, "next_action": next_what},
            steps=preview["steps"], review=preview["review"], repeat=preview["repeat"],
            ack_expiry_minutes=policy.ack_expiry_minutes, fallback=preview["fallback"],
            warnings=preview["warnings"])
        return out
    if route.source == "legacy_levels":
        levels = [lv for lv in (legacy or []) if isinstance(lv, dict)][:MAX_LEVELS]
        st = stored if isinstance(stored, dict) and not eps.is_policy_state(stored) else {}
        current = int(st.get("level") or 0)
        ids = sorted({str(c) for lv in levels for c in (lv.get("notify_channels") or [])})
        names = {str(r.id): r.name for r in (await db.execute(text(
            "SELECT id, name FROM notification_channels WHERE id::text = ANY(CAST(:ids AS text[]))"
        ), {"ids": ids})).fetchall()} if ids else {}
        base = _parse_ts(anchor)
        steps, next_at, next_what = [], None, None
        for i, lv in enumerate(levels, start=1):
            due = base + timedelta(minutes=float(lv.get("after_minutes") or 0)) if base else None
            steps.append({"position": i, "name": f"Level {i}", "at_offset_minutes": lv.get("after_minutes"),
                          "at": due, "repeat_every_minutes": lv.get("repeat_every_minutes"),
                          "recipients": [{"kind": "channel", "address": None, "user_id": None,
                                          "user_name": None, "via": f"channel:{names.get(str(c), c)}",
                                          "channel_id": str(c)} for c in lv.get("notify_channels") or []],
                          "gaps": [], "notified_at": st.get("last_at") if i == current else None,
                          "current": i == current})
            if next_at is None and i > current and due and row.status == "active" and not row.suppressed:
                next_at, next_what = max(due, now), f"level:{i}"
        out.update(state={"step": current, "repeats": int(st.get("repeats") or 0),
                          "acknowledged": acknowledged, "suppressed": bool(row.suppressed),
                          "closed": bool(st.get("closed")), "last_at": st.get("last_at"),
                          "next_at": next_at, "next_action": next_what},
                   steps=steps)
        if acknowledged:
            out["notes"].append("Acknowledged: legacy escalation levels stop at acknowledgement.")
    return out


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
        await __import__("app.services.notification_grouping", fromlist=["release_tick"]).release_tick()
        await asyncio.sleep(EVAL_INTERVAL_S)
