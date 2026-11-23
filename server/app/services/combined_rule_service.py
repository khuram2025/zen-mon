"""Combined (multi-condition) rules on every subject but servers
(docs/multi-condition-rules.md, migrate-211/212).

Rules with a ``condition_tree`` and ``subject_type`` device or service check
store ``metric = 'combined'``, so no single-condition evaluator loads them;
this pass owns them. Every ``EVAL_INTERVAL_S`` it judges each rule's tree for
each subject in scope (``rule_conditions.judge``) on readings from
``combined_readers`` (and ``host_alert_service.server_readings`` for linked
servers), keeps the holds in ``combined_rule_state`` and raises, continues or
resolves one incident per rule + subject:

* the subject in maintenance is not judged (open incidents stay open and
  quiet); a shared rule excluded for a device skips it and resolves its open
  incident silently, as does a subject that left the rule's scope;
* a device behind an unhealthy dependency parent is not judged (open
  incidents are stamped ``suppressed_by_dependency``), as in the device pass;
* a device incident honours the device's snooze (``alert_silences``);
* cooldown delays a re-opened trigger, the schedule delays the send, the
  priority is stamped at raise, escalation follows the rule, and every send
  goes through ``host_alert_service.dispatch_to_channels`` with the alert id
  and delivery event. The pass runs in one worker at a time
  (``alert_worker_lock``) and each raise takes a per rule + subject lock.

Server rules stay with ``host_alert_service`` (same engine and state table).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import alert_notify_state as ns
from app.services import alert_phrasing as ap
from app.services import combined_readers as cr
from app.services import rule_conditions as rc

logger = logging.getLogger("zenplus.combined_rules")

EVAL_INTERVAL_S = 60
SOURCE = "combined_rule"
SUBJECTS = ("device", "service_check")
_ASSET_TYPE = {"device": "Device", "service_check": "Service check"}


@dataclass
class Subject:
    key: str                       # "device:<id>" / "check:<id>"
    kind: str
    name: str                      # hostname or check name
    ip: str = ""
    device_id: Optional[str] = None
    service_check_id: Optional[str] = None
    server_id: Optional[str] = None
    link_device: Optional[str] = None
    link_server: Optional[str] = None

    @property
    def label(self) -> str:
        return ap.host_label(self.name, self.ip)


@dataclass
class _Open:
    id: object
    notified: Optional[str]
    triggered_at: object
    status: str


_RULES_SQL = (
    "SELECT id, name, metric, operator, threshold, severity, min_duration, clear_duration, cooldown, "
    "notify_channels, recovery_alert, condition_tree, condition_logic, conditions, subject_type, "
    "schedule_start, schedule_end, schedule_days, {prio}, device_id, group_id, device_type, location, "
    "scope_tag, profile_id, service_check_id, service_check_group_id, "
    "email_subject, email_body, sms_template, recovery_email_subject, recovery_email_body, "
    "recovery_sms_template "
    "FROM alert_rules WHERE enabled = true AND condition_tree IS NOT NULL "
    "AND subject_type = ANY(CAST(:subjects AS text[])) ORDER BY created_at, id"
)


# ── Subjects in scope ────────────────────────────────────────────────────────

async def _subjects(db: AsyncSession, rule, cache: dict) -> dict[str, Subject]:
    """Every subject the rule covers, maintenance and exclusions not yet applied."""
    if rule.subject_type == "device":
        from app.services.network_alert_service import _device_in_scope
        if "devices" not in cache:
            cache["devices"] = await cr.load_devices(db)
        out = {}
        for did, dev in cache["devices"].items():
            if _device_in_scope(rule, did, dev.as_scope()):
                out[f"device:{did}"] = Subject(f"device:{did}", "device", dev.hostname, dev.ip, device_id=did)
        return out
    if "checks" not in cache:
        cache["checks"] = await cr.load_checks(db)
    out = {}
    tag = (getattr(rule, "scope_tag", None) or "").strip().lower()
    for cid, chk in cache["checks"].items():
        if rule.service_check_id and str(rule.service_check_id) != cid:
            continue
        if rule.service_check_group_id and str(rule.service_check_group_id) != (chk.group_id or ""):
            continue
        if tag and tag not in {str(t).strip().lower() for t in chk.tags}:
            continue
        out[f"check:{cid}"] = Subject(f"check:{cid}", "service_check", chk.name, "", service_check_id=cid,
                                      device_id=chk.device_id, link_device=chk.device_id,
                                      link_server=chk.server_id)
    return out


async def _links(db: AsyncSession, rule, subjects: dict[str, Subject], cache: dict) -> None:
    """Fill each subject's linked server (and a device's linked server)."""
    if "server_links" not in cache:
        cache["server_links"] = await cr.server_device_links(db)      # server -> device
        by_device: dict[str, str] = {}
        for sid, did in sorted(cache["server_links"].items()):
            by_device.setdefault(did, sid)
        cache["device_servers"] = by_device                            # device -> first server
    for subj in subjects.values():
        if subj.kind == "device":
            subj.link_server = cache["device_servers"].get(subj.device_id or "")
        elif not subj.link_server and subj.link_device:
            subj.link_server = cache["device_servers"].get(subj.link_device)


async def _readings(db: AsyncSession, rule, tree: dict, subjects: dict[str, Subject], *, now: float,
                    cache: dict, maintained_devices: set[str]) -> Optional[dict[str, dict]]:
    """{subject key: {condition id: LeafReading}}; None when a source cannot be read."""
    from app.services import host_alert_service as has
    parts = cr.split(tree)
    out: dict[str, dict] = {k: {} for k in subjects}
    try:
        own = parts.get("self", [])
        if own and rule.subject_type == "device":
            devices = {s.device_id: cache["devices"][s.device_id] for s in subjects.values()}
            per = await cr.device_readings(db, devices, own, now=now)
            for k, s in subjects.items():
                out[k].update(per.get(s.device_id, {}))
        elif own:
            checks = {s.service_check_id: cache["checks"][s.service_check_id] for s in subjects.values()}
            per = await cr.check_readings(checks, own)
            for k, s in subjects.items():
                out[k].update(per.get(s.service_check_id, {}))
        if parts.get("device"):
            ids = {s.link_device for s in subjects.values() if s.link_device}
            devices = await cr.load_devices(db, ids)
            per = await cr.device_readings(db, devices, parts["device"], now=now, unknown=maintained_devices)
            linked = cr.relink({k: s.link_device for k, s in subjects.items()}, per, parts["device"],
                               {d: v.hostname for d, v in devices.items()})
            for k in subjects:
                out[k].update(linked.get(k, {}))
        if parts.get("server"):
            ids = sorted({s.link_server for s in subjects.values() if s.link_server})
            per = await has.server_readings(db, parts["server"], ids, now=now,
                                            caches=cache.setdefault("server_caches", {}))
            if per is None:
                return None
            names = await cr.server_names(db, ids)
            linked = cr.relink({k: s.link_server for k, s in subjects.items()}, per, parts["server"], names)
            for k in subjects:
                out[k].update(linked.get(k, {}))
    except Exception:
        logger.exception("combined rule %s readings unavailable; preserving alerts", rule.id)
        return None
    return out


# ── Incidents ────────────────────────────────────────────────────────────────

async def _open_incidents(db: AsyncSession, rule) -> dict[str, _Open]:
    rows = (await db.execute(text(
        "SELECT id, metadata->>'subject_key', metadata->>'notified', triggered_at, status FROM alerts"
        " WHERE rule_id = :rid AND status IN ('active', 'acknowledged') AND metadata->>'source' = :src"
        " ORDER BY triggered_at"), {"rid": str(rule.id), "src": SOURCE})).all()
    return {r[1]: _Open(r[0], r[2], r[3], r[4]) for r in rows if r[1]}


def _dedupe(rule) -> str:
    """The snooze key, as the device pass uses it (alert_silences)."""
    return f"rule:{rule.id}"


async def _silenced(db: AsyncSession, rule, subj: Subject) -> bool:
    if not subj.device_id or subj.kind != "device":
        return False
    return (await db.execute(text(
        "SELECT 1 FROM alert_silences WHERE device_id = :did AND dedupe = :d"
        " AND (until IS NULL OR until > NOW()) LIMIT 1"), {"did": subj.device_id, "d": _dedupe(rule)})).first() is not None


async def _raise(db: AsyncSession, rule, subj: Subject, message: str, meta: dict):
    """Insert the subject's incident unless one is open (under the rule +
    subject lock). Returns the new id or None."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                     {"k": f"combined:{rule.id}:{subj.key}"})
    if (await db.execute(text(
            "SELECT 1 FROM alerts WHERE rule_id = :rid AND status IN ('active', 'acknowledged')"
            " AND metadata->>'source' = :src AND metadata->>'subject_key' = :k LIMIT 1"),
            {"rid": str(rule.id), "src": SOURCE, "k": subj.key})).first():
        return None
    if await _silenced(db, rule, subj):
        return None
    new_id = (await db.execute(text(
        "INSERT INTO alerts (rule_id, device_id, server_id, service_check_id, severity, message, status, metadata)"
        " VALUES (:rid, CAST(:did AS uuid), CAST(:sid AS uuid), CAST(:cid AS uuid), :sev, :msg, 'active',"
        " CAST(:meta AS jsonb)) RETURNING id"),
        {"rid": str(rule.id), "did": subj.device_id, "sid": subj.server_id, "cid": subj.service_check_id,
         "sev": rule.severity or "warning", "msg": message, "meta": json.dumps(meta)})).scalar()
    from app.services import alert_funnel
    await alert_funnel.stamp_priority(db, new_id, getattr(rule, "priority", None), rule.severity or "warning")
    return new_id


