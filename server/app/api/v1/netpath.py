"""NetPath — hop-by-hop WAN/Internet path monitoring.

A SolarWinds NetPath competitor. The appliance runs a Paris-style multi-flow
traceroute (poller, internal/checker/netpath) to each probe target, discovering
every ECMP path with per-hop RTT/loss and detecting path changes. This router
serves the UI:

  Probes
    GET    /netpath/summary                 module KPIs
    GET    /netpath/probes                   list probes + latest state
    POST   /netpath/probes                   create probe
    GET    /netpath/probes/{id}              probe detail
    PATCH  /netpath/probes/{id}              edit probe
    DELETE /netpath/probes/{id}              delete probe
    POST   /netpath/probes/{id}/run          run now (on-demand trace)

  History & visualization
    GET    /netpath/probes/{id}/snapshots    end-to-end timeline (slider/heatmap)
    GET    /netpath/probes/{id}/path         enriched path graph at a point in time
    GET    /netpath/probes/{id}/hops         per-hop metric history (heatmap)
    GET    /netpath/probes/{id}/paths        distinct discovered routes
    GET    /netpath/probes/{id}/events       activity / change feed
    GET    /netpath/probes/{id}/compare      structural diff of two snapshots
"""
from __future__ import annotations

import ipaddress
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/netpath", tags=["Network Path"])

VIEW = require_permission("netpath.view", "netpath.manage")
MANAGE = require_permission("netpath.manage")


# ------------------------------------------------------------------ schemas
class ProbeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    target_host: str = Field(..., min_length=1, max_length=255)
    target_port: Optional[int] = Field(None, ge=1, le=65535)
    protocol: str = Field("icmp", pattern="^(icmp|tcp|udp)$")
    max_hops: int = Field(30, ge=1, le=64)
    probes_per_hop: int = Field(3, ge=1, le=10)
    flows: int = Field(4, ge=1, le=16)
    interval_s: int = Field(300, ge=30, le=86400)
    enabled: bool = True
    internal_cidrs: list[str] = Field(default_factory=list)
    rtt_warn_ms: float = 150
    rtt_crit_ms: float = 400
    loss_warn_pct: float = 2
    loss_crit_pct: float = 10
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class ProbeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    target_host: Optional[str] = Field(None, min_length=1, max_length=255)
    target_port: Optional[int] = Field(None, ge=1, le=65535)
    protocol: Optional[str] = Field(None, pattern="^(icmp|tcp|udp)$")
    max_hops: Optional[int] = Field(None, ge=1, le=64)
    probes_per_hop: Optional[int] = Field(None, ge=1, le=10)
    flows: Optional[int] = Field(None, ge=1, le=16)
    interval_s: Optional[int] = Field(None, ge=30, le=86400)
    enabled: Optional[bool] = None
    internal_cidrs: Optional[list[str]] = None
    rtt_warn_ms: Optional[float] = None
    rtt_crit_ms: Optional[float] = None
    loss_warn_pct: Optional[float] = None
    loss_crit_pct: Optional[float] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None


_PROBE_COLS = """
    id, name, target_host, host(target_ip)::text AS target_ip, target_port, protocol,
    max_hops, probes_per_hop, flows, interval_s, enabled, run_now, internal_cidrs,
    rtt_warn_ms, rtt_crit_ms, loss_warn_pct, loss_crit_pct, description, tags,
    last_run_at, last_status, last_rtt_ms, last_loss_pct, last_hop_count,
    last_num_paths, last_reached, last_error, created_at, updated_at
"""


def _probe_dict(r) -> dict:
    d = dict(r)
    d["id"] = str(d["id"])
    for k in ("last_run_at", "created_at", "updated_at"):
        if d.get(k):
            d[k] = d[k].isoformat()
    return d


def _validate_cidrs(cidrs: list[str]) -> list[str]:
    out = []
    for c in cidrs or []:
        try:
            ipaddress.ip_network(c, strict=False)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid CIDR: {c}")
        out.append(c)
    return out


