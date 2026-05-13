from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator_user
from app.models.user import User

router = APIRouter(prefix="/maps", tags=["Manual Maps"])


class MapCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class MapUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None


class NodeCreate(BaseModel):
    device_id: uuid.UUID
    label: Optional[str] = None
    icon: str = Field(default="auto", max_length=40)
    x_pct: float = Field(default=50, ge=2, le=98)
    y_pct: float = Field(default=50, ge=2, le=98)


class NodeUpdate(BaseModel):
    label: Optional[str] = None
    icon: Optional[str] = Field(default=None, max_length=40)
    x_pct: Optional[float] = Field(default=None, ge=2, le=98)
    y_pct: Optional[float] = Field(default=None, ge=2, le=98)


class LinkCreate(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    label: Optional[str] = None
    link_type: str = Field(default="manual", max_length=40)


def _row_map(row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _row_node(row) -> dict:
    return {
        "id": str(row["id"]),
        "map_id": str(row["map_id"]),
        "device_id": str(row["device_id"]),
        "label": row["label"] or row["hostname"],
        "icon": row["icon"],
        "x_pct": float(row["x_pct"]),
        "y_pct": float(row["y_pct"]),
        "hostname": row["hostname"],
        "ip_address": row["ip_address"],
        "device_type": row["device_type"],
        "status": row["status"],
        "location": row["location"],
        "vendor": row["vendor"],
        "model": row["model"],
        "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
        "metadata": row["metadata"] or {},
    }


def _row_link(row) -> dict:
    return {
        "id": str(row["id"]),
        "map_id": str(row["map_id"]),
        "source_node_id": str(row["source_node_id"]),
        "target_node_id": str(row["target_node_id"]),
        "label": row["label"],
        "link_type": row["link_type"],
        "metadata": row["metadata"] or {},
    }


@router.get("")
async def list_maps(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    rows = (await db.execute(
        text("""
            SELECT m.id, m.name, m.description, m.created_at, m.updated_at,
                   COALESCE(n.node_count, 0) AS node_count,
                   COALESCE(l.link_count, 0) AS link_count,
                   COALESCE(s.down_count, 0) AS down_count,
                   COALESCE(s.degraded_count, 0) AS degraded_count,
                   COALESCE(s.up_count, 0) AS up_count,
                   COALESCE(s.unknown_count, 0) AS unknown_count,
                   COALESCE(s.maintenance_count, 0) AS maintenance_count
            FROM manual_maps m
            LEFT JOIN (
                SELECT map_id, COUNT(*) AS node_count
                FROM manual_map_nodes
                GROUP BY map_id
            ) n ON n.map_id = m.id
            LEFT JOIN (
                SELECT map_id, COUNT(*) AS link_count
                FROM manual_map_links
                GROUP BY map_id
            ) l ON l.map_id = m.id
            LEFT JOIN (
                SELECT mn.map_id,
                       COUNT(*) FILTER (WHERE d.status = 'down') AS down_count,
                       COUNT(*) FILTER (WHERE d.status = 'degraded') AS degraded_count,
                       COUNT(*) FILTER (WHERE d.status = 'up') AS up_count,
                       COUNT(*) FILTER (WHERE d.status = 'unknown') AS unknown_count,
                       COUNT(*) FILTER (WHERE d.status = 'maintenance') AS maintenance_count
                FROM manual_map_nodes mn
                JOIN devices d ON d.id = mn.device_id
                GROUP BY mn.map_id
            ) s ON s.map_id = m.id
            ORDER BY m.updated_at DESC, m.name
        """)
    )).mappings().all()
    return {
        "data": [
            {
                **_row_map(row),
                "node_count": row["node_count"],
                "link_count": row["link_count"],
                "status_counts": {
                    "up": row["up_count"],
                    "down": row["down_count"],
                    "degraded": row["degraded_count"],
                    "unknown": row["unknown_count"],
                    "maintenance": row["maintenance_count"],
                },
            }
            for row in rows
        ]
    }


@router.post("", status_code=201)
async def create_map(
    data: MapCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("""
            INSERT INTO manual_maps (name, description, created_by)
            VALUES (:name, :description, :created_by)
            RETURNING id, name, description, created_at, updated_at
        """),
        {"name": data.name, "description": data.description, "created_by": user.id},
    )).mappings().first()
    await db.commit()
    return _row_map(row)


@router.get("/{map_id}")
async def get_map(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    map_row = (await db.execute(
        text("""
            SELECT id, name, description, created_at, updated_at
            FROM manual_maps
            WHERE id = :id
        """),
        {"id": map_id},
    )).mappings().first()
    if not map_row:
        raise HTTPException(status_code=404, detail="Map not found")

    nodes = (await db.execute(
        text("""
            SELECT mn.id, mn.map_id, mn.device_id, mn.label, mn.icon, mn.x_pct, mn.y_pct, mn.metadata,
                   d.hostname, host(d.ip_address) AS ip_address, d.device_type, d.status,
                   d.location, d.vendor, d.model, d.last_seen
            FROM manual_map_nodes mn
            JOIN devices d ON d.id = mn.device_id
            WHERE mn.map_id = :map_id
            ORDER BY mn.created_at, d.hostname
        """),
        {"map_id": map_id},
    )).mappings().all()

    links = (await db.execute(
        text("""
            SELECT id, map_id, source_node_id, target_node_id, label, link_type, metadata
            FROM manual_map_links
            WHERE map_id = :map_id
            ORDER BY created_at
        """),
        {"map_id": map_id},
    )).mappings().all()

    status_counts: dict[str, int] = {}
    for node in nodes:
        status_counts[node["status"]] = status_counts.get(node["status"], 0) + 1

    return {
        **_row_map(map_row),
        "summary": {
            "nodes": len(nodes),
            "links": len(links),
            "status_counts": status_counts,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "nodes": [_row_node(row) for row in nodes],
        "links": [_row_link(row) for row in links],
    }


@router.put("/{map_id}")
async def update_map(
    map_id: uuid.UUID,
    data: MapUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    parts = ["updated_at = NOW()"]
    params = {"id": map_id}
    for key, value in fields.items():
        parts.append(f"{key} = :{key}")
        params[key] = value
    row = (await db.execute(
        text(f"UPDATE manual_maps SET {', '.join(parts)} WHERE id = :id RETURNING id, name, description, created_at, updated_at"),
        params,
    )).mappings().first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Map not found")
    await db.commit()
    return _row_map(row)


@router.delete("/{map_id}", status_code=204)
async def delete_map(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(text("DELETE FROM manual_maps WHERE id = :id RETURNING id"), {"id": map_id})).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Map not found")
    await db.commit()


@router.post("/{map_id}/nodes", status_code=201)
async def create_node(
    map_id: uuid.UUID,
    data: NodeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(status_code=404, detail="Map not found")
    device_exists = (await db.execute(text("SELECT id FROM devices WHERE id = :id"), {"id": data.device_id})).first()
    if not device_exists:
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        row = (await db.execute(
            text("""
                INSERT INTO manual_map_nodes (map_id, device_id, label, icon, x_pct, y_pct)
                VALUES (:map_id, :device_id, :label, :icon, :x_pct, :y_pct)
                RETURNING id
            """),
            {"map_id": map_id, **data.model_dump()},
        )).first()
        await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
        await db.commit()
        return {"id": str(row.id)}
    except Exception as exc:
        await db.rollback()
        if "manual_map_nodes_map_id_device_id_key" in str(exc):
            raise HTTPException(status_code=409, detail="Device already exists on this map") from exc
        raise


@router.put("/{map_id}/nodes/{node_id}")
async def update_node(
    map_id: uuid.UUID,
    node_id: uuid.UUID,
    data: NodeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    parts = ["updated_at = NOW()"]
    params = {"map_id": map_id, "node_id": node_id}
    for key, value in fields.items():
        parts.append(f"{key} = :{key}")
        params[key] = value
    row = (await db.execute(
        text(f"""
            UPDATE manual_map_nodes
            SET {', '.join(parts)}
            WHERE id = :node_id AND map_id = :map_id
            RETURNING id
        """),
        params,
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Node not found")
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/{map_id}/nodes/{node_id}", status_code=204)
async def delete_node(
    map_id: uuid.UUID,
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("DELETE FROM manual_map_nodes WHERE id = :node_id AND map_id = :map_id RETURNING id"),
        {"node_id": node_id, "map_id": map_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Node not found")
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()


@router.post("/{map_id}/links", status_code=201)
async def create_link(
    map_id: uuid.UUID,
    data: LinkCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if data.source_node_id == data.target_node_id:
        raise HTTPException(status_code=400, detail="Source and target must be different")
    rows = (await db.execute(
        text("""
            SELECT id FROM manual_map_nodes
            WHERE map_id = :map_id
              AND (id = :source_node_id OR id = :target_node_id)
        """),
        {"map_id": map_id, "source_node_id": data.source_node_id, "target_node_id": data.target_node_id},
    )).all()
    if len(rows) != 2:
        raise HTTPException(status_code=400, detail="Both nodes must belong to this map")
    try:
        row = (await db.execute(
            text("""
                INSERT INTO manual_map_links (map_id, source_node_id, target_node_id, label, link_type)
                VALUES (:map_id, :source_node_id, :target_node_id, :label, :link_type)
                RETURNING id
            """),
            {"map_id": map_id, **data.model_dump()},
        )).first()
        await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
        await db.commit()
        return {"id": str(row.id)}
    except Exception as exc:
        await db.rollback()
        if "idx_manual_map_links_unique_pair" in str(exc):
            raise HTTPException(status_code=409, detail="Link already exists") from exc
        raise


@router.delete("/{map_id}/links/{link_id}", status_code=204)
async def delete_link(
    map_id: uuid.UUID,
    link_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("DELETE FROM manual_map_links WHERE id = :link_id AND map_id = :map_id RETURNING id"),
        {"link_id": link_id, "map_id": map_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Link not found")
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