async def _stamp(db: AsyncSession, alert_id, meta: dict) -> None:
    await db.execute(text("UPDATE alerts SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb)"
                          " WHERE id = :id"), {"id": alert_id, "m": json.dumps(meta)})


async def _resolve(db: AsyncSession, alert_id, note: Optional[dict] = None) -> int:
    res = await db.execute(text(
        "UPDATE alerts SET status = 'resolved', resolved_at = NOW(),"
        " metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:m AS jsonb)"
        " WHERE id = :id AND status IN ('active', 'acknowledged')"),
        {"id": alert_id, "m": json.dumps(note or {})})
    return res.rowcount or 0


async def _in_cooldown(db: AsyncSession, rule, subj: Subject, now: float, exclude_id=None) -> bool:
    cooldown = int(getattr(rule, "cooldown", 0) or 0)
    if cooldown <= 0:
        return False
    return (await db.execute(text(
        """SELECT 1 FROM alerts
            WHERE rule_id = :rid AND metadata->>'source' = :src AND metadata->>'subject_key' = :k
              AND (CAST(:aid AS uuid) IS NULL OR id <> CAST(:aid AS uuid))
              AND metadata->>'notified' = 'true'
              AND COALESCE(CASE WHEN metadata->>'notified_at' ~ '^[0-9.]+$'
                                THEN (metadata->>'notified_at')::float8 END,
                           extract(epoch FROM triggered_at)) > :since
            LIMIT 1"""),
        {"rid": str(rule.id), "src": SOURCE, "k": subj.key, "aid": str(exclude_id) if exclude_id else None,
         "since": now - cooldown})).first() is not None


