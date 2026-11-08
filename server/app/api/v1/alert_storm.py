"""Alert storm (correlation) policy and storm history.

* ``GET/PUT /alerts/storm-policy`` — ``system_settings['alert_storm']`` (OFF by
  default) with the event-type and scope choices and a plain-language summary.
* ``GET /alerts/storms`` — recent storms, newest first.
* ``GET /alerts/storms/{id}`` — one storm with its affected assets.

Reads need ``alerts.view``, changes ``alerts.manage``. A scoped user sees only
storms with at least one incident in their scope, and only those members.
Mounted before the Alert Center router so ``/alerts/storms`` is never read as
an alert id. Logic: app.services.alert_storm.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import alert_storm as storm
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/alerts", tags=["Alerts"])

can_view = require_permission("alerts.view")
can_manage = require_permission("alerts.manage")

Family = Literal["reachability", "reboot", "service", "interface", "latency", "hardware", "network",
                 "process", "trap", "syslog"]
Scope = Literal["all", "site", "device_group", "scope_tag", "appliance"]


class StormPolicy(BaseModel):
    enabled: bool = False
    threshold: int = Field(default=5, ge=2, le=1000)
    window_minutes: int = Field(default=5, ge=1, le=120)
    families: list[Family] = Field(default_factory=lambda: ["reachability", "reboot"], max_length=10)
    scope: Scope = "all"
    update_minutes: int = Field(default=15, ge=5, le=240)
    hold_escalations: bool = True


def _payload(settings: dict) -> dict:
    return {**settings, "summary": storm.policy_sentence(settings),
            "family_choices": [{"value": f, "label": storm.FAMILY_TITLES[f]} for f in storm.FAMILY_CHOICES],
            "scope_choices": list(storm.SCOPE_CHOICES), "defaults": dict(storm.DEFAULT_SETTINGS)}


@router.get("/storm-policy")
async def get_policy(db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    return _payload(await storm.load_settings(db))


@router.put("/storm-policy")
async def put_policy(body: StormPolicy, db: AsyncSession = Depends(get_db), user: User = Depends(can_manage)):
    before = await storm.load_settings(db)
    after = await storm.save_settings(db, body.model_dump(), actor_id=getattr(user, "id", None))
    await write_audit_log(db, actor=user, action="alert.storm_policy.update", resource_type="settings",
                          resource_id=storm.SETTINGS_KEY, metadata={"before": before, "after": after})
    await db.commit()
    return _payload(after)


_LIST_SQL = (
    "SELECT s.*, (SELECT count(*) FROM alert_storm_members m WHERE m.storm_id = s.id{mf}) AS member_count,"
    " (SELECT count(*) FROM alert_storm_members m LEFT JOIN alerts a ON a.id = m.alert_id"
    "  WHERE m.storm_id = s.id AND COALESCE(a.status, 'resolved') = 'resolved'{mf}) AS recovered_count"
    " FROM alert_storms s"
)


def _member_filter(scope) -> str:
    if scope is None:
        return ""
    return (" AND EXISTS (SELECT 1 FROM alerts WHERE alerts.id = m.alert_id AND "
            f"{scoping.alert_visible('alerts')})")


@router.get("/storms")
async def list_storms(status: Literal["active", "ended"] | None = None,
                      limit: int = Query(50, ge=1, le=200),
                      db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    if not await storm.available(db):
        return {"items": []}
    scope = await scoping.visible_tags(db, user)
    mf = _member_filter(scope)
    conds, params = [], {"lim": limit, scoping.SCOPE_PARAM: scope}
    if status:
        conds.append("s.status = :st")
        params["st"] = status
    if scope is not None:
        conds.append(f"EXISTS (SELECT 1 FROM alert_storm_members m WHERE m.storm_id = s.id{mf})")
    sql = _LIST_SQL.format(mf=mf) + ((" WHERE " + " AND ".join(conds)) if conds else "")
    sql += " ORDER BY (s.status = 'active') DESC, s.started_at DESC LIMIT :lim"
    rows = (await db.execute(text(sql), params)).all()
    return {"items": [storm.storm_payload(r) for r in rows]}


@router.get("/storms/{storm_id}")
async def get_storm(storm_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(can_view)):
    if not await storm.available(db):
        raise HTTPException(404, "Storm not found")
    scope = await scoping.visible_tags(db, user)
    mf = _member_filter(scope)
    where = " WHERE s.id = CAST(:id AS uuid)"
    if scope is not None:
        where += f" AND EXISTS (SELECT 1 FROM alert_storm_members m WHERE m.storm_id = s.id{mf})"
    row = (await db.execute(text(_LIST_SQL.format(mf=mf) + where),
                            {"id": str(storm_id), scoping.SCOPE_PARAM: scope})).first()
    if not row:
        raise HTTPException(404, "Storm not found")
    members = await storm._members(db, str(storm_id))
    if scope is not None:
        visible = {str(r[0]) for r in (await db.execute(text(
            "SELECT m.alert_id FROM alert_storm_members m WHERE m.storm_id = CAST(:id AS uuid)" + mf),
            {"id": str(storm_id), scoping.SCOPE_PARAM: scope})).all()}
        members = [m for m in members if str(m.alert_id) in visible]
    return storm.storm_payload(row, members=members)
