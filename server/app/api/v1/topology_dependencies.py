"""Parent/child dependencies API (``/api/v1/topology-dependencies``; docs/dependencies.md).

Reading needs ``devices.view`` (router policy) and shows the dependencies whose
parent and child are both in the caller's scope. Adding and removing needs
``devices.manage`` and both ends in scope. Rows a discovery rule or the
controller sync wrote stay theirs: they are listed but never deleted here.
Suggestions from the LLDP/CDP topology are only offered; accepting one creates
a ``topology`` row through the same POST.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoping
from app.core.database import get_db
from app.core.security import get_current_user, require_permission
from app.models.user import User
from app.services import topology_dependencies as deps
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/topology-dependencies", tags=["Dependencies"])

MANAGE = "devices.manage"


class DependencyIn(BaseModel):
    parent_device_id: uuid.UUID
    child_device_id: Optional[uuid.UUID] = None
    child_server_id: Optional[uuid.UUID] = None
    dependency_type: Literal["uplink", "wan", "power", "site", "service", "manual"] = "uplink"
    notes: Optional[str] = Field(default=None, max_length=500)
    source: Literal["manual", "topology"] = "manual"

    @model_validator(mode="after")
    def _one_child(self):
        if (self.child_device_id is None) == (self.child_server_id is None):
            raise ValueError("Give exactly one of child_device_id or child_server_id")
        return self


def _http(exc: deps.DependencyError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


@router.get("")
async def list_dependencies(device_id: Optional[uuid.UUID] = Query(default=None),
                            server_id: Optional[uuid.UUID] = Query(default=None),
                            db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    scope = await scoping.visible_tags(db, user)
    rows = await deps.list_dependencies(db, scope, device_id=str(device_id) if device_id else None,
                                        server_id=str(server_id) if server_id else None)
    return {"data": rows}


@router.get("/suggestions")
async def suggestions(root_device_id: Optional[uuid.UUID] = Query(default=None),
                      db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    scope = await scoping.visible_tags(db, user)
    try:
        return await deps.suggest(db, scope, root_device_id=str(root_device_id) if root_device_id else None)
    except deps.DependencyError as exc:
        raise _http(exc) from None


@router.post("", status_code=201)
async def create_dependency(data: DependencyIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission(MANAGE))):
    scope = await scoping.visible_tags(db, user)
    kind = "device" if data.child_device_id else "server"
    child_id = str(data.child_device_id or data.child_server_id)
    try:
        made = await deps.create(db, parent_device_id=str(data.parent_device_id), child_kind=kind,
                                 child_id=child_id, dependency_type=data.dependency_type, notes=data.notes,
                                 source=data.source, user_id=str(user.id) if user.id else None, scope=scope)
    except deps.DependencyError as exc:
        await db.rollback()
        raise _http(exc) from None
    await write_audit_log(db, actor=user, action="topology_dependency.create", resource_type="topology_dependency",
                          resource_id=made["id"], metadata={
                              "parent": made["parent"]["name"], "child": made["child"]["name"],
                              "child_kind": kind, "dependency_type": data.dependency_type,
                              "source": data.source})
    await db.commit()
    rows = await deps.list_dependencies(db, scope)
    return next((r for r in rows if r["id"] == made["id"]), {"id": made["id"]})


@router.delete("/{dependency_id}")
async def delete_dependency(dependency_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_permission(MANAGE))):
    scope = await scoping.visible_tags(db, user)
    try:
        gone = await deps.delete(db, str(dependency_id), scope)
    except deps.DependencyError as exc:
        await db.rollback()
        raise _http(exc) from None
    await write_audit_log(db, actor=user, action="topology_dependency.delete", resource_type="topology_dependency",
                          resource_id=str(dependency_id), metadata={
                              "parent": gone["parent"]["name"], "child": gone["child"]["name"],
                              "child_kind": gone["child"]["kind"], "source": gone["source"]})
    await db.commit()
    return {"deleted": True, "id": str(dependency_id)}
