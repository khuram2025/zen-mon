from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.netflow import access as nf_access
from app.api.v1.netflow.common import RAW, table_ref
from app.api.v1.netflow.sql import EXPORTER, IF_IN, IF_OUT, VOLUME_GUARD, display
from app.core import scoping
from app.core.database import get_clickhouse_client, get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import map_topology as topo

logger = logging.getLogger("zenplus.manual_maps")

router = APIRouter(prefix="/maps", tags=["Manual Maps"])

# Snapshots kept per map (older ones are pruned when a new one is taken).
MAX_VERSIONS = 50
# "Build from site" refuses selections larger than this.
MAX_POPULATE = 400


class MapCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    metadata: Optional[dict] = None


class MapUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    # Holds background image, theme preset, snap-to-grid toggle, default
    # shape, and other per-map UI state. Stored verbatim as JSONB, except that
    # a payload without an ``annotation_links`` key keeps the stored ones
    # (those are edited through the atomic annotation-link endpoints).
    metadata: Optional[dict] = None


class NodeCreate(BaseModel):
    # Exactly one of the three entity ids.
    device_id: Optional[uuid.UUID] = None
    server_id: Optional[uuid.UUID] = None
    service_check_id: Optional[uuid.UUID] = None
    label: Optional[str] = None
    icon: str = Field(default="auto", max_length=40)
    x_pct: float = Field(default=50, ge=2, le=98)
    y_pct: float = Field(default=50, ge=2, le=98)
    # Carried over on duplicate so the copy keeps its styling (size, frame,
    # label style, label offset).
    metadata: Optional[dict] = None


class NodeUpdate(BaseModel):
    label: Optional[str] = None
    icon: Optional[str] = Field(default=None, max_length=40)
    x_pct: Optional[float] = Field(default=None, ge=2, le=98)
    y_pct: Optional[float] = Field(default=None, ge=2, le=98)
    # Swap which device this node represents ("change device profile") —
    # position, styling and links stay; interface labels are re-mapped by
    # the user afterwards via the link editor. Device nodes only.
    device_id: Optional[uuid.UUID] = None
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
    source_node_id: Optional[uuid.UUID] = None
    target_node_id: Optional[uuid.UUID] = None


class AnnotationLinkCreate(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1, max_length=80)
    source: str = Field(..., min_length=1, max_length=80)
    target: str = Field(..., min_length=1, max_length=80)
    source_type: str = Field(..., max_length=10)
    target_type: str = Field(..., max_length=10)
    label: Optional[str] = None
    link_type: Optional[str] = Field(default="manual", max_length=40)
    metadata: Optional[dict] = None


class AnnotationLinkUpdate(BaseModel):
    label: Optional[str] = None
    link_type: Optional[str] = Field(default=None, max_length=40)
    metadata: Optional[dict] = None
    source: Optional[str] = Field(default=None, min_length=1, max_length=80)
    target: Optional[str] = Field(default=None, min_length=1, max_length=80)
    source_type: Optional[str] = Field(default=None, max_length=10)
    target_type: Optional[str] = Field(default=None, max_length=10)


class AutoConnectBody(BaseModel):
    # Reviewed subset: [[source_node_id, target_node_id], ...] (either order).
    pairs: Optional[list[list[str]]] = None
    include_stale: bool = False


class PopulateBody(BaseModel):
    location: Optional[str] = None
    cidr: Optional[str] = None
    seed_device_id: Optional[uuid.UUID] = None
    depth: int = Field(default=2, ge=1, le=6)
    include_links: bool = True
    replace: bool = False


class ExpandBody(BaseModel):
    include_unmanaged: bool = True


class VersionCreate(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=120)


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


class BulkNode(NodeCreate):
    client_id: str = Field(..., min_length=1, max_length=80)

    # Backups and snapshots may carry an explicit null icon.
    @field_validator("icon", mode="before")
    @classmethod
    def _icon_default(cls, v):
        return v or "auto"


class BulkShape(ShapeCreate):
    client_id: str = Field(..., min_length=1, max_length=80)


class BulkLink(BaseModel):
    source: str = Field(..., min_length=1, max_length=80)
    target: str = Field(..., min_length=1, max_length=80)
    label: Optional[str] = None
    link_type: str = Field(default="manual", max_length=40)
    metadata: Optional[dict] = None

    @field_validator("link_type", mode="before")
    @classmethod
    def _type_default(cls, v):
        return v or "manual"


class BulkAnnotationLink(AnnotationLinkCreate):
    # Legacy cables may lack the endpoint types; they are inferred from the ids.
    source_type: Optional[str] = Field(default=None, max_length=10)
    target_type: Optional[str] = Field(default=None, max_length=10)


class BulkCreate(BaseModel):
    nodes: list[BulkNode] = Field(default_factory=list)
    shapes: list[BulkShape] = Field(default_factory=list)
    links: list[BulkLink] = Field(default_factory=list)
    annotation_links: list[BulkAnnotationLink] = Field(default_factory=list)


class BulkDelete(BaseModel):
    node_ids: list[uuid.UUID] = Field(default_factory=list)
    shape_ids: list[uuid.UUID] = Field(default_factory=list)
    link_ids: list[uuid.UUID] = Field(default_factory=list)
    annotation_link_ids: list[str] = Field(default_factory=list)


# ─── Row shaping ───────────────────────────────────────────────────────────

def _md(value: Any) -> dict:
    """A JSONB value as a dict (asyncpg may hand back a str)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def _row_map(row) -> dict:
    md = row["metadata"] if "metadata" in row.keys() else None
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "metadata": _md(md),
    }


_ENTITY_FIELDS = ("device_id", "server_id", "service_check_id")


def _row_node(row, *, raw_label: bool = False) -> dict:
    ids = {k: (str(row[k]) if row[k] is not None else None) for k in _ENTITY_FIELDS}
    return {
        "id": str(row["id"]),
        "map_id": str(row["map_id"]),
        "kind": row["kind"],
        **ids,
        "entity_id": ids["device_id"] or ids["server_id"] or ids["service_check_id"],
        "label": row["label"] if raw_label else (row["label"] or row["hostname"]),
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
        "metadata": _md(row["metadata"]),
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
        "metadata": _md(row["metadata"]),
    }


def _row_link(row) -> dict:
    return {
        "id": str(row["id"]),
        "map_id": str(row["map_id"]),
        "source_node_id": str(row["source_node_id"]),
        "target_node_id": str(row["target_node_id"]),
        "label": row["label"],
        "link_type": row["link_type"],
        "metadata": _md(row["metadata"]),
    }


# A node is a device, a monitored server or a service check (migrate-167).
# Servers and checks are shaped like devices so the canvas renders them the
# same way: status is mapped onto the device vocabulary.
_NODE_JOINS = """
    LEFT JOIN devices d ON d.id = mn.device_id
    LEFT JOIN servers s ON s.id = mn.server_id
    LEFT JOIN service_checks sc ON sc.id = mn.service_check_id
"""

_NODE_STATUS_SQL = """
    CASE
        WHEN mn.device_id IS NOT NULL THEN COALESCE(d.status, 'unknown')
        WHEN mn.server_id IS NOT NULL THEN
            CASE s.status
                WHEN 'healthy' THEN 'up'
                WHEN 'warning' THEN 'degraded'
                WHEN 'critical' THEN 'down'
                WHEN 'disabled' THEN 'maintenance'
                ELSE 'unknown'
            END
        ELSE
            CASE COALESCE(sc.status, 'unknown')
                WHEN 'warning' THEN 'degraded'
                ELSE COALESCE(sc.status, 'unknown')
            END
    END
"""

_NODE_KIND_SQL = """
    CASE WHEN mn.device_id IS NOT NULL THEN 'device'
         WHEN mn.server_id IS NOT NULL THEN 'server'
         ELSE 'service_check' END
"""

_NODE_SELECT = f"""
    SELECT mn.id, mn.map_id, mn.device_id, mn.server_id, mn.service_check_id,
           mn.label, mn.icon, mn.x_pct, mn.y_pct, mn.metadata,
           {_NODE_KIND_SQL} AS kind,
           COALESCE(d.hostname, s.display_name, sc.name) AS hostname,
           COALESCE(host(d.ip_address), host(s.primary_ip), sc.target_host) AS ip_address,
           CASE WHEN mn.device_id IS NOT NULL THEN d.device_type
                WHEN mn.server_id IS NOT NULL THEN 'server'
                ELSE 'service_check' END AS device_type,
           {_NODE_STATUS_SQL} AS status,
           d.location, d.vendor,
           CASE WHEN mn.device_id IS NOT NULL THEN d.model
                WHEN mn.server_id IS NOT NULL THEN s.os_name
                ELSE sc.check_type END AS model,
           CASE WHEN mn.device_id IS NOT NULL THEN d.last_seen
                WHEN mn.server_id IS NOT NULL THEN s.last_seen
                ELSE sc.last_check_at END AS last_seen
    FROM manual_map_nodes mn
    {_NODE_JOINS}
