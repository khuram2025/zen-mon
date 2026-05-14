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
    metadata: Optional[dict] = None


class LinkUpdate(BaseModel):
    label: Optional[str] = None
    link_type: Optional[str] = Field(default=None, max_length=40)
    metadata: Optional[dict] = None


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
# Live link statistics
#
# Match the user-typed src_interface / dst_interface strings on each
# link against discovered device_interfaces, then overlay NetFlow
# byte counters from ClickHouse over a recent window so the map can
# show real throughput and utilization next to each link.
# ─────────────────────────────────────────────────────────────────

_LIVE_WINDOW_SECONDS = 300  # 5-minute rolling window

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
            if payload["if_index"] is not None and side_node["ip"]:
                flow_keys.add((str(side_node["ip"]), int(payload["if_index"])))
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
            if not f:
                continue
            in_bps = (f["in_bytes"] * 8) / window
            out_bps = (f["out_bytes"] * 8) / window
            side["in_bps"] = round(in_bps, 1)
            side["out_bps"] = round(out_bps, 1)
            side["in_packets"] = f["in_packets"]
            side["out_packets"] = f["out_packets"]
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