# ------------------------------------------------------------------ summary
@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db), user: User = Depends(VIEW)):
    rows = (await db.execute(text("""
        SELECT COALESCE(last_status, 'pending') AS status, COUNT(*) AS n
        FROM netpath_probes GROUP BY 1
    """))).mappings().all()
    by_status = {r["status"]: r["n"] for r in rows}
    total = sum(by_status.values())
    enabled = (await db.execute(text(
        "SELECT COUNT(*) FROM netpath_probes WHERE enabled"))).scalar()
    changes_24h = (await db.execute(text("""
        SELECT COUNT(*) FROM netpath_events
        WHERE event_type = 'path_change' AND created_at > NOW() - INTERVAL '24 hours'
    """))).scalar()
    unreachable = by_status.get("unreached", 0) + by_status.get("down", 0)
    recent = (await db.execute(text("""
        SELECT e.id, e.probe_id, p.name AS probe_name, e.event_type, e.severity,
               e.details, e.created_at
        FROM netpath_events e JOIN netpath_probes p ON p.id = e.probe_id
        ORDER BY e.id DESC LIMIT 12
    """))).mappings().all()
    return {
        "total_probes": total,
        "enabled": enabled or 0,
        "by_status": by_status,
        "ok": by_status.get("ok", 0),
        "degraded": by_status.get("degraded", 0),
        "unreachable": unreachable,
        "path_changes_24h": changes_24h or 0,
        "recent_events": [
            {**dict(r), "id": r["id"], "probe_id": str(r["probe_id"]),
             "created_at": r["created_at"].isoformat()} for r in recent
        ],
    }


# ------------------------------------------------------------------ probes CRUD
@router.get("/probes")
async def list_probes(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(VIEW),
    status: Optional[str] = None,
    q: Optional[str] = None,
    tag: Optional[str] = None,
):
    where, params = [], {}
    if status:
        where.append("COALESCE(last_status,'pending') = :status")
        params["status"] = status
    if q:
        where.append("(name ILIKE :q OR target_host ILIKE :q)")
        params["q"] = f"%{q}%"
    if tag:
        where.append(":tag = ANY(tags)")
        params["tag"] = tag
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = (await db.execute(text(
        f"SELECT {_PROBE_COLS} FROM netpath_probes{where_sql} ORDER BY name"
    ), params)).mappings().all()
    return {"data": [_probe_dict(r) for r in rows]}


@router.post("/probes", status_code=201)
async def create_probe(
    data: ProbeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(MANAGE),
):
    if data.protocol == "tcp" and not data.target_port:
        data.target_port = 80
    _validate_cidrs(data.internal_cidrs)
    pid = uuid.uuid4()
    await db.execute(text("""
        INSERT INTO netpath_probes
            (id, name, target_host, target_port, protocol, max_hops, probes_per_hop,
             flows, interval_s, enabled, internal_cidrs, rtt_warn_ms, rtt_crit_ms,
             loss_warn_pct, loss_crit_pct, description, tags, created_by, last_status)
        VALUES
            (:id, :name, :host, :port, :proto, :max_hops, :pph, :flows, :interval,
             :enabled, :cidrs, :rw, :rc, :lw, :lc, :desc, :tags, :uid, 'pending')
    """), {
        "id": str(pid), "name": data.name, "host": data.target_host,
        "port": data.target_port, "proto": data.protocol, "max_hops": data.max_hops,
        "pph": data.probes_per_hop, "flows": data.flows, "interval": data.interval_s,
        "enabled": data.enabled, "cidrs": data.internal_cidrs, "rw": data.rtt_warn_ms,
        "rc": data.rtt_crit_ms, "lw": data.loss_warn_pct, "lc": data.loss_crit_pct,
        "desc": data.description, "tags": data.tags, "uid": str(user.id),
    })
    await write_audit_log(db, actor=user, action="netpath.probe.create",
                          resource_type="netpath_probe", resource_id=str(pid),
                          metadata={"name": data.name, "target": data.target_host})
    await db.commit()
    row = (await db.execute(text(
        f"SELECT {_PROBE_COLS} FROM netpath_probes WHERE id = :id"), {"id": str(pid)})).mappings().first()
    return _probe_dict(row)


async def _get_probe_or_404(db: AsyncSession, probe_id: str) -> dict:
    try:
        uuid.UUID(probe_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="probe not found")
    row = (await db.execute(text(
        f"SELECT {_PROBE_COLS} FROM netpath_probes WHERE id = :id"), {"id": probe_id})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="probe not found")
    return row


