from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_clickhouse_client, get_db
from app.core.security import require_operator_user
from app.models.user import User

router = APIRouter(prefix="/maps", tags=["Manual Maps"])


class MapCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    metadata: Optional[dict] = None


class MapUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    # Holds background image, theme preset, snap-to-grid toggle, default
    # shape, and other per-map UI state. Stored verbatim as JSONB.
    metadata: Optional[dict] = None


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
    # Free-form per-node metadata. Used today for `size_scale` (0.5-2.5) so
    # admins can resize the rendered disc; future shape / colour tweaks go
    # here as well without needing a migration.
    metadata: Optional[dict] = None


class LinkCreate(BaseModel):
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    label: Optional[str] = None
    link_type: str = Field(default="manual", max_length=40)
    metadata: Optional[dict] = None


class LinkUpdate(BaseModel):
    label: Optional[str] = None
    link_type: Optional[str] = Field(default=None, max_length=40)
    metadata: Optional[dict] = None


# Annotation shapes are positioned/sized as percentages of the canvas so the
# map scales cleanly with the viewport, the same way nodes do.
SHAPE_KINDS = (
    "rectangle", "circle", "text",
    "line", "arrow", "diamond", "hexagon", "image", "sticky",
)


class ShapeCreate(BaseModel):
    kind: str = Field(..., max_length=20)
    x_pct: float = Field(default=50, ge=0, le=100)
    y_pct: float = Field(default=50, ge=0, le=100)
    w_pct: float = Field(default=14, ge=1, le=100)
    h_pct: float = Field(default=8, ge=1, le=100)
    text: Optional[str] = None
    fill: Optional[str] = Field(default=None, max_length=40)
    stroke: Optional[str] = Field(default=None, max_length=40)
    z_index: int = 0
    metadata: Optional[dict] = None


class ShapeUpdate(BaseModel):
    kind: Optional[str] = Field(default=None, max_length=20)
    x_pct: Optional[float] = Field(default=None, ge=0, le=100)
    y_pct: Optional[float] = Field(default=None, ge=0, le=100)
    w_pct: Optional[float] = Field(default=None, ge=1, le=100)
    h_pct: Optional[float] = Field(default=None, ge=1, le=100)
    text: Optional[str] = None
    fill: Optional[str] = Field(default=None, max_length=40)
    stroke: Optional[str] = Field(default=None, max_length=40)
    z_index: Optional[int] = None
    metadata: Optional[dict] = None


def _row_map(row) -> dict:
    md = row["metadata"] if "metadata" in row.keys() else None
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "metadata": md or {},
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