"""

_ENTITY_TABLES = {
    "device_id": ("devices", "Device not found"),
    "server_id": ("servers", "Server not found"),
    "service_check_id": ("service_checks", "Service check not found"),
}


def _entity_of(payload: dict) -> tuple[str, Any]:
    chosen = [(k, payload.get(k)) for k in _ENTITY_FIELDS if payload.get(k) is not None]
    if len(chosen) != 1:
        raise HTTPException(status_code=400,
                            detail="Exactly one of device_id, server_id or service_check_id is required")
    return chosen[0]


async def _require_entity(db: AsyncSession, field: str, value: Any) -> None:
    table, missing = _ENTITY_TABLES[field]
    found = (await db.execute(text(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": value})).first()
    if not found:
        raise HTTPException(status_code=404, detail=missing)


async def _insert_node(db: AsyncSession, map_id, *, field: str, entity_id, label=None, icon="auto",
                       x_pct=50.0, y_pct=50.0, metadata=None, node_id=None) -> Optional[str]:
    """Insert one node; with ``node_id`` keep that id (None when it is taken)."""
    params = {
        "map_id": map_id, "eid": entity_id, "label": label, "icon": icon or "auto",
        "x": x_pct, "y": y_pct, "metadata": json.dumps(metadata or {}), "id": node_id,
    }
    id_col, id_val, conflict = ("id, ", ":id, ", " ON CONFLICT (id) DO NOTHING") if node_id else ("", "", "")
    row = (await db.execute(
        text(f"""
            INSERT INTO manual_map_nodes ({id_col}map_id, {field}, label, icon, x_pct, y_pct, metadata)
            VALUES ({id_val}:map_id, :eid, :label, :icon, :x, :y, CAST(:metadata AS JSONB)){conflict}
            RETURNING id
        """),
        params,
    )).first()
    return str(row.id) if row else None


async def _insert_link(db: AsyncSession, map_id, src, dst, *, label=None, link_type="manual",
                       metadata=None, link_id=None) -> Optional[str]:
    params = {"map_id": map_id, "src": src, "dst": dst, "label": label, "lt": link_type or "manual",
              "md": json.dumps(metadata or {}), "id": link_id}
    id_col, id_val, conflict = ("id, ", ":id, ", " ON CONFLICT (id) DO NOTHING") if link_id else ("", "", "")
    row = (await db.execute(
        text(f"""
            INSERT INTO manual_map_links ({id_col}map_id, source_node_id, target_node_id, label, link_type, metadata)
            VALUES ({id_val}:map_id, :src, :dst, :label, :lt, CAST(:md AS JSONB)){conflict}
            RETURNING id
        """),
        params,
    )).first()
    return str(row.id) if row else None


async def _insert_shape(db: AsyncSession, map_id, spec: dict, shape_id=None) -> Optional[str]:
    params = {
        "map_id": map_id, "kind": spec["kind"],
        "x": spec.get("x_pct", 50), "y": spec.get("y_pct", 50),
        "w": spec.get("w_pct", 14), "h": spec.get("h_pct", 8),
        "text": spec.get("text"), "fill": spec.get("fill"), "stroke": spec.get("stroke"),
        "z": spec.get("z_index") or 0, "metadata": json.dumps(spec.get("metadata") or {}), "id": shape_id,
    }
    id_col, id_val, conflict = ("id, ", ":id, ", " ON CONFLICT (id) DO NOTHING") if shape_id else ("", "", "")
    row = (await db.execute(
        text(f"""
            INSERT INTO manual_map_shapes ({id_col}map_id, kind, x_pct, y_pct, w_pct, h_pct,
                                           text, fill, stroke, z_index, metadata)
            VALUES ({id_val}:map_id, :kind, :x, :y, :w, :h, :text, :fill, :stroke, :z,
                    CAST(:metadata AS JSONB)){conflict}
            RETURNING id
        """),
        params,
    )).first()
    return str(row.id) if row else None


# ─── Map row lock + annotation links ───────────────────────────────────────
#
# Annotation links (cables with a shape at one end, or drawn between any two
# canvas items) live in manual_maps.metadata.annotation_links. Every edit
# locks the map row, edits the array in Python and writes it back in the same
# transaction, so two editors can no longer overwrite each other's cables.

async def _lock_map(db: AsyncSession, map_id) -> dict:
    row = (await db.execute(
        text("SELECT metadata FROM manual_maps WHERE id = :id FOR UPDATE"), {"id": map_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Map not found")
    return _md(row["metadata"])


async def _touch(db: AsyncSession, map_id) -> None:
    await db.execute(text("UPDATE manual_maps SET updated_at = NOW() WHERE id = :id"), {"id": map_id})


async def _save_map_metadata(db: AsyncSession, map_id, md: dict) -> None:
    await db.execute(
        text("UPDATE manual_maps SET metadata = CAST(:md AS JSONB), updated_at = NOW() WHERE id = :id"),
        {"id": map_id, "md": json.dumps(md)},
    )


def _ann_links(md: dict) -> list:
    links = md.get("annotation_links")
    return list(links) if isinstance(links, list) else []


def _prune_ann(md: dict, removed_ids: Iterable[str]) -> tuple[dict, int]:
    """Drop annotation links touching any of ``removed_ids``."""
    removed = {str(i) for i in removed_ids}
    links = md.get("annotation_links")
    if not removed or not isinstance(links, list):
        return md, 0
    keep = [al for al in links
            if not (isinstance(al, dict) and (str(al.get("source")) in removed or str(al.get("target")) in removed))]
    dropped = len(links) - len(keep)
    if dropped:
        md = {**md, "annotation_links": keep}
    return md, dropped


async def _endpoint_sets(db: AsyncSession, map_id) -> tuple[set[str], set[str]]:
    nodes = {str(r[0]) for r in (await db.execute(
        text("SELECT id FROM manual_map_nodes WHERE map_id = :m"), {"m": map_id})).all()}
    shapes = {str(r[0]) for r in (await db.execute(
        text("SELECT id FROM manual_map_shapes WHERE map_id = :m"), {"m": map_id})).all()}
    return nodes, shapes


def _validate_ann(al: dict, nodes: set[str], shapes: set[str]) -> None:
    for side in ("source", "target"):
        kind = al.get(f"{side}_type")
        value = al.get(side)
        if kind not in ("node", "shape"):
            raise HTTPException(status_code=400, detail=f"{side}_type must be 'node' or 'shape'")
        if not value:
            raise HTTPException(status_code=400, detail=f"{side} is required")
        if str(value) not in (nodes if kind == "node" else shapes):
            raise HTTPException(status_code=400, detail=f"{side} {kind} not found on this map")
    if str(al["source"]) == str(al["target"]):
        raise HTTPException(status_code=400, detail="Source and target must be different")


def _new_ann_id() -> str:
    return f"al-{uuid.uuid4().hex[:16]}"


def _ann_record(data: dict, al_id: str) -> dict:
    return {
        "id": al_id,
        "source": str(data["source"]),
        "target": str(data["target"]),
        "source_type": data["source_type"],
        "target_type": data["target_type"],
        "label": data.get("label"),
        "link_type": data.get("link_type") or "manual",
        "metadata": data.get("metadata") or {},
    }


# ─── Maps ──────────────────────────────────────────────────────────────────

@router.get("")
async def list_maps(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
):
    rows = (await db.execute(
        text(f"""
            SELECT m.id, m.name, m.description, m.metadata, m.created_at, m.updated_at,
                   COALESCE(n.node_count, 0) AS node_count,
                   COALESCE(l.link_count, 0) AS link_count,
                   COALESCE(st.down_count, 0) AS down_count,
                   COALESCE(st.degraded_count, 0) AS degraded_count,
                   COALESCE(st.up_count, 0) AS up_count,
                   COALESCE(st.unknown_count, 0) AS unknown_count,
                   COALESCE(st.maintenance_count, 0) AS maintenance_count
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
                SELECT ns.map_id,
                       COUNT(*) FILTER (WHERE ns.status = 'down') AS down_count,
                       COUNT(*) FILTER (WHERE ns.status = 'degraded') AS degraded_count,
                       COUNT(*) FILTER (WHERE ns.status = 'up') AS up_count,
                       COUNT(*) FILTER (WHERE ns.status = 'unknown') AS unknown_count,
                       COUNT(*) FILTER (WHERE ns.status = 'maintenance') AS maintenance_count
                FROM (
                    SELECT mn.map_id, {_NODE_STATUS_SQL} AS status
                    FROM manual_map_nodes mn
                    {_NODE_JOINS}
                ) ns
                GROUP BY ns.map_id
            ) st ON st.map_id = m.id
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
    user: User = Depends(require_permission("maps.manage")),
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


# Declared before /{map_id} so "populate" is not parsed as a map id.
@router.get("/populate/options")
async def populate_options(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
):
    """Device locations offered by the "Build from site" dialog — only from
    the devices the caller may see."""
    scope = await scoping.visible_tags(db, user)
    where, params = "location IS NOT NULL AND btrim(location) <> ''", {}
    if scope is not None:
        where += f" AND {scoping.jsonb_tags_visible('tags')}"
        params[scoping.SCOPE_PARAM] = scope
    rows = (await db.execute(text(f"""
        SELECT btrim(location) AS name, COUNT(*) AS device_count
        FROM devices
        WHERE {where}
        GROUP BY btrim(location)
        ORDER BY lower(btrim(location))
    """), params)).mappings().all()
    return {"locations": [{"name": r["name"], "device_count": int(r["device_count"])} for r in rows]}


