"""NetFlow settings (FE-09, CR-12, CR-13): IP groups, application rules,
enrichment (reverse DNS, GeoIP) and what flow export cannot see (NAT, tunnels).

Routes, mounted under ``/netflow`` (router-level ``netflow.view``):

    GET    /settings/ip-groups                    groups, overlaps, references, import status
    GET    /settings/ip-groups/usage              per-group hosts / bytes / flows in a window
    POST   /settings/ip-groups/preview            validate a draft group and show its traffic
    GET    /settings/ip-groups/export             every group as a JSON document
    POST   /settings/ip-groups/import             merge or replace from JSON (dry run first)   manage
    POST   /settings/ip-groups                    create                                       manage
    PUT    /settings/ip-groups/{id}               replace                                      manage
    DELETE /settings/ip-groups/{id}               delete                                       manage
    GET    /settings/app-rules                    operator application rules
    POST   /settings/app-rules                    create                                       manage
    PUT    /settings/app-rules/{id}               replace                                      manage
    PATCH  /settings/app-rules/{id}               enable / disable / re-prioritise             manage
    DELETE /settings/app-rules/{id}               delete                                       manage
    POST   /settings/app-rules/preview            what a draft (or a removal) would reclassify
    GET    /settings/applications                 built-in rules (read-only), categories
    GET    /settings/applications/exporter-names  NBAR2 / App-ID names announced by exporters
    POST   /settings/applications/test            classify one flow tuple (current / with a draft)
    GET    /settings/enrichment                   reverse-DNS settings and cache, GeoIP databases
    PUT    /settings/enrichment/rdns              enable / disable lookups, deadline           manage
    POST   /settings/enrichment/rdns/flush        forget cached names                          manage
    POST   /settings/enrichment/rdns/test         resolve one address now                      manage
    POST   /settings/enrichment/geoip             upload a .mmdb (air-gapped appliances)       manage
    GET    /settings/visibility                   tunnel / NAT / App-ID visibility in a window
    GET    /settings/history                      audit trail of these settings                manage

Rules every write follows (contract §6): ``netflow.manage``, ``write_audit_log``
with the before/after values, and the cache of the service that reads the table
is invalidated after the commit (``netflow_groups.invalidate_groups_cache``,
``netflow_classify.invalidate_rules_cache``), so filters, breakdowns and the
classifier see the change on the next request in this process (other workers
within the services' 60 s cache).

Validation failures are ``400 {"detail": str, "code": "invalid_value",
"fields": {field: message}}`` so a form can mark the offending field; a
duplicate name or an edit of a stale copy is ``409 {"detail", "code":
"conflict"}``. Flow-derived reads (usage, previews, visibility) take the page
window, ``FlowFilter`` and ``FlowVisibility``, go through ``run_query`` and the
source planner, and return the response envelope on ``envelope=1``.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import logging
import os
import re
import tarfile
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.services import geoip, netflow_classify, netflow_groups
from app.services.audit_service import write_audit_log

from . import common
from .access import MANAGE_PERMISSION, FlowVisibility, canonical_ip, flow_visibility, require_manage
from .common import (
    CONVERSATIONS_1H,
    RAW,
    Window,
    _to_iso_utc,
    current_meta,
    now_utc,
    run_query,
    table_ref,
    window_params,
)
from .filters import FlowFilter, compile_filter, flow_filter
from .sql import DST, EXPORTER, SRC, VOLUME_GUARD, display, plan_source

logger = logging.getLogger(__name__)

router = APIRouter()

ENRICHMENT_KEY = "netflow_enrichment"
RDNS_DEADLINE_MIN_S = 0.2
RDNS_DEADLINE_MAX_S = 10.0
RDNS_DEFAULTS = {"enabled": True, "deadline_s": 2.0}

MAX_GROUPS = 2000
MAX_GROUP_CIDRS = 4096
MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_USAGE_GROUPS = 400
MAX_RULE_CIDRS = 256
MAX_GEOIP_BYTES = 256 * 1024 * 1024
GEOIP_KINDS = {"country": "country.mmdb", "asn": "asn.mmdb"}
GEOIP_STALE_DAYS = 90
# Protocols whose transport header carries ports (a port rule on another
# protocol could never match); the classifier's own set.
PORT_PROTOCOLS = netflow_classify.PORT_PROTOCOLS

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── Responses ───────────────────────────────────────────────────────────────

def _invalid(fields: dict[str, str], message: str | None = None) -> JSONResponse:
    """400 with one message per offending field (forms highlight them)."""
    detail = message or "; ".join(f"{k}: {v}" for k, v in fields.items()) or "Invalid request"
    return JSONResponse({"detail": detail, "code": "invalid_value", "error_id": None, "fields": fields},
                        status_code=400)


def _conflict(message: str) -> JSONResponse:
    return JSONResponse({"detail": message, "code": "conflict", "error_id": None}, status_code=409)


def _not_found(what: str) -> JSONResponse:
    return JSONResponse({"detail": f"{what} not found", "code": "not_found", "error_id": None}, status_code=404)


def _iso(value) -> str | None:
    """Full-precision RFC3339 (optimistic-concurrency tokens compare exactly)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = _CONTROL_RE.sub(" ", str(value)).strip()
    return value[:limit] if value else None


# ── Address helpers ─────────────────────────────────────────────────────────

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_cidr(raw: str) -> tuple[Network | None, str | None, str | None]:
    """(network, error, note) for one CIDR or address typed by an operator.

    Host bits are dropped with a note (``10.0.0.5/24`` is stored as
    ``10.0.0.0/24``); zone ids, leading zeros and garbage are errors. An
    IPv4-mapped IPv6 range becomes the IPv4 range flows are stored under.
    """
    text_value = str(raw or "").strip()
    if not text_value:
        return None, "Empty entry", None
    if "%" in text_value:
        return None, f"'{text_value[:64]}': zone ids are not allowed", None
    try:
        strict = ipaddress.ip_network(text_value, strict=True)
        note = None
    except ValueError:
        try:
            loose = ipaddress.ip_network(text_value, strict=False)
        except ValueError:
            return None, f"'{text_value[:64]}' is not an IP address or CIDR", None
        strict = loose
        note = f"{text_value} has host bits set; stored as {loose}"
    net = netflow_groups.parse_network(str(strict))
    if net is None:
        return None, f"'{text_value[:64]}' is not an IP address or CIDR", None
    if str(net) != str(strict) and note is None:
        note = f"{text_value} is an IPv4-mapped range; stored as {net}"
    return net, None, note


def _net_pred(expr: str, net: Network) -> str:
    """Predicate on a unified (IPv6-typed, IPv4-mapped) address expression.

    Literals come from a parsed network, never from request text."""
    lo, hi = net.network_address, net.broadcast_address
    if isinstance(net, ipaddress.IPv4Network):
        return f"({expr} BETWEEN toIPv6('::ffff:{lo}') AND toIPv6('::ffff:{hi}'))"
    return f"({expr} BETWEEN toIPv6('{lo}') AND toIPv6('{hi}'))"


def _nets_pred(expr: str, nets) -> str:
    terms = [_net_pred(expr, n) for n in nets]
    if not terms:
        return "0"
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def address_count(nets) -> dict:
    """How many addresses a set of networks spans, readable for either family."""
    v4 = [n for n in nets if isinstance(n, ipaddress.IPv4Network)]
    v6 = [n for n in nets if isinstance(n, ipaddress.IPv6Network)]
    v4c = sum(n.num_addresses for n in ipaddress.collapse_addresses(v4)) if v4 else 0
    v6c = list(ipaddress.collapse_addresses(v6)) if v6 else []
    parts = []
    if v4c:
        parts.append(f"{v4c:,} IPv4 address{'es' if v4c != 1 else ''}")
    if v6c:
        parts.append(f"{len(v6c)} IPv6 prefix{'es' if len(v6c) != 1 else ''}")
    return {"ipv4": v4c, "ipv6_prefixes": len(v6c), "label": " · ".join(parts) or "No addresses"}


def _relation(a: Network, b: Network) -> str:
    if a == b:
        return "equal"
    if a.subnet_of(b):
        return "inside"
    if b.subnet_of(a):
        return "contains"
    return "overlaps"


# ── IP groups: loading and validation ───────────────────────────────────────

_GROUP_SELECT = """
    SELECT g.id, g.name, g.description, g.cidrs, g.color, g.tags, g.parent_id,
           g.created_at, g.updated_at, cu.username AS created_by, uu.username AS updated_by
      FROM netflow_ip_groups g
      LEFT JOIN users cu ON cu.id = g.created_by
      LEFT JOIN users uu ON uu.id = g.updated_by
"""


def _tags(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    return [str(t) for t in value if t] if isinstance(value, list) else []


async def _group_rows(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text(_GROUP_SELECT + " ORDER BY lower(g.name)"))).mappings().all()
    out = []
    for r in rows:
        out.append({
            "id": str(r["id"]), "name": r["name"], "description": r["description"],
            "cidrs": list(r["cidrs"] or []), "color": r["color"], "tags": _tags(r["tags"]),
            "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
            "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"]),
            "created_by": r["created_by"], "updated_by": r["updated_by"],
        })
    return out


def _nets_of(cidrs) -> list[Network]:
    out = []
    for c in cidrs or []:
        net = netflow_groups.parse_network(c)
        if net is not None:
            out.append(net)
    return out


