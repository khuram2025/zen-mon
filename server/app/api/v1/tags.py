"""Tag registry API.

CRUD for the `tags` catalog plus the consistency work a registry owes its
callers: renaming a tag rewrites it everywhere it is used, deleting strips it
everywhere. Assignments live as JSONB text arrays on the tagged rows and are
edited through each surface's own endpoints.

The set of places a tag can appear is declared once, in TAG_ARRAY_TABLES and
TAG_SCOPE_COLUMNS below. That is deliberate: propagation used to be two
hand-written UPDATEs, so when alert_rules.scope_tag (migrate-077) and
service_check_maintenance.scope_tag were added nobody updated this file, and a
rename silently pointed those scopes at a name nothing carried any more —
matching no devices, with no error. Adding a tagged surface means adding a line
here, not remembering to.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.models.user import User
from app.services.tag_service import (
    MAX_TAG_LEN, TAG_ARRAY_TABLES, TAG_SCOPE_COLUMNS, adopt_device_tags, auto_color,
)

router = APIRouter(prefix="/tags", tags=["Tags"])

COLOR_RE = r"^#[0-9a-fA-F]{6}$"

async def _rename_everywhere(db: AsyncSession, old: str, new: str) -> None:
    for table in TAG_ARRAY_TABLES:
        await db.execute(text(f"""
            UPDATE {table} SET tags = (
                SELECT COALESCE(jsonb_agg(DISTINCT
                           CASE WHEN LOWER(el) = LOWER(:old) THEN :new ELSE el END),
                       '[]'::jsonb)
                FROM jsonb_array_elements_text(COALESCE({table}.tags, '[]'::jsonb)) el
            )
            WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(COALESCE({table}.tags, '[]'::jsonb)) el
                WHERE LOWER(el) = LOWER(:old))
        """), {"old": old, "new": new})
    for table, col, pred in TAG_SCOPE_COLUMNS:
        where = f"LOWER({col}) = LOWER(:old)" + (f" AND {pred}" if pred else "")
        await db.execute(
            text(f"UPDATE {table} SET {col} = :new WHERE {where}"),
            {"old": old, "new": new},
        )


async def _delete_everywhere(db: AsyncSession, name: str) -> None:
    for table in TAG_ARRAY_TABLES:
        await db.execute(text(f"""
            UPDATE {table} SET tags = (
                SELECT COALESCE(jsonb_agg(el), '[]'::jsonb)
                FROM jsonb_array_elements_text(COALESCE({table}.tags, '[]'::jsonb)) el
                WHERE LOWER(el) <> LOWER(:n)
            )
            WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(COALESCE({table}.tags, '[]'::jsonb)) el
                WHERE LOWER(el) = LOWER(:n))
        """), {"n": name})
    # A scope pointing at a deleted tag would match nothing forever; clearing it
    # makes the rule/window unscoped, which is visible in the UI.
    for table, col, pred in TAG_SCOPE_COLUMNS:
        where = f"LOWER({col}) = LOWER(:n)" + (f" AND {pred}" if pred else "")
        await db.execute(text(f"UPDATE {table} SET {col} = NULL WHERE {where}"), {"n": name})


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_TAG_LEN)
    color: Optional[str] = Field(default=None, pattern=COLOR_RE)
    description: Optional[str] = None


class TagUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=MAX_TAG_LEN)
    color: Optional[str] = Field(default=None, pattern=COLOR_RE)
    description: Optional[str] = None


class TagResponse(BaseModel):
    id: UUID
    name: str
    color: Optional[str]
    description: Optional[str]
    device_count: int
    maintenance_count: int


# device_count/maintenance_count are case-insensitive on purpose: rows
# written before the registry existed may not match its spelling exactly.
_SELECT = """
    SELECT t.id, t.name, t.color, t.description,
           (SELECT COUNT(*) FROM devices d
             WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(COALESCE(d.tags, '[]'::jsonb)) el
                WHERE LOWER(el) = LOWER(t.name)))            AS device_count,
           (SELECT COUNT(*) FROM device_maintenance m
             WHERE m.scope_type = 'tag'
               AND LOWER(m.scope_tag) = LOWER(t.name))       AS maintenance_count
    FROM tags t
"""


async def _tag_response(db: AsyncSession, tag_id: UUID) -> TagResponse:
    row = (await db.execute(
        text(_SELECT + " WHERE t.id = :id"), {"id": tag_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Tag not found")
    return TagResponse(**row)


@router.get("", response_model=list[TagResponse])
async def list_tags(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await adopt_device_tags(db)
    rows = (await db.execute(text(_SELECT + " ORDER BY LOWER(t.name)"))).mappings().all()
    return [TagResponse(**r) for r in rows]


@router.post("", response_model=TagResponse, status_code=201)
async def create_tag(
    payload: TagCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Tag name cannot be empty")
    dup = (await db.execute(
        text("SELECT 1 FROM tags WHERE LOWER(name) = LOWER(:n)"), {"n": name}
    )).first()
    if dup:
        raise HTTPException(status_code=409, detail=f"Tag '{name}' already exists")
    tag_id = (await db.execute(
        text("""INSERT INTO tags (name, color, description)
                VALUES (:n, :c, :d) RETURNING id"""),
        {"n": name, "c": payload.color or auto_color(name), "d": payload.description},
    )).scalar_one()
    await db.commit()
    return await _tag_response(db, tag_id)


@router.patch("/{tag_id}", response_model=TagResponse)
async def update_tag(
    tag_id: UUID,
    payload: TagUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    cur = (await db.execute(
        text("SELECT name FROM tags WHERE id = :id"), {"id": tag_id}
    )).first()
    if not cur:
        raise HTTPException(status_code=404, detail="Tag not found")
    old_name = cur[0]

    new_name = old_name
    if payload.name is not None:
        new_name = payload.name.strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="Tag name cannot be empty")
        clash = (await db.execute(
            text("SELECT 1 FROM tags WHERE LOWER(name) = LOWER(:n) AND id <> :id"),
            {"n": new_name, "id": tag_id},
        )).first()
        if clash:
            raise HTTPException(status_code=409, detail=f"Tag '{new_name}' already exists")

    fields: dict = {"name": new_name}
    if payload.color is not None:
        fields["color"] = payload.color
    if payload.description is not None:
        fields["description"] = payload.description
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await db.execute(
        text(f"UPDATE tags SET {sets}, updated_at = NOW() WHERE id = :id"),
        {**fields, "id": tag_id},
    )

    if new_name != old_name:
        # The registry is authoritative: a rename follows the tag everywhere it
        # is used — every tagged surface and every tag-scoped rule or window.
        await _rename_everywhere(db, old_name, new_name)

    await db.commit()
    return await _tag_response(db, tag_id)


@router.delete("/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    cur = (await db.execute(
        text("SELECT name FROM tags WHERE id = :id"), {"id": tag_id}
    )).first()
    if not cur:
        raise HTTPException(status_code=404, detail="Tag not found")
    name = cur[0]

    await _delete_everywhere(db, name)
    await db.execute(text("DELETE FROM tags WHERE id = :id"), {"id": tag_id})
    await db.commit()
