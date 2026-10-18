"""Notification grouping settings and digests (NOTIFY 02).

* ``GET/PUT /alerts/notification-grouping`` — the grouping/budget settings
  (``system_settings['notification_grouping']``, OFF by default) plus the
  "recommended" preset and the group-by choices.
* ``GET /alerts/digests`` — held-notification summaries, newest first, with
  status, reason, release condition and release time.
* ``GET /alerts/digests/{id}`` — one digest with its items and alert links.
* ``POST /alerts/digests/{id}/release`` — send an open (or failed) digest now.

Reads need ``alerts.view``, changes ``alerts.manage``. A scoped user sees only
digests holding at least one incident in their scope, and only those items.
Mounted before the Alert Center router so ``/alerts/digests`` is never read as
an alert id. Logic: app.services.notification_grouping.
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import notification_grouping as ng
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/alerts", tags=["Alerts"])

can_view = require_permission("alerts.view")
can_manage = require_permission("alerts.manage")

GroupBy = Literal["appliance", "site", "device_group", "scope_tag", "event_family"]


class GroupingSettings(BaseModel):
    enabled: bool = False
    group_by: list[GroupBy] = Field(default_factory=lambda: ["site", "event_family"], max_length=5)
    group_wait_seconds: int = Field(default=60, ge=0, le=3600)
    group_interval_seconds: int = Field(default=300, ge=30, le=86400)
    max_items: int = Field(default=50, ge=5, le=500)
    budget_per_recipient_per_hour: Optional[int] = Field(default=30, ge=0, le=10000)
    critical_reserve: bool = True


def _settings_payload(settings: dict) -> dict:
    return {**settings, "recommended": dict(ng.RECOMMENDED_SETTINGS),
            "group_by_choices": list(ng.GROUP_BY_CHOICES)}


@router.get("/notification-grouping")
async def get_grouping(db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    return _settings_payload(await ng.load_settings(db))


@router.put("/notification-grouping")
async def put_grouping(body: GroupingSettings, db: AsyncSession = Depends(get_db),
                       user: User = Depends(can_manage)):
    before = await ng.load_settings(db)
    after = await ng.save_settings(db, body.model_dump(), actor_id=getattr(user, "id", None))
    await write_audit_log(db, actor=user, action="alert.notification_grouping.update",
                          resource_type="settings", resource_id=ng.SETTINGS_KEY,
                          metadata={"before": before, "after": after})
    await db.commit()
    return _settings_payload(after)


_LIST_SQL = (
    "SELECT d.id, d.group_key, d.group_label, d.channel_id, d.recipient, d.status, d.reason,"
    " d.release_condition, d.opened_at, d.release_at, d.sent_at, d.item_count, d.error,"
    " c.name AS channel_name, c.type AS channel_type"
    " FROM notification_digests d LEFT JOIN notification_channels c ON c.id = d.channel_id"
)


def _scope_clause(scope) -> str:
    if scope is None:
        return ""
    return (" EXISTS (SELECT 1 FROM notification_digest_items si JOIN alerts ON alerts.id = si.alert_id"
            f" WHERE si.digest_id = d.id AND {scoping.alert_visible('alerts')})")


async def _digest_or_404(db: AsyncSession, digest_id, scope) -> object:
    where = " WHERE d.id = CAST(:id AS uuid)"
    clause = _scope_clause(scope)
    if clause:
        where += " AND" + clause
    row = (await db.execute(text(_LIST_SQL + where),
                            {"id": str(digest_id), scoping.SCOPE_PARAM: scope})).first()
    if not row:
        raise HTTPException(404, "Digest not found")
    return row


@router.get("/digests")
async def list_digests(
    status: Optional[Literal["open", "sending", "sent", "failed", "cancelled"]] = None,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db), user: User = Depends(can_view),
):
    if not await ng.available(db):
        return {"items": []}
    scope = await scoping.visible_tags(db, user)
    conds, params = [], {"lim": limit, scoping.SCOPE_PARAM: scope}
    if status:
        conds.append(" d.status = :st")
        params["st"] = status
    clause = _scope_clause(scope)
    if clause:
        conds.append(clause)
    sql = _LIST_SQL + ((" WHERE" + " AND".join(conds)) if conds else "")
    sql += " ORDER BY (d.status = 'open') DESC, d.opened_at DESC LIMIT :lim"
    rows = (await db.execute(text(sql), params)).all()
    return {"items": [ng.digest_payload(r) for r in rows]}


@router.get("/digests/{digest_id}")
async def get_digest(digest_id: UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(can_view)):
    if not await ng.available(db):
        raise HTTPException(404, "Digest not found")
    scope = await scoping.visible_tags(db, user)
    row = await _digest_or_404(db, digest_id, scope)
    vis = f" AND {scoping.alert_visible('alerts')}" if scope is not None else ""
    items = (await db.execute(text(
        "SELECT i.id, i.alert_id, i.event, i.severity, i.summary, i.added_at,"
        " alerts.status AS alert_status, alerts.resolved_at"
        " FROM notification_digest_items i LEFT JOIN alerts ON alerts.id = i.alert_id"
        f" WHERE i.digest_id = CAST(:id AS uuid){vis} ORDER BY i.added_at, i.id"),
        {"id": str(digest_id), scoping.SCOPE_PARAM: scope})).all()
    payload = ng.digest_payload(row)
    payload["items"] = [{
        "id": str(i.id), "alert_id": str(i.alert_id) if i.alert_id else None, "event": i.event,
        "severity": i.severity, "added_at": i.added_at, "alert_status": i.alert_status,
        "resolved_at": i.resolved_at,
        "already_recovered": i.alert_id is None or i.alert_status in (None, "resolved"),
        "link": f"/alerts/{i.alert_id}" if i.alert_id else None,
        **{k: (i.summary or {}).get(k) for k in ("incident_ref", "asset", "condition", "value",
                                                  "first_seen", "priority", "rule_name")},
    } for i in items]
    payload["hidden_items"] = max(0, int(row.item_count or 0) - len(items)) if scope is not None else 0
    return payload


@router.post("/digests/{digest_id}/release")
async def release_digest(digest_id: UUID, db: AsyncSession = Depends(get_db),
                         user: User = Depends(can_manage)):
    if not await ng.available(db):
        raise HTTPException(404, "Digest not found")
    scope = await scoping.visible_tags(db, user)
    await _digest_or_404(db, digest_id, scope)
    try:
        outcome = await ng.release_now(db, str(digest_id))
    except LookupError:
        raise HTTPException(404, "Digest not found")
    except ng.ReleaseConflict as exc:
        raise HTTPException(409, f"Cannot release: {exc}")
    await write_audit_log(db, actor=user, action="alert.digest.release", resource_type="notification_digest",
                          resource_id=str(digest_id), metadata={"outcome": outcome})
    await db.commit()
    payload = ng.digest_payload(await _digest_or_404(db, digest_id, None))
    payload["outcome"] = outcome
    return payload