def _render(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


async def _notify(db: AsyncSession, rule, subj: Subject, *, detail: str, label: str, is_recovery: bool,
                  alert_id, first_seen=None, duration: str = "", branding=None, now: float) -> int:
    from app.services import alert_funnel, notification_context
    from app.services.host_alert_service import dispatch_to_channels
    sev = rule.severity or "warning"
    at = datetime.fromtimestamp(now, timezone.utc)
    incident = ap.incident_context(alert_id=alert_id, first_seen=first_seen, now=at,
                                   lifecycle="recovered" if is_recovery else "new")
    asset_type = _ASSET_TYPE.get(subj.kind, "")
    v = ap.rule_phrasing(rule, hostname=subj.label, is_recovery=is_recovery, duration=duration,
                         asset_type=asset_type, branding=branding, now=at)
    who = v.get("asset") or ap.asset_label(subj.name, subj.ip, asset_type)
    words = (detail or "").rstrip(".")
    lowered = rc._lower_first(words, 1)
    if is_recovery:
        v["event_sentence"] = f"{who} is back within the rule's limits" + (f": {lowered}." if words else ".")
    else:
        v["event_sentence"] = f"{who}: {lowered}." if words else f"{who}."
    v["matched_conditions"] = words
    v["condition_sentence"] = v["recovery_sentence"] = f"{label.rstrip('.')}." if label else ""
    v["host_suffix"] = f" — {subj.label}"
    v.update({"rule_name": rule.name or "Alert", "hostname": subj.name, "ip_address": subj.ip or "",
              "severity": sev.upper(), "status": "RESOLVED" if is_recovery else "ALERT"})
    v.update(incident)
    v.update(await notification_context.asset_place(db, {"alert_id": alert_id}))

    def tpl(own, default):
        return _render(own or default, v)

    if is_recovery:
        subject = tpl(rule.recovery_email_subject, ap.DEFAULT_RECOVERY_EMAIL_SUBJECT)
        body = tpl(rule.recovery_email_body, ap.DEFAULT_RECOVERY_EMAIL_BODY)
        sms = tpl(rule.recovery_sms_template, ap.DEFAULT_RECOVERY_SMS)
    else:
        subject = tpl(rule.email_subject, ap.DEFAULT_EMAIL_SUBJECT)
        body = tpl(rule.email_body, ap.DEFAULT_EMAIL_BODY)
        sms = tpl(rule.sms_template, ap.DEFAULT_SMS)
    return await dispatch_to_channels(db, rule.notify_channels or [], {
        "subject": subject, "body": body, "message": sms,
        "event_sentence": v.get("event_sentence"),
        "hostname": subj.name, "ip_address": subj.ip or "",
        "status": "RESOLVED" if is_recovery else "ALERT",
        "severity": sev, "resolved": is_recovery, "headline_metric": None,
        "details": [("Alert rule", rule.name), ("Condition", label or None), ("Measured", detail or None),
                    ("Active for", duration if is_recovery else None)],
        "triggered_at": at.isoformat(),
        "rule_id": str(rule.id), "rule_name": rule.name,
        "alert_id": str(alert_id) if alert_id else None,
        "delivery_event": "recovery" if is_recovery else "trigger",
        "is_recovery": is_recovery,
        **alert_funnel.notification_fields(sev, getattr(rule, "priority", None), trigger=not is_recovery),
        **incident,
    })


# ── The pass ─────────────────────────────────────────────────────────────────

async def _resolve_unwatched(db: AsyncSession, rule, opened: dict[str, _Open], watched: set[str],
                             counts: dict) -> None:
    """Close incidents of subjects the rule no longer watches (scope changed,
    excluded, deleted), without a recovery notice."""
    from app.services import alert_workflow as wf
    for key, inc in list(opened.items()):
        if key in watched:
            continue
        note = f"Rule “{rule.name}” no longer watches this {key.split(':')[0]}"
        if await _resolve(db, inc.id, {"resolved_by": "out_of_scope", "resolution_note": note}):
            await wf.record_event(db, inc.id, "resolved", source="rule_scope", reason=note, old_status=inc.status,
                                  new_status="resolved", detail={"rule_id": str(rule.id), "subject": key})
            counts["resolved"] += 1
        opened.pop(key)
    await db.commit()


async def _evaluate_rule(db: AsyncSession, rule, *, now: float, cache: dict, counts: dict, excluded,
                         maintained_devices: set[str], maintained_checks: set[str], branding, tz) -> None:
    from app.services.alert_schedule import notifications_allowed
    from app.api.v1.alert_engine import _find_suppressing_dependency
    from app.services.network_conditions import clear_hold_seconds
    try:
        tree = rc.validate_tree(rc.parse(rule.condition_tree), rule.subject_type)
    except (rc.TreeError, TypeError) as exc:
        logger.error("combined rule %s skipped: %s", rule.id, exc)
        return
    allowed = notifications_allowed(getattr(rule, "schedule_start", None), getattr(rule, "schedule_end", None),
                                    getattr(rule, "schedule_days", None), tz)
    subjects = await _subjects(db, rule, cache)
    if rule.subject_type == "device" and excluded:
        subjects = {k: s for k, s in subjects.items() if not excluded.device(rule.id, s.device_id)}
    opened = await _open_incidents(db, rule)
    await _resolve_unwatched(db, rule, opened, set(subjects), counts)
    # Not judged: a subject in maintenance (its incident stays open and quiet).
    judged = {k: s for k, s in subjects.items()
              if not (s.kind == "device" and (s.device_id in maintained_devices
                                              or cache["devices"][s.device_id].status == "maintenance"))
              and not (s.kind == "service_check" and (s.service_check_id in maintained_checks
                                                      or (s.device_id and s.device_id in maintained_devices)))}
    if not judged:
        return
    await _links(db, rule, judged, cache)
    readings = await _readings(db, rule, tree, judged, now=now, cache=cache, maintained_devices=maintained_devices)
    if readings is None:
        await db.rollback()
        return
    states = await cr.load_states(db, rule.id)
    label = rc.tree_label(tree)
    at = datetime.fromtimestamp(now, timezone.utc)
    for key, subj in judged.items():
        current = opened.get(key)
        if subj.kind == "device":
            deps = cache.setdefault("dependencies", {})
            if subj.device_id not in deps:
                deps[subj.device_id] = await _find_suppressing_dependency(db, subj.device_id)
            parent = deps[subj.device_id]
            if parent:
                if current is not None:
                    await _stamp(db, current.id, {"suppressed_by_dependency": True,
                                                  "parent_device_id": str(parent["parent_device_id"])})
                    await db.commit()
                continue
        per = readings.get(key, {})
        hold = states.get(key) or cr.HoldState()
        result = rc.judge(tree, per, active=current is not None, leaf_since=hold.leaf_since, now=now)
        if current is None and hold.breach_since is None and result.state is not True \
                and result.leaf_since == hold.leaf_since:
            continue
        st = rc.step(result.state, open_incident=current is not None, breach_since=hold.breach_since,
                     clear_since=hold.clear_since, hold_s=int(rule.min_duration or 0),
                     clear_hold_s=clear_hold_seconds(getattr(rule, "clear_duration", None)), now=at)
        new = cr.HoldState(st.breach_since, st.clear_since, result.leaf_since)
        if (new.breach_since, new.clear_since, new.leaf_since) != (hold.breach_since, hold.clear_since,
                                                                   hold.leaf_since):
            await cr.save_state(db, rule.id, key, new, at)
            await db.commit()
        meta = {"combined": True, "data_state": "current", "matched": rc.matched_metadata(tree, result),
                "condition_states": rc.states_metadata(result)}
        detail = rc.describe(tree, result, per)
        if st.action == "raise":
            message = f"{rule.name}: {subj.label} — {detail}" if detail else f"{rule.name}: {subj.label}"
            new_id = await _raise(db, rule, subj, message, {
                "rule_id": str(rule.id), "source": SOURCE, "subject_key": key, "subject_type": subj.kind,
                "dedupe": _dedupe(rule), **ns.sending_metadata(), **meta})
            await db.commit()
            if not new_id:
                continue
            counts["raised"] += 1
            send = allowed and not await _in_cooldown(db, rule, subj, now, exclude_id=new_id)
            if allowed and not send:
                await _stamp(db, new_id, {"cooldown_until": now + int(rule.cooldown or 0)})
            if send:
                await _notify(db, rule, subj, detail=detail, label=label, is_recovery=False, alert_id=new_id,
                              first_seen=at, branding=branding, now=now)
                await _stamp(db, new_id, {"notified_at": now})
            await ns.stamp(db, new_id, send)
            await db.commit()
        elif st.action == "continue":
            await _stamp(db, current.id, meta)
            await db.commit()
            if (current.status == "active" and allowed and await ns.is_pending(db, current.id)
                    and not await _in_cooldown(db, rule, subj, now, exclude_id=current.id)
                    and await ns.claim_pending(db, current.id)):
                await db.commit()
                await _notify(db, rule, subj, detail=detail, label=label, is_recovery=False, alert_id=current.id,
                              first_seen=current.triggered_at, branding=branding, now=now)
                await _stamp(db, current.id, {"notified_at": now})
                await ns.stamp(db, current.id, True)
                await db.commit()
        elif st.action == "resolve":
            n = await _resolve(db, current.id)
            await db.commit()
            counts["resolved"] += n
            if n and ns.flag_means_notified(current.notified) and getattr(rule, "recovery_alert", False):
                await _notify(db, rule, subj, detail=rc.describe(tree, result, per, matched_only=False),
                              label=label, is_recovery=True, alert_id=current.id, first_seen=current.triggered_at,
                              duration=ap.duration_between(current.triggered_at), branding=branding, now=now)
        elif current is not None:
            if result.state is None:
                await _stamp(db, current.id, {"data_state": "no_data", "condition_states": meta["condition_states"]})
            elif st.clear_since is not None:
                await _stamp(db, current.id, {"data_state": "pending_clear",
                                              "condition_states": meta["condition_states"],
                                              "cleared_seconds": round((at - st.clear_since).total_seconds())})
            await db.commit()


async def evaluate_combined_rules(db: AsyncSession, *, now: Optional[float] = None) -> dict[str, int]:
    from app.services import alert_funnel, maintenance_windows
    from app.services import alert_rule_exclusions as rx
    from app.services.alert_schedule import get_configured_timezone
    from app.services.regional_time import notification_branding
    from app.services.service_maintenance import active_check_ids
    prio = await alert_funnel.rule_priority_column(db)
    rules = (await db.execute(text(_RULES_SQL.format(prio=prio)), {"subjects": list(SUBJECTS)})).all()
    counts = {"rules": len(rules), "raised": 0, "resolved": 0}
    if not rules:
        return counts
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    tz = await get_configured_timezone(db)
    branding = await notification_branding(db)
    excluded = await rx.load(db)
    maintained_devices = {str(d) for d in await maintenance_windows.active_device_ids(db)}
    maintained_checks = {str(c) for c in await active_check_ids(db)}
    cache: dict = {}
    for rule in rules:
        try:
            await _evaluate_rule(db, rule, now=now, cache=cache, counts=counts, excluded=excluded,
                                 maintained_devices=maintained_devices, maintained_checks=maintained_checks,
                                 branding=branding, tz=tz)
        except Exception:
            logger.exception("combined rule %s failed; its incidents are left as they are", rule.id)
            await db.rollback()
    if counts["raised"] or counts["resolved"]:
        logger.info("combined rules: %d rules, %d raised, %d resolved", len(rules), counts["raised"],
                    counts["resolved"])
    return counts


async def run_combined_rule_pass(session_factory=None):
    """One pass, in one API worker at a time (None when skipped)."""
    from app.services.alert_worker_lock import COMBINED_RULE_PASS, run_pass
    return await run_pass(COMBINED_RULE_PASS, evaluate_combined_rules, session_factory)


async def combined_rule_evaluator_loop() -> None:
    await asyncio.sleep(25)
    while True:
        try:
            await run_combined_rule_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("combined rule evaluation failed")
        await asyncio.sleep(EVAL_INTERVAL_S)
