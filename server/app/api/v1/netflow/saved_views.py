"""NetFlow saved views CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.netflow_saved_view import NetflowSavedView
from app.models.user import User
from pydantic import BaseModel, Field

router = APIRouter()


# ─────────────────────────────────────────────────────────────────
# Saved Views CRUD
# ─────────────────────────────────────────────────────────────────


class SavedViewIn(BaseModel):
    name: str = Field(..., max_length=120)
    description: str | None = Field(None, max_length=500)
    query: dict
    pinned: bool = False


class SavedViewOut(BaseModel):
    id: str
    name: str
    description: str | None
    query: dict
    pinned: bool
    created_at: str
    updated_at: str


@router.get("/saved-views", response_model=list[SavedViewOut])
async def list_saved_views(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(NetflowSavedView).order_by(NetflowSavedView.pinned.desc(), NetflowSavedView.updated_at.desc()))).scalars().all()
    return [
        SavedViewOut(
            id=str(v.id),
            name=v.name,
            description=v.description,
            query=v.query,
            pinned=v.pinned,
            created_at=v.created_at.isoformat(),
            updated_at=v.updated_at.isoformat(),
        )
        for v in rows
    ]


@router.post("/saved-views", response_model=SavedViewOut)
async def create_saved_view(
    body: SavedViewIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    view = NetflowSavedView(name=body.name, description=body.description, query=body.query, pinned=body.pinned, owner_id=user.id)
    db.add(view)
    await db.commit()
    await db.refresh(view)
    return SavedViewOut(
        id=str(view.id),
        name=view.name,
        description=view.description,
        query=view.query,
        pinned=view.pinned,
        created_at=view.created_at.isoformat(),
        updated_at=view.updated_at.isoformat(),
    )


@router.delete("/saved-views/{view_id}")
async def delete_saved_view(
    view_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(NetflowSavedView).where(NetflowSavedView.id == view_id))
    view = res.scalar_one_or_none()
    if not view:
        raise HTTPException(status_code=404, detail="Saved view not found")
    await db.delete(view)
    await db.commit()
    return {"deleted": view_id}