def _descendants(groups: list[dict], gid: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for g in groups:
        if g["parent_id"]:
            children.setdefault(g["parent_id"], []).append(g["id"])
    seen: set[str] = set()
    stack = [gid]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def _ancestors(groups: list[dict], parent_id: str | None) -> list[str]:
    by_id = {g["id"]: g for g in groups}
    chain: list[str] = []
    cur = parent_id
    while cur and cur in by_id and cur not in chain:
        chain.append(cur)
        cur = by_id[cur]["parent_id"]
    return chain


def overlaps_for(gid: str | None, nets: list[Network], groups: list[dict], parent_id: str | None) -> list[dict]:
    """Where ``nets`` intersect other groups' CIDRs. Overlap with an ancestor
    or descendant is expected nesting and is labelled so; anything else means
    a host belongs to two unrelated groups (filters on either include it, the
    host label names the more specific one)."""
    family = set(_ancestors(groups, parent_id)) | (_descendants(groups, gid) if gid else set())
    out = []
    for other in groups:
        if gid and other["id"] == gid:
            continue
        for other_cidr in other["cidrs"]:
            o = netflow_groups.parse_network(other_cidr)
            if o is None:
                continue
            for n in nets:
                if n.version == o.version and n.overlaps(o):
                    out.append({
                        "group_id": other["id"], "group": other["name"], "cidr": str(n), "other_cidr": str(o),
                        "relation": _relation(n, o), "nested": other["id"] in family,
                    })
    return out


class IpGroupIn(BaseModel):
    name: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    cidrs: list[str] = Field(default_factory=list, max_length=MAX_GROUP_CIDRS)
    color: str | None = Field(default=None, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=64)
    parent_id: str | None = None
    # The updated_at the editor loaded; a PUT whose copy is stale is a 409.
    expected_updated_at: str | None = None


def validate_group(payload: IpGroupIn, groups: list[dict], gid: str | None) -> tuple[dict, dict[str, str], list[str]]:
    """(normalized row, field errors, warnings) for a create (gid None) or update."""
    errors: dict[str, str] = {}
    warnings: list[str] = []
    name = _clean_text(payload.name, 1000) or ""
    if not name:
        errors["name"] = "Name is required"
    elif len(name) > netflow_groups.NAME_MAX:
        errors["name"] = f"Name is limited to {netflow_groups.NAME_MAX} characters"
    elif any(g["name"].lower() == name.lower() and g["id"] != gid for g in groups):
        errors["name"] = f"A group named '{name}' already exists"

    nets: list[Network] = []
    cidr_errors: list[str] = []
    for raw in payload.cidrs:
        if not str(raw or "").strip():
            continue
        net, err, note = parse_cidr(raw)
        if err:
            cidr_errors.append(err)
            continue
        if note:
            warnings.append(note)
        if net in nets:
            warnings.append(f"{net} is listed twice; kept once")
            continue
        nets.append(net)
    if cidr_errors:
        errors["cidrs"] = "; ".join(cidr_errors[:5]) + (f" (+{len(cidr_errors) - 5} more)" if len(cidr_errors) > 5 else "")
    elif not nets:
        errors["cidrs"] = "Add at least one address or CIDR"
    for n in nets:
        if n.prefixlen == 0:
            warnings.append(f"{n} matches every {'IPv4' if n.version == 4 else 'IPv6'} address")
        for m in nets:
            if n != m and n.version == m.version and n.subnet_of(m):
                warnings.append(f"{n} is inside {m} in this group and adds nothing")
                break

    color = _clean_text(payload.color, 32)
    if color and not _COLOR_RE.match(color):
        errors["color"] = "Use a hex colour such as #3b82f6"
    tags: list[str] = []
    for t in payload.tags:
        t = _clean_text(t, 200)
        if not t:
            continue
        if len(t) > 64:
            errors["tags"] = "Tags are limited to 64 characters"
            break
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)

    parent_id = payload.parent_id or None
    if parent_id:
        try:
            parent_id = str(uuid.UUID(str(parent_id)))
        except ValueError:
            errors["parent_id"] = "Unknown parent group"
            parent_id = None
    if parent_id:
        ids = {g["id"] for g in groups}
        if parent_id not in ids:
            errors["parent_id"] = "Unknown parent group"
        elif gid and (parent_id == gid or parent_id in _descendants(groups, gid)):
            errors["parent_id"] = "A group cannot be nested inside itself or one of its sub-groups"

    description = _clean_text(payload.description, 2000) if payload.description else None
    row = {
        "name": name, "description": description, "cidrs": [str(n) for n in nets],
        "color": color or None, "tags": tags, "parent_id": parent_id,
    }
    return row, errors, list(dict.fromkeys(warnings))


def _decorate_groups(groups: list[dict], references: dict[str, int]) -> list[dict]:
    by_id = {g["id"]: g for g in groups}
    snapshot = {g.id: g for g in netflow_groups.groups_snapshot() if g.id}
    out = []
    for g in groups:
        nets = _nets_of(g["cidrs"])
        depth = len(_ancestors(groups, g["parent_id"]))
        eff = snapshot.get(g["id"])
        eff_nets = list(eff.effective) if eff is not None else nets
        children = sorted((c["name"] for c in groups if c["parent_id"] == g["id"]), key=str.lower)
        out.append({
            **g,
            "parent_name": by_id[g["parent_id"]]["name"] if g["parent_id"] in by_id else None,
            "depth": depth,
            "children": children,
            "addresses": address_count(nets),
            "effective_cidrs": [str(n) for n in eff_nets],
            "effective_addresses": address_count(eff_nets),
            "overlaps": overlaps_for(g["id"], nets, groups, g["parent_id"]),
            "references": {"saved_views": references.get(g["name"], 0)},
        })
    return out


