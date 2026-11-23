"""Combined (multi-condition) rules: replay a draft over stored history.

POST /alert-rules/combined/replay   "Test against the last 7 days" (combined_replay)

Registered before the alert rule router so ``combined`` never reaches the
``/alert-rules/{rule_id}`` routes. Read only.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import combined_replay
from app.services import rule_conditions as rc

router = APIRouter(prefix="/alert-rules/combined", tags=["Alert Rules"])


class ReplayBody(BaseModel):
    subject_type: str = Field(..., pattern="^(server|device|service_check|apm_service|virtualization|appliance)$")
    condition_tree: dict
    min_duration: int = Field(0, ge=0, le=86400)
    clear_duration: Optional[int] = Field(None, ge=0, le=86400)
    days: int = Field(combined_replay.DEFAULT_DAYS, ge=1, le=combined_replay.MAX_DAYS)
    # The draft's scope, as the rule would store it.
    server_id: Optional[UUID] = None
    device_id: Optional[UUID] = None
    group_id: Optional[UUID] = None
    device_type: Optional[str] = None
    location: Optional[str] = None
    scope_tag: Optional[str] = None
    profile_id: Optional[UUID] = None
    service_check_id: Optional[UUID] = None
    service_check_group_id: Optional[UUID] = None
    target: Optional[str] = None


@router.post("/replay")
async def replay_combined_rule(body: ReplayBody, db: AsyncSession = Depends(get_db),
                               user: User = Depends(require_permission("alerts.view"))):
    """When the draft rule would have fired over the last days, per subject."""
    try:
        tree = rc.validate_tree(body.condition_tree, body.subject_type)
    except rc.TreeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rule = SimpleNamespace(**{**body.model_dump(exclude={"condition_tree", "days"}), "condition_tree": tree,
                              "id": None, "if_role": None})
    return await combined_replay.replay(db, rule, tree, body.subject_type, days=body.days)