@router.get("/probes/{probe_id}")
async def get_probe(probe_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(VIEW)):
    row = await _get_probe_or_404(db, probe_id)
    d = _probe_dict(row)
    latest = (await db.execute(text("""
        SELECT id, run_at, status, reached, rtt_ms, loss_pct, jitter_ms, hop_count,
               num_paths, path_changed, duration_ms
        FROM netpath_snapshots WHERE probe_id = :id ORDER BY run_at DESC LIMIT 1
    """), {"id": probe_id})).mappings().first()
    if latest:
        d["latest_snapshot"] = {**dict(latest), "id": latest["id"],
                                "run_at": latest["run_at"].isoformat()}
    distinct_paths = (await db.execute(text(
        "SELECT COUNT(*) FROM netpath_paths WHERE probe_id = :id"), {"id": probe_id})).scalar()
    d["distinct_paths"] = distinct_paths or 0
    return d


@router.patch("/probes/{probe_id}")
async def update_probe(
    probe_id: str, data: ProbeUpdate,
    db: AsyncSession = Depends(get_db), user: User = Depends(MANAGE),
):
    await _get_probe_or_404(db, probe_id)
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        return await get_probe(probe_id, db, user)
    if "internal_cidrs" in fields:
        _validate_cidrs(fields["internal_cidrs"])
    col_map = {
        "name": "name", "target_host": "target_host", "target_port": "target_port",
        "protocol": "protocol", "max_hops": "max_hops", "probes_per_hop": "probes_per_hop",
        "flows": "flows", "interval_s": "interval_s", "enabled": "enabled",
        "internal_cidrs": "internal_cidrs", "rtt_warn_ms": "rtt_warn_ms",
        "rtt_crit_ms": "rtt_crit_ms", "loss_warn_pct": "loss_warn_pct",
        "loss_crit_pct": "loss_crit_pct", "description": "description", "tags": "tags",
    }
    sets, params = [], {"id": probe_id}
    for k, v in fields.items():
        if k in col_map:
            sets.append(f"{col_map[k]} = :{k}")
            params[k] = v
    # editing the target invalidates the resolved IP so the poller re-resolves
    if "target_host" in fields:
        sets.append("target_ip = NULL")
    sets.append("updated_at = NOW()")
    await db.execute(text(f"UPDATE netpath_probes SET {', '.join(sets)} WHERE id = :id"), params)
    await write_audit_log(db, actor=user, action="netpath.probe.update",
                          resource_type="netpath_probe", resource_id=probe_id,
                          metadata={"fields": list(fields.keys())})
    await db.commit()
    return await get_probe(probe_id, db, user)


@router.delete("/probes/{probe_id}")
async def delete_probe(probe_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(MANAGE)):
    row = await _get_probe_or_404(db, probe_id)
    await db.execute(text("DELETE FROM netpath_probes WHERE id = :id"), {"id": probe_id})
    await write_audit_log(db, actor=user, action="netpath.probe.delete",
                          resource_type="netpath_probe", resource_id=probe_id,
                          metadata={"name": row["name"]})
    await db.commit()
    return {"deleted": True}


@router.post("/probes/{probe_id}/run")
async def run_now(probe_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(MANAGE)):
    await _get_probe_or_404(db, probe_id)
    await db.execute(text(
        "UPDATE netpath_probes SET run_now = TRUE WHERE id = :id"), {"id": probe_id})
    await db.commit()
    return {"queued": True}


# ------------------------------------------------------------------ timeline
@router.get("/probes/{probe_id}/snapshots")
async def snapshots(
    probe_id: str,
    db: AsyncSession = Depends(get_db), user: User = Depends(VIEW),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(500, ge=1, le=5000),
):
    await _get_probe_or_404(db, probe_id)
    rows = (await db.execute(text("""
        SELECT id, run_at, status, reached, path_changed, rtt_ms, loss_pct,
               worst_hop_loss_pct, jitter_ms, hop_count, num_paths, duration_ms
        FROM netpath_snapshots
        WHERE probe_id = :id AND run_at > NOW() - make_interval(hours => :h)
        ORDER BY run_at ASC LIMIT :lim
    """), {"id": probe_id, "h": hours, "lim": limit})).mappings().all()
    return {"data": [
        {**dict(r), "id": r["id"], "run_at": r["run_at"].isoformat()} for r in rows
    ]}