def _row_shape(row) -> dict:
    return {
        "id": str(row["id"]),
        "map_id": str(row["map_id"]),
        "kind": row["kind"],
        "x_pct": float(row["x_pct"]),
        "y_pct": float(row["y_pct"]),
        "w_pct": float(row["w_pct"]),
        "h_pct": float(row["h_pct"]),
        "text": row["text"],
        "fill": row["fill"],
        "stroke": row["stroke"],
        "z_index": row["z_index"],
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
            SELECT m.id, m.name, m.description, m.metadata, m.created_at, m.updated_at,
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
            INSERT INTO manual_maps (name, description, metadata, created_by)
            VALUES (:name, :description, CAST(:metadata AS JSONB), :created_by)
            RETURNING id, name, description, metadata, created_at, updated_at
        """),
        {
            "name": data.name,
            "description": data.description,
            "metadata": json.dumps(data.metadata or {}),
            "created_by": user.id,
        },
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
            SELECT id, name, description, metadata, created_at, updated_at
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

    shapes = (await db.execute(
        text("""
            SELECT id, map_id, kind, x_pct, y_pct, w_pct, h_pct, text, fill, stroke,
                   z_index, metadata
            FROM manual_map_shapes
            WHERE map_id = :map_id
            ORDER BY z_index, created_at
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
            "shapes": len(shapes),
            "status_counts": status_counts,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "nodes": [_row_node(row) for row in nodes],
        "links": [_row_link(row) for row in links],
        "shapes": [_row_shape(row) for row in shapes],
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
        if key == "metadata":
            parts.append("metadata = CAST(:metadata AS JSONB)")
            params["metadata"] = json.dumps(value or {})
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value
    row = (await db.execute(
        text(f"UPDATE manual_maps SET {', '.join(parts)} WHERE id = :id RETURNING id, name, description, metadata, created_at, updated_at"),
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
        if key == "metadata":
            parts.append("metadata = CAST(:metadata AS JSONB)")
            params["metadata"] = json.dumps(value or {})
        else:
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
        params = {
            "map_id": map_id,
            "source_node_id": data.source_node_id,
            "target_node_id": data.target_node_id,
            "label": data.label,
            "link_type": data.link_type,
            "metadata": json.dumps(data.metadata or {}),
        }
        row = (await db.execute(
            text("""
                INSERT INTO manual_map_links (map_id, source_node_id, target_node_id, label, link_type, metadata)
                VALUES (:map_id, :source_node_id, :target_node_id, :label, :link_type, CAST(:metadata AS JSONB))
                RETURNING id
            """),
            params,
        )).first()
        await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
        await db.commit()
        return {"id": str(row.id)}
    except Exception as exc:
        await db.rollback()
        if "idx_manual_map_links_unique_pair" in str(exc):
            raise HTTPException(status_code=409, detail="Link already exists") from exc
        raise


@router.put("/{map_id}/links/{link_id}")
async def update_link(
    map_id: uuid.UUID,
    link_id: uuid.UUID,
    data: LinkUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    parts = ["updated_at = NOW()"]
    params: dict = {"map_id": map_id, "link_id": link_id}
    for key, value in fields.items():
        if key == "metadata":
            parts.append("metadata = CAST(:metadata AS JSONB)")
            params["metadata"] = json.dumps(value or {})
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value
    row = (await db.execute(
        text(f"""
            UPDATE manual_map_links
            SET {', '.join(parts)}
            WHERE id = :link_id AND map_id = :map_id
            RETURNING id
        """),
        params,
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Link not found")
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
    return {"id": str(row.id)}


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


# ─────────────────────────────────────────────────────────────────
# LLDP/CDP link assistance
#
# Cross-reference the devices currently placed on the map against the
# discovered topology (topology_links). When two placed devices have a
# real LLDP/CDP adjacency but no manual link yet, surface it as a
# "suggested" link the operator can add with one click — pre-bound to
# the correct interface pair so it inherits live weathermap stats.
# ─────────────────────────────────────────────────────────────────

def _speed_to_link_type(speed: Optional[int]) -> str:
    """Map an interface speed (bps) to a sensible default link styling."""
    if not speed:
        return "ethernet"
    if speed >= 10_000_000_000:
        return "fiber"        # 10G+ → fiber styling
    return "ethernet"


async def _compute_suggested_links(db: AsyncSession, map_id: uuid.UUID) -> list[dict]:
    """Discovered adjacencies among placed devices that aren't linked yet.

    Deduped to one suggestion per unordered device pair, preferring the
    edge that carries both interface names and the highest confidence.
    """
    placed = (await db.execute(
        text("""
            SELECT mn.id AS node_id, mn.device_id, d.hostname
            FROM manual_map_nodes mn JOIN devices d ON d.id = mn.device_id
            WHERE mn.map_id = :map_id
        """),
        {"map_id": map_id},
    )).mappings().all()
    if len(placed) < 2:
        return []
    node_by_device = {str(p["device_id"]): p for p in placed}
    device_ids = list(node_by_device.keys())

    # Node pairs that already have a manual link (either direction).
    existing = (await db.execute(
        text("""
            SELECT source_node_id, target_node_id FROM manual_map_links WHERE map_id = :map_id
        """),
        {"map_id": map_id},
    )).all()
    linked_pairs = {frozenset((str(a), str(b))) for a, b in existing}

    # Discovered edges where BOTH ends are placed on this map.
    edges = (await db.execute(
        text("""
            SELECT local_device_id, remote_device_id, local_if_name,
                   remote_if_name, remote_port_id, protocol, confidence
            FROM topology_links
            WHERE remote_device_id IS NOT NULL
              AND local_device_id  = ANY(:ids)
              AND remote_device_id = ANY(:ids)
        """),
        {"ids": device_ids},
    )).mappings().all()

    # The clean port-id (e.g. "Twe1/0/10") is a better interface name than the
    # verbose LLDP port-DESCRIPTION ("*** To_… ***") for both display and
    # weathermap matching, so prefer it.
    def _remote_iface(e) -> Optional[str]:
        return e["remote_port_id"] or e["remote_if_name"]

    # Group by unordered device pair; pick the best representative edge —
    # prefer one carrying both interface names, then highest confidence.
    best: dict[frozenset, dict] = {}
    counts: dict[frozenset, int] = {}
    for e in edges:
        a, b = str(e["local_device_id"]), str(e["remote_device_id"])
        if a == b:
            continue
        key = frozenset((a, b))
        counts[key] = counts.get(key, 0) + 1
        score = (1 if (e["local_if_name"] and _remote_iface(e)) else 0, e["confidence"] or 0)
        cur = best.get(key)
        if cur is None or score > cur["_score"]:
            best[key] = {**dict(e), "_score": score, "_a": a, "_b": b}

    suggestions = []
    for key, e in best.items():
        src = node_by_device.get(e["_a"])
        dst = node_by_device.get(e["_b"])
        if not src or not dst:
            continue
        if frozenset((str(src["node_id"]), str(dst["node_id"]))) in linked_pairs:
            continue
        suggestions.append({
            "source_node_id": str(src["node_id"]),
            "target_node_id": str(dst["node_id"]),
            "source_hostname": src["hostname"],
            "target_hostname": dst["hostname"],
            "src_interface": e["local_if_name"],
            "dst_interface": _remote_iface(e),
            "protocol": e["protocol"],
            "confidence": e["confidence"],
            "physical_links": counts[key],
        })
    suggestions.sort(key=lambda s: (-(s["confidence"] or 0), s["source_hostname"] or ""))
    return suggestions


@router.get("/{map_id}/suggested-links")
async def suggested_links(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(404, "Map not found")
    data = await _compute_suggested_links(db, map_id)
    return {"data": data, "count": len(data)}


@router.post("/{map_id}/auto-connect")
async def auto_connect_discovered(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Create manual links for every discovered adjacency among placed
    devices that isn't linked yet. Each link is bound to its interface
    pair so it lights up the live weathermap immediately."""
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(404, "Map not found")
    suggestions = await _compute_suggested_links(db, map_id)
    created = 0
    for s in suggestions:
        md = {
            "src_interface": s["src_interface"],
            "dst_interface": s["dst_interface"],
            "discovered": True,
            "protocol": s["protocol"],
        }
        try:
            # Savepoint per insert: a single bad/duplicate row doesn't poison
            # the whole batch.
            async with db.begin_nested():
                await db.execute(
                    text("""
                        INSERT INTO manual_map_links (map_id, source_node_id, target_node_id, label, link_type, metadata)
                        VALUES (:map_id, :src, :dst, :label, :lt, CAST(:md AS JSONB))
                    """),
                    {"map_id": map_id, "src": s["source_node_id"], "dst": s["target_node_id"],
                     "label": (s["protocol"] or "lldp").upper(), "lt": "ethernet", "md": json.dumps(md)},
                )
            created += 1
        except Exception:
            continue
    if created:
        await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
    return {"created": created, "suggested": len(suggestions)}


# ─────────────────────────────────────────────────────────────────
# Live link statistics
#
# Match the user-typed src_interface / dst_interface strings on each
# link against discovered device_interfaces, then overlay NetFlow
# byte counters from ClickHouse over a recent window so the map can
# show real throughput and utilization next to each link.
# ─────────────────────────────────────────────────────────────────

_LIVE_WINDOW_SECONDS = 300  # 5-minute rolling window (NetFlow rate window)
# SNMP interface counters arrive in poller bursts every ~8 minutes, so accept
# the latest sample up to 15 minutes back — otherwise links go blank between
# polls. The query takes only the newest sample (argMax), so a wider lookback
# changes staleness tolerance, not the computed rate.
_SNMP_LOOKBACK_SECONDS = 900

# Normalize Cisco-style abbreviations so "Gi0/1" matches
# "GigabitEthernet0/1", "Te1/0/24" matches "TenGigabitEthernet1/0/24",
# etc. Keeps comparison case-insensitive.
_IFACE_ABBREV = [
    ("tengigabitethernet", "te"),
    ("gigabitethernet",    "gi"),
    ("fastethernet",       "fa"),
    ("hundredgige",        "hu"),
    ("fortygige",          "fo"),
    ("twentyfivegige",     "twe"),
    ("ethernet",           "eth"),
    ("port-channel",       "po"),
]


def _norm_iface(name: Optional[str]) -> str:
    if not name:
        return ""
    n = name.strip().lower()
    for full, short in _IFACE_ABBREV:
        n = n.replace(full, short)
    # collapse repeating whitespace + remove spaces between letter and digit
    n = re.sub(r"\s+", "", n)
    return n


def _match_interface(typed: Optional[str], ifaces: list[dict]) -> Optional[dict]:
    """Pick the best device_interfaces row matching the user-typed string."""
    if not typed or not ifaces:
        return None
    needle = _norm_iface(typed)
    if not needle:
        return None
    # Pass 1 — exact match on any name field
    for it in ifaces:
        for field in ("if_name", "if_descr", "if_alias"):
            if _norm_iface(it.get(field)) == needle:
                return it
    # Pass 2 — needle is prefix/suffix of any field (or vice versa)
    for it in ifaces:
        for field in ("if_name", "if_descr", "if_alias"):
            val = _norm_iface(it.get(field))
            if not val:
                continue
            if val.startswith(needle) or val.endswith(needle) or needle in val:
                return it
    return None


def _empty_iface() -> dict:
    return {
        "matched": False,
        "if_index": None,
        "if_name": None,
        "if_descr": None,
        "if_alias": None,
        "if_speed": None,
        "admin_status": None,
        "oper_status": None,
        "in_bps": None,
        "out_bps": None,
        "in_packets": None,
        "out_packets": None,
        "util_pct": None,
    }


def _iface_payload(row: dict) -> dict:
    return {
        "matched": True,
        "if_index": int(row.get("if_index")) if row.get("if_index") is not None else None,
        "if_name": row.get("if_name"),
        "if_descr": row.get("if_descr"),
        "if_alias": row.get("if_alias"),
        "if_speed": int(row["if_speed"]) if row.get("if_speed") is not None else None,
        "admin_status": row.get("admin_status"),
        "oper_status": row.get("oper_status"),
        "in_bps": None,
        "out_bps": None,
        "in_packets": None,
        "out_packets": None,
        "util_pct": None,
    }


@router.get("/{map_id}/links-live")
async def links_live(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Return live interface + throughput data for every link on the map.

    Response: {"data": {<link_id>: {source, target, window_seconds, generated_at}}}

    Each link's source/target carries `matched=False` when the typed
    interface name didn't resolve to a discovered interface, otherwise
    the discovered fields plus a NetFlow-derived `in_bps`, `out_bps`,
    `util_pct` over the last 5 minutes.
    """
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(404, "Map not found")

    # Nodes (device + IP, used to find the right device_interfaces rows
    # and to map back to NetFlow exporter_ip).
    node_rows = (await db.execute(
        text("""
            SELECT mn.id AS node_id, mn.device_id,
                   host(d.ip_address) AS ip_address
            FROM manual_map_nodes mn
            JOIN devices d ON d.id = mn.device_id
            WHERE mn.map_id = :map_id
        """),
        {"map_id": map_id},
    )).mappings().all()
    node_info = {str(r["node_id"]): {"device_id": r["device_id"], "ip": r["ip_address"]} for r in node_rows}

    # Links
    link_rows = (await db.execute(
        text("""
            SELECT id, source_node_id, target_node_id, metadata
            FROM manual_map_links WHERE map_id = :map_id
        """),
        {"map_id": map_id},
    )).mappings().all()

    if not link_rows:
        return {"data": {}, "window_seconds": _LIVE_WINDOW_SECONDS,
                "generated_at": datetime.now(timezone.utc).isoformat()}

    # Pull device_interfaces for every device referenced. One query keeps it cheap.
    device_ids = {str(info["device_id"]) for info in node_info.values()}
    ifaces_by_device: dict[str, list[dict]] = {did: [] for did in device_ids}
    if device_ids:
        ifr = (await db.execute(
            text("""
                SELECT device_id, if_index, if_name, if_descr, if_alias,
                       if_speed, admin_status, oper_status
                FROM device_interfaces
                WHERE device_id = ANY(:ids)
            """),
            {"ids": list(device_ids)},
        )).mappings().all()
        for row in ifr:
            ifaces_by_device.setdefault(str(row["device_id"]), []).append(dict(row))

    # For each link's two endpoints, try to match the typed interface
    # name and collect (exporter_ip, ifindex) pairs we need traffic for.
    out: dict[str, dict] = {}
    flow_keys: set[tuple[str, int]] = set()
    snmp_keys: set[tuple[str, int]] = set()  # (device_id, if_index) for SNMP throughput
    for lr in link_rows:
        md = lr["metadata"] or {}
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        src_node = node_info.get(str(lr["source_node_id"]))
        dst_node = node_info.get(str(lr["target_node_id"]))

        def resolve(side_node, typed_iface):
            if not side_node:
                return _empty_iface()
            ifs = ifaces_by_device.get(str(side_node["device_id"])) or []
            matched = _match_interface(typed_iface, ifs)
            if not matched:
                return _empty_iface()
            payload = _iface_payload(matched)
            if payload["if_index"] is not None:
                if side_node["ip"]:
                    flow_keys.add((str(side_node["ip"]), int(payload["if_index"])))
                snmp_keys.add((str(side_node["device_id"]), int(payload["if_index"])))
            return payload

        out[str(lr["id"])] = {
            "source": resolve(src_node, md.get("src_interface")),
            "target": resolve(dst_node, md.get("dst_interface")),
            "_src_node": src_node,
            "_dst_node": dst_node,
        }

    # NetFlow query — one round-trip for every (exporter_ip, ifindex) we
    # need. If ClickHouse is unavailable or there are no matched interfaces
    # we just return matched=true with null throughput.
    flow_map: dict[tuple[str, int], dict] = {}
    if flow_keys:
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=_LIVE_WINDOW_SECONDS)
        ip_list = sorted({ip for ip, _ in flow_keys})
        idx_list = sorted({idx for _, idx in flow_keys})
        try:
            client = get_clickhouse_client()
            res = client.query(
                """
                SELECT toString(exporter_ip) AS exporter_ip,
                       ifindex,
                       sum(in_bytes)   AS in_bytes,
                       sum(out_bytes)  AS out_bytes,
                       sum(in_packets) AS in_packets,
                       sum(out_packets) AS out_packets
                FROM (
                    SELECT exporter_ip, input_snmp AS ifindex,
                           sum(bytes) AS in_bytes, 0 AS out_bytes,
                           sum(packets) AS in_packets, 0 AS out_packets
                    FROM zenplus.flow_records
                    WHERE timestamp BETWEEN %(start)s AND %(end)s
                      AND input_snmp != 0
                      AND toString(exporter_ip) IN %(ips)s
                      AND input_snmp IN %(idx)s
                    GROUP BY exporter_ip, input_snmp
                    UNION ALL
                    SELECT exporter_ip, output_snmp AS ifindex,
                           0 AS in_bytes, sum(bytes) AS out_bytes,
                           0 AS in_packets, sum(packets) AS out_packets
                    FROM zenplus.flow_records
                    WHERE timestamp BETWEEN %(start)s AND %(end)s
                      AND output_snmp != 0
                      AND toString(exporter_ip) IN %(ips)s
                      AND output_snmp IN %(idx)s
                    GROUP BY exporter_ip, output_snmp
                )
                GROUP BY exporter_ip, ifindex
                """,
                parameters={"start": start, "end": end, "ips": ip_list, "idx": idx_list},
            )
            for r in res.result_rows:
                flow_map[(str(r[0]), int(r[1]))] = {
                    "in_bytes": int(r[2] or 0),
                    "out_bytes": int(r[3] or 0),
                    "in_packets": int(r[4] or 0),
                    "out_packets": int(r[5] or 0),
                }
        except Exception:
            # ClickHouse may not be reachable or netflow may be disabled —
            # interface status still useful, just no throughput.
            flow_map = {}

    # SNMP interface counters — the universal throughput source. NetFlow only
    # exists for flow-exporting devices (firewalls/routers); SNMP polls every
    # monitored interface, so this lights up switch↔switch links too. Keyed by
    # (device_id, if_index); we take the most recent sample's pre-computed bps.
    snmp_map: dict[tuple[str, int], dict] = {}
    if snmp_keys:
        start = datetime.now(timezone.utc) - timedelta(seconds=_SNMP_LOOKBACK_SECONDS)
        did_list = sorted({d for d, _ in snmp_keys})
        sidx_list = sorted({i for _, i in snmp_keys})
        try:
            client = get_clickhouse_client()
            res = client.query(
                """
                SELECT toString(device_id) AS device_id, if_index,
                       argMax(in_bps, timestamp)  AS in_bps,
                       argMax(out_bps, timestamp) AS out_bps,
                       argMax(in_ucast_pkts, timestamp)  AS in_pkts,
                       argMax(out_ucast_pkts, timestamp) AS out_pkts
                FROM zenplus.snmp_if_metrics
                WHERE timestamp >= %(start)s
                  AND toString(device_id) IN %(dids)s
                  AND if_index IN %(idxs)s
                GROUP BY device_id, if_index
                """,
                parameters={"start": start, "dids": did_list, "idxs": sidx_list},
            )
            for r in res.result_rows:
                snmp_map[(str(r[0]), int(r[1]))] = {
                    "in_bps": float(r[2] or 0), "out_bps": float(r[3] or 0),
                    "in_packets": int(r[4] or 0), "out_packets": int(r[5] or 0),
                }
        except Exception:
            snmp_map = {}

    # Stitch throughput back into the result and drop internal fields.
    window = _LIVE_WINDOW_SECONDS
    final: dict[str, dict] = {}
    for lid, payload in out.items():
        for side_key, node_key in (("source", "_src_node"), ("target", "_dst_node")):
            side = payload[side_key]
            node = payload[node_key]
            if not side.get("matched") or not node:
                continue
            f = flow_map.get((str(node["ip"]), int(side["if_index"])))
            if f:
                # NetFlow: bytes over the window → bits/sec.
                in_bps = (f["in_bytes"] * 8) / window
                out_bps = (f["out_bytes"] * 8) / window
                side["in_bps"] = round(in_bps, 1)
                side["out_bps"] = round(out_bps, 1)
                side["in_packets"] = f["in_packets"]
                side["out_packets"] = f["out_packets"]
            else:
                # Fall back to SNMP interface counters (pre-computed bps).
                s = snmp_map.get((str(node["device_id"]), int(side["if_index"])))
                if not s:
                    continue
                in_bps = s["in_bps"]
                out_bps = s["out_bps"]
                side["in_bps"] = round(in_bps, 1)
                side["out_bps"] = round(out_bps, 1)
                side["in_packets"] = s["in_packets"]
                side["out_packets"] = s["out_packets"]
            if side.get("if_speed"):
                # Peak direction divided by line speed.
                util = max(in_bps, out_bps) / float(side["if_speed"]) * 100
                side["util_pct"] = round(max(0.0, min(util, 100.0)), 2)
        final[lid] = {
            "source": payload["source"],
            "target": payload["target"],
            "window_seconds": window,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    return {"data": final, "window_seconds": window,
            "generated_at": datetime.now(timezone.utc).isoformat()}


@router.get("/{map_id}/nodes-live")
async def nodes_live(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    """Live per-device health for every node on the map.

    Response: {"data": {<node_id>: {status, last_seen, rtt_ms, cpu_pct,
    mem_pct, uptime_seconds, temperature_c, alerts: {active, critical,
    warning}}}}

    Status/RTT come from the devices table, CPU/memory/uptime/temperature from
    the latest ClickHouse snmp_metrics samples (15-minute lookback), and alert
    counts from currently-active alerts. One call drives the NOC overlay.
    """
    node_rows = (await db.execute(
        text("""
            SELECT mn.id AS node_id, mn.device_id, d.status, d.last_seen, d.last_rtt_ms
            FROM manual_map_nodes mn
            JOIN devices d ON d.id = mn.device_id
            WHERE mn.map_id = :map_id
        """),
        {"map_id": map_id},
    )).mappings().all()
    if not node_rows:
        return {"data": {}, "generated_at": datetime.now(timezone.utc).isoformat()}

    device_ids = [str(r["device_id"]) for r in node_rows]

    # Active alert counts per device (severity split).
    alert_rows = (await db.execute(
        text("""
            SELECT device_id,
                   COUNT(*) AS active,
                   COUNT(*) FILTER (WHERE severity = 'critical') AS critical,
                   COUNT(*) FILTER (WHERE severity = 'warning') AS warning
            FROM alerts
            WHERE status = 'active' AND device_id = ANY(:ids)
            GROUP BY device_id
        """),
        {"ids": device_ids},
    )).mappings().all()
    alerts_by_device = {
        str(r["device_id"]): {"active": int(r["active"]), "critical": int(r["critical"]), "warning": int(r["warning"])}
        for r in alert_rows
    }

    # Latest scalar health metrics from ClickHouse (cpu %, memory %, uptime s,
    # hottest temperature sensor). Missing/old samples simply yield nulls.
    metrics_by_device: dict[str, dict] = {}
    try:
        client = get_clickhouse_client()
        res = client.query(
            """
            SELECT device_id, metric_key, argMax(value, timestamp) AS val
            FROM zenplus.snmp_metrics
            WHERE timestamp >= now() - INTERVAL 15 MINUTE
              AND device_id IN %(ids)s
              AND (metric_key IN ('cpu', 'memory', 'uptime') OR metric_key LIKE 'temperature%%')
            GROUP BY device_id, metric_key
            """,
            parameters={"ids": device_ids},
        )
        for did, key, val in res.result_rows:
            bucket = metrics_by_device.setdefault(str(did), {})
            v = float(val)
            if key == "cpu":
                bucket["cpu_pct"] = round(v, 1)
            elif key == "memory":
                bucket["mem_pct"] = round(v, 1)
            elif key == "uptime":
                bucket["uptime_seconds"] = round(v)
            elif key.startswith("temperature"):
                bucket["temperature_c"] = max(bucket.get("temperature_c") or -1e9, round(v, 1))
    except Exception:
        pass  # ClickHouse down → status/alerts still render

    data: dict[str, dict] = {}
    for r in node_rows:
        did = str(r["device_id"])
        m = metrics_by_device.get(did, {})
        data[str(r["node_id"])] = {
            "device_id": did,
            "status": r["status"],
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "rtt_ms": float(r["last_rtt_ms"]) if r["last_rtt_ms"] is not None else None,
            "cpu_pct": m.get("cpu_pct"),
            "mem_pct": m.get("mem_pct"),
            "uptime_seconds": m.get("uptime_seconds"),
            "temperature_c": m.get("temperature_c"),
            "alerts": alerts_by_device.get(did, {"active": 0, "critical": 0, "warning": 0}),
        }
    return {"data": data, "generated_at": datetime.now(timezone.utc).isoformat()}


# ─── Annotation shapes (rectangles, circles, text) ─────────────────────────
#
# Shapes are standalone canvas annotations not tied to any device. The frontend
# uses them as zone callouts, group boxes, and free-floating text labels.

@router.post("/{map_id}/shapes")
async def create_shape(
    map_id: uuid.UUID,
    data: ShapeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    if data.kind not in SHAPE_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {SHAPE_KINDS}")
    map_exists = (await db.execute(
        text("SELECT 1 FROM manual_maps WHERE id = :id"), {"id": map_id},
    )).first()
    if not map_exists:
        raise HTTPException(status_code=404, detail="Map not found")
    row = (await db.execute(
        text("""
            INSERT INTO manual_map_shapes (map_id, kind, x_pct, y_pct, w_pct, h_pct,
                                           text, fill, stroke, z_index, metadata)
            VALUES (:map_id, :kind, :x, :y, :w, :h, :text, :fill, :stroke, :z,
                    CAST(:metadata AS JSONB))
            RETURNING id, map_id, kind, x_pct, y_pct, w_pct, h_pct, text, fill, stroke,
                      z_index, metadata
        """),
        {
            "map_id": map_id, "kind": data.kind,
            "x": data.x_pct, "y": data.y_pct, "w": data.w_pct, "h": data.h_pct,
            "text": data.text, "fill": data.fill, "stroke": data.stroke,
            "z": data.z_index, "metadata": json.dumps(data.metadata or {}),
        },
    )).mappings().first()
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
    return _row_shape(row)


@router.put("/{map_id}/shapes/{shape_id}")
async def update_shape(
    map_id: uuid.UUID,
    shape_id: uuid.UUID,
    data: ShapeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "kind" in fields and fields["kind"] not in SHAPE_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {SHAPE_KINDS}")
    parts = ["updated_at = NOW()"]
    params = {"map_id": map_id, "shape_id": shape_id}
    for key, value in fields.items():
        if key == "metadata":
            parts.append("metadata = CAST(:metadata AS JSONB)")
            params["metadata"] = json.dumps(value or {})
        else:
            parts.append(f"{key} = :{key}")
            params[key] = value
    row = (await db.execute(
        text(f"""
            UPDATE manual_map_shapes
            SET {', '.join(parts)}
            WHERE id = :shape_id AND map_id = :map_id
            RETURNING id, map_id, kind, x_pct, y_pct, w_pct, h_pct, text, fill, stroke,
                      z_index, metadata
        """),
        params,
    )).mappings().first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Shape not found")
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
    return _row_shape(row)


@router.delete("/{map_id}/shapes/{shape_id}", status_code=204)
async def delete_shape(
    map_id: uuid.UUID,
    shape_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator_user),
):
    row = (await db.execute(
        text("DELETE FROM manual_map_shapes WHERE id = :shape_id AND map_id = :map_id RETURNING id"),
        {"shape_id": shape_id, "map_id": map_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Shape not found")
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})
    await db.commit()
    return None