async def _group_references(db: AsyncSession) -> dict[str, int]:
    """Saved views that filter on a group by name (renaming or deleting the
    group makes them fail with 'Unknown IP group' until edited)."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text(
                "SELECT COALESCE(query->'filter'->>'group', query->>'group') AS g, count(*) AS n "
                "FROM netflow_saved_views GROUP BY 1"
            ))).all()
    except Exception:
        logger.debug("saved-view group references unavailable", exc_info=True)
        return {}
    return {r.g: int(r.n) for r in rows if r.g}


async def _import_marker(db: AsyncSession) -> dict | None:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"),
                            {"k": netflow_groups.IMPORT_MARKER_KEY})).first()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _refresh_groups() -> None:
    netflow_groups.invalidate_groups_cache()


# ── IP groups: routes ───────────────────────────────────────────────────────

@router.get("/settings/ip-groups")
async def list_ip_groups(
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """Every group with its effective CIDRs (own + sub-groups), address counts,
    overlaps with other groups and saved-view references."""
    await netflow_groups.load_groups()   # imports the legacy JSON file once
    groups = await _group_rows(db)
    refs = await _group_references(db)
    current_meta().set_source("pg")
    return {
        "items": _decorate_groups(groups, refs),
        "import": await _import_marker(db),
        "counts": {"groups": len(groups), "cidrs": sum(len(g["cidrs"]) for g in groups)},
        "can_manage": vis.can(MANAGE_PERMISSION),
        "limits": {"groups": MAX_GROUPS, "cidrs_per_group": MAX_GROUP_CIDRS},
    }


def _usage_columns() -> dict:
    return {"s": SRC, "d": DST, "bytes": "bytes", "flows": ("flows", "toUInt64(1)")}


@router.get("/settings/ip-groups/usage")
async def ip_group_usage(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """Per group, in the window: distinct hosts seen, and bytes/flows with
    either side in the group (a flow between two groups counts for both), with
    whole-scope totals as the denominator."""
    groups = [g for g in await netflow_groups.load_groups() if g.id]
    shown = groups[:MAX_USAGE_GROUPS]
    if len(groups) > len(shown):
        current_meta().warn("groups_truncated", f"Usage is shown for the first {len(shown)} groups")
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H)
    sub, params = plan.from_sql(_usage_columns(), f, vis)
    traffic_cols = ["sum(bytes)", "sum(flows)"]
    host_cols = ["uniqExact(a)"]
    any_pred_s = _nets_pred("s", [n for g in shown for n in g.effective])
    any_pred_d = _nets_pred("d", [n for g in shown for n in g.effective])
    for g in shown:
        ps, pd, pa = _nets_pred("s", g.effective), _nets_pred("d", g.effective), _nets_pred("a", g.effective)
        traffic_cols += [f"sumIf(bytes, {ps} OR {pd})", f"sumIf(flows, {ps} OR {pd})"]
        host_cols.append(f"uniqExactIf(a, {pa})")
    traffic_cols += [f"sumIf(bytes, NOT ({any_pred_s} OR {any_pred_d}))",
                     f"sumIf(flows, NOT ({any_pred_s} OR {any_pred_d}))"]
    q_traffic = f"SELECT {', '.join(traffic_cols)} FROM {sub} AS src"
    q_hosts = f"SELECT {', '.join(host_cols)} FROM (SELECT arrayJoin([s, d]) AS a FROM {sub} AS src)"
    t_res, h_res = await asyncio.gather(
        run_query(q_traffic, params, label="settings-group-usage"),
        run_query(q_hosts, params, label="settings-group-hosts"),
    )
    t = list(t_res.result_rows[0]) if t_res.result_rows else [0] * len(traffic_cols)
    h = list(h_res.result_rows[0]) if h_res.result_rows else [0] * len(host_cols)
    items = []
    for i, g in enumerate(shown):
        items.append({
            "id": g.id, "name": g.name,
            "hosts": int(h[1 + i] or 0),
            "bytes": int(t[2 + 2 * i] or 0), "flows": int(t[3 + 2 * i] or 0),
        })
    return {
        "items": items,
        "total": {"hosts": int(h[0] or 0), "bytes": int(t[0] or 0), "flows": int(t[1] or 0)},
        "ungrouped": {"bytes": int(t[-2] or 0), "flows": int(t[-1] or 0)},
        "counting": "A flow between two groups counts for both; hosts are distinct addresses seen as source or destination.",
    }


class GroupPreviewIn(BaseModel):
    id: str | None = None
    name: str = ""
    cidrs: list[str] = Field(default_factory=list, max_length=MAX_GROUP_CIDRS)
    parent_id: str | None = None


@router.post("/settings/ip-groups/preview")
async def preview_ip_group(
    body: GroupPreviewIn,
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """A draft group checked without saving: normalised CIDRs, problems,
    overlaps with other groups, address count, and the hosts and traffic it
    would cover in the window."""
    groups = await _group_rows(db)
    gid = body.id if body.id and any(g["id"] == body.id for g in groups) else None
    row, errors, warnings = validate_group(
        IpGroupIn.model_construct(name=(body.name or "draft")[:200], description=None, cidrs=body.cidrs,
                                  color=None, tags=[], parent_id=body.parent_id), groups, gid)
    errors.pop("name", None)   # the name is checked on save; the preview is about addresses
    nets = _nets_of(row["cidrs"])
    out: dict[str, Any] = {
        "cidrs": row["cidrs"], "errors": errors, "warnings": warnings,
        "addresses": address_count(nets),
        "overlaps": overlaps_for(gid, nets, groups, row["parent_id"]),
        "traffic": None,
    }
    if not nets:
        return out
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H)
    sub, params = plan.from_sql(_usage_columns(), f, vis)
    ps, pd, pa = _nets_pred("s", nets), _nets_pred("d", nets), _nets_pred("a", nets)
    q_tot = f"SELECT sumIf(bytes, {ps} OR {pd}), sumIf(flows, {ps} OR {pd}), sum(bytes) FROM {sub} AS src"
    q_top = (f"SELECT {display('a')} AS ip, sum(bytes) AS b, sum(flows) AS fl, count() OVER () AS n "
             f"FROM (SELECT arrayJoin([s, d]) AS a, bytes, flows FROM {sub} AS src) WHERE {pa} "
             "GROUP BY a ORDER BY b DESC LIMIT 10")
    tot, top = await asyncio.gather(
        run_query(q_tot, params, label="settings-group-preview"),
        run_query(q_top, params, label="settings-group-preview-top"),
    )
    b, fl, all_b = tot.result_rows[0] if tot.result_rows else (0, 0, 0)
    rows = top.result_rows
    out["traffic"] = {
        "bytes": int(b or 0), "flows": int(fl or 0), "total_bytes": int(all_b or 0),
        "hosts": int(rows[0][3]) if rows else 0,
        "top_hosts": [{"ip": r[0], "bytes": int(r[1] or 0), "flows": int(r[2] or 0)} for r in rows],
    }
    return out


@router.get("/settings/ip-groups/export")
async def export_ip_groups(db: AsyncSession = Depends(get_db)):
    """Every group as a JSON document the import accepts (parents by name).
    The legacy file format (a bare list of {name, cidrs}) is a subset."""
    groups = await _group_rows(db)
    by_id = {g["id"]: g["name"] for g in groups}
    doc = {
        "format": "zenplus-netflow-ip-groups",
        "version": 1,
        "exported_at": _to_iso_utc(now_utc()),
        "groups": [
            {"name": g["name"], "description": g["description"], "cidrs": g["cidrs"], "color": g["color"],
             "tags": g["tags"], "parent": by_id.get(g["parent_id"]) if g["parent_id"] else None}
            for g in groups
        ],
    }
    stamp = now_utc().strftime("%Y%m%d-%H%M")
    return Response(
        json.dumps(doc, indent=2), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="netflow-ip-groups-{stamp}.json"'},
    )


class ImportIn(BaseModel):
    document: Any
    mode: Literal["merge", "replace"] = "merge"
    dry_run: bool = True


def _import_entries(document: Any) -> tuple[list[dict], list[str]]:
    if isinstance(document, dict):
        entries = document.get("groups")
    else:
        entries = document
    if not isinstance(entries, list):
        return [], ["The file must be a list of groups or an object with a 'groups' list"]
    if len(entries) > MAX_GROUPS:
        return [], [f"At most {MAX_GROUPS} groups can be imported"]
    problems: list[str] = []
    out: list[dict] = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"Entry {i + 1}: not an object")
            continue
        name = _clean_text(e.get("name"), 1000) or ""
        label = f"Entry {i + 1}" + (f" ({name})" if name else "")
        if not name:
            problems.append(f"{label}: missing name")
            continue
        if name.lower() in seen:
            problems.append(f"{label}: the name appears more than once in the file")
            continue
        seen.add(name.lower())
        cidrs = e.get("cidrs")
        if not isinstance(cidrs, list):
            problems.append(f"{label}: 'cidrs' must be a list")
            continue
        tags = e.get("tags") if isinstance(e.get("tags"), list) else []
        out.append({"name": name, "description": e.get("description"), "cidrs": [str(c) for c in cidrs],
                    "color": e.get("color"), "tags": [str(t) for t in tags], "parent": e.get("parent")})
    return out, problems


@router.post("/settings/ip-groups/import")
async def import_ip_groups(
    body: ImportIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    """Merge (create new names, update existing ones) or replace (also delete
    groups missing from the document). ``dry_run`` reports the plan without
    writing. Any invalid entry rejects the whole import (400), so a file is
    applied completely or not at all."""
    entries, problems = _import_entries(body.document)
    existing = await _group_rows(db)
    by_name = {g["name"].lower(): g for g in existing}
    warnings: list[str] = []
    planned: list[tuple[dict, dict | None]] = []   # (normalized, existing row)
    import_names = {e["name"].lower() for e in entries}
    # Parents a file entry may name: other entries, plus (merge only) groups
    # already on the appliance -- a replace deletes everything not in the file.
    parent_names = import_names | (set(by_name) if body.mode == "merge" else set())
    for e in entries:
        current = by_name.get(e["name"].lower())
        payload = IpGroupIn.model_construct(
            name=e["name"], description=_clean_text(e["description"], 2000) if e["description"] else None,
            cidrs=e["cidrs"][:MAX_GROUP_CIDRS + 1], color=str(e["color"]) if e["color"] else None,
            tags=e["tags"][:64], parent_id=None)
        if len(e["cidrs"]) > MAX_GROUP_CIDRS:
            problems.append(f"{e['name']}: at most {MAX_GROUP_CIDRS} CIDRs per group")
        # Names are checked against the post-import set below; parents after.
        others = [g for g in existing if g["name"].lower() not in import_names]
        row, errors, warns = validate_group(payload, others, current["id"] if current else None)
        for k, v in errors.items():
            problems.append(f"{e['name']}: {v}")
        warnings.extend(f"{e['name']}: {w}" for w in warns)
        parent = _clean_text(e["parent"], 1000) if e["parent"] else None
        if parent:
            if parent.lower() == e["name"].lower():
                problems.append(f"{e['name']}: cannot be its own parent")
            elif parent.lower() not in parent_names:
                problems.append(f"{e['name']}: parent '{parent}' is not in the file"
                                + (" nor on the appliance" if body.mode == "merge" else ""))
        row["parent"] = parent
        planned.append((row, current))
    # Cycle check over the final parent relation (by lower-case name).
    final_parent: dict[str, str | None] = {
        g["name"].lower(): (next((x["name"].lower() for x in existing if x["id"] == g["parent_id"]), None))
        for g in existing
        if body.mode == "merge" or g["name"].lower() in import_names
    }
    for row, _cur in planned:
        final_parent[row["name"].lower()] = row["parent"].lower() if row["parent"] else None
    for start in final_parent:
        seen_chain = set()
        cur = start
        while cur is not None:
            if cur in seen_chain:
                problems.append(f"{start}: the parent chain loops back to itself")
                break
            seen_chain.add(cur)
            cur = final_parent.get(cur)
    created = [r["name"] for r, cur in planned if cur is None]
    updated, unchanged = [], []
    for r, cur in planned:
        if cur is None:
            continue
        cur_parent = next((x["name"] for x in existing if x["id"] == cur["parent_id"]), None)
        same = (sorted(cur["cidrs"]) == sorted(r["cidrs"]) and (cur["description"] or None) == r["description"]
                and (cur["color"] or None) == r["color"] and cur["tags"] == r["tags"]
                and (cur_parent or "").lower() == (r["parent"] or "").lower() and cur["name"] == r["name"])
        (unchanged if same else updated).append(r["name"])
    deleted = [g["name"] for g in existing if body.mode == "replace" and g["name"].lower() not in import_names]
    result = {"dry_run": body.dry_run, "mode": body.mode, "created": created, "updated": updated,
              "unchanged": unchanged, "deleted": deleted, "problems": problems,
              "warnings": list(dict.fromkeys(warnings))}
    if problems:
        if body.dry_run:
            return result
        return JSONResponse({"detail": f"The import has {len(problems)} problem(s); nothing was changed",
                             "code": "invalid_value", "error_id": None, "fields": {"document": problems[0]},
                             "result": result}, status_code=400)
    if body.dry_run:
        return result
    if len(existing) - len(deleted) + len(created) > MAX_GROUPS:
        return _invalid({"document": f"At most {MAX_GROUPS} groups are supported"})
    ids: dict[str, str] = {g["name"].lower(): g["id"] for g in existing}
    try:
        for name in deleted:
            await db.execute(text("DELETE FROM netflow_ip_groups WHERE id = :id"),
                             {"id": uuid.UUID(ids.pop(name.lower()))})
        for row, cur in planned:
            params = {"name": row["name"], "description": row["description"], "cidrs": row["cidrs"],
                      "color": row["color"], "tags": json.dumps(row["tags"]), "uid": user.id}
            if cur is None:
                new_id = (await db.execute(text(
                    "INSERT INTO netflow_ip_groups (name, description, cidrs, color, tags, created_by, updated_by) "
                    "VALUES (:name, :description, CAST(:cidrs AS text[]), :color, CAST(:tags AS jsonb), :uid, :uid) "
                    "RETURNING id"), params)).scalar_one()
                ids[row["name"].lower()] = str(new_id)
            elif row["name"] in updated:
                await db.execute(text(
                    "UPDATE netflow_ip_groups SET name = :name, description = :description, "
                    "cidrs = CAST(:cidrs AS text[]), color = :color, tags = CAST(:tags AS jsonb), "
                    "updated_by = :uid, updated_at = NOW() WHERE id = :id"), {**params, "id": uuid.UUID(cur["id"])})
        for row, cur in planned:
            if cur is not None and row["name"] not in updated:
                continue
            parent_id = ids.get(row["parent"].lower()) if row["parent"] else None
            await db.execute(text("UPDATE netflow_ip_groups SET parent_id = :p WHERE id = :id"),
                             {"p": uuid.UUID(parent_id) if parent_id else None,
                              "id": uuid.UUID(ids[row["name"].lower()])})
        await write_audit_log(db, actor=user, action="netflow.ip_groups.import", resource_type="netflow_ip_group",
                              resource_id=None, metadata={k: v for k, v in result.items() if k != "warnings"})
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _conflict("The groups changed while importing; review and try again")
    _refresh_groups()
    return result


async def _load_group(db: AsyncSession, gid: str) -> dict | None:
    try:
        key = str(uuid.UUID(str(gid)))
    except ValueError:
        return None
    return next((g for g in await _group_rows(db) if g["id"] == key), None)


async def _decorated(db: AsyncSession, gid: str) -> dict | None:
    groups = await _group_rows(db)
    return next((g for g in _decorate_groups(groups, await _group_references(db)) if g["id"] == gid), None)


def _group_audit(row: dict) -> dict:
    return {k: row.get(k) for k in ("name", "description", "cidrs", "color", "tags", "parent_id")}


@router.post("/settings/ip-groups", status_code=201)
async def create_ip_group(
    body: IpGroupIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    groups = await _group_rows(db)
    if len(groups) >= MAX_GROUPS:
        return _invalid({"name": f"At most {MAX_GROUPS} groups are supported"})
    row, errors, warnings = validate_group(body, groups, None)
    if errors:
        return _invalid(errors)
    try:
        new_id = (await db.execute(text(
            "INSERT INTO netflow_ip_groups (name, description, cidrs, color, tags, parent_id, created_by, updated_by) "
            "VALUES (:name, :description, CAST(:cidrs AS text[]), :color, CAST(:tags AS jsonb), :parent, :uid, :uid) "
            "RETURNING id"),
            {"name": row["name"], "description": row["description"], "cidrs": row["cidrs"], "color": row["color"],
             "tags": json.dumps(row["tags"]), "parent": uuid.UUID(row["parent_id"]) if row["parent_id"] else None,
             "uid": user.id})).scalar_one()
        await write_audit_log(db, actor=user, action="netflow.ip_group.create", resource_type="netflow_ip_group",
                              resource_id=str(new_id), metadata={"after": _group_audit(row)})
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _conflict(f"A group named '{row['name']}' already exists, or its parent was just removed")
    _refresh_groups()
    await netflow_groups.load_groups(force=True)
    return {"item": await _decorated(db, str(new_id)), "warnings": warnings}


@router.put("/settings/ip-groups/{group_id}")
async def update_ip_group(
    group_id: str,
    body: IpGroupIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    current = await _load_group(db, group_id)
    if current is None:
        return _not_found("IP group")
    expected = _parse_iso(body.expected_updated_at)
    if body.expected_updated_at and expected != _parse_iso(current["updated_at"]):
        who = current["updated_by"] or "another user"
        return _conflict(f"This group was changed by {who} at {current['updated_at']}; reload it before saving")
    groups = await _group_rows(db)
    row, errors, warnings = validate_group(body, groups, current["id"])
    if errors:
        return _invalid(errors)
    refs = (await _group_references(db)).get(current["name"], 0)
    if refs and row["name"] != current["name"]:
        warnings.append(f"{refs} saved view(s) filter on '{current['name']}' by name and will fail until they are "
                        f"edited to use '{row['name']}'")
    try:
        await db.execute(text(
            "UPDATE netflow_ip_groups SET name = :name, description = :description, cidrs = CAST(:cidrs AS text[]), "
            "color = :color, tags = CAST(:tags AS jsonb), parent_id = :parent, updated_by = :uid, updated_at = NOW() "
            "WHERE id = :id"),
            {"name": row["name"], "description": row["description"], "cidrs": row["cidrs"], "color": row["color"],
             "tags": json.dumps(row["tags"]), "parent": uuid.UUID(row["parent_id"]) if row["parent_id"] else None,
             "uid": user.id, "id": uuid.UUID(current["id"])})
        await write_audit_log(db, actor=user, action="netflow.ip_group.update", resource_type="netflow_ip_group",
                              resource_id=current["id"],
                              metadata={"before": _group_audit(current), "after": _group_audit(row)})
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _conflict(f"A group named '{row['name']}' already exists, or its parent was just removed")
    _refresh_groups()
    await netflow_groups.load_groups(force=True)
    return {"item": await _decorated(db, current["id"]), "warnings": warnings}


@router.delete("/settings/ip-groups/{group_id}")
async def delete_ip_group(
    group_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    current = await _load_group(db, group_id)
    if current is None:
        return _not_found("IP group")
    groups = await _group_rows(db)
    children = [g["name"] for g in groups if g["parent_id"] == current["id"]]
    refs = (await _group_references(db)).get(current["name"], 0)
    await db.execute(text("DELETE FROM netflow_ip_groups WHERE id = :id"), {"id": uuid.UUID(current["id"])})
    await write_audit_log(db, actor=user, action="netflow.ip_group.delete", resource_type="netflow_ip_group",
                          resource_id=current["id"], metadata={"before": _group_audit(current),
                                                               "detached_children": children})
    await db.commit()
    _refresh_groups()
    await netflow_groups.load_groups(force=True)
    return {"deleted": current["id"], "name": current["name"], "detached_children": children,
            "saved_view_references": refs}


# ── Application rules ───────────────────────────────────────────────────────

_RULE_SELECT = """
    SELECT r.id, r.application, r.service, r.category, r.protocol, r.port_lo, r.port_hi, r.cidrs,
           r.priority, r.enabled, r.created_at, r.updated_at, cu.username AS created_by, uu.username AS updated_by
      FROM netflow_app_rules r
      LEFT JOIN users cu ON cu.id = r.created_by
      LEFT JOIN users uu ON uu.id = r.updated_by
