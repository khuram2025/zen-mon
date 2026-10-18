"""Alert Center API.

Contract: docs/alerts-api-contract.md. Authorization is two independent
checks on every endpoint (Alerts review A02–A05, A18):

* the permission vocabulary — ``alerts.view`` to read, ``alerts.acknowledge``
  to triage (acknowledge, resolve, snooze, assign, comment), ``alerts.manage``
  for indefinite suppression and policy;
* the user's tag scope — every per-alert action, silence and related-alert
  query is bound to what the user can see. Absent and out-of-scope objects
  both answer 404, so ids cannot be probed.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.permissions import has_permission
from app.core.security import get_role_permissions, require_permission
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertStats
from app.services import alert_priority as prio
from app.services import alert_rule_scope as rs
from app.services import alert_service as svc
from app.services import alert_workflow as wf
from app.services import netflow_alert_service as nfa
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/alerts", tags=["Alerts"])

can_view = require_permission("alerts.view")
can_triage = require_permission("alerts.acknowledge")
can_manage = require_permission("alerts.manage")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _uuid_list(values) -> list[str]:
    return [str(v) for v in values if v]


def _name(username, full_name) -> str | None:
    return (full_name or username) if (full_name or username) else None


def _person(uid, username, full_name) -> dict | None:
    if not uid:
        return None
    return {"id": str(uid), "name": _name(username, full_name) or "Unknown user"}


def _entity(row) -> dict:
    meta = row.metadata or {}
    if row.service_check_id:
        return {"kind": "service_check", "id": str(row.service_check_id),
                "name": row.service_check_name or "Service check",
                "subtitle": row.device_hostname or "service check",
                "href": f"/services/{row.service_check_id}"}
    if row.device_id:
        return {"kind": "device", "id": str(row.device_id),
                "name": row.device_hostname or row.device_ip or "Device",
                "subtitle": row.device_ip, "href": f"/devices/{row.device_id}"}
    if row.server_id:
        return {"kind": "server", "id": str(row.server_id),
                "name": row.server_display or row.server_hostname or "Server",
                "subtitle": row.server_ip or "server", "href": f"/servers/{row.server_id}"}
    if row.sensor_id:
        return {"kind": "sensor", "id": str(row.sensor_id),
                "name": row.sensor_name or "Sensor", "subtitle": "sensor",
                "href": f"/sensors/{row.sensor_id}"}
    source = str(meta.get("source") or "")
    if source.startswith("apm") or (getattr(row, "rule_metric", None) or "").startswith("apm_"):
        if source == "apm_synthetic":
            return {"kind": "apm", "id": meta.get("monitor_id"),
                    "name": meta.get("monitor_name") or "Synthetic monitor",
                    "subtitle": "synthetic monitor", "href": "/apm/synthetics"}
        if source == "apm_slo_burn":
            sid = meta.get("slo_id")
            return {"kind": "apm", "id": sid, "name": meta.get("slo_name") or "SLO",
                    "subtitle": f"SLO · {meta.get('service') or 'service'}",
                    "href": f"/apm/slos/{sid}" if sid else "/apm/slos"}
        if source == "apm_rum_metric":
            return {"kind": "apm", "id": meta.get("application_id"),
                    "name": meta.get("application_id") or "Browser application",
                    "subtitle": "real user monitoring", "href": "/apm/rum"}
        svc_name = meta.get("service")
        return {"kind": "apm", "id": svc_name, "name": svc_name or "APM service",
                "subtitle": "APM service",
                "href": f"/apm/services/{quote(str(svc_name), safe='')}" if svc_name else "/apm/services"}
    return {"kind": "system", "id": None, "name": "ZenPlus appliance",
            "subtitle": source.replace("_", " ") or "system", "href": None}


def _closure(row) -> dict | None:
    reason = wf.closure_reason(row.status, row.metadata or {})
    if not reason:
        return None
    meta = row.metadata or {}
    by = None
    if reason == "manual" and meta.get("resolved_by_user"):
        by = {"id": meta.get("resolved_by_user"), "name": meta.get("resolved_by_name") or "Operator"}
    elif reason == "suppressed" and meta.get("snoozed_by"):
        by = {"id": meta.get("snoozed_by"), "name": "Operator"}
    return {"reason": reason, "label": wf.CLOSURE_LABELS.get(reason, reason),
            "by": by, "note": meta.get("resolution_note")}


async def _netflow_redacted(db: AsyncSession, user: User | None) -> bool:
    """Whether NetFlow alert details must be withheld from this caller (it
    lacks netflow.view; X-01). No caller (internal use) sees everything."""
    return await nfa.netflow_details_withheld(db, user)


async def _enrich(db: AsyncSession, rows, user: User | None = None) -> dict:
    """Batch lookups for a page of rows: channels, silences, deliveries."""
    ids = [str(r.id) for r in rows]
    channel_ids = sorted({str(c) for r in rows for c in (r.notify_channels or [])
                          if isinstance(r.notify_channels, list)})
    channels: dict[str, dict] = {}
    if channel_ids:
        for c in (await db.execute(text(
            "SELECT id::text AS id, name, type, enabled FROM notification_channels"
            " WHERE id::text = ANY(CAST(:ids AS text[]))"
        ), {"ids": channel_ids})).fetchall():
            channels[c.id] = {"id": c.id, "name": c.name, "type": c.type, "enabled": c.enabled}
    silences: dict[str, dict] = {}
    deliveries: dict[str, dict] = {}
    if ids:
        for s in (await db.execute(text(
            "SELECT a.id::text AS alert_id, s.id, s.until, s.reason, s.created_at, s.created_by,"
            " u.username, u.full_name"
            " FROM alerts a JOIN alert_silences s ON (s.until IS NULL OR s.until > NOW())"
            f"  AND {wf.silence_match_sql('a', 's')}"
            " LEFT JOIN users u ON u.id = s.created_by"
            " WHERE a.id = ANY(CAST(:ids AS uuid[]))"
        ), {"ids": ids})).fetchall():
            silences[s.alert_id] = {
                "silence_id": str(s.id), "until": s.until, "forever": s.until is None,
                "reason": s.reason, "created_at": s.created_at,
                "by": _person(s.created_by, s.username, s.full_name),
            }
        if await wf.supported(db):
            try:
                for d in (await db.execute(text(
                    "SELECT alert_id::text AS alert_id, count(*) AS attempts,"
                    " count(*) FILTER (WHERE outcome = 'accepted') AS accepted,"
                    " count(*) FILTER (WHERE outcome = 'failed') AS failed,"
                    " max(attempted_at) AS last_at"
                    " FROM alert_deliveries WHERE alert_id = ANY(CAST(:ids AS uuid[]))"
                    " GROUP BY alert_id"
                ), {"ids": ids})).fetchall():
                    deliveries[d.alert_id] = {"attempts": d.attempts, "accepted": d.accepted,
                                              "failed": d.failed, "last_at": d.last_at}
            except Exception:
                await db.rollback()
    return {"channels": channels, "silences": silences, "deliveries": deliveries,
            "targets": await wf.response_targets(db),
            "redact_netflow": await _netflow_redacted(db, user)}


def serialize(row, ctx: dict) -> dict:
    meta = row.metadata or {}
    rid = str(row.rule_id) if row.rule_id else None
    sil = ctx["silences"].get(str(row.id))
    suppressed = bool(sil) and row.status in ("active", "acknowledged")
    priority = prio.effective_priority(getattr(row, "alert_priority", None),
                                       getattr(row, "rule_priority", None), row.severity)
    due, overdue = wf.ack_due(row.status, row.severity, wf.ack_anchor(row.triggered_at, meta),
                              ctx["targets"], suppressed, priority=priority)
    chans = [ctx["channels"][str(c)] for c in (row.notify_channels or [])
             if isinstance(row.notify_channels, list) and str(c) in ctx["channels"]]
    assigned = None
    if getattr(row, "assigned_to", None):
        assigned = _person(row.assigned_to, row.assignee_username, row.assignee_full_name)
    out = {
        "id": str(row.id),
        "status": row.status,
        "severity": row.severity,
        # P1..P4: stamped when raised; derived from severity for older rows.
        "priority": priority,
        "priority_label": prio.priority_label(priority),
        "message": row.message,
        "triggered_at": row.triggered_at,
        "acknowledged_at": row.acknowledged_at,
        "resolved_at": row.resolved_at,
        "rule_id": rid,
        "rule_name": row.rule_name,
        "device_id": str(row.device_id) if row.device_id else None,
        "server_id": str(row.server_id) if row.server_id else None,
        "service_check_id": str(row.service_check_id) if row.service_check_id else None,
        "sensor_id": str(row.sensor_id) if row.sensor_id else None,
        "device_hostname": row.device_hostname,
        "device_ip": row.device_ip,
        "server_hostname": row.server_display or row.server_hostname,
        "service_check_name": row.service_check_name,
        "sensor_name": row.sensor_name,
        "entity": _entity(row),
        "acknowledged_by": _person(row.acknowledged_by, row.ack_username, row.ack_full_name),
        "assigned_to": assigned,
        "assigned_at": getattr(row, "assigned_at", None),
        "closure": _closure(row),
        "is_notice": wf.notice_kind(meta) is not None,
        "notice_kind": wf.notice_kind(meta),
        # A pending row is parked until its hold expires; flap_held says the
        # hold is there because the device is flapping ("Held: device
        # flapping"), hold_until when the sweeper decides.
        "flap_held": row.status == "pending" and str(meta.get("flap_held", "")).lower() == "true",
        "flap_kind": meta.get("flap_kind") if row.status == "pending" else None,
        "hold_until": meta.get("hold_until") if row.status == "pending" else None,
        "suppression": sil if suppressed else None,
        "snoozed": suppressed,
        "snoozed_until": sil["until"] if suppressed else None,
        "ack_due_at": due,
        "ack_overdue": overdue,
        "channels": chans,
        "delivery": ctx["deliveries"].get(str(row.id)),
        "metadata": meta,
    }
    if ctx.get("redact_netflow"):
        return nfa.redact_alert_payload(out)
    return out


def _parse_severity(value: str | None) -> list[str]:
    if not value:
        return []
    out = [v.strip().lower() for v in value.split(",") if v.strip()]
    bad = [v for v in out if v not in ("critical", "warning", "info")]
    if bad:
        raise HTTPException(422, f"Unknown severity: {', '.join(bad)}")
    return out


async def alert_filters(
    view: Optional[str] = Query(default=None),
    status: Optional[str] = None,
    severity: Optional[str] = None,
    device_id: Optional[UUID] = None,
    server_id: Optional[UUID] = None,
    service_check_id: Optional[UUID] = None,
    sensor_id: Optional[UUID] = None,
    rule_id: Optional[UUID] = None,
    entity_kind: Optional[str] = None,
    assignee: Optional[str] = None,
    population: Optional[str] = None,
    hours: Optional[int] = Query(default=None, ge=1, le=8760),
    from_time: Optional[datetime] = Query(default=None, alias="from"),
    to_time: Optional[datetime] = Query(default=None, alias="to"),
    basis: str = "triggered",
    age: Optional[str] = None,
    rule_state: Optional[str] = None,
    search: Optional[str] = Query(default=None, max_length=200),
    delivery: Optional[str] = None,
    as_of: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
) -> svc.AlertFilters:
    if view is not None and view not in svc.VIEWS:
        raise HTTPException(422, f"view must be one of {', '.join(svc.VIEWS)}")
    if status is not None and status not in ("pending", "active", "acknowledged", "resolved"):
        raise HTTPException(422, "Unknown status")
    if entity_kind is not None and entity_kind not in svc.ENTITY_KINDS:
        raise HTTPException(422, "Unknown entity_kind")
    if population is not None and population not in ("incidents", "notices", "all"):
        raise HTTPException(422, "population must be incidents, notices or all")
    if basis not in svc.BASES:
        raise HTTPException(422, "basis must be triggered, resolved or acknowledged")
    if age is not None and age not in svc.AGES:
        raise HTTPException(422, "age must be gt1h, gt24h or gt7d")
    if rule_state is not None and rule_state not in ("disabled", "missing"):
        raise HTTPException(422, "rule_state must be disabled or missing")
    if assignee is not None and assignee not in ("me", "unassigned"):
        try:
            UUID(assignee)
        except ValueError:
            raise HTTPException(422, "assignee must be me, unassigned or a user id")
    if delivery is not None and delivery not in ("failed", "none"):
        raise HTTPException(422, "delivery must be failed or none")
    if (assignee is not None or delivery is not None) and not await wf.supported(db):
        raise HTTPException(503, "Assignment and delivery filters need database migration 135.")
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "'as_of' must include a timezone offset")
    for ts_name, ts in (("from", from_time), ("to", to_time)):
        if ts is not None and ts.tzinfo is None:
            raise HTTPException(422, f"'{ts_name}' must include a timezone offset")
    return svc.AlertFilters(
        view=view, status=status, severity=_parse_severity(severity),
        device_id=device_id, server_id=server_id, service_check_id=service_check_id,
        sensor_id=sensor_id, rule_id=rule_id, entity_kind=entity_kind, assignee=assignee,
        population=population, hours=hours, from_time=from_time, to_time=to_time,
        basis=basis, age=age, rule_state=rule_state, search=search,
        delivery=delivery, as_of=as_of,
        visible_tags=await scoping.visible_tags(db, user), user_id=user.id,
        hide_netflow_text=await _netflow_redacted(db, user),
    )


async def _in_scope_or_404(db: AsyncSession, user: User, alert_id) -> None:
    exists = (await db.execute(text("SELECT 1 FROM alerts WHERE id = :id"),
                               {"id": alert_id})).first()
    if not exists or not await scoping.alert_in_scope(db, alert_id, await scoping.visible_tags(db, user)):
        raise HTTPException(404, "Alert not found")


async def _row_payload(db: AsyncSession, user: User, alert_id) -> dict:
    f = svc.AlertFilters(ids=[str(alert_id)], visible_tags=await scoping.visible_tags(db, user))
    rows, _ = await svc.list_rows(db, f, limit=1, count=False)
    if not rows:
        raise HTTPException(404, "Alert not found")
    return serialize(rows[0], await _enrich(db, rows, user))


async def _perms(db: AsyncSession, user: User) -> list[str]:
    return list(await get_role_permissions(db, user.role))


def _workflow_error(exc: wf.WorkflowError):
    raise HTTPException(exc.status_code, exc.detail)


# ── Reads ───────────────────────────────────────────────────────────────────

@router.get("", response_model=dict)
async def list_alerts(
    f: svc.AlertFilters = Depends(alert_filters),
    sort: str = "newest",
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    if sort not in svc.SORTS:
        raise HTTPException(422, f"sort must be one of {', '.join(svc.SORTS)}")
    rows, total = await svc.list_rows(db, f, sort=sort, skip=skip, limit=limit)
    ctx = await _enrich(db, rows, user)
    now = (await db.execute(text("SELECT NOW()"))).scalar()
    newer = None
    if f.as_of is not None:
        # How many matching rows arrived after the paging snapshot.
        anchor = f.as_of
        f.as_of = None
        clauses, params = f.where()
        clauses.append("a.triggered_at > :anchor")
        params["anchor"] = anchor
        newer = (await db.execute(text(
            f"SELECT count(*) {svc.base_from(True)}{svc._where(clauses)}"), params)).scalar() or 0
        f.as_of = anchor
    return {
        "data": [serialize(r, ctx) for r in rows],
        "meta": {"total": total, "skip": skip, "limit": limit, "sort": sort,
                 "view": f.view, "server_time": now, "as_of": f.as_of, "newer": newer},
    }


@router.get("/summary")
async def alert_summary(
    f: svc.AlertFilters = Depends(alert_filters),
    window_hours: Optional[int] = Query(default=None, ge=1, le=2160),
    db: AsyncSession = Depends(get_db),
):
    """Open backlog is always "now"; closed-incident metrics cover
    ``window_hours`` (else the list's ``hours``, else 24) or from/to."""
    return await svc.summary(db, f, window_hours=window_hours or f.hours or 24,
                             targets=await wf.response_targets(db))


@router.get("/trend")
async def alert_trend(
    f: svc.AlertFilters = Depends(alert_filters),
    bucket: Optional[Literal["hour", "day"]] = None,
    db: AsyncSession = Depends(get_db),
):
    """``hours`` is the trend window here (default 168), not a row filter."""
    trend_hours = min(f.hours or 168, 2160)
    f.hours = None
    from app.services.alert_schedule import get_configured_timezone
    tz = await get_configured_timezone(db)
    tz_name = getattr(tz, "key", None) or str(tz or "UTC")
    try:
        return await svc.trend(db, f, hours=trend_hours, bucket=bucket, tz=tz_name)
    except Exception:
        await db.rollback()
        return await svc.trend(db, f, hours=trend_hours, bucket=bucket, tz="UTC")


@router.get("/stats", response_model=AlertStats)
async def alert_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    return await svc.get_alert_stats(db, visible_tags=await scoping.visible_tags(db, user))


@router.get("/device-counts")
async def alert_device_counts(
    hours: int = Query(default=24, ge=1, le=2160),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    end = to_time or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = from_time or (end - timedelta(hours=hours))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    # "Active" is a *current* state, not a window one: an alert that opened
    # before the selected range and is still firing must be counted, and a
    # resolved alert must not inflate the badge. Window-scoped history counts
    # (total / per-severity) are kept for context.
    from sqlalchemy import and_, or_

    in_window = and_(Alert.triggered_at >= start, Alert.triggered_at <= end)
    scope = await scoping.visible_tags(db, user)
    # Notices (recovery notices, cancelled holds, flapping start/stop) are
    # records, not alerts: a flapping device must not show a badge of dozens.
    # They are always resolved, so the "active" counts are unaffected.
    query = (
        select(
            Alert.device_id,
            func.sum(case((in_window, 1), else_=0)).label("total"),
            func.sum(case((Alert.status == "active", 1), else_=0)).label("active"),
            func.sum(case((and_(Alert.status == "active", Alert.severity == "critical"), 1), else_=0)).label("active_critical"),
            func.sum(case((and_(in_window, Alert.severity == "critical"), 1), else_=0)).label("critical"),
            func.sum(case((and_(in_window, Alert.severity == "warning"), 1), else_=0)).label("warning"),
            func.sum(case((and_(in_window, Alert.severity == "info"), 1), else_=0)).label("info"),
            func.max(Alert.triggered_at).label("last_triggered_at"),
        )
        .where(Alert.device_id.is_not(None))
        .where(or_(in_window, Alert.status == "active"))
        .where(text(f"NOT {wf.notice_sql('alerts')}"))
        .group_by(Alert.device_id)
    )
    if scope is not None:
        query = query.where(
            text("EXISTS (SELECT 1 FROM devices _vd WHERE _vd.id = alerts.device_id AND "
                 + scoping.jsonb_tags_visible("_vd.tags") + ")")
            .bindparams(**{scoping.SCOPE_PARAM: scope})
        )
    result = await db.execute(query)

    devices = {}
    for row in result.all():
        devices[str(row.device_id)] = {
            "total": int(row.total or 0),
            "active": int(row.active or 0),
            "active_critical": int(row.active_critical or 0),
            "critical": int(row.critical or 0),
            "warning": int(row.warning or 0),
            "info": int(row.info or 0),
            "last_triggered_at": row.last_triggered_at.isoformat() if row.last_triggered_at else None,
        }
    return {"from": start.isoformat(), "to": end.isoformat(), "devices": devices}


@router.get("/export")
async def export_alerts(
    f: svc.AlertFilters = Depends(alert_filters),
    sort: str = "newest",
    format: Literal["csv"] = "csv",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    """The complete matching population as CSV (capped at 50 000 rows)."""
    if sort not in svc.SORTS:
        raise HTTPException(422, "Unknown sort")
    rows, total = await svc.list_rows(db, f, sort=sort, skip=0, limit=svc.EXPORT_CAP)
    ctx = await _enrich(db, rows, user)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "severity", "status", "closure", "suppressed", "entity_kind", "entity",
                "entity_detail", "rule", "message", "triggered_at_utc", "acknowledged_at_utc",
                "acknowledged_by", "resolved_at_utc", "assigned_to", "open_or_closed_seconds",
                "notice"])

    def iso(ts):
        return ts.astimezone(timezone.utc).isoformat() if ts else ""

    now = datetime.now(timezone.utc)
    for r in rows:
        a = serialize(r, ctx)
        end = r.resolved_at or now
        dur = int((end - r.triggered_at).total_seconds()) if r.triggered_at else ""
        w.writerow([
            a["id"], a["severity"], a["status"], (a["closure"] or {}).get("label", ""),
            "yes" if a["suppression"] else "", a["entity"]["kind"], a["entity"]["name"],
            a["entity"].get("subtitle") or "", a["rule_name"] or "",
            _csv_safe(a["message"]), iso(r.triggered_at), iso(r.acknowledged_at),
            (a["acknowledged_by"] or {}).get("name", ""), iso(r.resolved_at),
            (a["assigned_to"] or {}).get("name", ""), dur,
            a["notice_kind"] or "",
        ])
    await write_audit_log(db, actor=user, action="alert.export", resource_type="alert",
                          metadata={"rows": len(rows), "total": total, "filters": f.describe(),
                                    "view": f.view})
    await db.commit()
    stamp = now.strftime("%Y%m%d-%H%M")
    headers = {"Content-Disposition": f'attachment; filename="alerts-{stamp}.csv"',
               "X-Total-Count": str(total), "X-Exported-Rows": str(len(rows))}
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


def _csv_safe(value: str | None) -> str:
    """Neutralise spreadsheet formula injection in free text."""
    v = value or ""
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@router.get("/assignees")
async def list_assignees(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    rows = (await db.execute(text(
        "SELECT id, username, full_name FROM users WHERE is_active = true"
        " ORDER BY COALESCE(NULLIF(full_name, ''), username)"
    ))).fetchall()
    return {"items": [{"id": str(r.id), "username": r.username,
                       "name": _name(r.username, r.full_name)} for r in rows]}


@router.get("/channel-directory")
async def channel_directory(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_manage),
):
    """Channel names/types for routing editors. /settings/channels is
    admin-only because it returns configuration; this returns none."""
    rows = (await db.execute(text(
        "SELECT id::text AS id, name, type, enabled FROM notification_channels ORDER BY name"
    ))).fetchall()
    return {"data": [{"id": r.id, "name": r.name, "type": r.type, "enabled": r.enabled}
                     for r in rows]}


class ResponseTargets(BaseModel):
    """Acknowledgement targets in minutes per priority (null = no target).
    Only the keys sent change. The severity keys of earlier releases are
    still accepted (critical = P2, warning = P3, info = P4)."""
    P1: Optional[int] = Field(default=None, ge=1, le=10080)
    P2: Optional[int] = Field(default=None, ge=1, le=10080)
    P3: Optional[int] = Field(default=None, ge=1, le=10080)
    P4: Optional[int] = Field(default=None, ge=1, le=10080)
    critical: Optional[int] = Field(default=None, ge=1, le=10080)
    warning: Optional[int] = Field(default=None, ge=1, le=10080)
    info: Optional[int] = Field(default=None, ge=1, le=10080)


@router.get("/response-targets")
async def get_response_targets(db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    """{"P1".."P4": minutes|null} plus the legacy severity mirrors."""
    return wf.legacy_target_view(await wf.response_targets(db))


@router.put("/response-targets")
async def put_response_targets(body: ResponseTargets, db: AsyncSession = Depends(get_db),
                               user: User = Depends(can_manage)):
    before = await wf.response_targets(db)
    given = body.model_dump(exclude_unset=True)
    if any(p in given for p in prio.PRIORITIES):
        # P-keys are authoritative when both forms are sent.
        given = {k: v for k, v in given.items() if k in prio.PRIORITIES}
    after = await wf.save_response_targets(db, given)
    await write_audit_log(db, actor=user, action="alert.response_targets.update",
                          resource_type="settings", resource_id=wf.RESPONSE_TARGETS_KEY,
                          metadata={"before": before, "after": after})
    await db.commit()
    return wf.legacy_target_view(after)


# ── Silences (suppression workspace) ─────────────────────────────────────────

@router.get("/silences")
async def list_silences(
    server_id: UUID | None = None,
    device_id: UUID | None = None,
    include_expired: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    """Silences the user can see. With an entity id, that entity's silences
    (404 when the entity is out of scope); without, every visible silence."""
    scope = await scoping.visible_tags(db, user)
    params: dict = {}
    where = [] if include_expired else ["(s.until IS NULL OR s.until > NOW())"]
    if server_id is not None:
        where.append("s.server_id = :sid")
        params["sid"] = server_id
    if device_id is not None:
        where.append("s.device_id = :did")
        params["did"] = device_id
    if scope is not None:
        where.append(
            "((s.device_id IS NOT NULL AND EXISTS (SELECT 1 FROM devices _vd WHERE _vd.id = s.device_id AND "
            + scoping.jsonb_tags_visible("_vd.tags") + "))"
            " OR (s.server_id IS NOT NULL AND EXISTS (SELECT 1 FROM servers _vs WHERE _vs.id = s.server_id AND "
            + scoping.jsonb_tags_visible("_vs.tags") + ")))")
        params[scoping.SCOPE_PARAM] = scope
    workflow = await wf.supported(db)
    extra = ", s.alert_id, s.condition_label" if workflow else ", NULL AS alert_id, NULL AS condition_label"
    rows = (await db.execute(text(
        f"SELECT s.id, s.server_id, s.device_id, s.dedupe, s.until, s.reason, s.created_at,"
        f" s.created_by, u.username, u.full_name, d.hostname AS device_name,"
        f" COALESCE(sv.display_name, sv.hostname) AS server_name, r.name AS rule_name{extra},"
        f" (SELECT count(*) FROM alerts a WHERE a.status IN ('active','acknowledged')"
        f"   AND {wf.silence_match_sql('a', 's')}) AS open_alerts"
        f" FROM alert_silences s"
        f" LEFT JOIN users u ON u.id = s.created_by"
        f" LEFT JOIN devices d ON d.id = s.device_id"
        f" LEFT JOIN servers sv ON sv.id = s.server_id"
        f" LEFT JOIN alert_rules r ON s.dedupe ~ '^rule.[0-9a-f-]{{36}}'"
        f"   AND r.id = CAST(substring(s.dedupe from 6 for 36) AS uuid)"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.created_at DESC LIMIT 500"
    ), params)).fetchall()
    if (server_id or device_id) and not rows and scope is not None:
        # Do not reveal whether an out-of-scope entity has silences.
        pass
    items = []
    for r in rows:
        kind = "server" if r.server_id else "device"
        items.append({
            "id": str(r.id),
            "entity": {"kind": kind, "id": str(r.server_id or r.device_id),
                       "name": (r.server_name if r.server_id else r.device_name) or kind.title(),
                       "href": f"/servers/{r.server_id}" if r.server_id else f"/devices/{r.device_id}"},
            "dedupe": r.dedupe,
            "condition_label": r.condition_label or (f"{r.rule_name}" if r.rule_name else r.dedupe),
            "rule_name": r.rule_name,
            "until": r.until, "forever": r.until is None,
            "expired": bool(r.until and r.until <= datetime.now(timezone.utc)),
            "reason": r.reason,
            "created_by": _person(r.created_by, r.username, r.full_name),
            "created_at": r.created_at,
            "alert_id": str(r.alert_id) if r.alert_id else None,
            "open_alerts": int(r.open_alerts or 0),
        })
    return {"items": items}


async def _silence_in_scope(db: AsyncSession, user: User, silence_id) -> object:
    row = (await db.execute(text(
        "SELECT id, server_id, device_id, until FROM alert_silences WHERE id = :id"
    ), {"id": silence_id})).first()
    if not row:
        raise HTTPException(404, "Silence not found")
    scope = await scoping.visible_tags(db, user)
    if scope is not None:
        table, eid = ("servers", row.server_id) if row.server_id else ("devices", row.device_id)
        tags = (await db.execute(text(f"SELECT tags FROM {table} WHERE id = :id"),
                                 {"id": eid})).scalar()
        if not scoping.entity_visible(tags, scope):
            raise HTTPException(404, "Silence not found")
    return row


@router.delete("/silences/{silence_id}")
async def delete_silence(
    silence_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_triage),
):
    row = await _silence_in_scope(db, user, silence_id)
    if row.until is None and not has_permission(await _perms(db, user), "alerts.manage"):
        raise HTTPException(403, "Removing an indefinite suppression needs alerts.manage.")
    removed = await wf.remove_silence(db, silence_id, user)
    await db.commit()
    return {"removed": removed}


# ── Detail ──────────────────────────────────────────────────────────────────

_EVENT_LABELS = {
    "triggered": "Triggered", "held": "Held for confirmation", "confirmed": "Hold confirmed",
    "acknowledged": "Acknowledged", "assigned": "Assigned", "unassigned": "Unassigned",
    "comment": "Comment", "snoozed": "Snoozed (notifications suppressed)",
    "unsnoozed": "Snooze removed", "suppression_expired": "Snooze expired — still active",
    "escalated": "Escalated", "resolved": "Closed by operator",
    "review": "Unresolved update sent", "ack_expired": "Acknowledgement expired — escalation re-opened",
    "coverage_gap": "Coverage gap — fallback route used", "route_failed": "Delivery failed on this route",
    "recovered": "Recovered automatically", "superseded": "Superseded by a newer status",
    "hold_cancelled": "Hold cancelled", "dependency": "Suppressed by upstream dependency",
    "closed": "Closed",
}


def _ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _events(db: AsyncSession, alert: dict, row) -> list[dict]:
    meta = row.metadata or {}
    recorded = []
    if await wf.supported(db):
        recorded = (await db.execute(text(
            "SELECT e.at, e.kind, e.actor_id, e.actor_name, e.source, e.reason, e.old_status,"
            " e.new_status, e.detail FROM alert_events e WHERE e.alert_id = :id ORDER BY e.at, e.id"
        ), {"id": row.id})).fetchall()
    kinds = {e.kind for e in recorded}
    out: list[dict] = []

    def add(at, kind, *, actor=None, source="evaluator", reason=None, old=None, new=None, detail=None):
        if at is None:
            return
        out.append({"at": at, "kind": kind, "label": _EVENT_LABELS.get(kind, kind.title()),
                    "actor": actor, "source": source, "reason": reason,
                    "old_status": old, "new_status": new, "detail": detail or {}})

    if meta.get("hold_until"):
        add(row.triggered_at, "held", new="pending",
            detail={"hold_until": meta.get("hold_until"),
                    **({"flap_held": True, "flap_kind": meta.get("flap_kind")}
                       if meta.get("flap_held") else {})})
        if meta.get("hold_confirmed_at"):
            add(_ts(meta.get("hold_confirmed_at")), "confirmed", old="pending", new="active")
    else:
        add(row.triggered_at, "triggered", new="active" if not alert["is_notice"] else "resolved",
            detail={k: meta.get(k) for k in ("old_status", "new_status") if meta.get(k)})
    if row.acknowledged_at and "acknowledged" not in kinds:
        add(row.acknowledged_at, "acknowledged", actor=alert["acknowledged_by"], source="user",
            old="active", new="acknowledged")
    esc = meta.get("escalation") if isinstance(meta.get("escalation"), dict) else None
    if esc and esc.get("level") and "escalated" not in kinds:
        add(_ts(esc.get("last_at")), "escalated", source="system",
            detail={"level": esc.get("level"), "repeats": esc.get("repeats")})
    sup = meta.get("suppression") if isinstance(meta.get("suppression"), dict) else None
    if sup and "snoozed" not in kinds:
        add(_ts(sup.get("at")), "snoozed", source="user", reason=sup.get("reason"),
            actor={"id": sup.get("by"), "name": sup.get("by_name") or "Operator"} if sup.get("by") else None,
            detail={"until": sup.get("until")})
    closure = alert.get("closure")
    if row.status == "resolved" and closure and "resolved" not in kinds and not alert["is_notice"]:
        kind = {"recovered": "recovered", "superseded": "superseded",
                "hold_cancelled": "hold_cancelled", "dependency": "dependency",
                "manual": "resolved"}.get(closure["reason"], "closed")
        add(row.resolved_at, kind, actor=closure.get("by"),
            source="user" if closure["reason"] in ("manual", "suppressed") else "evaluator",
            reason=closure.get("note"), new="resolved",
            detail={"closure": closure["reason"], "label": closure["label"]})
    for e in recorded:
        actor = {"id": str(e.actor_id), "name": e.actor_name or "User"} if e.actor_id else (
            {"id": None, "name": e.actor_name} if e.actor_name else None)
        add(e.at, e.kind, actor=actor, source=e.source, reason=e.reason,
            old=e.old_status, new=e.new_status, detail=e.detail or {})
    out.sort(key=lambda e: e["at"])
    return out


_EVIDENCE_KEYS = [
    ("metric", "Metric"), ("value", "Measured value"), ("threshold", "Threshold"),
    ("reading", "Reading"), ("rtt_ms", "Round-trip time (ms)"), ("packet_loss", "Packet loss"),
    ("if_name", "Interface"), ("if_index", "Interface index"), ("error", "Error"),
    ("trap_oid", "Trap OID"), ("source_ip", "Source address"), ("status_at_check", "Status at hold check"),
    ("monitor_name", "Synthetic monitor"), ("failed_step", "Failed step"),
    ("slo_name", "SLO"), ("tier", "Burn tier"), ("long_burn", "Long-window burn"),
    ("short_burn", "Short-window burn"), ("service", "Service"), ("source", "Source"),
    ("parent_hostname", "Upstream dependency"), ("parent_status", "Upstream status"),
    ("flap_kind", "Held while flapping"),
]

# A device flapping notice (device_flap_notice_service) describes its episode;
# these keys are generic enough to mean something else on other rows.
_FLAP_NOTICE_EVIDENCE_KEYS = [
    ("kind", "Flapping"), ("changes", "Status changes"),
    ("window_minutes", "Flap window (minutes)"), ("end_reason", "Flapping ended"),
    ("device_status", "Device status"),
]


def _evidence(meta: dict) -> list[dict]:
    out = []
    if meta.get("old_status") or meta.get("new_status"):
        out.append({"label": "Status change",
                    "value": f"{meta.get('old_status') or '?'} → {meta.get('new_status') or '?'}"})
    keys = _EVIDENCE_KEYS + (_FLAP_NOTICE_EVIDENCE_KEYS
                             if wf.notice_kind(meta) == "flapping" else [])
    for key, label in keys:
        v = meta.get(key)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v)[:200]
        out.append({"label": label, "value": str(v)[:300]})
    return out


def _condition_where(row) -> tuple[str, dict] | None:
    meta = row.metadata or {}
    if row.service_check_id and row.rule_id:
        return "a.service_check_id = :e AND a.rule_id = :r", {"e": row.service_check_id, "r": row.rule_id}
    if row.device_id and row.rule_id:
        return ("a.device_id = :e AND a.rule_id = :r AND COALESCE(a.metadata->>'if_index', '') = :ifx",
                {"e": row.device_id, "r": row.rule_id, "ifx": meta.get("if_index") or ""})
    if meta.get("dedupe"):
        for col in ("server_id", "sensor_id"):
            if getattr(row, col):
                return f"a.{col} = :e AND a.metadata->>'dedupe' = :d", {"e": getattr(row, col), "d": meta["dedupe"]}
        return "a.metadata->>'dedupe' = :d", {"d": meta["dedupe"]}
    return None


async def _recurrence(db: AsyncSession, row, scope) -> dict:
    cond = _condition_where(row)
    if not cond:
        return {"same_condition_24h": None, "same_condition_7d": None, "flapping": False, "previous": []}
    where, params = cond
    scope_sql = ""
    if scope is not None:
        scope_sql = f" AND {scoping.alert_visible('a')}"
        params[scoping.SCOPE_PARAM] = scope
    params["id"] = row.id
    base = f"FROM alerts a WHERE {where} AND NOT {wf.notice_sql('a')}{scope_sql}"
    counts = (await db.execute(text(
        f"SELECT count(*) FILTER (WHERE a.triggered_at >= NOW() - interval '24 hours') AS d1,"
        f" count(*) FILTER (WHERE a.triggered_at >= NOW() - interval '7 days') AS d7,"
        f" min(a.triggered_at) AS first_seen {base}"
    ), params)).first()
    prev = (await db.execute(text(
        f"SELECT a.id, a.triggered_at, a.resolved_at, a.status {base} AND a.id <> :id"
        f" ORDER BY a.triggered_at DESC LIMIT 6"
    ), params)).fetchall()
    return {
        "same_condition_24h": counts.d1, "same_condition_7d": counts.d7,
        "first_seen": counts.first_seen, "flapping": (counts.d1 or 0) >= 3,
        "previous": [{"id": str(p.id), "triggered_at": p.triggered_at,
                      "resolved_at": p.resolved_at, "status": p.status} for p in prev],
    }


async def _related(db: AsyncSession, row, scope, redact_netflow: bool = False) -> list[dict]:
    """Related incidents, each with WHY it is related. Every query carries the
    caller's visibility scope (A03) and a typed entity relationship — the old
    'same severity' fallback matched unrelated incidents fleet-wide."""
    groups: list[tuple[str, str, svc.AlertFilters, str]] = []
    base = dict(visible_tags=scope, population="incidents", view="all")
    ent = None
    if row.service_check_id:
        ent = ("service_check_id", row.service_check_id, "Same service check")
    elif row.device_id:
        ent = ("device_id", row.device_id, "Same device")
    elif row.server_id:
        ent = ("server_id", row.server_id, "Same server")
    elif row.sensor_id:
        ent = ("sensor_id", row.sensor_id, "Same sensor")
    out: list[dict] = []
    seen = {str(row.id)}

    async def take(f: svc.AlertFilters, relation: str, label: str, limit: int = 8, extra=None):
        clauses, params = f.where()
        if extra:
            clauses.append(extra[0])
            params.update(extra[1])
        frm = svc.base_from(await wf.supported(db))
        owner = (", a.assigned_to, a.assigned_at, NULL AS assignee_username, NULL AS assignee_full_name"
                 if await wf.supported(db) else "")
        rows = (await db.execute(text(
            "SELECT a.id, a.status, a.severity, a.message, a.triggered_at, a.acknowledged_at,"
            " a.resolved_at, r.id AS rule_id, r.name AS rule_name, r.metric AS rule_metric,"
            " r.notify_channels, a.device_id, a.server_id, a.service_check_id, a.sensor_id,"
            " a.metadata, a.acknowledged_by, NULL AS ack_username, NULL AS ack_full_name,"
            " d.hostname AS device_hostname, host(d.ip_address) AS device_ip,"
            " s.hostname AS server_hostname, s.display_name AS server_display,"
            " host(s.primary_ip) AS server_ip, sc.name AS service_check_name, sen.name AS sensor_name"
            f"{owner} {frm}" + svc._where(clauses) + " ORDER BY a.triggered_at DESC LIMIT :lim"
        ), {**params, "lim": limit + len(seen)})).fetchall()
        ctx = {"channels": {}, "silences": {}, "deliveries": {}, "targets": {},
               "redact_netflow": redact_netflow}
        n = 0
        for r in rows:
            if str(r.id) in seen or n >= limit:
                continue
            seen.add(str(r.id))
            item = serialize(r, ctx)
            item["relation"] = relation
            item["relation_label"] = label
            out.append(item)
            n += 1

    if ent:
        col, val, label = ent
        f = svc.AlertFilters(**base, **{col: val})
        await take(f, "same_entity", label)
    if row.rule_id:
        f = svc.AlertFilters(**base, rule_id=row.rule_id)
        await take(f, "same_rule_window", "Same rule fired within 15 minutes", limit=6, extra=(
            "a.triggered_at BETWEEN :t0 AND :t1",
            {"t0": row.triggered_at - timedelta(minutes=15),
             "t1": row.triggered_at + timedelta(minutes=15)}))
    parent = (row.metadata or {}).get("parent_device_id")
    if parent:
        f = svc.AlertFilters(**base, device_id=parent)
        await take(f, "dependency_parent", "Upstream dependency", limit=4, extra=(
            "a.triggered_at BETWEEN :t0 AND :t1",
            {"t0": row.triggered_at - timedelta(hours=1),
             "t1": row.triggered_at + timedelta(minutes=15)}))
    return out


async def _rule_payload(db: AsyncSession, rule_id, user: User, perms: list[str], scope) -> dict | None:
    if not rule_id:
        return None
    r = (await db.execute(text("SELECT * FROM alert_rules WHERE id = :id"), {"id": rule_id})).mappings().first()
    if not r:
        return None
    rule = dict(r)
    from app.services import alert_phrasing as ap
    conds = rule.get("conditions") or []
    if isinstance(conds, str):
        conds = json.loads(conds)
    if not conds:
        conds = [{"metric": rule["metric"], "operator": rule["operator"],
                  "threshold": float(rule["threshold"]) if rule.get("threshold") is not None else None}]
    try:
        label = ap.conditions_label(rule.get("conditions"), rule.get("condition_logic") or "AND",
                                    metric=rule["metric"], operator=rule["operator"],
                                    threshold=rule.get("threshold"))
    except Exception:
        label = " AND ".join(f"{c.get('metric')} {c.get('operator')} {c.get('threshold')}" for c in conds)
    levels = rule.get("escalation_levels") or []
    ch_ids = sorted({str(c) for lv in levels if isinstance(lv, dict) for c in (lv.get("notify_channels") or [])})
    names = {}
    if ch_ids:
        names = {c.id: c.name for c in (await db.execute(text(
            "SELECT id::text AS id, name FROM notification_channels WHERE id::text = ANY(CAST(:ids AS text[]))"
        ), {"ids": ch_ids})).fetchall()}
    sched = None
    days = [d for d in (rule.get("schedule_days") or []) if str(d).isdigit()]
    restricted_days = bool(days) and len({int(d) for d in days}) < 7
    if (rule.get("schedule_start") and rule.get("schedule_end")) or restricted_days:
        fmt = lambda t: t.strftime("%H:%M") if hasattr(t, "strftime") else t  # noqa: E731
        sched = {"start": fmt(rule.get("schedule_start")), "end": fmt(rule.get("schedule_end")),
                 "days": rule.get("schedule_days") or []}
    in_scope = await rs.rule_in_user_scope(db, rule, scope)
    editable = has_permission(perms, "alerts.manage") and in_scope
    return {
        "id": str(rule["id"]), "name": rule["name"], "enabled": rule["enabled"],
        "severity": rule.get("severity"), "metric": rule["metric"], "operator": rule["operator"],
        "threshold": float(rule["threshold"]) if rule.get("threshold") is not None else None,
        "conditions": conds, "condition_logic": rule.get("condition_logic") or "AND",
        "condition_label": label, "trigger_on": rule.get("trigger_on"),
        "recovery_alert": rule.get("recovery_alert"), "min_duration": rule.get("min_duration") or 0,
        "notify_while_flapping": bool(rule.get("notify_while_flapping")),
        "duration": rule.get("duration") or 0, "cooldown": rule.get("cooldown"),
        "max_repeat": rule.get("max_repeat"), "schedule": sched,
        "escalation_levels": [{**lv, "channel_names": [names.get(str(c), "Deleted channel")
                                                       for c in (lv.get("notify_channels") or [])]}
                              for lv in levels if isinstance(lv, dict)],
        "scope": rs.describe_scope(rule, await rs.scope_names(db, rule)),
        "affected": await rs.affected(db, rule),
        "notify_channels": [str(c) for c in (rule.get("notify_channels") or [])],
        "target": rule.get("target"),
        "editable": editable,
        "in_scope": in_scope,
        "updated_at": rule.get("updated_at"),
    }


@router.get("/{alert_id}")
async def get_alert_detail(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(can_view),
):
    scope = await scoping.visible_tags(db, user)
    f = svc.AlertFilters(ids=[str(alert_id)], visible_tags=scope)
    rows, _ = await svc.list_rows(db, f, limit=1, count=False)
    if not rows:
        # 404, not 403: out-of-scope ids must not be confirmable.
        raise HTTPException(404, "Alert not found")
    row = rows[0]
    ctx = await _enrich(db, rows, user)
    payload = serialize(row, ctx)
    perms = await _perms(db, user)
    payload["server_time"] = (await db.execute(text("SELECT NOW()"))).scalar()
    payload["rule"] = await _rule_payload(db, row.rule_id, user, perms, scope)
    payload["permissions"] = {
        "can_triage": has_permission(perms, "alerts.acknowledge"),
        "can_manage_rules": has_permission(perms, "alerts.manage"),
        "can_suppress_forever": has_permission(perms, "alerts.manage"),
        "can_edit_rule": bool(payload["rule"] and payload["rule"]["editable"]),
    }
    # The (possibly redacted) metadata: evidence must not re-expose what the
    # payload withholds from a caller without netflow.view.
    meta = payload["metadata"] or {}
    payload["policy_snapshot"] = meta.get("policy_snapshot")
    payload["escalation_state"] = meta.get("escalation") if isinstance(meta.get("escalation"), dict) else None
    payload["evidence"] = _evidence(meta)
    payload["recurrence"] = await _recurrence(db, row, scope)
    payload["related"] = await _related(db, row, scope, ctx.get("redact_netflow", False))
    payload["related_alerts"] = [r for r in payload["related"] if r["relation"] == "same_entity"]
    payload["events"] = await _events(db, payload, row)
    payload["deliveries"] = []
    if await wf.supported(db):
        payload["deliveries"] = [
            {"at": d.attempted_at, "channel_id": str(d.channel_id) if d.channel_id else None,
             "channel_name": d.channel_name, "channel_type": d.channel_type, "kind": d.kind,
             "outcome": d.outcome, "error": d.error, "latency_ms": d.latency_ms}
            for d in (await db.execute(text(
                "SELECT attempted_at, channel_id, channel_name, channel_type, kind, outcome,"
                " error, latency_ms FROM alert_deliveries WHERE alert_id = :id"
                " ORDER BY attempted_at DESC LIMIT 100"
            ), {"id": row.id})).fetchall()
        ]
    from app.services import notification_grouping as ng
    await ng.attach_alert_holds(db, payload, row.id)
    return payload


# ── Actions ─────────────────────────────────────────────────────────────────

class NoteBody(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


class ResolveBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class AssignBody(BaseModel):
    user_id: Optional[UUID] = None
    note: Optional[str] = Field(default=None, max_length=500)


class CommentBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class SnoozeBody(BaseModel):
    # None = indefinite (needs alerts.manage + reason). Bounds are enforced in
    # alert_workflow.validate_snooze so single and bulk paths agree.
    minutes: Optional[int] = None
    reason: Optional[str] = Field(default=None, max_length=500)


async def _act(db: AsyncSession, user: User, alert_id, fn) -> dict:
    await _in_scope_or_404(db, user, alert_id)
    try:
        changed = await fn()
    except wf.WorkflowError as exc:
        await db.rollback()
        _workflow_error(exc)
    await db.commit()
    payload = await _row_payload(db, user, alert_id)
    payload["changed"] = bool(changed)
    return payload


@router.post("/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: UUID, body: Optional[NoteBody] = None,
                            db: AsyncSession = Depends(get_db), user: User = Depends(can_triage)):
    return await _act(db, user, alert_id,
                      lambda: wf.acknowledge(db, alert_id, user, note=body.note if body else None))


@router.post("/{alert_id}/resolve")
async def resolve_alert(alert_id: UUID, body: Optional[ResolveBody] = None,
                        db: AsyncSession = Depends(get_db), user: User = Depends(can_triage)):
    return await _act(db, user, alert_id,
                      lambda: wf.resolve(db, alert_id, user, reason=(body.reason or None) if body else None))


@router.post("/{alert_id}/assign")
async def assign_alert(alert_id: UUID, body: AssignBody,
                       db: AsyncSession = Depends(get_db), user: User = Depends(can_triage)):
    return await _act(db, user, alert_id,
                      lambda: wf.assign(db, alert_id, user, body.user_id, note=body.note))


@router.post("/{alert_id}/comments")
async def comment_alert(alert_id: UUID, body: CommentBody,
                        db: AsyncSession = Depends(get_db), user: User = Depends(can_triage)):
    async def run():
        await wf.comment(db, alert_id, user, body.text.strip())
        return True
    return await _act(db, user, alert_id, run)


@router.get("/{alert_id}/route")
async def alert_route(alert_id: UUID, db: AsyncSession = Depends(get_db),
                      user: User = Depends(can_view)):
    """The incident's escalation route now: which policy (and why), its
    position in that policy, the next timed action and who each step
    reaches. Read-only; scope-checked like the detail (404)."""
    from app.services.alert_escalation_service import explain_route

    await _in_scope_or_404(db, user, alert_id)
    perms = await _perms(db, user)
    reveal = has_permission(perms, "alerts.manage") or has_permission(perms, "users.manage")
    out = await explain_route(db, alert_id, reveal=reveal)
    if out is None:
        raise HTTPException(404, "Alert not found")
    return out


@router.get("/{alert_id}/snooze-preview")
async def snooze_preview(alert_id: UUID, db: AsyncSession = Depends(get_db),
                         user: User = Depends(can_view)):
    await _in_scope_or_404(db, user, alert_id)
    key = await wf.condition_key(db, alert_id)
    payload = await _row_payload(db, user, alert_id)
    perms = await _perms(db, user)
    if not key:
        return {"snoozable": False, "reason": "This alert has no condition identity that can be snoozed.",
                "entity": payload["entity"], "can_forever": has_permission(perms, "alerts.manage")}
    open_rows = await wf.open_condition_alerts(db, key)
    existing = (await db.execute(text(
        f"SELECT id, until, reason FROM alert_silences WHERE {key.scope_col} = :sid AND dedupe = :d"
        " AND (until IS NULL OR until > NOW())"
    ), {"sid": key.scope_id, "d": key.dedupe})).first()
    return {
        "snoozable": True,
        "condition_label": await wf.condition_label(db, alert_id),
        "entity": payload["entity"],
        "rule": {"id": payload["rule_id"], "name": payload["rule_name"]} if payload["rule_id"] else None,
        "open_alerts": len(open_rows),
        "channels": payload["channels"],
        "existing": {"id": str(existing.id), "until": existing.until, "forever": existing.until is None,
                     "reason": existing.reason} if existing else None,
        "can_forever": has_permission(perms, "alerts.manage"),
        "min_minutes": wf.MIN_SNOOZE_MINUTES, "max_minutes": wf.MAX_SNOOZE_MINUTES,
    }


@router.post("/{alert_id}/snooze")
async def snooze_alert(alert_id: UUID, body: SnoozeBody,
                       db: AsyncSession = Depends(get_db), user: User = Depends(can_triage)):
    """Suppress notifications for this alert's condition. Does NOT resolve:
    the open incidents stay open and are shown as suppressed."""
    await _in_scope_or_404(db, user, alert_id)
    can_forever = has_permission(await _perms(db, user), "alerts.manage")
    try:
        result = await wf.snooze(db, alert_id, user, minutes=body.minutes,
                                 reason=(body.reason or "").strip() or None, can_forever=can_forever)
    except wf.WorkflowError as exc:
        await db.rollback()
        _workflow_error(exc)
    await db.commit()
    return result


@router.post("/{alert_id}/unsnooze")
async def unsnooze_alert(alert_id: UUID, db: AsyncSession = Depends(get_db),
                         user: User = Depends(can_triage)):
    await _in_scope_or_404(db, user, alert_id)
    key = await wf.condition_key(db, alert_id)
    if key:
        forever = (await db.execute(text(
            f"SELECT 1 FROM alert_silences WHERE {key.scope_col} = :sid AND dedupe = :d AND until IS NULL"
        ), {"sid": key.scope_id, "d": key.dedupe})).first()
        if forever and not has_permission(await _perms(db, user), "alerts.manage"):
            raise HTTPException(403, "Removing an indefinite suppression needs alerts.manage.")
    try:
        removed = await wf.unsnooze(db, alert_id, user)
    except wf.WorkflowError as exc:
        await db.rollback()
        _workflow_error(exc)
    await db.commit()
    return {"removed": removed}


class BulkAcknowledgeBody(BaseModel):
    service_check_id: Optional[UUID] = None
    alert_ids: Optional[list[UUID]] = Field(default=None, max_length=1000)


@router.post("/acknowledge-bulk")
async def acknowledge_alerts_bulk(body: BulkAcknowledgeBody, db: AsyncSession = Depends(get_db),
                                  user: User = Depends(can_triage)):
    """Acknowledge every ACTIVE alert matching service_check_id and/or
    alert_ids (at least one), inside the caller's scope. Used by the service
    detail page; each alert goes through the guarded workflow transition."""
    if body.service_check_id is None and not body.alert_ids:
        raise HTTPException(422, "Give service_check_id or alert_ids")
    f = svc.AlertFilters(status="active",
                         service_check_id=body.service_check_id,
                         ids=[str(i) for i in body.alert_ids] if body.alert_ids else None,
                         visible_tags=await scoping.visible_tags(db, user))
    rows, _ = await svc.list_rows(db, f, limit=1000, count=False)
    done = 0
    for r in rows:
        try:
            async with db.begin_nested():
                if await wf.acknowledge(db, r.id, user, source="bulk"):
                    done += 1
        except wf.WorkflowError:
            continue
    await db.commit()
    return {"acknowledged": done}


class BulkBody(BaseModel):
    ids: list[UUID] = Field(..., min_length=1, max_length=500)
    action: Literal["acknowledge", "resolve", "snooze", "assign"]
    reason: Optional[str] = Field(default=None, max_length=500)
    minutes: Optional[int] = None
    user_id: Optional[UUID] = None


@router.post("/bulk")
async def bulk_action(body: BulkBody, db: AsyncSession = Depends(get_db),
                      user: User = Depends(can_triage)):
    """Apply one action to many alerts; every id is scope- and transition-
    checked on its own and reported individually."""
    scope = await scoping.visible_tags(db, user)
    can_forever = has_permission(await _perms(db, user), "alerts.manage")
    if body.action == "snooze":
        try:
            wf.validate_snooze(body.minutes, body.reason, can_forever=can_forever)
        except wf.WorkflowError as exc:
            _workflow_error(exc)
    results = []
    snoozed_conditions: set[tuple] = set()
    for aid in dict.fromkeys(body.ids):
        res = {"id": str(aid), "ok": False, "changed": False, "error": None}
        try:
            exists = (await db.execute(text("SELECT 1 FROM alerts WHERE id = :id"), {"id": aid})).first()
            if not exists or not await scoping.alert_in_scope(db, aid, scope):
                res["error"] = "Not found"
                results.append(res)
                continue
            async with db.begin_nested():
                if body.action == "acknowledge":
                    res["changed"] = await wf.acknowledge(db, aid, user, note=body.reason, source="bulk")
                elif body.action == "resolve":
                    res["changed"] = await wf.resolve(db, aid, user, reason=body.reason, source="bulk")
                elif body.action == "assign":
                    res["changed"] = await wf.assign(db, aid, user, body.user_id, note=body.reason,
                                                     source="bulk")
                elif body.action == "snooze":
                    key = await wf.condition_key(db, aid)
                    ident = (key.scope_col, key.scope_id, key.dedupe) if key else None
                    if ident and ident in snoozed_conditions:
                        res["ok"] = True
                        res["error"] = "Same condition already snoozed in this request"
                        results.append(res)
                        continue
                    await wf.snooze(db, aid, user, minutes=body.minutes, reason=body.reason,
                                    can_forever=can_forever, source="bulk")
                    if ident:
                        snoozed_conditions.add(ident)
                    res["changed"] = True
            res["ok"] = True
        except wf.WorkflowError as exc:
            res["error"] = exc.detail
        except Exception as exc:  # one bad row must not sink the batch
            res["error"] = f"Failed: {exc.__class__.__name__}"
        results.append(res)
    await write_audit_log(db, actor=user, action=f"alert.bulk.{body.action}", resource_type="alert",
                          metadata={"requested": len(body.ids),
                                    "ok": sum(1 for r in results if r["ok"]),
                                    "changed": sum(1 for r in results if r["changed"]),
                                    "reason": body.reason, "minutes": body.minutes})
    await db.commit()
    return {"results": results, "summary": {
        "ok": sum(1 for r in results if r["ok"]),
        "changed": sum(1 for r in results if r["changed"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }}