@router.get("/{map_id}")
async def get_map(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
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
        text(f"""
            {_NODE_SELECT}
            WHERE mn.map_id = :map_id
            ORDER BY mn.created_at, COALESCE(d.hostname, s.display_name, sc.name)
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
    user: User = Depends(require_permission("maps.manage")),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    parts = ["updated_at = NOW()"]
    params = {"id": map_id}
    for key, value in fields.items():
        if key == "metadata":
            value = value or {}
            params["metadata"] = json.dumps(value)
            if "annotation_links" in value:
                # Legacy clients send the whole array: replace verbatim.
                parts.append("metadata = CAST(:metadata AS JSONB)")
            else:
                # Keep the stored annotation links. One UPDATE statement, so a
                # concurrent annotation-link edit is re-read, never lost.
                parts.append("""metadata = CASE
                    WHEN manual_maps.metadata -> 'annotation_links' IS NOT NULL
                    THEN CAST(:metadata AS JSONB)
                         || jsonb_build_object('annotation_links', manual_maps.metadata -> 'annotation_links')
                    ELSE CAST(:metadata AS JSONB) END""")
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
    user: User = Depends(require_permission("maps.manage")),
):
    row = (await db.execute(text("DELETE FROM manual_maps WHERE id = :id RETURNING id"), {"id": map_id})).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Map not found")
    await db.commit()


# ─── Nodes ─────────────────────────────────────────────────────────────────

@router.post("/{map_id}/nodes", status_code=201)
async def create_node(
    map_id: uuid.UUID,
    data: NodeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(status_code=404, detail="Map not found")
    payload = data.model_dump()
    field, entity_id = _entity_of(payload)
    await _require_entity(db, field, entity_id)
    node_id = await _insert_node(
        db, map_id, field=field, entity_id=entity_id, label=data.label, icon=data.icon,
        x_pct=data.x_pct, y_pct=data.y_pct, metadata=data.metadata,
    )
    await _touch(db, map_id)
    await db.commit()
    return {"id": node_id}


@router.put("/{map_id}/nodes/{node_id}")
async def update_node(
    map_id: uuid.UUID,
    node_id: uuid.UUID,
    data: NodeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "device_id" in fields:
        if fields["device_id"] is None:
            raise HTTPException(status_code=400, detail="device_id cannot be cleared")
        current = (await db.execute(
            text("SELECT device_id FROM manual_map_nodes WHERE id = :node_id AND map_id = :map_id"),
            {"node_id": node_id, "map_id": map_id},
        )).first()
        if not current:
            raise HTTPException(status_code=404, detail="Node not found")
        if current.device_id is None:
            raise HTTPException(status_code=400, detail="Only device nodes can change device")
        await _require_entity(db, "device_id", fields["device_id"])
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
    await _touch(db, map_id)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/{map_id}/nodes/{node_id}", status_code=204)
async def delete_node(
    map_id: uuid.UUID,
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    md = await _lock_map(db, map_id)
    row = (await db.execute(
        text("DELETE FROM manual_map_nodes WHERE id = :node_id AND map_id = :map_id RETURNING id"),
        {"node_id": node_id, "map_id": map_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Node not found")
    md, dropped = _prune_ann(md, [str(node_id)])
    if dropped:
        await _save_map_metadata(db, map_id, md)
    else:
        await _touch(db, map_id)
    await db.commit()


# ─── Links ─────────────────────────────────────────────────────────────────

@router.post("/{map_id}/links", status_code=201)
async def create_link(
    map_id: uuid.UUID,
    data: LinkCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
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
    link_id = await _insert_link(
        db, map_id, data.source_node_id, data.target_node_id,
        label=data.label, link_type=data.link_type, metadata=data.metadata,
    )
    await _touch(db, map_id)
    await db.commit()
    return {"id": link_id}


@router.put("/{map_id}/links/{link_id}")
async def update_link(
    map_id: uuid.UUID,
    link_id: uuid.UUID,
    data: LinkUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "source_node_id" in fields or "target_node_id" in fields:
        cur = (await db.execute(
            text("SELECT source_node_id, target_node_id FROM manual_map_links WHERE id = :link_id AND map_id = :map_id"),
            {"link_id": link_id, "map_id": map_id},
        )).mappings().first()
        if not cur:
            raise HTTPException(status_code=404, detail="Link not found")
        src = fields.get("source_node_id", cur["source_node_id"])
        tgt = fields.get("target_node_id", cur["target_node_id"])
        if src == tgt:
            raise HTTPException(status_code=400, detail="Source and target must be different")
        node_rows = (await db.execute(
            text("""
                SELECT id FROM manual_map_nodes
                WHERE map_id = :map_id
                  AND (id = :source_node_id OR id = :target_node_id)
            """),
            {"map_id": map_id, "source_node_id": src, "target_node_id": tgt},
        )).all()
        if len(node_rows) != 2:
            raise HTTPException(status_code=400, detail="Both nodes must belong to this map")
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
    await _touch(db, map_id)
    await db.commit()
    return {"id": str(row.id)}


@router.delete("/{map_id}/links/{link_id}", status_code=204)
async def delete_link(
    map_id: uuid.UUID,
    link_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    row = (await db.execute(
        text("DELETE FROM manual_map_links WHERE id = :link_id AND map_id = :map_id RETURNING id"),
        {"link_id": link_id, "map_id": map_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Link not found")
    await _touch(db, map_id)
    await db.commit()


# ─── Annotation links (atomic edits of metadata.annotation_links) ──────────

@router.post("/{map_id}/annotation-links", status_code=201)
async def create_annotation_link(
    map_id: uuid.UUID,
    data: AnnotationLinkCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    md = await _lock_map(db, map_id)
    payload = data.model_dump()
    nodes, shapes = await _endpoint_sets(db, map_id)
    _validate_ann(payload, nodes, shapes)
    links = _ann_links(md)
    existing = {str(al.get("id")) for al in links if isinstance(al, dict)}
    al_id = payload.get("id") or _new_ann_id()
    if al_id in existing:
        raise HTTPException(status_code=409, detail="Annotation link already exists")
    record = _ann_record(payload, al_id)
    links.append(record)
    await _save_map_metadata(db, map_id, {**md, "annotation_links": links})
    await db.commit()
    return record


@router.put("/{map_id}/annotation-links/{al_id}")
async def update_annotation_link(
    map_id: uuid.UUID,
    al_id: str,
    data: AnnotationLinkUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    md = await _lock_map(db, map_id)
    links = _ann_links(md)
    idx = next((i for i, al in enumerate(links) if isinstance(al, dict) and str(al.get("id")) == al_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Annotation link not found")
    updated = {**links[idx]}
    for key, value in fields.items():
        if key in ("source", "target", "source_type", "target_type") and value is None:
            raise HTTPException(status_code=400, detail=f"{key} cannot be cleared")
        updated[key] = value
    if updated.get("metadata") is None:
        updated["metadata"] = {}
    if any(k in fields for k in ("source", "target", "source_type", "target_type")):
        nodes, shapes = await _endpoint_sets(db, map_id)
        _validate_ann(updated, nodes, shapes)
    links[idx] = updated
    await _save_map_metadata(db, map_id, {**md, "annotation_links": links})
    await db.commit()
    return updated


@router.delete("/{map_id}/annotation-links/{al_id}", status_code=204)
async def delete_annotation_link(
    map_id: uuid.UUID,
    al_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    md = await _lock_map(db, map_id)
    links = _ann_links(md)
    keep = [al for al in links if not (isinstance(al, dict) and str(al.get("id")) == al_id)]
    if len(keep) == len(links):
        raise HTTPException(status_code=404, detail="Annotation link not found")
    await _save_map_metadata(db, map_id, {**md, "annotation_links": keep})
    await db.commit()


# ─────────────────────────────────────────────────────────────────
# LLDP/CDP link assistance
#
# Cross-reference the devices currently placed on the map against the
# discovered topology (topology_links). When two placed devices have a
# real LLDP/CDP adjacency but no manual link yet, surface it as a
# "suggested" link the operator can add with one click — pre-bound to
# the correct interface pair (the devices' own interface names) so it
# inherits live weathermap stats. Old adjacencies (rows are never
# deleted) and unused ports are reported as stale and hidden by default;
# see app.services.map_topology.
# ─────────────────────────────────────────────────────────────────

async def _load_topology(db: AsyncSession, device_ids: Iterable) -> tuple[list[dict], dict[str, list[dict]], dict[str, str]]:
    """topology_links rows touching ``device_ids`` plus the interface tables
    and hostnames of every device those rows reference."""
    ids = sorted({str(i) for i in device_ids if i})
    if not ids:
        return [], {}, {}
    edges = []
    for r in (await db.execute(
        text("""
            SELECT local_device_id, local_if_index, local_if_name, remote_device_id,
                   remote_chassis_id, remote_port_id, remote_hostname, remote_if_name,
                   protocol, confidence, last_seen_at, metadata
            FROM topology_links
            WHERE local_device_id = ANY(:ids) OR remote_device_id = ANY(:ids)
        """),
        {"ids": ids},
    )).mappings().all():
        e = dict(r)
        e["local_device_id"] = str(e["local_device_id"])
        e["remote_device_id"] = str(e["remote_device_id"]) if e["remote_device_id"] is not None else None
        e["metadata"] = _md(e.get("metadata"))
        edges.append(e)
    every = set(ids)
    for e in edges:
        every.add(e["local_device_id"])
        if e["remote_device_id"]:
            every.add(e["remote_device_id"])
    every_list = sorted(every)
    ifaces: dict[str, list[dict]] = {}
    for r in (await db.execute(
        text("""
            SELECT device_id, if_index, if_name, if_descr, if_alias, if_speed,
                   configured_speed_bps, admin_status, oper_status, last_up_at,
                   mac_address::text AS mac_address
            FROM device_interfaces
            WHERE device_id = ANY(:ids)
        """),
        {"ids": every_list},
    )).mappings().all():
        ifaces.setdefault(str(r["device_id"]), []).append(dict(r))
    hostnames = {str(r[0]): r[1] for r in (await db.execute(
        text("SELECT id, hostname FROM devices WHERE id = ANY(:ids)"), {"ids": every_list},
    )).all()}
    return edges, ifaces, hostnames


async def _placed_device_nodes(db: AsyncSession, map_id) -> dict[str, dict]:
    """device_id → {node_id, hostname, device_type, x_pct, y_pct} (first placement wins)."""
    rows = (await db.execute(
        text("""
            SELECT mn.id AS node_id, mn.device_id, mn.x_pct, mn.y_pct, d.hostname, d.device_type
            FROM manual_map_nodes mn JOIN devices d ON d.id = mn.device_id
            WHERE mn.map_id = :map_id
            ORDER BY mn.created_at, mn.id
        """),
        {"map_id": map_id},
    )).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(str(r["device_id"]), {
            "node_id": str(r["node_id"]), "hostname": r["hostname"], "device_type": r["device_type"],
            "x_pct": float(r["x_pct"]), "y_pct": float(r["y_pct"]),
        })
    return out


async def _linked_node_pairs(db: AsyncSession, map_id) -> set[frozenset]:
    rows = (await db.execute(
        text("SELECT source_node_id, target_node_id FROM manual_map_links WHERE map_id = :map_id"),
        {"map_id": map_id},
    )).all()
    return {frozenset((str(a), str(b))) for a, b in rows}


def _orient(a: str, b: str, hostnames: dict) -> tuple[str, str]:
    """Stable source/target order for a device pair: by hostname, then id."""
    return tuple(sorted((a, b), key=lambda d: ((hostnames.get(d) or "").lower(), d)))  # type: ignore[return-value]


async def _compute_suggested_links(db: AsyncSession, map_id: uuid.UUID) -> list[dict]:
    """Discovered adjacencies among placed devices that aren't linked yet,
    one per unordered device pair, stale ones included (flagged)."""
    placed = await _placed_device_nodes(db, map_id)
    if len(placed) < 2:
        return []
    linked = await _linked_node_pairs(db, map_id)
    edges, ifaces, hostnames = await _load_topology(db, placed.keys())
    edges = [e for e in edges if e["local_device_id"] in placed and e["remote_device_id"] in placed]
    adjacency = topo.build_adjacencies(edges, ifaces, hostnames)
    hosts = {d: p["hostname"] for d, p in placed.items()}
    suggestions = []
    for key, members in adjacency.items():
        a, b = tuple(key)
        if a not in placed or b not in placed:
            continue
        src, dst = _orient(a, b, hosts)
        if frozenset((placed[src]["node_id"], placed[dst]["node_id"])) in linked:
            continue
        suggestions.append({
            "source_node_id": placed[src]["node_id"],
            "target_node_id": placed[dst]["node_id"],
            "source_hostname": placed[src]["hostname"],
            "target_hostname": placed[dst]["hostname"],
            **topo.summarize_pair(members, src),
        })
    suggestions.sort(key=lambda s: (s["stale"], -(s["confidence"] or 0),
                                    (s["source_hostname"] or "").lower(), (s["target_hostname"] or "").lower()))
    return suggestions


@router.get("/{map_id}/suggested-links")
async def suggested_links(
    map_id: uuid.UUID,
    include_stale: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
):
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(404, "Map not found")
    every = await _compute_suggested_links(db, map_id)
    stale_count = sum(1 for s in every if s["stale"])
    data = every if include_stale else [s for s in every if not s["stale"]]
    return {"data": data, "count": len(data), "stale_count": stale_count}


async def _create_discovered_link(db: AsyncSession, map_id, src_node: str, dst_node: str,
                                  summary: dict) -> Optional[str]:
    """A link from a discovered adjacency (no label, straight, kind by speed).
    Savepoint per insert: a single bad row doesn't poison the batch."""
    md = topo.link_metadata(summary)
    try:
        async with db.begin_nested():
            return await _insert_link(db, map_id, src_node, dst_node, label=None,
                                      link_type=md["kind"], metadata=md)
    except Exception:
        logger.exception("could not create discovered link %s -> %s on map %s", src_node, dst_node, map_id)
        return None


@router.post("/{map_id}/auto-connect")
async def auto_connect_discovered(
    map_id: uuid.UUID,
    data: Optional[AutoConnectBody] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    """Create manual links for discovered adjacencies among placed devices
    that aren't linked yet (all fresh ones, or the reviewed ``pairs``). Each
    link is bound to its interface pair so it lights up the live weathermap
    immediately."""
    body = data or AutoConnectBody()
    map_exists = (await db.execute(text("SELECT id FROM manual_maps WHERE id = :id"), {"id": map_id})).first()
    if not map_exists:
        raise HTTPException(404, "Map not found")
    every = await _compute_suggested_links(db, map_id)
    candidates = every if body.include_stale else [s for s in every if not s["stale"]]
    skipped_stale = len(every) - len(candidates)
    todo = candidates
    if body.pairs is not None:
        wanted = set()
        for pair in body.pairs:
            if len(pair) != 2:
                raise HTTPException(status_code=400, detail="Each pair must be [source_node_id, target_node_id]")
            wanted.add(frozenset(str(p) for p in pair))
        todo = [s for s in candidates if frozenset((s["source_node_id"], s["target_node_id"])) in wanted]
    created_ids: list[str] = []
    for s in todo:
        link_id = await _create_discovered_link(db, map_id, s["source_node_id"], s["target_node_id"], s)
        if link_id:
            created_ids.append(link_id)
    if created_ids:
        await _touch(db, map_id)
    await db.commit()
    return {"created": len(created_ids), "created_ids": created_ids,
            "suggested": len(candidates), "skipped_stale": skipped_stale}


# ─── Build from site (populate) and add neighbours (expand) ───────────────

async def _device_rows(db: AsyncSession, where: str, params: dict,
                       scope: Optional[list[str]] = None) -> dict[str, dict]:
    """Devices matching ``where`` that the caller may see (``scope`` from
    scoping.visible_tags; None = unrestricted)."""
    if scope is not None:
        where = f"({where}) AND {scoping.jsonb_tags_visible('tags')}"
        params = {**params, scoping.SCOPE_PARAM: scope}
    rows = (await db.execute(
        text(f"SELECT id, hostname, device_type FROM devices WHERE {where}"), params,
    )).mappings().all()
    return {str(r["id"]): {"id": str(r["id"]), "hostname": r["hostname"], "device_type": r["device_type"]}
            for r in rows}


@router.post("/{map_id}/populate")
async def populate_map(
    map_id: uuid.UUID,
    data: PopulateBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    """Place every device of a location / CIDR / seed neighbourhood on the
    map in a tiered layout and connect fresh discovered adjacencies.

    Selectors are combined (union). A version snapshot is taken first.
    ``replace`` removes the map's current nodes (and their links and
    annotation links) before placing; shapes are kept.
    """
    location = (data.location or "").strip() or None
    cidr = (data.cidr or "").strip() or None
    if not (location or cidr or data.seed_device_id):
        raise HTTPException(status_code=400, detail="Choose a location, a CIDR or a seed device")
    network = None
    if cidr:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid CIDR")
        if network.prefixlen < (8 if network.version == 4 else 32):
            raise HTTPException(status_code=400, detail="The subnet is too wide — use /8 or narrower (IPv6: /32)")
    # Only devices the caller may see are placed (the user's tag scope).
    scope = await scoping.visible_tags(db, user)
    md = await _lock_map(db, map_id)

    selected: dict[str, dict] = {}
    if location:
        selected.update(await _device_rows(db, "lower(btrim(location)) = lower(:loc)", {"loc": location}, scope))
    if network is not None:
        selected.update(await _device_rows(db, "ip_address <<= CAST(:net AS inet)", {"net": str(network)}, scope))
    if data.seed_device_id:
        seed = str(data.seed_device_id)
        if not await _device_rows(db, "id = :id", {"id": data.seed_device_id}, scope):
            raise HTTPException(status_code=404, detail="Seed device not found")
        seen = {seed}
        frontier = {seed}
        for _ in range(data.depth):
            edges, ifaces, hosts = await _load_topology(db, frontier)
            adjacency = topo.build_adjacencies(edges, ifaces, hosts)
            nxt: set[str] = set()
            for key in topo.fresh_pairs(adjacency):
                a, b = tuple(key)
                for x, y in ((a, b), (b, a)):
                    if x in frontier and y not in seen:
                        nxt.add(y)
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        selected.update(await _device_rows(db, "id = ANY(:ids)", {"ids": sorted(seen)}, scope))
    if not selected:
        raise HTTPException(status_code=400, detail="No devices match the selection")
    if len(selected) > MAX_POPULATE:
        raise HTTPException(status_code=400,
                            detail=f"{len(selected)} devices match; narrow the selection to {MAX_POPULATE} or fewer")

    version = await _create_version(db, map_id, user.id, "Before build from site")

    if data.replace:
        removed = [str(r[0]) for r in (await db.execute(
            text("DELETE FROM manual_map_nodes WHERE map_id = :m RETURNING id"), {"m": map_id})).all()]
        md, _ = _prune_ann(md, removed)

    existing = await _placed_device_nodes(db, map_id)
    edges, ifaces, hostnames = await _load_topology(db, set(selected) | set(existing))
    adjacency = topo.build_adjacencies(edges, ifaces, hostnames)
    fresh = topo.fresh_pairs(adjacency)
    chosen = set(selected)
    positions, _all_tiers, tier_of = topo.tiered_layout(
        list(selected.values()), [p for p in fresh if p <= chosen])

    new_nodes: dict[str, str] = {}
    tiers: dict[str, int] = {}
    skipped = 0
    for did in sorted(selected, key=lambda d: ((selected[d]["hostname"] or "").lower(), d)):
        if did in existing:
            skipped += 1
            continue
        x, y = positions[did]
        new_nodes[did] = await _insert_node(db, map_id, field="device_id", entity_id=did, x_pct=x, y_pct=y)
        name = topo.tier_name(tier_of.get(did))
        tiers[name] = tiers.get(name, 0) + 1

    links = 0
    if data.include_links and new_nodes:
        node_of = {d: p["node_id"] for d, p in existing.items()}
        node_of.update(new_nodes)
        hosts = {d: hostnames.get(d) or (selected.get(d) or {}).get("hostname") for d in node_of}
        linked = await _linked_node_pairs(db, map_id)
        for key in sorted(fresh, key=lambda k: sorted(k)):
            a, b = tuple(key)
            if a not in node_of or b not in node_of or (a not in new_nodes and b not in new_nodes):
                continue
            if frozenset((node_of[a], node_of[b])) in linked:
                continue
            src, dst = _orient(a, b, hosts)
            if await _create_discovered_link(db, map_id, node_of[src], node_of[dst],
                                             topo.summarize_pair(adjacency[key], src)):
                links += 1
    await _save_map_metadata(db, map_id, md)
    await db.commit()
    return {"placed": len(new_nodes), "links": links, "skipped_existing": skipped,
            "version_id": version["id"], "tiers": tiers}


def _unmanaged_ident(value: dict) -> str:
    return str(value.get("chassis_id") or value.get("hostname") or "").strip().lower()


@router.post("/{map_id}/nodes/{node_id}/expand")
async def expand_node(
    map_id: uuid.UUID,
    node_id: uuid.UUID,
    data: Optional[ExpandBody] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    """Add the fresh LLDP/CDP neighbours of a device node around it.

    Monitored neighbours become device nodes (plus links); unmonitored ones
    become grey icon shapes joined by annotation links.
    """
    body = data or ExpandBody()
    md = await _lock_map(db, map_id)
    node = (await db.execute(
        text("SELECT id, device_id, x_pct, y_pct FROM manual_map_nodes WHERE id = :n AND map_id = :m"),
        {"n": node_id, "m": map_id},
    )).mappings().first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if node["device_id"] is None:
        raise HTTPException(status_code=400, detail="Only device nodes can be expanded")
    did = str(node["device_id"])
    this_node = str(node["id"])

    edges, ifaces, hostnames = await _load_topology(db, [did])
    adjacency = topo.build_adjacencies(edges, ifaces, hostnames)
    neighbours = sorted(
        {other for key in topo.fresh_pairs(adjacency) if did in key for other in key if other != did},
        key=lambda d: ((hostnames.get(d) or "").lower(), d),
    )
    # Neighbours outside the caller's tag scope are never placed.
    scope = await scoping.visible_tags(db, user)
    if scope is not None and neighbours:
        visible = await _device_rows(db, "id = ANY(CAST(:ids AS uuid[]))", {"ids": neighbours}, scope)
        neighbours = [n for n in neighbours if n in visible]
    existing = await _placed_device_nodes(db, map_id)
    new_devices = [n for n in neighbours if n not in existing]

    unmanaged = topo.unmanaged_neighbours(edges, did, ifaces.get(did, [])) if body.include_unmanaged else []
    shape_by_ident: dict[str, str] = {}
    for r in (await db.execute(
        text("SELECT id, metadata FROM manual_map_shapes WHERE map_id = :m ORDER BY created_at"), {"m": map_id},
    )).mappings().all():
        um = _md(r["metadata"]).get("unmanaged")
        if isinstance(um, dict) and _unmanaged_ident(um):
            shape_by_ident.setdefault(_unmanaged_ident(um), str(r["id"]))
    new_unmanaged = [u for u in unmanaged if _unmanaged_ident(u) not in shape_by_ident]

    node_centres = [(float(r[0]), float(r[1])) for r in (await db.execute(
        text("SELECT x_pct, y_pct FROM manual_map_nodes WHERE map_id = :m"), {"m": map_id})).all()]
    shape_rects = [(float(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in (await db.execute(
        text("SELECT x_pct, y_pct, w_pct, h_pct FROM manual_map_shapes WHERE map_id = :m"), {"m": map_id})).all()]
    slots = topo.free_positions(float(node["x_pct"]), float(node["y_pct"]),
                                len(new_devices) + len(new_unmanaged), node_centres, shape_rects)
    placed_ids: list[str] = []
    node_of = {d: p["node_id"] for d, p in existing.items()}
    for i, n in enumerate(new_devices):
        x, y = slots[i]
        nid = await _insert_node(db, map_id, field="device_id", entity_id=n, x_pct=x, y_pct=y)
        node_of[n] = nid
        placed_ids.append(nid)

    link_ids: list[str] = []
    linked = await _linked_node_pairs(db, map_id)
    for n in neighbours:
        other = node_of.get(n)
        if not other or other == this_node or frozenset((this_node, other)) in linked:
            continue
        link_id = await _create_discovered_link(
            db, map_id, this_node, other, topo.summarize_pair(adjacency[frozenset((did, n))], did))
        if link_id:
            link_ids.append(link_id)

    shape_ids: list[str] = []
    al_ids: list[str] = []
    ann = _ann_links(md)
    ann_pairs = {frozenset((str(al.get("source")), str(al.get("target")))) for al in ann if isinstance(al, dict)}
    slot = len(new_devices)
    for u in unmanaged:
        sid = shape_by_ident.get(_unmanaged_ident(u))
        if sid is None:
            x, y = slots[slot]
            slot += 1
            sid = await _insert_shape(db, map_id, {
                "kind": "image",
                "x_pct": max(0.0, min(94.0, x - 3)), "y_pct": max(0.0, min(91.0, y - 4.5)),
                "w_pct": 6, "h_pct": 9,
                "text": u["hostname"],
                "metadata": {
                    "icon": u["icon"], "color": "#94a3b8",
                    "unmanaged": {"hostname": u["hostname"], "chassis_id": u["chassis_id"], "port_id": u["port_id"]},
                },
            })
            shape_by_ident[_unmanaged_ident(u)] = sid
            shape_ids.append(sid)
        if frozenset((this_node, sid)) in ann_pairs:
            continue
        kind = topo.link_kind(u["local_if"], u["port_id"], u["speed_bps"])
        al_md = {"src_interface": u["local_if"], "dst_interface": u["port_id"], "kind": kind,
                 "shape": "straight", "width_scale": 1, "discovered": True, "protocol": u["protocol"]}
        label = topo.speed_label(u["speed_bps"])
        if label:
            al_md["speed"] = label
        record = {"id": _new_ann_id(), "source": this_node, "target": sid, "source_type": "node",
                  "target_type": "shape", "label": None, "link_type": kind, "metadata": al_md}
        ann.append(record)
        ann_pairs.add(frozenset((this_node, sid)))
        al_ids.append(record["id"])
    if al_ids:
        await _save_map_metadata(db, map_id, {**md, "annotation_links": ann})
    else:
        await _touch(db, map_id)
    await db.commit()
    return {"placed_node_ids": placed_ids, "link_ids": link_ids,
            "unmanaged_shape_ids": shape_ids, "annotation_link_ids": al_ids}


# ─── Verify topology ───────────────────────────────────────────────────────

@router.get("/{map_id}/verify")
async def verify_map(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
):
    """Check every link's bound interfaces against the interface table and
    the discovered topology, and every node's status."""
    map_row = (await db.execute(text("SELECT metadata FROM manual_maps WHERE id = :id"), {"id": map_id})).mappings().first()
    if not map_row:
        raise HTTPException(404, "Map not found")
    nodes = (await db.execute(
        text(f"{_NODE_SELECT} WHERE mn.map_id = :map_id ORDER BY mn.created_at"), {"map_id": map_id},
    )).mappings().all()
    endpoints: dict[str, dict] = {}
    for n in nodes:
        endpoints[str(n["id"])] = {
            "kind": n["kind"], "device_id": str(n["device_id"]) if n["device_id"] else None,
            "hostname": n["label"] or n["hostname"],
        }
    for r in (await db.execute(
        text("SELECT id, kind, text FROM manual_map_shapes WHERE map_id = :m"), {"m": map_id},
    )).mappings().all():
        endpoints[str(r["id"])] = {"kind": "shape", "device_id": None, "hostname": r["text"] or r["kind"]}
    links = (await db.execute(
        text("SELECT id, source_node_id, target_node_id, metadata FROM manual_map_links WHERE map_id = :m ORDER BY created_at"),
        {"m": map_id},
    )).mappings().all()

    device_ids = {e["device_id"] for e in endpoints.values() if e["device_id"]}
    edges, ifaces, hostnames = await _load_topology(db, device_ids)
    evidence = topo.evidence_index(edges, ifaces, hostnames)

    link_issues: list[dict] = []
    for link in links:
        lmd = _md(link["metadata"])
        link_issues += topo.verify_link(
            str(link["id"]), False,
            [(endpoints.get(str(link["source_node_id"])), lmd.get("src_interface")),
             (endpoints.get(str(link["target_node_id"])), lmd.get("dst_interface"))],
            ifaces, evidence)
    for al in _ann_links(_md(map_row["metadata"])):
        if not isinstance(al, dict) or not al.get("id"):
            continue
        src, dst = endpoints.get(str(al.get("source"))), endpoints.get(str(al.get("target")))
        if not src or not dst:
            continue  # dangling: its endpoint was deleted
        amd = _md(al.get("metadata"))
        link_issues += topo.verify_link(
            str(al["id"]), True,
            [(src, amd.get("src_interface")), (dst, amd.get("dst_interface"))], ifaces, evidence)

    node_issues: list[dict] = []
    for n in nodes:
        name = n["label"] or n["hostname"] or "node"
        if n["status"] == "down":
            node_issues.append({"node_id": str(n["id"]), "severity": "critical", "code": "device_down",
                                "message": f"{name} is down"})
        elif n["status"] == "unknown":
            node_issues.append({"node_id": str(n["id"]), "severity": "warning", "code": "device_unknown",
                                "message": f"{name} status is unknown"})
    summary = {"critical": 0, "warning": 0, "info": 0}
    for issue in link_issues + node_issues:
        summary[issue["severity"]] = summary.get(issue["severity"], 0) + 1
    return {"links": link_issues, "nodes": node_issues, "summary": summary,
            "generated_at": datetime.now(timezone.utc).isoformat()}

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

# Normalize Cisco-style abbreviations so "Gi0/1" matches "GigabitEthernet0/1",
# "Fo1/1/1" matches "FortyGigabitEthernet1/1/1", etc. One table for the whole
# maps feature (map_topology), longest names first.
def _norm_iface(name: Optional[str]) -> str:
    return topo.norm_iface(name)


def _contains_at_boundary(haystack: str, needle: str) -> bool:
    """needle occurs in haystack and is not followed by another digit, so a
    partial "gi1/0/5" never matches "gi1/0/50"."""
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        if end == len(haystack) or not haystack[end].isdigit():
            return True
        start = haystack.find(needle, start + 1)
    return False


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
    # Pass 2 — needle inside a field (or a field inside the needle), ending on a
    # port-number boundary
    for it in ifaces:
        for field in ("if_name", "if_descr", "if_alias"):
            val = _norm_iface(it.get(field))
            if not val:
                continue
            if _contains_at_boundary(val, needle) or _contains_at_boundary(needle, val):
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
    user: User = Depends(require_permission("maps.view")),
):
    """Return live interface + throughput data for every link on the map.

    Response: {"data": {<link_id>: {source, target, window_seconds, generated_at}}}

    Each link's source/target carries `matched=False` when the typed
    interface name didn't resolve to a discovered interface, otherwise
    the discovered fields plus a NetFlow-derived `in_bps`, `out_bps`,
    `util_pct` over the last 5 minutes.
    """
    map_row = (await db.execute(text("SELECT id, metadata FROM manual_maps WHERE id = :id"), {"id": map_id})).mappings().first()
    if not map_row:
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
    link_rows = [dict(r) for r in (await db.execute(
        text("""
            SELECT id, source_node_id, target_node_id, metadata
            FROM manual_map_links WHERE map_id = :map_id
        """),
        {"map_id": map_id},
    )).mappings().all()]

    # Annotation links (device ↔ shape cables stored in the map metadata) can
    # bind their device end to a real interface too — process them through the
    # same pipeline so they light up with live throughput.
    map_meta = map_row["metadata"] or {}
    if isinstance(map_meta, str):
        try:
            map_meta = json.loads(map_meta)
        except Exception:
            map_meta = {}
    ann = map_meta.get("annotation_links") or []
    live_nodes, live_shapes = await _endpoint_sets(db, map_id) if ann else (set(), set())
    for al in ann:
        if not isinstance(al, dict) or not al.get("id"):
            continue
        # Skip dangling cables whose node/shape was deleted (pre-pruning data).
        if not all(str(al.get(side)) in live_nodes or str(al.get(side)) in live_shapes
                   for side in ("source", "target")):
            continue
        link_rows.append({
            "id": al["id"],
            "source_node_id": al.get("source"),
            "target_node_id": al.get("target"),
            "metadata": al.get("metadata") or {},
        })

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
    # name and collect (device_id, ifindex) pairs we need traffic for.
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
                flow_keys.add((str(side_node["device_id"]), int(payload["if_index"])))
                snmp_keys.add((str(side_node["device_id"]), int(payload["if_index"])))
            return payload

        out[str(lr["id"])] = {
            "source": resolve(src_node, md.get("src_interface")),
            "target": resolve(dst_node, md.get("dst_interface")),
            "_src_node": src_node,
            "_dst_node": dst_node,
        }

    # NetFlow query — one round-trip for every (device, ifindex) we need.
    # A device's flows come from every exporter it owns under the NetFlow
    # ownership rule (explicit exporter->device mapping first, then the
    # management IP) that the caller may see; without netflow.view, or when
    # no visible exporter belongs to a device, its links fall back to SNMP
    # below. If ClickHouse is unavailable we return matched=true with SNMP
    # throughput or null.
    flow_map: dict[tuple[str, int], dict] = {}
    vis = await nf_access.visibility_for_user(db, user)
    if flow_keys and vis.can(nf_access.VIEW_PERMISSION):
        owners = await nf_access.resolve_exporter_owners(db)
        wanted_devices = {did for did, _ in flow_keys}
        exporters = sorted(
            ip for ip, o in owners.items() if o.device_id in wanted_devices and vis.allows(ip)
        )
        if exporters:
            end = datetime.now(timezone.utc)
            start = end - timedelta(seconds=_LIVE_WINDOW_SECONDS)
            exp_sql, params = nf_access.exporters_predicate(exporters, param="mm_exp")
            params.update(start=start, end=end, idx=sorted({idx for _, idx in flow_keys}))
            where = f"timestamp >= %(start)s AND timestamp < %(end)s AND {exp_sql} AND {VOLUME_GUARD}"
            try:
                client = get_clickhouse_client()
                res = client.query(
                    f"""
                    SELECT {display(EXPORTER)} AS exporter,
                           ifindex,
                           sum(in_bytes)   AS in_bytes,
                           sum(out_bytes)  AS out_bytes,
                           sum(in_packets) AS in_packets,
                           sum(out_packets) AS out_packets
                    FROM (
                        SELECT exporter_ip, exporter_ip6, {IF_IN} AS ifindex,
                               sum(bytes) AS in_bytes, 0 AS out_bytes,
                               sum(packets) AS in_packets, 0 AS out_packets
                        FROM {table_ref(RAW)}
                        WHERE {where} AND {IF_IN} IN %(idx)s
                        GROUP BY exporter_ip, exporter_ip6, ifindex
                        UNION ALL
                        SELECT exporter_ip, exporter_ip6, {IF_OUT} AS ifindex,
                               0 AS in_bytes, sum(bytes) AS out_bytes,
                               0 AS in_packets, sum(packets) AS out_packets
                        FROM {table_ref(RAW)}
                        WHERE {where} AND {IF_OUT} IN %(idx)s
                        GROUP BY exporter_ip, exporter_ip6, ifindex
                    )
                    GROUP BY exporter_ip, exporter_ip6, ifindex
                    """,
                    parameters=params,
                )
                for r in res.result_rows:
                    owner = owners.get(nf_access.canonical_ip(r[0]))
                    if owner is None:
                        continue
                    key = (owner.device_id, int(r[1]))
                    if key not in flow_keys:
                        continue
                    # A device exporting from several addresses sums them:
                    # the ifIndex names the same interface on each.
                    agg = flow_map.setdefault(key, {"in_bytes": 0, "out_bytes": 0,
                                                    "in_packets": 0, "out_packets": 0})
                    agg["in_bytes"] += int(r[2] or 0)
                    agg["out_bytes"] += int(r[3] or 0)
                    agg["in_packets"] += int(r[4] or 0)
                    agg["out_packets"] += int(r[5] or 0)
            except Exception:
                # ClickHouse may not be reachable or netflow may be disabled —
                # interface status still useful, just no NetFlow throughput.
                logger.exception("NetFlow link rates failed for map %s", map_id)
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
            f = flow_map.get((str(node["device_id"]), int(side["if_index"])))
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
    user: User = Depends(require_permission("maps.view")),
):
    """Live per-node health for every node on the map.

    Response: {"data": {<node_id>: {kind, device_id, server_id,
    service_check_id, status, last_seen, rtt_ms, cpu_pct, mem_pct,
    uptime_seconds, temperature_c, alerts: {active, critical, warning}}}}

    Status/RTT come from the devices table (servers and service checks map
    their status onto the device vocabulary; a check's RTT is its last
    response time), CPU/memory/uptime/temperature from the latest ClickHouse
    snmp_metrics samples for devices (15-minute lookback), and alert counts
    from currently-active alerts. One call drives the NOC overlay.
    """
    node_rows = (await db.execute(
        text(f"""
            SELECT mn.id AS node_id, mn.device_id, mn.server_id, mn.service_check_id,
                   {_NODE_KIND_SQL} AS kind,
                   {_NODE_STATUS_SQL} AS status,
                   CASE WHEN mn.device_id IS NOT NULL THEN d.last_seen
                        WHEN mn.server_id IS NOT NULL THEN s.last_seen
                        ELSE sc.last_check_at END AS last_seen,
                   d.last_rtt_ms, sc.last_response_ms
            FROM manual_map_nodes mn
            {_NODE_JOINS}
            WHERE mn.map_id = :map_id
        """),
        {"map_id": map_id},
    )).mappings().all()
    if not node_rows:
        return {"data": {}, "generated_at": datetime.now(timezone.utc).isoformat()}

    device_ids = sorted({str(r["device_id"]) for r in node_rows if r["device_id"]})
    server_ids = sorted({str(r["server_id"]) for r in node_rows if r["server_id"]})
    check_ids = sorted({str(r["service_check_id"]) for r in node_rows if r["service_check_id"]})

    # Active alert counts per entity (severity split).
    alert_rows = (await db.execute(
        text("""
            SELECT device_id, server_id, service_check_id,
                   COUNT(*) AS active,
                   COUNT(*) FILTER (WHERE severity = 'critical') AS critical,
                   COUNT(*) FILTER (WHERE severity = 'warning') AS warning
            FROM alerts
            WHERE status = 'active'
              AND (device_id = ANY(:dids) OR server_id = ANY(:sids) OR service_check_id = ANY(:cids))
            GROUP BY device_id, server_id, service_check_id
        """),
        {"dids": device_ids, "sids": server_ids, "cids": check_ids},
    )).mappings().all()
    alerts_by: dict[tuple[str, str], dict] = {}
    for r in alert_rows:
        for col in ("device_id", "server_id", "service_check_id"):
            if r[col] is None:
                continue
            bucket = alerts_by.setdefault((col, str(r[col])), {"active": 0, "critical": 0, "warning": 0})
            bucket["active"] += int(r["active"])
            bucket["critical"] += int(r["critical"])
            bucket["warning"] += int(r["warning"])

    # Latest scalar health metrics from ClickHouse (cpu %, memory %, uptime s,
    # hottest temperature sensor). Missing/old samples simply yield nulls.
    metrics_by_device: dict[str, dict] = {}
    if device_ids:
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
        kind = r["kind"]
        col = {"device": "device_id", "server": "server_id"}.get(kind, "service_check_id")
        eid = str(r[col])
        m = metrics_by_device.get(eid, {}) if kind == "device" else {}
        rtt = r["last_rtt_ms"] if kind == "device" else (r["last_response_ms"] if kind == "service_check" else None)
        data[str(r["node_id"])] = {
            "kind": kind,
            "device_id": str(r["device_id"]) if r["device_id"] else None,
            "server_id": str(r["server_id"]) if r["server_id"] else None,
            "service_check_id": str(r["service_check_id"]) if r["service_check_id"] else None,
            "status": r["status"],
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "rtt_ms": float(rtt) if rtt is not None else None,
            "cpu_pct": m.get("cpu_pct"),
            "mem_pct": m.get("mem_pct"),
            "uptime_seconds": m.get("uptime_seconds"),
            "temperature_c": m.get("temperature_c"),
            "alerts": alerts_by.get((col, eid), {"active": 0, "critical": 0, "warning": 0}),
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
    user: User = Depends(require_permission("maps.manage")),
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
    await _touch(db, map_id)
    await db.commit()
    return _row_shape(row)


@router.put("/{map_id}/shapes/{shape_id}")
async def update_shape(
    map_id: uuid.UUID,
    shape_id: uuid.UUID,
    data: ShapeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
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
    await _touch(db, map_id)
    await db.commit()
    return _row_shape(row)


@router.delete("/{map_id}/shapes/{shape_id}", status_code=204)
async def delete_shape(
    map_id: uuid.UUID,
    shape_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    md = await _lock_map(db, map_id)
    row = (await db.execute(
        text("DELETE FROM manual_map_shapes WHERE id = :shape_id AND map_id = :map_id RETURNING id"),
        {"shape_id": shape_id, "map_id": map_id},
    )).first()
    if not row:
        await db.commit()
        raise HTTPException(status_code=404, detail="Shape not found")
    md, dropped = _prune_ann(md, [str(shape_id)])
    if dropped:
        await _save_map_metadata(db, map_id, md)
    else:
        await _touch(db, map_id)
    await db.commit()
    return None


# ─── Versions (snapshots) ──────────────────────────────────────────────────

async def _snapshot(db: AsyncSession, map_id) -> dict:
    map_row = (await db.execute(
        text("SELECT name, description, metadata FROM manual_maps WHERE id = :id"), {"id": map_id},
    )).mappings().first()
    if not map_row:
        raise HTTPException(status_code=404, detail="Map not found")
    nodes = (await db.execute(
        text(f"{_NODE_SELECT} WHERE mn.map_id = :m ORDER BY mn.created_at, mn.id"), {"m": map_id},
    )).mappings().all()
    links = (await db.execute(
        text("""SELECT id, map_id, source_node_id, target_node_id, label, link_type, metadata
                FROM manual_map_links WHERE map_id = :m ORDER BY created_at, id"""), {"m": map_id},
    )).mappings().all()
    shapes = (await db.execute(
        text("""SELECT id, map_id, kind, x_pct, y_pct, w_pct, h_pct, text, fill, stroke, z_index, metadata
                FROM manual_map_shapes WHERE map_id = :m ORDER BY z_index, created_at, id"""), {"m": map_id},
    )).mappings().all()
    return {
        "name": map_row["name"],
        "description": map_row["description"],
        "metadata": _md(map_row["metadata"]),
        # Node objects as GET /maps/{id} returns them, except that ``label``
        # is the stored label (None = show the hostname).
        "nodes": [_row_node(r, raw_label=True) for r in nodes],
        "links": [_row_link(r) for r in links],
        "shapes": [_row_shape(r) for r in shapes],
    }


def _row_version(row, created_by_name: Optional[str] = None) -> dict:
    keys = row.keys()
    return {
        "id": str(row["id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "created_by": str(row["created_by"]) if row["created_by"] else None,
        "created_by_name": row["created_by_name"] if "created_by_name" in keys else created_by_name,
        "reason": row["reason"],
        "node_count": row["node_count"],
        "link_count": row["link_count"],
        "shape_count": row["shape_count"],
    }


# Inline data: URLs above this size are stored once per map in
# manual_map_version_blobs and referenced from snapshots by hash.
_BLOB_MIN_BYTES = 8192
_BLOB_PREFIX = "zp-version-blob:"


def _externalise_blobs(value, found: dict[str, str]):
    """Copy of ``value`` with every large ``data:`` string replaced by a
    blob reference; ``found`` collects sha256 → data."""
    if isinstance(value, dict):
        return {k: _externalise_blobs(v, found) for k, v in value.items()}
    if isinstance(value, list):
        return [_externalise_blobs(v, found) for v in value]
    if isinstance(value, str) and value.startswith("data:") and len(value) >= _BLOB_MIN_BYTES:
        digest = hashlib.sha256(value.encode()).hexdigest()
        found[digest] = value
        return _BLOB_PREFIX + digest
    return value


def _internalise_blobs(value, blobs: dict[str, str]):
    if isinstance(value, dict):
        return {k: _internalise_blobs(v, blobs) for k, v in value.items()}
    if isinstance(value, list):
        return [_internalise_blobs(v, blobs) for v in value]
    if isinstance(value, str) and value.startswith(_BLOB_PREFIX):
        return blobs.get(value[len(_BLOB_PREFIX):], "")
    return value


async def _resolve_snapshot(db: AsyncSession, map_id, snap: dict) -> dict:
    """A stored snapshot with its blob references replaced by the images."""
    text_form = json.dumps(snap)
    if _BLOB_PREFIX not in text_form:
        return snap
    digests = sorted(set(re.findall(re.escape(_BLOB_PREFIX) + r"([0-9a-f]{64})", text_form)))
    rows = (await db.execute(
        text("SELECT sha256, data FROM manual_map_version_blobs WHERE map_id = :m AND sha256 = ANY(:d)"),
        {"m": map_id, "d": digests},
    )).all()
    return _internalise_blobs(snap, {r[0]: r[1] for r in rows})


async def _create_version(db: AsyncSession, map_id, user_id, reason: Optional[str]) -> dict:
    """Snapshot the map now and keep only the newest MAX_VERSIONS."""
    blobs: dict[str, str] = {}
    snap = _externalise_blobs(await _snapshot(db, map_id), blobs)
    for digest, data_url in blobs.items():
        await db.execute(
            text("""INSERT INTO manual_map_version_blobs (map_id, sha256, data)
                    VALUES (:m, :d, :data) ON CONFLICT (map_id, sha256) DO NOTHING"""),
            {"m": map_id, "d": digest, "data": data_url},
        )
    row = (await db.execute(
        text("""
            INSERT INTO manual_map_versions
                (map_id, created_at, created_by, reason, node_count, link_count, shape_count, snapshot)
            VALUES (:m, clock_timestamp(), :u, :reason, :nc, :lc, :sc, CAST(:snap AS JSONB))
            RETURNING id, created_at, created_by, reason, node_count, link_count, shape_count,
                      (SELECT COALESCE(NULLIF(full_name, ''), username) FROM users WHERE id = :u) AS created_by_name
        """),
        {"m": map_id, "u": user_id, "reason": (reason or None) and reason[:120],
         "nc": len(snap["nodes"]), "lc": len(snap["links"]), "sc": len(snap["shapes"]),
         "snap": json.dumps(snap, default=str)},
    )).mappings().first()
    await db.execute(
        text("""
            DELETE FROM manual_map_versions
            WHERE map_id = :m AND id NOT IN (
                SELECT id FROM manual_map_versions WHERE map_id = :m
                ORDER BY created_at DESC, id DESC LIMIT :keep)
        """),
        {"m": map_id, "keep": MAX_VERSIONS},
    )
    # Drop images no remaining snapshot references.
    await db.execute(
        text("""
            DELETE FROM manual_map_version_blobs b
            WHERE b.map_id = :m AND NOT EXISTS (
                SELECT 1 FROM manual_map_versions v
                WHERE v.map_id = :m AND strpos(v.snapshot::text, b.sha256) > 0)
        """),
        {"m": map_id},
    )
    return _row_version(row)


@router.get("/{map_id}/versions")
async def list_versions(
    map_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
):
    if not (await db.execute(text("SELECT 1 FROM manual_maps WHERE id = :id"), {"id": map_id})).first():
        raise HTTPException(status_code=404, detail="Map not found")
    rows = (await db.execute(
        text("""
            SELECT v.id, v.created_at, v.created_by, v.reason, v.node_count, v.link_count, v.shape_count,
                   COALESCE(NULLIF(u.full_name, ''), u.username) AS created_by_name
            FROM manual_map_versions v
            LEFT JOIN users u ON u.id = v.created_by
            WHERE v.map_id = :m
            ORDER BY v.created_at DESC, v.id DESC
            LIMIT :keep
        """),
        {"m": map_id, "keep": MAX_VERSIONS},
    )).mappings().all()
    return {"data": [_row_version(r) for r in rows]}


@router.post("/{map_id}/versions", status_code=201)
async def create_version(
    map_id: uuid.UUID,
    data: Optional[VersionCreate] = Body(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    await _lock_map(db, map_id)
    version = await _create_version(db, map_id, user.id, (data.reason if data else None) or "Manual snapshot")
    await db.commit()
    return version


@router.get("/{map_id}/versions/{version_id}")
async def get_version(
    map_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.view")),
):
    row = (await db.execute(
        text("""
            SELECT v.id, v.created_at, v.created_by, v.reason, v.node_count, v.link_count, v.shape_count,
                   v.snapshot, COALESCE(NULLIF(u.full_name, ''), u.username) AS created_by_name
            FROM manual_map_versions v
            LEFT JOIN users u ON u.id = v.created_by
            WHERE v.id = :v AND v.map_id = :m
        """),
        {"v": version_id, "m": map_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    return {**_row_version(row), "snapshot": await _resolve_snapshot(db, map_id, _md(row["snapshot"]))}


@router.post("/{map_id}/versions/{version_id}/restore")
async def restore_version(
    map_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    """Replace the map's nodes, links, shapes, name, description and metadata
    with a snapshot, in one transaction, after snapshotting the current
    state. Ids are kept; nodes whose device/server/check no longer exists are
    skipped together with their links and annotation links."""
    await _lock_map(db, map_id)
    row = (await db.execute(
        text("SELECT snapshot FROM manual_map_versions WHERE id = :v AND map_id = :m"),
        {"v": version_id, "m": map_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    snap = await _resolve_snapshot(db, map_id, _md(row["snapshot"]))
    backup = await _create_version(db, map_id, user.id, "Before restore")

    await db.execute(text("DELETE FROM manual_map_links WHERE map_id = :m"), {"m": map_id})
    await db.execute(text("DELETE FROM manual_map_nodes WHERE map_id = :m"), {"m": map_id})
    await db.execute(text("DELETE FROM manual_map_shapes WHERE map_id = :m"), {"m": map_id})

    snap_nodes = [n for n in (snap.get("nodes") or []) if isinstance(n, dict)]
    alive: dict[str, set[str]] = {}
    for field, (table, _) in _ENTITY_TABLES.items():
        wanted = sorted({str(n[field]) for n in snap_nodes if n.get(field)})
        alive[field] = {str(r[0]) for r in (await db.execute(
            text(f"SELECT id FROM {table} WHERE id = ANY(:ids)"), {"ids": wanted})).all()} if wanted else set()

    id_map: dict[str, str] = {}
    for n in snap_nodes:
        field = next((f for f in _ENTITY_FIELDS if n.get(f)), None)
        if field is None or str(n[field]) not in alive[field]:
            continue
        kwargs = dict(field=field, entity_id=str(n[field]), label=n.get("label"), icon=n.get("icon") or "auto",
                      x_pct=min(98.0, max(2.0, float(n.get("x_pct") or 50))),
                      y_pct=min(98.0, max(2.0, float(n.get("y_pct") or 50))),
                      metadata=n.get("metadata") or {})
        new_id = await _insert_node(db, map_id, node_id=n.get("id"), **kwargs) if n.get("id") else None
        id_map[str(n.get("id"))] = new_id or await _insert_node(db, map_id, **kwargs)
    for s in snap.get("shapes") or []:
        if not isinstance(s, dict) or s.get("kind") not in SHAPE_KINDS:
            continue
        new_id = await _insert_shape(db, map_id, s, shape_id=s.get("id")) if s.get("id") else None
        id_map[str(s.get("id"))] = new_id or await _insert_shape(db, map_id, s)
    for link in snap.get("links") or []:
        if not isinstance(link, dict):
            continue
        src, dst = id_map.get(str(link.get("source_node_id"))), id_map.get(str(link.get("target_node_id")))
        if not src or not dst or src == dst:
            continue
        kwargs = dict(label=link.get("label"), link_type=link.get("link_type") or "manual",
                      metadata=link.get("metadata") or {})
        new_id = await _insert_link(db, map_id, src, dst, link_id=link.get("id"), **kwargs) if link.get("id") else None
        if not new_id:
            await _insert_link(db, map_id, src, dst, **kwargs)

    md = _md(snap.get("metadata"))
    if isinstance(md.get("annotation_links"), list):
        restored = []
        for al in md["annotation_links"]:
            if not isinstance(al, dict):
                continue
            src, dst = id_map.get(str(al.get("source"))), id_map.get(str(al.get("target")))
            if src and dst:
                restored.append({**al, "source": src, "target": dst})
        md = {**md, "annotation_links": restored}
    await db.execute(
        text("""
            UPDATE manual_maps
            SET name = COALESCE(NULLIF(:name, ''), name), description = :description,
                metadata = CAST(:md AS JSONB), updated_at = NOW()
            WHERE id = :id
        """),
        {"id": map_id, "name": (snap.get("name") or "")[:255], "description": snap.get("description"),
         "md": json.dumps(md)},
    )
    await db.commit()
    return {"restored": True, "backup_version_id": backup["id"]}


# ─── Bulk create / delete (import, undo of deletes) ────────────────────────

@router.post("/{map_id}/bulk")
async def bulk_create(
    map_id: uuid.UUID,
    data: BulkCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    """Create nodes, shapes, links and annotation links in one transaction.
    Links and annotation links may reference the batch's client ids."""
    md = await _lock_map(db, map_id)
    id_map: dict[str, str] = {}
    for n in data.nodes:
        payload = n.model_dump()
        field, entity_id = _entity_of(payload)
        await _require_entity(db, field, entity_id)
        if n.client_id in id_map:
            raise HTTPException(status_code=400, detail=f"Duplicate client_id {n.client_id}")
        id_map[n.client_id] = await _insert_node(
            db, map_id, field=field, entity_id=entity_id, label=n.label, icon=n.icon,
            x_pct=n.x_pct, y_pct=n.y_pct, metadata=n.metadata)
    for s in data.shapes:
        if s.kind not in SHAPE_KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of {SHAPE_KINDS}")
        if s.client_id in id_map:
            raise HTTPException(status_code=400, detail=f"Duplicate client_id {s.client_id}")
        id_map[s.client_id] = await _insert_shape(db, map_id, s.model_dump())

    nodes, shapes = await _endpoint_sets(db, map_id)
    link_ids: list[str] = []
    for link in data.links:
        src = id_map.get(link.source, link.source)
        dst = id_map.get(link.target, link.target)
        if src not in nodes or dst not in nodes:
            raise HTTPException(status_code=400, detail="Both link nodes must belong to this map")
        if src == dst:
            raise HTTPException(status_code=400, detail="Source and target must be different")
        link_ids.append(await _insert_link(db, map_id, src, dst, label=link.label,
                                           link_type=link.link_type, metadata=link.metadata))

    ann = _ann_links(md)
    taken = {str(al.get("id")) for al in ann if isinstance(al, dict)}
    al_ids: list[str] = []
    for al in data.annotation_links:
        payload = al.model_dump()
        payload["source"] = id_map.get(payload["source"], payload["source"])
        payload["target"] = id_map.get(payload["target"], payload["target"])
        for end in ("source", "target"):
            if not payload.get(f"{end}_type"):
                payload[f"{end}_type"] = "node" if payload[end] in nodes else "shape"
        _validate_ann(payload, nodes, shapes)
        al_id = payload.get("id")
        if not al_id or al_id in taken:
            al_id = _new_ann_id()
        if payload.get("id"):
            id_map.setdefault(payload["id"], al_id)
        taken.add(al_id)
        ann.append(_ann_record(payload, al_id))
        al_ids.append(al_id)
    if al_ids:
        await _save_map_metadata(db, map_id, {**md, "annotation_links": ann})
    else:
        await _touch(db, map_id)
    await db.commit()
    return {"id_map": id_map, "link_ids": link_ids, "annotation_link_ids": al_ids}


@router.post("/{map_id}/bulk-delete")
async def bulk_delete(
    map_id: uuid.UUID,
    data: BulkDelete,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission("maps.manage")),
):
    """Delete nodes, shapes, links and annotation links in one transaction.
    ``links`` counts every link removed, including those of deleted nodes;
    ``annotation_links`` includes the ones pruned with deleted nodes/shapes."""
    md = await _lock_map(db, map_id)
    node_ids = [str(i) for i in data.node_ids]
    shape_ids = [str(i) for i in data.shape_ids]
    link_ids = [str(i) for i in data.link_ids]
    removed_links = (await db.execute(
        text("""
            DELETE FROM manual_map_links
            WHERE map_id = :m AND (id = ANY(:lids) OR source_node_id = ANY(:nids) OR target_node_id = ANY(:nids))
            RETURNING id
        """),
        {"m": map_id, "lids": link_ids, "nids": node_ids},
    )).all()
    removed_nodes = [str(r[0]) for r in (await db.execute(
        text("DELETE FROM manual_map_nodes WHERE map_id = :m AND id = ANY(:ids) RETURNING id"),
        {"m": map_id, "ids": node_ids},
    )).all()]
    removed_shapes = [str(r[0]) for r in (await db.execute(
        text("DELETE FROM manual_map_shapes WHERE map_id = :m AND id = ANY(:ids) RETURNING id"),
        {"m": map_id, "ids": shape_ids},
    )).all()]
    md, pruned = _prune_ann(md, removed_nodes + removed_shapes)
    explicit = set(data.annotation_link_ids)
    ann = md.get("annotation_links")
    if explicit and isinstance(ann, list):
        keep = [al for al in ann if not (isinstance(al, dict) and str(al.get("id")) in explicit)]
        pruned += len(ann) - len(keep)
        md = {**md, "annotation_links": keep}
    await _save_map_metadata(db, map_id, md)
    await db.commit()
    return {"deleted": {"nodes": len(removed_nodes), "shapes": len(removed_shapes),
                        "links": len(removed_links), "annotation_links": pruned}}