"""


def _rule_kind(r: dict) -> str:
    if r.get("port_lo") is not None:
        return "port"
    if r.get("cidrs"):
        return "address"
    return "protocol"


def _rule_payload(r) -> dict:
    d = dict(r)
    out = {
        "id": str(d["id"]), "application": d["application"], "service": d["service"], "category": d["category"],
        "protocol": d["protocol"],
        "protocol_name": netflow_classify.protocol_name(d["protocol"]) if d["protocol"] is not None else None,
        "port_lo": d["port_lo"], "port_hi": d["port_hi"], "cidrs": list(d["cidrs"] or []),
        "priority": d["priority"], "enabled": d["enabled"],
        "created_at": _iso(d["created_at"]), "updated_at": _iso(d["updated_at"]),
        "created_by": d["created_by"], "updated_by": d["updated_by"],
    }
    out["kind"] = _rule_kind(out)
    return out


async def _rule_rows(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(text(_RULE_SELECT + " ORDER BY r.priority, r.created_at, r.id"))).mappings().all()
    return [_rule_payload(r) for r in rows]


def _as_app_rule(r: dict, *, application: str | None = None) -> netflow_classify.AppRule:
    return netflow_classify.AppRule(
        id=r.get("id") or "draft", application=application or r["application"], service=r.get("service"),
        category=r.get("category"), protocol=r.get("protocol"), port_lo=r.get("port_lo"),
        port_hi=r.get("port_hi"), cidrs=tuple(r.get("cidrs") or ()), priority=int(r.get("priority") or 100),
    )


class AppRuleIn(BaseModel):
    application: str = Field(max_length=500)
    service: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=500)
    protocol: int | None = None
    port_lo: int | None = None
    port_hi: int | None = None
    cidrs: list[str] = Field(default_factory=list, max_length=MAX_RULE_CIDRS * 4)
    priority: int = 100
    enabled: bool = True
    expected_updated_at: str | None = None


_SENTINEL = "\x00candidate"


def validate_rule(body: AppRuleIn) -> tuple[dict, dict[str, str], list[str]]:
    """(normalized row, field errors, warnings), mirroring the table's CHECK
    constraints and what the classifier can use (a port rule on a portless
    protocol, or a rule whose CIDRs are all invalid, would never match)."""
    errors: dict[str, str] = {}
    warnings: list[str] = []
    application = _clean_text(body.application, 1000) or ""
    if not application:
        errors["application"] = "Application name is required"
    elif len(application) > 80:
        errors["application"] = "Application names are limited to 80 characters"
    service = _clean_text(body.service, 1000)
    if service and len(service) > 80:
        errors["service"] = "Service labels are limited to 80 characters"
    category = _clean_text(body.category, 1000)
    if category and len(category) > 64:
        errors["category"] = "Categories are limited to 64 characters"
    protocol = body.protocol
    if protocol is not None and not 0 <= protocol <= 255:
        errors["protocol"] = "IP protocol must be 0-255"
    lo, hi = body.port_lo, body.port_hi
    if hi is not None and lo is None:
        errors["port_lo"] = "A port range needs its first port"
    if lo is not None and not 1 <= lo <= 65535:
        errors["port_lo"] = "Ports are 1-65535"
    if hi is not None and not 1 <= hi <= 65535:
        errors["port_hi"] = "Ports are 1-65535"
    elif lo is not None and hi is not None and hi < lo:
        errors["port_hi"] = "The last port must not be below the first"
    if lo is not None and hi == lo:
        hi = None
    if lo is not None and protocol is not None and protocol not in PORT_PROTOCOLS and "protocol" not in errors:
        name = netflow_classify.protocol_name(protocol)
        errors["protocol"] = (f"{name} has no ports, so a port rule on it could never match; "
                              "remove the ports to classify the whole protocol")
    nets: list[Network] = []
    bad: list[str] = []
    for raw in body.cidrs:
        if not str(raw or "").strip():
            continue
        net, err, note = parse_cidr(raw)
        if err:
            bad.append(err)
        elif net not in nets:
            nets.append(net)
            if note:
                warnings.append(note)
    if bad:
        errors["cidrs"] = "; ".join(bad[:5])
    elif len(nets) > MAX_RULE_CIDRS:
        errors["cidrs"] = f"At most {MAX_RULE_CIDRS} CIDRs per rule"
    if lo is None and hi is None and protocol is None and not nets and not bad:
        errors["port_lo"] = "A rule needs a port, a protocol or an address range"
    if not 0 <= body.priority <= 100000:
        errors["priority"] = "Priority must be 0-100000"
    elif body.priority >= netflow_classify.BUILTIN_PRIORITY:
        warnings.append(f"Priority {body.priority} ranks at or below the built-in rules "
                        f"({netflow_classify.BUILTIN_PRIORITY}); the rule only applies where no built-in rule matches")
    if lo is not None and hi is not None and hi - lo >= 1024:
        warnings.append(f"The range covers {hi - lo + 1} ports; wide ranges also match ephemeral client ports")
    row = {"application": application, "service": service, "category": category, "protocol": protocol,
           "port_lo": lo, "port_hi": hi, "cidrs": [str(n) for n in nets], "priority": body.priority,
           "enabled": body.enabled}
    return row, errors, warnings


def _rule_conflicts(row: dict, db_rules: list[dict], exclude_id: str | None) -> list[str]:
    """Warnings for a port rule whose ports another rule already claims first."""
    if row.get("port_lo") is None:
        return []
    rs = netflow_classify.current_rules()
    others = [_as_app_rule(r) for r in db_rules if r["enabled"] and r["id"] != exclude_id]
    cand = netflow_classify.build_rules(others + [_as_app_rule(row, application=_SENTINEL)],
                                        rs.app_names, rs.ephemeral_min)
    lo = row["port_lo"]
    hi = row["port_hi"] if row["port_hi"] is not None else lo
    protos = [row["protocol"]] if row["protocol"] is not None else [6, 17]
    addr = None
    if row["cidrs"]:
        first = netflow_groups.parse_network(row["cidrs"][0])
        addr = str(first.network_address) if first is not None else None
    shadow: dict[str, list[str]] = {}
    ports = range(lo, hi + 1) if hi - lo < 2048 else list(range(lo, lo + 1024)) + list(range(hi - 1023, hi + 1))
    for proto in protos:
        for port in ports:
            winner = next((c for c in cand.port_clauses if c.matches(proto, port, addr)), None)
            if winner is not None and winner.application != _SENTINEL:
                label = f"{port}/{netflow_classify.protocol_name(proto).lower()}"
                shadow.setdefault(f"{winner.application} ({winner.source})", []).append(label)
    out = []
    for app, ports_hit in shadow.items():
        shown = ", ".join(ports_hit[:6]) + (f" and {len(ports_hit) - 6} more" if len(ports_hit) > 6 else "")
        out.append(f"{shown} already classified as {app} by a rule that ranks first; lower this rule's "
                   "priority number to take precedence")
    return out


def _rule_audit(row: dict) -> dict:
    return {k: row.get(k) for k in ("application", "service", "category", "protocol", "port_lo", "port_hi",
                                    "cidrs", "priority", "enabled")}


def _refresh_rules() -> None:
    netflow_classify.invalidate_rules_cache()


@router.get("/settings/app-rules")
async def list_app_rules(
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
):
    await netflow_classify.load_rules()
    rules = await _rule_rows(db)
    current_meta().set_source("pg")
    return {"items": rules, "can_manage": vis.can(MANAGE_PERMISSION),
            "builtin_priority": netflow_classify.BUILTIN_PRIORITY,
            "counts": {"rules": len(rules), "enabled": sum(1 for r in rules if r["enabled"])}}


async def _get_rule(db: AsyncSession, rule_id: str) -> dict | None:
    try:
        key = uuid.UUID(str(rule_id))
    except ValueError:
        return None
    row = (await db.execute(text(_RULE_SELECT + " WHERE r.id = :id"), {"id": key})).mappings().first()
    return _rule_payload(row) if row else None


_RULE_WRITE_COLS = ("application", "service", "category", "protocol", "port_lo", "port_hi", "priority", "enabled")


def _rule_params(row: dict, uid) -> dict:
    return {**{k: row[k] for k in _RULE_WRITE_COLS}, "cidrs": row["cidrs"] or None, "uid": uid}


@router.post("/settings/app-rules", status_code=201)
async def create_app_rule(
    body: AppRuleIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    row, errors, warnings = validate_rule(body)
    if errors:
        return _invalid(errors)
    await netflow_classify.load_rules()
    warnings += _rule_conflicts(row, await _rule_rows(db), None)
    new_id = (await db.execute(text(
        "INSERT INTO netflow_app_rules (application, service, category, protocol, port_lo, port_hi, cidrs, "
        "priority, enabled, created_by, updated_by) VALUES (:application, :service, :category, :protocol, "
        ":port_lo, :port_hi, CAST(:cidrs AS text[]), :priority, :enabled, :uid, :uid) RETURNING id"),
        _rule_params(row, user.id))).scalar_one()
    await write_audit_log(db, actor=user, action="netflow.app_rule.create", resource_type="netflow_app_rule",
                          resource_id=str(new_id), metadata={"after": _rule_audit(row)})
    await db.commit()
    _refresh_rules()
    await netflow_classify.load_rules(force=True)
    return {"item": await _get_rule(db, str(new_id)), "warnings": warnings}


@router.put("/settings/app-rules/{rule_id}")
async def update_app_rule(
    rule_id: str,
    body: AppRuleIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    current = await _get_rule(db, rule_id)
    if current is None:
        return _not_found("Application rule")
    if body.expected_updated_at and _parse_iso(body.expected_updated_at) != _parse_iso(current["updated_at"]):
        return _conflict(f"This rule was changed by {current['updated_by'] or 'another user'} at "
                         f"{current['updated_at']}; reload it before saving")
    row, errors, warnings = validate_rule(body)
    if errors:
        return _invalid(errors)
    await netflow_classify.load_rules()
    warnings += _rule_conflicts(row, await _rule_rows(db), current["id"])
    await db.execute(text(
        "UPDATE netflow_app_rules SET application = :application, service = :service, category = :category, "
        "protocol = :protocol, port_lo = :port_lo, port_hi = :port_hi, cidrs = CAST(:cidrs AS text[]), "
        "priority = :priority, enabled = :enabled, updated_by = :uid, updated_at = NOW() WHERE id = :id"),
        {**_rule_params(row, user.id), "id": uuid.UUID(current["id"])})
    await write_audit_log(db, actor=user, action="netflow.app_rule.update", resource_type="netflow_app_rule",
                          resource_id=current["id"], metadata={"before": _rule_audit(current), "after": _rule_audit(row)})
    await db.commit()
    _refresh_rules()
    await netflow_classify.load_rules(force=True)
    return {"item": await _get_rule(db, current["id"]), "warnings": warnings}


class AppRulePatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = None


@router.patch("/settings/app-rules/{rule_id}")
async def patch_app_rule(
    rule_id: str,
    body: AppRulePatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    current = await _get_rule(db, rule_id)
    if current is None:
        return _not_found("Application rule")
    if body.priority is not None and not 0 <= body.priority <= 100000:
        return _invalid({"priority": "Priority must be 0-100000"})
    after = {**current}
    if body.enabled is not None:
        after["enabled"] = body.enabled
    if body.priority is not None:
        after["priority"] = body.priority
    await db.execute(text(
        "UPDATE netflow_app_rules SET enabled = :enabled, priority = :priority, updated_by = :uid, "
        "updated_at = NOW() WHERE id = :id"),
        {"enabled": after["enabled"], "priority": after["priority"], "uid": user.id, "id": uuid.UUID(current["id"])})
    await write_audit_log(db, actor=user, action="netflow.app_rule.update", resource_type="netflow_app_rule",
                          resource_id=current["id"], metadata={"before": _rule_audit(current), "after": _rule_audit(after)})
    await db.commit()
    _refresh_rules()
    await netflow_classify.load_rules(force=True)
    return {"item": await _get_rule(db, current["id"]), "warnings": []}


@router.delete("/settings/app-rules/{rule_id}")
async def delete_app_rule(
    rule_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    current = await _get_rule(db, rule_id)
    if current is None:
        return _not_found("Application rule")
    await db.execute(text("DELETE FROM netflow_app_rules WHERE id = :id"), {"id": uuid.UUID(current["id"])})
    await write_audit_log(db, actor=user, action="netflow.app_rule.delete", resource_type="netflow_app_rule",
                          resource_id=current["id"], metadata={"before": _rule_audit(current)})
    await db.commit()
    _refresh_rules()
    await netflow_classify.load_rules(force=True)
    return {"deleted": current["id"]}


class RulePreviewIn(BaseModel):
    rule: AppRuleIn | None = None
    # The saved rule the draft replaces (editing), or the rule whose removal
    # is previewed (no ``rule``).
    replace_id: str | None = None


async def _candidate_rules(db: AsyncSession, body: RulePreviewIn):
    """(current RuleSet, candidate RuleSet, normalized draft | None, errors)."""
    current = await netflow_classify.load_rules()
    db_rules = await _rule_rows(db)
    draft = None
    if body.rule is not None:
        draft, errors, _warn = validate_rule(body.rule)
        if errors:
            return current, None, draft, errors
    replace_id = str(body.replace_id) if body.replace_id else None
    base = [_as_app_rule(r) for r in db_rules if r["enabled"] and r["id"] != replace_id]
    if draft is not None and draft["enabled"]:
        base.append(_as_app_rule(draft))
    candidate = netflow_classify.build_rules(base, current.app_names, current.ephemeral_min)
    return current, candidate, draft, {}


@router.post("/settings/app-rules/preview")
async def preview_app_rule(
    body: RulePreviewIn,
    win: Window = Depends(window_params(1, timedelta(hours=24))),
    f: FlowFilter = Depends(flow_filter),
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """What a draft rule (or removing a saved one) would change: raw flow
    records of the window (default the last hour) classified with the current
    rules and with the candidate set, grouped by (from, to) application."""
    if body.rule is None and not body.replace_id:
        return _invalid({"rule": "Nothing to preview: pass a draft rule or the rule to remove"})
    current, candidate, draft, errors = await _candidate_rules(db, body)
    if errors:
        return _invalid(errors)
    kw = dict(app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr")
    old = netflow_classify.classification_sql("src_port", "dst_port", "protocol", rules=current, **kw)
    new = netflow_classify.classification_sql("src_port", "dst_port", "protocol", rules=candidate, **kw)
    where, params = compile_filter(f, RAW, vis)
    params.update({"p_from": win.start, "p_to": win.end})
    current_meta().set_source("raw", win.start, win.end, 1)
    inner = (f"SELECT {old['application']} AS old_app, {new['application']} AS new_app, "
             f"{new['service_port']} AS new_port, protocol, flow_records.bytes AS b "
             f"FROM {table_ref(RAW)} WHERE timestamp >= %(p_from)s AND timestamp < %(p_to)s AND {where}")
    q_pairs = (f"SELECT old_app, new_app, sum(b), count() FROM ({inner}) "
               "GROUP BY old_app, new_app ORDER BY sum(b) DESC")
    q_examples = (f"SELECT old_app, new_app, protocol, new_port, sum(b) AS eb, count() FROM ({inner}) "
                  "WHERE old_app != new_app GROUP BY old_app, new_app, protocol, new_port "
                  "ORDER BY eb DESC LIMIT 3 BY old_app, new_app")
    pairs, examples = await asyncio.gather(
        run_query(q_pairs, params, label="settings-rule-preview"),
        run_query(q_examples, params, label="settings-rule-preview-examples"),
    )
    ex: dict[tuple[str, str], list[dict]] = {}
    for o, n, proto, port, b, c in examples.result_rows:
        ex.setdefault((o, n), []).append({
            "protocol": int(proto), "port": int(port) or None,
            "service": netflow_classify.service_name(int(proto), int(port), candidate),
            "bytes": int(b or 0), "flows": int(c or 0),
        })
    total_b = total_f = changed_b = changed_f = same_b = same_f = 0
    changes = []
    app = draft["application"] if draft else None
    for o, n, b, c in pairs.result_rows:
        b, c = int(b or 0), int(c or 0)
        total_b += b
        total_f += c
        if o != n:
            changed_b += b
            changed_f += c
            changes.append({"from": o, "to": n, "bytes": b, "flows": c, "examples": ex.get((o, n), [])})
        elif app and o == app:
            same_b += b
            same_f += c
    return {
        "window": {"from": _to_iso_utc(win.start), "to": _to_iso_utc(win.end)},
        "total": {"bytes": total_b, "flows": total_f},
        "changed": {"bytes": changed_b, "flows": changed_f,
                    "share": (changed_b / total_b) if total_b else None},
        "already": {"application": app, "bytes": same_b, "flows": same_f},
        "changes": changes[:50],
        "truncated": len(changes) > 50,
        "basis": "Raw flow records in the window, classified with the current rules and with the change applied.",
    }


def _builtin_catalogue(rs) -> list[dict]:
    apps: dict[str, dict] = {}
    for c in rs.port_clauses:
        if c.source != "builtin":
            continue
        entry = apps.setdefault(c.application, {"application": c.application,
                                                "category": rs.categories.get(c.application, "Other"),
                                                "ports": {}, "protocols": []})
        for port, label in sorted(c.labels.items()):
            p = entry["ports"].setdefault(port, {"port": port, "service": label, "transports": []})
            for proto in sorted(c.protocols or ()):
                name = netflow_classify.protocol_name(proto)
                if name not in p["transports"]:
                    p["transports"].append(name)
    for c in rs.protocol_clauses:
        if c.source != "builtin":
            continue
        entry = apps.setdefault(c.application, {"application": c.application,
                                                "category": rs.categories.get(c.application, "Other"),
                                                "ports": {}, "protocols": []})
        for proto in sorted(c.protocols or ()):
            entry["protocols"].append({"protocol": proto, "name": netflow_classify.protocol_name(proto)})
    out = []
    for entry in apps.values():
        entry["ports"] = [entry["ports"][k] for k in sorted(entry["ports"])]
        out.append(entry)
    return sorted(out, key=lambda e: e["application"].lower())


@router.get("/settings/applications")
async def application_catalogue(vis: FlowVisibility = Depends(flow_visibility)):
    """The built-in classification rules (read-only), how precedence works,
    and every application name the classifier can currently produce."""
    rs = await netflow_classify.load_rules()
    current_meta().set_source("pg")
    return {
        "builtin": _builtin_catalogue(rs),
        "builtin_priority": netflow_classify.BUILTIN_PRIORITY,
        "ephemeral_min": rs.ephemeral_min,
        "operator_rules": rs.db_rules,
        "exporter_names": len(rs.app_names),
        "applications": netflow_classify.known_applications(rs),
        "categories": sorted(set(rs.categories.values()), key=str.lower),
        "precedence": [
            "The exporter's own application id (NBAR2 / App-ID), when it sent one and announced its name",
            "Operator rules, lowest priority number first (default 100); at equal priority, operator rules "
            "before built-ins and narrower port ranges before wider ones",
            f"Built-in rules (priority {netflow_classify.BUILTIN_PRIORITY})",
            "Unmatched well-known ports (1-1023): System Services; everything else: Other",
        ],
        "service_port": (
            "Port rules apply to whichever side carries the service port, so responses (443 -> client port) "
            "count as the same service as requests. When both ports match a rule, the non-ephemeral (below "
            f"{rs.ephemeral_min}) or lower port wins."
        ),
    }


@router.get("/settings/applications/exporter-names")
async def exporter_application_names(
    win: Window = Depends(window_params(24, timedelta(days=31))),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """Application names exporters announced (NBAR2 / App-ID option records,
    IE 95 -> 96/94/372) and how much traffic carried each id in the window."""
    name_where = "app_id != 0"
    params: dict[str, Any] = {}
    if vis.exporters is not None:
        name_where += " AND exporter IN %(vis_names)s"
        params["vis_names"] = sorted(vis.exporters) or [""]
    q_names = (
        "SELECT app_id, argMax(name, updated_at), argMax(description, updated_at), argMax(category, updated_at), "
        "argMax(exporter, updated_at), max(updated_at), uniqExact(exporter) "
        f"FROM {table_ref('flow_app_names')} WHERE {name_where} GROUP BY app_id ORDER BY 2 LIMIT 5000"
    )
    where, fparams = compile_filter(f, RAW, vis)
    fparams.update({"u_from": win.start, "u_to": win.end})
    current_meta().set_source("raw", win.start, win.end, 1)
    q_usage = (
        f"SELECT app_id, sum(bytes), count() FROM {table_ref(RAW)} "
        f"WHERE timestamp >= %(u_from)s AND timestamp < %(u_to)s AND {where} AND app_id != 0 GROUP BY app_id"
    )
    q_cov = (
        f"SELECT count(), countIf(bitTest(fields_present, 10)), countIf(app_id != 0), countIf(fields_present = 0) "
        f"FROM {table_ref(RAW)} WHERE timestamp >= %(u_from)s AND timestamp < %(u_to)s AND {where}"
    )
    names, usage, cov = await asyncio.gather(
        run_query(q_names, params, label="settings-app-names"),
        run_query(q_usage, fparams, label="settings-app-usage"),
        run_query(q_cov, fparams, label="settings-app-coverage"),
    )
    used = {int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in usage.result_rows}
    rs = netflow_classify.current_rules()
    items = []
    for app_id, name, desc, cat, exporter, updated, n_exp in names.result_rows:
        b, c = used.pop(int(app_id), (0, 0))
        mapped = rs.app_names.get(int(app_id))
        items.append({
            "app_id": str(int(app_id)), "engine": int(app_id) >> 32, "selector": int(app_id) & 0xFFFFFFFF,
            "name": name or None, "description": desc or None, "category": cat or None,
            # The application this id is classified as ("icmp" counts as ICMP).
            "application": mapped[0] if mapped else None,
            "exporter": exporter or None, "exporters": int(n_exp or 0), "updated_at": _to_iso_utc(updated),
            "bytes": b, "flows": c,
        })
    unnamed = [{"app_id": str(k), "engine": k >> 32, "selector": k & 0xFFFFFFFF, "bytes": v[0], "flows": v[1]}
               for k, v in sorted(used.items(), key=lambda kv: -kv[1][0])[:100]]
    records, field_records, id_records, legacy = (int(x or 0) for x in (cov.result_rows[0] if cov.result_rows else (0, 0, 0, 0)))
    return {
        "items": items,
        "unnamed": unnamed,
        "coverage": {
            "records": records, "with_app_id_field": field_records, "with_app_id": id_records,
            "legacy_records": legacy,
            "share": (id_records / records) if records else None,
        },
    }


class ClassifyTestIn(BaseModel):
    protocol: int = Field(ge=0, le=255)
    src_port: int = Field(default=0, ge=0, le=65535)
    dst_port: int = Field(default=0, ge=0, le=65535)
    src_ip: str | None = None
    dst_ip: str | None = None
    app_id: int = Field(default=0, ge=0)
    rule: AppRuleIn | None = None
    replace_id: str | None = None


@router.post("/settings/applications/test")
async def test_classification(
    body: ClassifyTestIn,
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """How the classifier labels one flow tuple, now and with a draft rule."""
    errors: dict[str, str] = {}
    ips: dict[str, str | None] = {}
    for key in ("src_ip", "dst_ip"):
        raw = getattr(body, key)
        if raw:
            ip = canonical_ip(raw)
            if ip is None or "/" in raw:
                errors[key] = "Enter a single IP address"
            ips[key] = ip
        else:
            ips[key] = None
    if (ips["src_ip"] is None) != (ips["dst_ip"] is None) and not errors:
        errors["dst_ip" if ips["src_ip"] else "src_ip"] = "Enter both addresses, or neither"
    if errors:
        return _invalid(errors)
    current, candidate, _draft, rule_errors = await _candidate_rules(
        db, RulePreviewIn(rule=body.rule, replace_id=body.replace_id))
    if rule_errors:
        return _invalid(rule_errors)
    kw = {"src_ip": ips["src_ip"], "dst_ip": ips["dst_ip"]}
    now = netflow_classify.classify(body.protocol, body.src_port, body.dst_port, body.app_id, rules=current, **kw)
    out = {"current": now.to_dict(), "candidate": None}
    if body.rule is not None or body.replace_id:
        out["candidate"] = netflow_classify.classify(body.protocol, body.src_port, body.dst_port, body.app_id,
                                                     rules=candidate, **kw).to_dict()
    return out


# ── Enrichment: reverse DNS and GeoIP ───────────────────────────────────────

async def load_enrichment_settings(db: AsyncSession | None = None) -> dict:
    """``{"rdns": {"enabled", "deadline_s"}}`` with defaults filled in.

    Other NetFlow endpoints that resolve names (``resolve=1`` breakdowns) can
    call this so the operator's switch applies to them too."""
    async def read(session: AsyncSession):
        row = (await session.execute(text("SELECT value, updated_at FROM system_settings WHERE key = :k"),
                                     {"k": ENRICHMENT_KEY})).first()
        return row

    try:
        if db is None:
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as own:
                row = await read(own)
        else:
            row = await read(db)
    except Exception:
        logger.warning("netflow enrichment settings unreadable; using defaults", exc_info=True)
        row = None
    value = row[0] if row is not None else {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    rdns = dict(RDNS_DEFAULTS)
    if isinstance(value, dict) and isinstance(value.get("rdns"), dict):
        src = value["rdns"]
        if isinstance(src.get("enabled"), bool):
            rdns["enabled"] = src["enabled"]
        d = src.get("deadline_s")
        if isinstance(d, (int, float)) and RDNS_DEADLINE_MIN_S <= float(d) <= RDNS_DEADLINE_MAX_S:
            rdns["deadline_s"] = float(d)
    return {"rdns": rdns, "updated_at": _iso(row[1]) if row is not None else None}


_RDNS_CONFIG_TTL_S = 30.0
_rdns_config_cache: dict[str, Any] = {"at": 0.0, "value": None}


async def rdns_config() -> dict:
    """``{"enabled", "deadline_s"}`` for any endpoint that resolves names
    (``resolve=1`` breakdowns): the operator's switch and deadline, cached
    30 s in this process and refreshed at once by a settings write here."""
    now = time.monotonic()
    if _rdns_config_cache["value"] is None or now - _rdns_config_cache["at"] > _RDNS_CONFIG_TTL_S:
        _rdns_config_cache["value"] = (await load_enrichment_settings())["rdns"]
        _rdns_config_cache["at"] = now
    return dict(_rdns_config_cache["value"])


def rdns_cache_stats() -> dict:
    now = time.monotonic()
    positive = negative = expired = 0
    with common._rdns_lock:
        for expiry, host in common._rdns_cache.values():
            if expiry <= now:
                expired += 1
            elif host:
                positive += 1
            else:
                negative += 1
        pending = len(common._rdns_inflight)
    return {"names": positive, "no_record": negative, "expired": expired, "pending": pending,
            "ttl_s": int(common._RDNS_TTL), "negative_ttl_s": int(common._RDNS_NEGATIVE_TTL)}


def _resolvers() -> list[str]:
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            return [line.split()[1] for line in fh if line.strip().startswith("nameserver") and len(line.split()) > 1][:6]
    except OSError:
        return []


def _mmdb_metadata(path: str) -> dict:
    """Metadata of a MaxMind DB file (raises when it is not one)."""
    reader = geoip.MMDBReader(path)
    buf = reader._buf
    marker = buf.rfind(geoip._METADATA_MARKER)
    start = marker + len(geoip._METADATA_MARKER)
    meta, _ = reader._decode(start, base=start)
    return {"reader": reader, **(meta if isinstance(meta, dict) else {})}


def geoip_status() -> dict:
    """What GeoIP databases the appliance has, how old they are and whether
    they work, per kind."""
    directory = geoip.GEOIP_DIR
    out = []
    now = datetime.now(timezone.utc)
    for kind, filename in GEOIP_KINDS.items():
        path = os.path.join(directory, filename)
        item: dict[str, Any] = {"kind": kind, "file": filename, "present": False, "valid": None,
                                "size": None, "modified_at": None, "build_date": None, "age_days": None,
                                "database_type": None, "description": None, "stamp": None, "stale": None,
                                "error": None, "sample": None}
        try:
            st = os.stat(path)
        except OSError:
            out.append(item)
            continue
        item.update(present=True, size=st.st_size,
                    modified_at=_to_iso_utc(datetime.fromtimestamp(st.st_mtime, timezone.utc)))
        try:
            with open(path + ".stamp", encoding="utf-8") as fh:
                item["stamp"] = fh.read().strip()[:64] or None
        except OSError:
            pass
        try:
            meta = _mmdb_metadata(path)
            built = meta.get("build_epoch")
            if isinstance(built, int) and built > 0:
                bdt = datetime.fromtimestamp(built, timezone.utc)
                item["build_date"] = _to_iso_utc(bdt)
                item["age_days"] = (now - bdt).days
                item["stale"] = item["age_days"] > GEOIP_STALE_DAYS
            item["database_type"] = meta.get("database_type")
            desc = meta.get("description")
            item["description"] = desc.get("en") if isinstance(desc, dict) else None
            item["valid"] = True
            sample_ip = "1.1.1.1"
            rec = meta["reader"].get(sample_ip) or {}
            if kind == "country":
                c = rec.get("country") or rec.get("registered_country") or {}
                item["sample"] = {"ip": sample_ip, "value": c.get("iso_code")}
            else:
                item["sample"] = {"ip": sample_ip, "value": rec.get("autonomous_system_number")}
        except Exception as exc:  # noqa: BLE001 -- report, never fail the page
            item["valid"] = False
            item["error"] = f"Not a readable MaxMind database ({exc.__class__.__name__})"
        out.append(item)
    return {
        "directory": directory,
        "writable": os.access(directory, os.W_OK) if os.path.isdir(directory) else False,
        "available": geoip.available(),
        "databases": out,
        "attribution": "IP geolocation by DB-IP (https://db-ip.com), CC BY 4.0, when the bundled DB-IP Lite "
                       "databases are used.",
        "refresh": "Appliances with internet access refresh the DB-IP Lite databases monthly "
                   "(scripts/fetch-geoip.py); an uploaded database is replaced by that refresh.",
        "stale_after_days": GEOIP_STALE_DAYS,
    }


@router.get("/settings/enrichment")
async def enrichment_status(
    db: AsyncSession = Depends(get_db),
    vis: FlowVisibility = Depends(flow_visibility),
):
    cfg = await load_enrichment_settings(db)
    current_meta().set_source("pg")
    return {
        "rdns": {**cfg["rdns"], "updated_at": cfg["updated_at"], "cache": rdns_cache_stats(),
                 "resolvers": _resolvers(),
                 "limits": {"deadline_min_s": RDNS_DEADLINE_MIN_S, "deadline_max_s": RDNS_DEADLINE_MAX_S},
                 "notes": [
                     "Names come from PTR lookups by the appliance's resolver; a name reflects DNS at "
                     "lookup time, not necessarily when the flow happened.",
                     "Lookups still running at the deadline finish in the background and appear on the "
                     "next request; they are shown as pending, never as 'no name'.",
                 ]},
        "geoip": await asyncio.to_thread(geoip_status),
        "can_manage": vis.can(MANAGE_PERMISSION),
    }


class RdnsSettingsIn(BaseModel):
    enabled: bool
    deadline_s: float = Field(default=RDNS_DEFAULTS["deadline_s"])


@router.put("/settings/enrichment/rdns")
async def update_rdns_settings(
    body: RdnsSettingsIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    if not RDNS_DEADLINE_MIN_S <= body.deadline_s <= RDNS_DEADLINE_MAX_S:
        return _invalid({"deadline_s": f"The deadline must be {RDNS_DEADLINE_MIN_S}-{RDNS_DEADLINE_MAX_S:g} seconds"})
    before = await load_enrichment_settings(db)
    value = {"rdns": {"enabled": body.enabled, "deadline_s": round(float(body.deadline_s), 2)}}
    await db.execute(text(
        "INSERT INTO system_settings (key, value, updated_at, updated_by) VALUES (:k, CAST(:v AS jsonb), NOW(), :u) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by"),
        {"k": ENRICHMENT_KEY, "v": json.dumps(value), "u": user.id})
    await write_audit_log(db, actor=user, action="netflow.enrichment.update", resource_type="netflow_settings",
                          resource_id=ENRICHMENT_KEY, metadata={"before": before["rdns"], "after": value["rdns"]})
    await db.commit()
    out = await load_enrichment_settings(db)
    _rdns_config_cache.update(at=time.monotonic(), value=dict(out["rdns"]))
    return out


@router.post("/settings/enrichment/rdns/flush")
async def flush_rdns_cache(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    with common._rdns_lock:
        dropped = len(common._rdns_cache)
        common._rdns_cache.clear()
    await write_audit_log(db, actor=user, action="netflow.enrichment.rdns_flush", resource_type="netflow_settings",
                          resource_id=ENRICHMENT_KEY, metadata={"dropped": dropped})
    await db.commit()
    return {"dropped": dropped, "cache": rdns_cache_stats()}


class RdnsTestIn(BaseModel):
    ip: str


@router.post("/settings/enrichment/rdns/test")
async def test_rdns(
    body: RdnsTestIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    ip = canonical_ip(body.ip)
    if ip is None or "/" in body.ip:
        return _invalid({"ip": "Enter a single IP address"})
    cfg = (await load_enrichment_settings(db))["rdns"]
    started = time.monotonic()
    result = await asyncio.to_thread(common._resolve_hostnames, [ip], cfg["deadline_s"])
    elapsed = int((time.monotonic() - started) * 1000)
    host = result.get(ip)
    status = "pending" if host is common.RDNS_PENDING else ("ok" if host else "no_record")
    return {"ip": ip, "status": status, "hostname": host if isinstance(host, str) else None,
            "elapsed_ms": elapsed, "deadline_s": cfg["deadline_s"], "enabled": cfg["enabled"]}


def _read_upload(raw: bytes) -> bytes:
    """The .mmdb bytes from a plain, gzip'ed or tar.gz upload (bounded)."""
    if raw[:2] == b"\x1f\x8b":
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                for member in tar:
                    if member.isfile() and member.name.endswith(".mmdb"):
                        if member.size > MAX_GEOIP_BYTES:
                            raise ValueError("The database inside the archive is too large")
                        fh = tar.extractfile(member)
                        if fh is not None:
                            return fh.read(MAX_GEOIP_BYTES + 1)
                raise ValueError("The archive contains no .mmdb file")
        except tarfile.ReadError:
            pass
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(raw, MAX_GEOIP_BYTES + 1)
        if len(out) > MAX_GEOIP_BYTES:
            raise ValueError("The decompressed database is too large")
        return out
    return raw


@router.post("/settings/enrichment/geoip")
async def upload_geoip(
    kind: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    """Install a GeoIP database from a file (air-gapped appliances): a
    MaxMind DB (.mmdb), gzip'ed, or a .tar.gz containing one. It is checked
    (format, database type, a sample lookup) before it replaces the current
    file atomically; the GeoIP service reloads it on the next lookup."""
    if kind not in GEOIP_KINDS:
        return _invalid({"kind": "Choose country or asn"})
    directory = geoip.GEOIP_DIR
    raw = await file.read(MAX_GEOIP_BYTES + 1)
    if len(raw) > MAX_GEOIP_BYTES:
        return _invalid({"file": f"The file is larger than {MAX_GEOIP_BYTES // (1024 * 1024)} MB"})
    if not raw:
        return _invalid({"file": "The file is empty"})
    try:
        data = await asyncio.to_thread(_read_upload, raw)
    except (ValueError, OSError, EOFError, zlib.error) as exc:
        return _invalid({"file": str(exc) if isinstance(exc, ValueError) else "The archive could not be read"})
    if len(data) > MAX_GEOIP_BYTES:
        return _invalid({"file": "The database is too large"})
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    if not os.access(directory, os.W_OK):
        return JSONResponse({"detail": f"The GeoIP directory {directory} is not writable by the API service",
                             "code": "unavailable", "error_id": None}, status_code=503)
    final = os.path.join(directory, GEOIP_KINDS[kind])
    tmp = f"{final}.upload-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            meta = await asyncio.to_thread(_mmdb_metadata, tmp)
        except Exception:
            return _invalid({"file": "This is not a MaxMind DB (.mmdb) file"})
        dbtype = str(meta.get("database_type") or "")
        wanted = ("country", "city") if kind == "country" else ("asn",)
        if not any(w in dbtype.lower() for w in wanted):
            return _invalid({"file": f"This is a '{dbtype or 'unknown'}' database, not a {kind} database"})
        try:
            meta["reader"].get("8.8.8.8")
            meta["reader"].get("2001:4860:4860::8888")
        except Exception:
            return _invalid({"file": "The database could not be searched; it may be truncated"})
        built = meta.get("build_epoch")
        build_date = datetime.fromtimestamp(built, timezone.utc) if isinstance(built, int) and built > 0 else None
        os.replace(tmp, final)
        tmp = None
        try:
            with open(final + ".stamp", "w", encoding="utf-8") as fh:
                fh.write(f"upload:{build_date.strftime('%Y-%m-%d') if build_date else 'unknown'}")
        except OSError:
            pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    await write_audit_log(db, actor=user, action="netflow.enrichment.geoip_upload", resource_type="netflow_settings",
                          resource_id=kind, metadata={"file": file.filename, "size": len(data),
                                                      "database_type": dbtype,
                                                      "build_date": _to_iso_utc(build_date)})
    await db.commit()
    return await asyncio.to_thread(geoip_status)


# ── What flow export cannot see (FE-09: NAT, tunnels, App-ID) ──────────────

@router.get("/settings/visibility")
async def visibility_notes(
    win: Window = Depends(window_params(24, timedelta(days=7))),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
):
    """Measured limits of the flow data in the window: how much traffic is
    tunnelled (its inner applications are invisible), which exporters send
    post-NAT addresses, and which send application ids."""
    await netflow_classify.load_rules()
    cls = netflow_classify.classification_sql("src_port", "dst_port", "protocol", app_id_expr="app_id",
                                              src_addr="src_addr", dst_addr="dst_addr")
    # Record counts cover every record (NSEL create/deny events carry the NAT
    # and firewall fields too); byte sums apply the volume guard (§3.3).
    where, params = compile_filter(f, RAW, vis, volume_guard=False)
    params.update({"v_from": win.start, "v_to": win.end, "v_vpn": netflow_classify.APP_VPN})
    current_meta().set_source("raw", win.start, win.end, 1)
    base = f"FROM {table_ref(RAW)} WHERE timestamp >= %(v_from)s AND timestamp < %(v_to)s AND {where}"
    q_exp = (
        f"SELECT {EXPORTER} AS e, count(), sumIf(bytes, {VOLUME_GUARD}), "
        f"sumIf(bytes, {VOLUME_GUARD} AND {cls['application']} = %(v_vpn)s), "
        "countIf(bitTest(fields_present, 12)), "
        "countIf(xlate_src_addr6 != toIPv6('::') OR xlate_dst_addr6 != toIPv6('::')), "
        "countIf(app_id != 0), countIf(fields_present = 0), countIf(fw_event != 0) "
        f"{base} GROUP BY e ORDER BY count() DESC LIMIT 500"
    )
    q_tun = (
        f"SELECT protocol, {cls['service_port']} AS sp, sum(bytes), count() {base} "
        f"AND {VOLUME_GUARD} AND {cls['application']} = %(v_vpn)s GROUP BY protocol, sp ORDER BY sum(bytes) DESC LIMIT 20"
    )
    exp_res, tun_res = await asyncio.gather(
        run_query(q_exp, params, label="settings-visibility"),
        run_query(q_tun, params, label="settings-visibility-tunnels"),
    )
    exporters = []
    tot = {"records": 0, "bytes": 0, "tunnel_bytes": 0, "nat_field_records": 0, "translated_records": 0,
           "app_id_records": 0, "legacy_records": 0, "fw_event_records": 0}
    for e, n, b, tb, nat_f, xl, aid, legacy, fw in exp_res.result_rows:
        row = {"exporter": canonical_ip(e), "records": int(n), "bytes": int(b or 0), "tunnel_bytes": int(tb or 0),
               "nat_field_records": int(nat_f), "translated_records": int(xl), "app_id_records": int(aid),
               "legacy_records": int(legacy), "fw_event_records": int(fw)}
        exporters.append(row)
        for k in tot:
            tot[k] += row[k]
    tunnels = []
    for proto, sp, b, c in tun_res.result_rows:
        label = netflow_classify.service_name(int(proto), int(sp)) if int(sp) else None
        tunnels.append({"protocol": int(proto), "protocol_name": netflow_classify.protocol_name(int(proto)),
                        "service": label, "bytes": int(b or 0), "flows": int(c)})
    records = tot["records"]
    return {
        "totals": {**tot, "tunnel_share": (tot["tunnel_bytes"] / tot["bytes"]) if tot["bytes"] else None,
                   "nat_share": (tot["nat_field_records"] / records) if records else None,
                   "app_id_share": (tot["app_id_records"] / records) if records else None,
                   "legacy_share": (tot["legacy_records"] / records) if records else None},
        "exporters": exporters,
        "tunnels": tunnels,
        "notes": {
            "tunnels": "Encrypted or encapsulated traffic (IPsec ESP/AH, GRE, IP-in-IP, IKE/NAT-T, OpenVPN, "
                       "L2TP, VXLAN) is counted as 'VPN / Tunneling': flow export sees the tunnel endpoints, "
                       "not the hosts or applications inside it. Export flows from a device that sees the "
                       "decrypted traffic to attribute it.",
            "nat": "An exporter reports addresses as it sees them at its observation point: behind a NAT "
                   "device you see private hosts, in front of it the translated public address. Only "
                   "exporters that send NAT fields (e.g. Cisco ASA/FTD NSEL, IE 225-228) let one record "
                   "show both.",
            "app_id": "Application ids (NBAR2 / App-ID) are only available from exporters configured to send "
                      "them; everything else is classified from protocol and ports.",
        },
    }


# ── Audit trail ─────────────────────────────────────────────────────────────

_HISTORY_TYPES = {
    "ip_group": "netflow_ip_group",
    "app_rule": "netflow_app_rule",
    "enrichment": "netflow_settings",
}


@router.get("/settings/history")
async def settings_history(
    resource: Literal["ip_group", "app_rule", "enrichment"] = Query(...),
    id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_manage),
):
    """Who changed IP groups, application rules or enrichment, and when."""
    sql = ("SELECT created_at, actor_username, action, resource_id, metadata FROM audit_logs "
           "WHERE resource_type = :t")
    params: dict[str, Any] = {"t": _HISTORY_TYPES[resource], "n": limit}
    if id:
        sql += " AND (resource_id = :id OR resource_id IS NULL)"
        params["id"] = id
    sql += " ORDER BY created_at DESC LIMIT :n"
    rows = (await db.execute(text(sql), params)).mappings().all()
    current_meta().set_source("pg")
    items = []
    for r in rows:
        md = r["metadata"]
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except ValueError:
                md = {}
        items.append({"at": _iso(r["created_at"]), "actor": r["actor_username"], "action": r["action"],
                      "resource_id": r["resource_id"], "metadata": md})
    return {"items": items}