# ------------------------------------------------------------------ path graph
async def _enrich_ips(db: AsyncSession, ips: list[str]) -> dict[str, dict]:
    if not ips:
        return {}
    rows = (await db.execute(text("""
        SELECT host(m.ip)::text AS ip, m.hostname, m.asn, m.as_name, m.country,
               m.is_internal, m.device_id, d.hostname AS device_name, d.device_type
        FROM netpath_hop_meta m
        LEFT JOIN devices d ON d.id = m.device_id
        WHERE m.ip = ANY(CAST(:ips AS inet[]))
    """), {"ips": ips})).mappings().all()
    return {r["ip"]: dict(r) for r in rows}


@router.get("/probes/{probe_id}/path")
async def path_graph(
    probe_id: str,
    db: AsyncSession = Depends(get_db), user: User = Depends(VIEW),
    snapshot_id: Optional[int] = None,
    at: Optional[str] = None,
):
    probe = await _get_probe_or_404(db, probe_id)
    if snapshot_id is not None:
        snap = (await db.execute(text(
            "SELECT * FROM netpath_snapshots WHERE id = :sid AND probe_id = :pid"),
            {"sid": snapshot_id, "pid": probe_id})).mappings().first()
    elif at:
        try:
            at_dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid 'at' timestamp")
        snap = (await db.execute(text("""
            SELECT * FROM netpath_snapshots WHERE probe_id = :pid AND run_at <= :at
            ORDER BY run_at DESC LIMIT 1
        """), {"pid": probe_id, "at": at_dt})).mappings().first()
    else:
        snap = (await db.execute(text(
            "SELECT * FROM netpath_snapshots WHERE probe_id = :pid ORDER BY run_at DESC LIMIT 1"),
            {"pid": probe_id})).mappings().first()

    if not snap:
        return {"probe": _probe_dict(probe), "snapshot": None, "hops": [], "edges": [], "as_groups": []}

    hops = snap["hops"] if isinstance(snap["hops"], list) else json.loads(snap["hops"] or "[]")
    flows = snap["flows"] if isinstance(snap["flows"], list) else json.loads(snap["flows"] or "[]")

    # gather + enrich all node IPs
    all_ips: list[str] = []
    for hop in hops:
        for n in (hop.get("nodes") or []):
            if n.get("ip"):
                all_ips.append(n["ip"])
    meta = await _enrich_ips(db, list(set(all_ips)))

    priv_nets = [ipaddress.ip_network(c, strict=False) for c in (probe.get("internal_cidrs") or [])]

    def is_internal(ip: str, m: dict) -> bool:
        if m.get("is_internal"):
            return True
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_private:
                return True
            return any(addr in n for n in priv_nets)
        except ValueError:
            return False

    enriched_hops = []
    for hop in hops:
        nodes = []
        for n in (hop.get("nodes") or []):
            ip = n.get("ip", "")
            m = meta.get(ip, {})
            nodes.append({
                **n,
                "hostname": m.get("hostname"),
                "asn": m.get("asn"),
                "as_name": m.get("as_name"),
                "country": m.get("country"),
                "device_id": str(m["device_id"]) if m.get("device_id") else None,
                "device_name": m.get("device_name"),
                "device_type": m.get("device_type"),
                "is_internal": is_internal(ip, m),
            })
        enriched_hops.append({"ttl": hop.get("ttl"), "anonymous": hop.get("anonymous", False), "nodes": nodes})

    # edges from flows: connect consecutive non-empty hops, weight = flow count
    edge_map: dict[tuple, dict] = {}
    for f in flows:
        seq = [(i + 1, ip) for i, ip in enumerate((f.get("path") or [])) if ip]
        for a, b in zip(seq, seq[1:]):
            key = (a[1], b[1])
            e = edge_map.get(key)
            if not e:
                e = {"from_ip": a[1], "to_ip": b[1], "from_ttl": a[0], "to_ttl": b[0],
                     "flows": 0, "gap": b[0] - a[0] > 1}
                edge_map[key] = e
            e["flows"] += 1

    # AS groups (clouds)
    as_groups: dict[int, dict] = {}
    for hop in enriched_hops:
        for n in hop["nodes"]:
            if n.get("asn"):
                g = as_groups.setdefault(n["asn"], {
                    "asn": n["asn"], "as_name": n.get("as_name"), "count": 0,
                    "is_internal": n.get("is_internal", False)})
                g["count"] += 1

    return {
        "probe": _probe_dict(probe),
        "snapshot": {
            "id": snap["id"], "run_at": snap["run_at"].isoformat(),
            "status": snap["status"], "reached": snap["reached"],
            "path_changed": snap["path_changed"], "rtt_ms": snap["rtt_ms"],
            "loss_pct": snap["loss_pct"], "worst_hop_loss_pct": snap["worst_hop_loss_pct"],
            "jitter_ms": snap["jitter_ms"], "hop_count": snap["hop_count"],
            "num_paths": snap["num_paths"], "protocol": snap["protocol"],
            "duration_ms": snap["duration_ms"], "path_hash": snap["path_hash"],
        },
        "target": {"host": probe["target_host"], "ip": probe["target_ip"],
                   "port": probe["target_port"], "reached": snap["reached"]},
        "hops": enriched_hops,
        "edges": list(edge_map.values()),
        "as_groups": sorted(as_groups.values(), key=lambda g: -g["count"]),
    }


