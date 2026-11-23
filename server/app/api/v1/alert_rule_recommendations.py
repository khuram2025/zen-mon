"""Recommended default alert rules (``app.services.recommended_rules``).

GET  /alert-rules/recommended               the catalogue with available / accepted / covered
POST /alert-rules/recommended/{key}/accept  create one ordinary rule from a recommendation

Registered before the alert rule router so ``recommended`` never reaches the
``/alert-rules/{rule_id}`` route.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import recommended_rules as rec

router = APIRouter(prefix="/alert-rules/recommended", tags=["Alert Rules"])


class AcceptBody(BaseModel):
    enabled: bool = True
    notify_channels: list[str] = Field(default_factory=list, max_length=50)
    escalation_policy_id: Optional[str] = None


@router.get("")
async def list_recommended(db: AsyncSession = Depends(get_db),
                           user: User = Depends(require_permission("alerts.view"))):
    """Recommended default rules. Nothing is created until one is accepted."""
    return {"items": await rec.state(db)}


@router.post("/{key}/accept", status_code=201)
async def accept_recommended(key: str, data: AcceptBody, db: AsyncSession = Depends(get_db),
                             user: User = Depends(require_permission("alerts.manage"))):
    item = rec.find(key)
    if item is None:
        raise HTTPException(status_code=404, detail="No such recommended rule")
    current = next(it for it in await rec.state(db) if it["key"] == key)
    if current["status"] == "accepted":
        raise HTTPException(status_code=409, detail="This recommendation is already accepted")
    description = item["description"] + " Accepted from the recommended default rules."
    if item["family"] == "server":
        from app.api.v1 import host_alert_rules as host_api
        body = host_api.HostRuleCreate(
            name=item["name"], description=description, enabled=data.enabled,
            metric=item["metric"], operator=item["operator"], threshold=item["threshold"],
            severity=item["severity"], min_duration=item["min_duration"],
            clear_duration=item["clear_duration"], recovery_alert=item["recovery_alert"],
            cooldown=item["cooldown"], notify_channels=data.notify_channels,
            priority=item["priority"], escalation_policy_id=data.escalation_policy_id,
            condition_tree=item.get("condition_tree"))
        created = await host_api.create_host_rule(body, db, user)
    else:
        from app.api.v1 import alert_rules as rules_api
        body = rules_api.AlertRuleCreate(
            name=item["name"], description=description, enabled=data.enabled,
            metric=item["metric"], operator=item["operator"], threshold=item["threshold"],
            trap_oid=item.get("trap_oid"), trigger_on=item["trigger_on"],
            recovery_alert=item["recovery_alert"], severity=item["severity"],
            notify_channels=data.notify_channels, cooldown=item["cooldown"],
            min_duration=item["min_duration"], clear_duration=item["clear_duration"],
            priority=item["priority"], escalation_policy_id=data.escalation_policy_id,
            condition_tree=item.get("condition_tree"), subject_type=item.get("subject_type"))
        created = await rules_api.create_alert_rule(body, db, user)
    rule = created.get("data", created) if isinstance(created, dict) else created
    rule_id = rule.get("id") if isinstance(rule, dict) else None
    if not rule_id:
        raise HTTPException(status_code=500, detail="The rule was not created")
    await db.execute(text("UPDATE alert_rules SET pack_key = :k, pack_accepted = CAST(:acc AS jsonb) WHERE id = :id"),
                     {"k": rec.PACK_PREFIX + key, "acc": rec.accepted_record(item), "id": str(rule_id)})
    await db.commit()
    return {**rule, "pack_key": rec.PACK_PREFIX + key}