# ------------------------------------------------------------------ per-hop history
@router.get("/probes/{probe_id}/hops")
async def hop_history(
    probe_id: str,
    db: AsyncSession = Depends(get_db), user: User = Depends(VIEW),
    hours: int = Query(24, ge=1, le=720),
):
    await _get_probe_or_404(db, probe_id)
    rows = (await db.execute(text("""
        SELECT s.run_at,
               (hop->>'ttl')::int AS ttl,
               (hop->'nodes'->0->>'ip') AS ip,
               (hop->'nodes'->0->>'rtt_avg')::float AS rtt,
               (hop->'nodes'->0->>'loss_pct')::float AS loss,
               COALESCE((hop->>'anonymous')::bool, false) AS anon,
               COALESCE((hop->'nodes'->0->>'is_dest')::bool, false) AS is_dest
        FROM netpath_snapshots s
        CROSS JOIN LATERAL jsonb_array_elements(s.hops) AS hop
        WHERE s.probe_id = :id AND s.run_at > NOW() - make_interval(hours => :h)
        ORDER BY s.run_at ASC, ttl ASC
    """), {"id": probe_id, "h": hours})).mappings().all()

    times: list[str] = []
    seen_times = set()
    per_ttl: dict[int, dict] = {}
    for r in rows:
        t = r["run_at"].isoformat()
        if t not in seen_times:
            seen_times.add(t)
            times.append(t)
        ttl = r["ttl"]
        slot = per_ttl.setdefault(ttl, {"ttl": ttl, "ip": r["ip"], "is_dest": r["is_dest"], "points": {}})
        # keep the most common ip label seen for this ttl
        if r["ip"] and not slot["ip"]:
            slot["ip"] = r["ip"]
        slot["points"][t] = {"rtt": r["rtt"], "loss": r["loss"], "anon": r["anon"], "ip": r["ip"]}
    ladder = []
    for ttl in sorted(per_ttl):
        slot = per_ttl[ttl]
        ladder.append({
            "ttl": ttl, "ip": slot["ip"], "is_dest": slot["is_dest"],
            "series": [slot["points"].get(t) for t in times],
        })
    return {"times": times, "ladder": ladder}


# ------------------------------------------------------------------ routes
@router.get("/probes/{probe_id}/paths")
async def distinct_paths(
    probe_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(VIEW),
):
    await _get_probe_or_404(db, probe_id)
    rows = (await db.execute(text("""
        SELECT id, path_hash, hop_count, hop_ips, label, first_seen, last_seen, seen_count
        FROM netpath_paths WHERE probe_id = :id ORDER BY last_seen DESC
    """), {"id": probe_id})).mappings().all()
    # enrich the hop IPs with AS names for a compact route label
    all_ips = list({ip for r in rows for ip in (r["hop_ips"] or [])})
    meta = await _enrich_ips(db, all_ips)
    out = []
    for r in rows:
        as_path = []
        for ip in (r["hop_ips"] or []):
            m = meta.get(ip, {})
            if m.get("asn") and (not as_path or as_path[-1]["asn"] != m["asn"]):
                as_path.append({"asn": m["asn"], "as_name": m.get("as_name")})
        out.append({
            "id": str(r["id"]), "path_hash": r["path_hash"], "hop_count": r["hop_count"],
            "hop_ips": r["hop_ips"], "label": r["label"],
            "first_seen": r["first_seen"].isoformat(), "last_seen": r["last_seen"].isoformat(),
            "seen_count": r["seen_count"], "as_path": as_path,
        })
    return {"data": out}


# ------------------------------------------------------------------ events
@router.get("/probes/{probe_id}/events")
async def events(
    probe_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(VIEW),
    limit: int = Query(100, ge=1, le=1000),
):
    await _get_probe_or_404(db, probe_id)
    rows = (await db.execute(text("""
        SELECT id, event_type, snapshot_id, severity, details, created_at
        FROM netpath_events WHERE probe_id = :id ORDER BY id DESC LIMIT :lim
    """), {"id": probe_id, "lim": limit})).mappings().all()
    return {"data": [
        {**dict(r), "id": r["id"], "created_at": r["created_at"].isoformat()} for r in rows
    ]}


# ------------------------------------------------------------------ diff
@router.get("/probes/{probe_id}/compare")
async def compare(
    probe_id: str, a: int, b: int,
    db: AsyncSession = Depends(get_db), user: User = Depends(VIEW),
):
    """Structural diff of two snapshots — the feature no competitor ships."""
    await _get_probe_or_404(db, probe_id)
    rows = (await db.execute(text("""
        SELECT id, run_at, hops, rtt_ms, loss_pct, hop_count, path_hash
        FROM netpath_snapshots WHERE probe_id = :id AND id IN (:a, :b)
    """), {"id": probe_id, "a": a, "b": b})).mappings().all()
    snaps = {r["id"]: r for r in rows}
    if a not in snaps or b not in snaps:
        raise HTTPException(status_code=404, detail="snapshot not found")

    def hop_ip_map(snap) -> dict[int, set]:
        hops = snap["hops"] if isinstance(snap["hops"], list) else json.loads(snap["hops"] or "[]")
        m = {}
        for hop in hops:
            m[hop.get("ttl")] = {n.get("ip") for n in (hop.get("nodes") or []) if n.get("ip")}
        return m

    ma, mb = hop_ip_map(snaps[a]), hop_ip_map(snaps[b])
    all_ttls = sorted(set(ma) | set(mb))
    all_ips_a = {ip for s in ma.values() for ip in s}
    all_ips_b = {ip for s in mb.values() for ip in s}
    meta = await _enrich_ips(db, list(all_ips_a | all_ips_b))

    def label(ip):
        m = meta.get(ip, {})
        return {"ip": ip, "hostname": m.get("hostname"), "asn": m.get("asn"),
                "as_name": m.get("as_name"), "device_name": m.get("device_name")}

    rows_out = []
    for ttl in all_ttls:
        sa, sb = ma.get(ttl, set()), mb.get(ttl, set())
        added = sb - sa
        removed = sa - sb
        same = sa & sb
        status = "same"
        if added and removed:
            status = "changed"
        elif added:
            status = "added"
        elif removed:
            status = "removed"
        rows_out.append({
            "ttl": ttl, "status": status,
            "same": [label(ip) for ip in sorted(same)],
            "added": [label(ip) for ip in sorted(added)],
            "removed": [label(ip) for ip in sorted(removed)],
        })
    ips_added = all_ips_b - all_ips_a
    ips_removed = all_ips_a - all_ips_b
    return {
        "a": {"id": a, "run_at": snaps[a]["run_at"].isoformat(), "rtt_ms": snaps[a]["rtt_ms"],
              "loss_pct": snaps[a]["loss_pct"], "hop_count": snaps[a]["hop_count"]},
        "b": {"id": b, "run_at": snaps[b]["run_at"].isoformat(), "rtt_ms": snaps[b]["rtt_ms"],
              "loss_pct": snaps[b]["loss_pct"], "hop_count": snaps[b]["hop_count"]},
        "rows": rows_out,
        "summary": {
            "hops_added": [label(ip) for ip in sorted(ips_added)],
            "hops_removed": [label(ip) for ip in sorted(ips_removed)],
            "identical": snaps[a]["path_hash"] == snaps[b]["path_hash"],
            "rtt_delta": round((snaps[b]["rtt_ms"] or 0) - (snaps[a]["rtt_ms"] or 0), 2),
        },
    }
