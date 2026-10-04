"""NetFlow top-N and distribution breakdowns (contract §2, B2).

Talkers, endpoints, conversations, protocols, services (ports), applications,
interfaces, DSCP, TCP flags, network classes, countries, ASNs, next hops and
IP groups.

Every endpoint here follows the same rules:

* **One filter, one scope.** The window comes from ``window_params``, the
  filter from ``flow_filter`` (every §8 key, validated) and the caller's
  exporter scope from ``flow_visibility``; the SQL is compiled per table by
  ``compile_filter`` (visibility + volume guard + filters). A breakdown only
  drops the dimension it breaks down by (the DSCP chart ignores ``dscp`` so it
  does not collapse to one bar); dropped keys are reported through
  ``meta.filters`` so the UI never claims a filter it did not apply.
* **One planner.** ``sql.plan_source`` picks raw records, a rollup or both
  (rollup interior + raw edges), half-open, with retention cuts disclosed.
* **One classifier.** Service, application, direction and confidence come
  from ``services/netflow_classify`` with exactly the arguments the ``app`` and
  ``service_port`` filters use, so a drill-down returns the flows behind the
  item that was clicked (NF-20).
* **Whole-scope denominators (NF-25).** With ``with_totals=1`` a list endpoint
  returns ``{items, total, distinct, limit, offset, basis, additive,
  remainder}``: ``total`` is the whole filtered scope (not the sum of the rows
  on screen), ``additive`` says whether the items partition it (sent bytes do,
  endpoint involvement does not: a flow counts for both of its endpoints).
  Without it the legacy list shape is returned unchanged for the dashboards,
  device page and maps widget that read these endpoints.
* **Unknown is not zero.** Fields an exporter did not send (TCP flags, flow
  durations, next hops) are reported as unknown with the share affected,
  never as zeros.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Mapping

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.device_interface import DeviceInterface
from app.services import geoip, netflow_groups
from app.services import netflow_classify as nc
from app.services.interface_speed import effective_speed_source

from . import common as C
from .access import FlowVisibility, canonical_ip, flow_visibility, resolve_exporter_owners
from .common import (
    CONVERSATIONS_1H,
    INTERFACE_5M,
    QOS_1H,
    RAW,
    Window,
    _dscp_label,
    _enrich_hosts,
    _to_iso_utc,
    current_meta,
    now_utc,
    run_query,
    window_params,
)
from .filters import FlowFilter, compile_filter, flow_filter, require_filter_exporters_visible
from .overview import _FLAG_CONDS as OVERVIEW_FLAG_CONDS
from .overview import rst_payload
from .sql import (
    DST,
    EXPORTER,
    NET_CLASSES,
    NEXT_HOP,
    SPECIAL_USE_CLASS,
    SRC,
    TOS_KNOWN_RAW,
    SourcePlan,
    ip_param,
    netclass_expr,
    plan_source,
    table_caps,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request context ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ctx:
    """Window, filter and visibility of one breakdown request."""

    win: Window
    f: FlowFilter
    vis: FlowVisibility


def _ctx(default_hours: int):
    """Dependency factory: resolved window (``default_hours`` when the request
    names none), validated FlowFilter and the caller's visibility. Naming an
    exporter outside the caller's scope is a 404."""

    async def dependency(
        win: Window = Depends(window_params(default_hours)),
        f: FlowFilter = Depends(flow_filter),
        vis: FlowVisibility = Depends(flow_visibility),
    ) -> Ctx:
        require_filter_exporters_visible(f, vis)
        if vis.restricted:
            current_meta().restricted = True
        return Ctx(win, f, vis)

    return dependency


# ── SQL assembly ──────────────────────────────────────────────────────────────

# A column is one expression for every table, or (rollup expression, raw
# expression) where the two need different spellings.
Column = str | tuple[str, str]

FLOWS: Column = ("flows", "toUInt64(1)")
FIRST: Column = ("first_seen", "timestamp")
LAST: Column = ("last_seen", "timestamp")

# The unified address of a row is IPv4 when it sits in ::ffff:0:0/96.
_V4_MAPPED = "BETWEEN toIPv6('::ffff:0.0.0.0') AND toIPv6('::ffff:255.255.255.255')"

# Raw duration validity (contract §3.4, NF-18): both switched times present,
# ordered, on the same clock base, at most a day apart.
_DURATION_VALID = (
    "(first_switched_ms > 0 AND last_switched_ms >= first_switched_ms"
    " AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)"
    " AND last_switched_ms - first_switched_ms <= 86400000)"
)
# A raw TCP record whose flags are observations: exported (fields_present
# bit 0), or a legacy record carrying non-zero flags.
_FLAGGED_RAW = "(protocol = 6 AND (bitTest(fields_present, 0) OR (fields_present = 0 AND tcp_flags != 0)))"


def _union(
    plan: SourcePlan,
    columns: Mapping[str, Column],
    ctx: Ctx,
    drop: Iterable[str] = (),
    *,
    where: Column | None = None,
    params: dict | None = None,
    raw_join: str = "",
) -> tuple[str, dict]:
    """``(subquery, params)``: one SELECT per table of the plan, UNION ALL-ed.

    The same assembly as ``SourcePlan.from_sql`` (half-open time ranges per
    segment, ``compile_filter`` per table so visibility, volume guard and
    filters always apply) plus two hooks it lacks: an extra predicate
    (``where``, e.g. "only rows touching these hosts" for a detail pass) and
    an ARRAY JOIN on the raw part (``raw_join``, interface rows per direction).
    """
    drop = frozenset(drop)
    by_table: dict[str, list] = {}
    for seg in plan.segments:
        by_table.setdefault(seg.table, []).append(seg)
    if not by_table:
        # Nothing retained covers the window: empty, with the expected columns.
        by_table = {RAW: []}
    out = dict(params or {})
    parts: list[str] = []
    n = 0
    for table, segs in by_table.items():
        caps = table_caps(table)
        is_raw = caps.kind == "raw"

        def pick(expr: Column) -> str:
            return (expr[1] if is_raw else expr[0]) if isinstance(expr, tuple) else expr

        exprs = ", ".join(f"{pick(e)} AS {alias}" for alias, e in columns.items())
        ranges = []
        for seg in segs:
            ranges.append(f"({caps.time_col} >= %(nf_t{n}_from)s AND {caps.time_col} < %(nf_t{n}_to)s)")
            out[f"nf_t{n}_from"] = seg.start
            out[f"nf_t{n}_to"] = seg.end
            n += 1
        fsql, fparams = compile_filter(ctx.f, table, ctx.vis, drop)
        out.update(fparams)
        join = f" {raw_join}" if is_raw and raw_join else ""
        sql = f"SELECT {exprs} FROM {caps.ref}{join} WHERE ({' OR '.join(ranges) or '0'}) AND {fsql}"
        extra = pick(where) if where is not None else ""
        if extra:
            sql += f" AND ({extra})"
        parts.append(sql)
    return "(\n" + "\nUNION ALL\n".join(parts) + "\n)", out


async def _plan(ctx: Ctx, rollup: str | None, drop: Iterable[str] = ()) -> SourcePlan:
    return await plan_source(ctx.win, ctx.f, rollup=rollup, drop=frozenset(drop))


def _classifier_columns() -> dict[str, Column]:
    """Service port, direction, application, confidence and basis per row,
    built exactly like the ``app``/``service_port`` filters build them
    (filters._classifier_sql), so breakdown rows and drill-downs agree."""
    kw = dict(app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr")
    raw = nc.classification_sql("src_port", "dst_port", "protocol", **kw)
    roll = nc.classification_sql(nc.ROLLUP_SRC_PORT_SQL, "dst_port", "protocol", **kw)
    names = {"x_sp": "service_port", "x_dir": "direction", "x_app": "application",
             "x_conf": "confidence", "x_basis": "basis"}
    return {alias: (roll[key], raw[key]) for alias, key in names.items()}


# The service key of a row: the classified service port, or for ICMP the
# type/code the collector carries in dst_port.
_SVC = "if(x_proto IN (1, 58), x_dport, x_sp)"


def _prefix(expr: str) -> str:
    """The /24 (IPv4) or /48 (IPv6) network of a unified address, for GeoIP."""
    return (f"if({expr} {_V4_MAPPED}, IPv6CIDRToRange({expr}, 120).1, "
            f"IPv6CIDRToRange({expr}, 48).1)")


# ── Small helpers ─────────────────────────────────────────────────────────────

def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ip(v: Any) -> str | None:
    """Display text of an address value (IPv4-mapped IPv6 becomes IPv4)."""
    if v is None:
        return None
    if isinstance(v, ipaddress.IPv6Address) and v.ipv4_mapped is not None:
        return str(v.ipv4_mapped)
    return canonical_ip(v) or str(v)


def _row(res, width: int) -> list:
    """The first result row padded to ``width`` (an aggregate without GROUP BY
    always returns one row; a stub client may return fewer columns)."""
    rows = getattr(res, "result_rows", None) or [()]
    row = list(rows[0])
    return row + [None] * (width - len(row))


def _pct(part: float, whole: float) -> float | None:
    """Percent of ``whole``; six decimals so a small non-zero share never
    rounds to an exact 0 (the UI prints it as "<0.1%")."""
    return round(100.0 * part / whole, 6) if whole else None


def _totals(b: Any, p: Any, n: Any) -> dict:
    return {"bytes": _i(b), "packets": _i(p), "flows": _i(n)}


def _paged(
    items: list[dict],
    *,
    total: dict | None,
    distinct: int | None,
    limit: int,
    offset: int,
    basis: str,
    additive: bool,
    measure: Mapping[str, str] | None = None,
    **extra: Any,
) -> dict:
    """The ``with_totals=1`` body. ``measure`` maps total keys to the item key
    that partitions them (e.g. bytes -> src_bytes for a sent ranking); the
    remainder is what the returned items leave of the whole scope."""
    remainder = None
    if additive and total is not None and offset == 0 and measure:
        remainder = {k: max(0, int(total.get(k) or 0) - sum(_i(it.get(v)) for it in items))
                     for k, v in measure.items()}
    return {
        "items": items,
        "total": total,
        "distinct": distinct,
        "limit": limit,
        "offset": offset,
        "basis": basis,
        "additive": additive,
        "remainder": remainder,
        **extra,
    }


def _truncation(limit: int, offset: int, returned: int, distinct: int | None) -> None:
    more = distinct is not None and distinct > offset + returned
    current_meta().set_truncated(limit, returned, more=more)


def _service_info(proto: Any, svc: Any) -> dict:
    """Service identity of a (protocol, service key) pair: the service port
    and its name for port protocols, the ICMP type/code label for ICMP, and
    "not applicable" for other portless protocols (ESP, GRE, OSPF, ...)."""
    p, s = _i(proto), _i(svc)
    if p in nc.ICMP_PROTOCOLS:
        return {"service_port": None, "icmp_type_code": s, "service": nc.icmp_label(p, s),
                "service_note": None, "port_applicable": False,
                "drill": {"protocol": p, "dst_port": s}}
    if p not in nc.PORT_PROTOCOLS:
        return {"service_port": None, "service": None, "service_note": "Not applicable",
                "port_applicable": False, "drill": {"protocol": p}}
    if s == 0:
        return {"service_port": None, "service": None,
                "service_note": "Unidentified (no known service port on either side)",
                "port_applicable": True, "drill": {"protocol": p, "service_port": 0}}
    return {"service_port": s, "service": nc.service_name(p, s), "service_note": None,
            "port_applicable": True, "drill": {"protocol": p, "service_port": s}}


def _service_label(info: dict) -> str:
    return info.get("service") or info.get("service_note") or "—"


_BASIS_TEXT = {
    "exporter": "Named by the exporter (NBAR2 / App-ID)",
    "dst_rule": "Inferred from the destination service port",
    "src_rule": "Inferred from the source service port (response direction)",
    "rule": "Matched an operator application rule",
    "protocol": "Derived from the IP protocol",
    "lower_port": "Guessed from the lower well-known port",
    "ephemeral": "Guessed from a registered port talking to an ephemeral one",
    "none": "No rule matched",
}


def _classification(app: Any, conf: Any, basis: Any, proto: int) -> dict:
    application = str(app or nc.APP_OTHER)
    out = {
        "application": application,
        "category": nc.category_for(application),
        "confidence": str(conf or "n/a"),
        "basis": str(basis or "none"),
        "basis_text": _BASIS_TEXT.get(str(basis or "none"), str(basis or "")),
        "ip_protocol": nc.protocol_name(proto),
        "visibility_note": None,
    }
    if application == nc.APP_VPN and proto not in nc.PORT_PROTOCOLS:
        out["visibility_note"] = "Encrypted tunnel: the applications inside it are not visible to flow records"
    elif application == nc.APP_VPN:
        out["visibility_note"] = "Tunnel endpoint traffic: inner applications are not visible to flow records"
    return out


# ── Reverse DNS with freshness ────────────────────────────────────────────────

def _rdns(ips: list[str], deadline: float = 2.0) -> dict[str, dict]:
    """{ip: {hostname, pending, resolved_at}} (blocking: call in a thread).

    ``resolved_at`` is when the cached answer was obtained, so the UI can say
    how fresh a name is; a lookup still running at the deadline is pending,
    not "no name"."""
    if not ips:
        return {}
    res = C._resolve_hostnames(ips, deadline)
    mono, wall = time.monotonic(), now_utc()
    out: dict[str, dict] = {}
    for ip in ips:
        host = res.get(ip)
        pending = host is C.RDNS_PENDING
        resolved_at = None
        ent = C._rdns_cache.get(ip)
        if ent and not pending:
            ttl = C._RDNS_TTL if ent[1] else C._RDNS_NEGATIVE_TTL
            age = max(0.0, ttl - (ent[0] - mono))
            resolved_at = _to_iso_utc(wall - timedelta(seconds=age))
        out[ip] = {"hostname": None if pending else host, "pending": pending, "resolved_at": resolved_at}
    return out


async def _enrich(rows: list[dict], key: str, resolve: bool) -> None:
    """Group, GeoIP country/ASN and (opt-in) reverse DNS for per-host rows.
    Reverse DNS follows the operator's switch and deadline (NetFlow settings
    → Enrichment); when it is off the names stay empty and the response says
    so instead of resolving anyway."""
    probe = [{"ip": r[key]} for r in rows if r.get(key)]
    _enrich_hosts(probe)
    by_ip = {p["ip"]: p for p in probe}
    names: dict[str, dict] = {}
    if resolve:
        from .settings import rdns_config

        cfg = await rdns_config()
        if cfg["enabled"]:
            names = await asyncio.to_thread(_rdns, list(by_ip), float(cfg["deadline_s"]))
        else:
            current_meta().warn("rdns_disabled", "Reverse DNS is turned off in NetFlow settings (Enrichment)")
    for r in rows:
        ip = r.get(key)
        p = by_ip.get(ip, {})
        prefix = "" if key == "ip" else f"{key}_"
        for field in ("group", "country", "country_name", "asn", "as_name"):
            r[f"{prefix}{field}"] = p.get(field)
        name = names.get(ip) if resolve else None
        r[f"{prefix}hostname"] = name["hostname"] if name else None
        if resolve:
            r[f"{prefix}hostname_pending"] = bool(name and name["pending"])
            r[f"{prefix}hostname_resolved_at"] = name["resolved_at"] if name else None


# ── Exporter and interface identity ───────────────────────────────────────────

async def _exporter_names(db: AsyncSession, exporters: Iterable[str]) -> dict[str, str | None]:
    ips = sorted({e for e in exporters if e})
    if not ips:
        return {}
    try:
        owners = await resolve_exporter_owners(db, ips)
    except Exception:
        logger.debug("exporter owner lookup failed", exc_info=True)
        return {}
    return {ip: (owners.get(ip).hostname if owners.get(ip) else None) for ip in ips}


async def _interface_meta(db: AsyncSession, keys: Iterable[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """{(exporter, ifindex): names, owning device and effective speed}.

    Ownership follows the one resolver visibility uses (explicit mapping wins
    over a management-IP match); speed follows the platform rule (configured
    circuit speed, else NetFlow override, else SNMP), with its source."""
    keys = {(e, int(i)) for e, i in keys if e and int(i) != 0}
    if not keys:
        return {}
    exporters = sorted({e for e, _ in keys})
    try:
        owners = await resolve_exporter_owners(db, exporters)
    except Exception:
        logger.debug("exporter owner lookup failed", exc_info=True)
        owners = {}
    dev_of: dict[str, tuple[uuid.UUID, str | None]] = {}
    for e in exporters:
        o = owners.get(e)
        if o is not None:
            try:
                dev_of[e] = (uuid.UUID(str(o.device_id)), o.hostname)
            except ValueError:
                continue
    by_device: dict = {}
    if dev_of:
        try:
            res = await db.execute(
                select(
                    DeviceInterface.device_id, DeviceInterface.if_index, DeviceInterface.if_name,
                    DeviceInterface.if_descr, DeviceInterface.if_alias, DeviceInterface.if_speed,
                    DeviceInterface.configured_speed_bps,
                ).where(DeviceInterface.device_id.in_(sorted({d for d, _ in dev_of.values()}, key=str)))
            )
            for r in res.all():
                by_device.setdefault(r.device_id, {})[int(r.if_index)] = r
        except Exception:
            logger.debug("interface lookup failed", exc_info=True)
    overrides: dict[tuple[str, int], Any] = {}
    try:
        # A savepoint, so an appliance without the table (pre migrate-138)
        # does not abort the request's transaction.
        async with db.begin_nested():
            res = await db.execute(
                text("SELECT host(exporter_ip) AS exporter, ifindex, display_name, speed_in_bps, speed_out_bps, role "
                     "FROM netflow_interface_overrides WHERE host(exporter_ip) = ANY(:ips)"),
                {"ips": exporters},
            )
            for r in res.all():
                overrides[(canonical_ip(r.exporter) or r.exporter, int(r.ifindex))] = r
    except Exception:
        logger.debug("interface override lookup skipped", exc_info=True)

    out: dict[tuple[str, int], dict] = {}
    for e, idx in keys:
        dev = dev_of.get(e)
        iface = (by_device.get(dev[0]) or {}).get(idx) if dev else None
        ov = overrides.get((e, idx))
        speed_in, src_in = effective_speed_source(
            getattr(iface, "configured_speed_bps", None), getattr(ov, "speed_in_bps", None),
            getattr(iface, "if_speed", None))
        speed_out, src_out = effective_speed_source(
            getattr(iface, "configured_speed_bps", None), getattr(ov, "speed_out_bps", None),
            getattr(iface, "if_speed", None))
        name = getattr(ov, "display_name", None) or getattr(iface, "if_name", None) \
            or getattr(iface, "if_descr", None) or getattr(iface, "if_alias", None)
        out[(e, idx)] = {
            "if_name": getattr(iface, "if_name", None),
            "if_descr": getattr(iface, "if_descr", None),
            "if_alias": getattr(iface, "if_alias", None),
            "if_speed": int(iface.if_speed) if iface is not None and iface.if_speed else None,
            "device_id": str(dev[0]) if dev else None,
            "device_hostname": dev[1] if dev else None,
            "display_name": name or f"ifIndex {idx}",
            "role": getattr(ov, "role", None),
            "speed_in_bps": speed_in,
            "speed_out_bps": speed_out,
            "speed_source": src_in if src_in == src_out else f"{src_in}/{src_out}",
        }
    return out


def _iface_payload(exporter: str, ifindex: int, meta: dict | None) -> dict:
    meta = meta or {}
    return {
        "exporter_ip": exporter,
        "ifindex": ifindex,
        "iface": f"{ifindex}@{exporter}",
        "if_name": meta.get("if_name"),
        "if_descr": meta.get("if_descr"),
        "if_alias": meta.get("if_alias"),
        "if_speed": meta.get("if_speed"),
        "device_hostname": meta.get("device_hostname"),
        "display_name": meta.get("display_name") or f"ifIndex {ifindex}",
    }


def _pairs(values: Iterable) -> list[tuple[str, int]]:
    """(exporter, ifindex) tuples from ClickHouse, display text, no zeros."""
    out = []
    for v in values or []:
        try:
            e, idx = v
        except (TypeError, ValueError):
            continue
        ip, idx = _ip(e), _i(idx)
        if ip and idx:
            out.append((ip, idx))
    return list(dict.fromkeys(out))


# ── Top talkers / endpoints (NF-24, NF-25) ────────────────────────────────────

_BY_LABEL = {"src": "sent", "dst": "received", "total": "endpoint"}


async def _talkers(
    ctx: Ctx, *, by: str, sort: str, limit: int, offset: int, resolve: bool, with_totals: bool,
) -> list[dict] | dict:
    """Hosts ranked by traffic they sent (``by=src``), received (``dst``) or
    were involved in (``total``: a flow counts for both of its endpoints, so
    those shares overlap and do not add up to the traffic total).

    Phase 1 ranks with sums only; ties are broken by address so the order is
    stable across refreshes; hosts that never appear on the ranked side are
    excluded (a receive-only host is not a top sender). Phase 2 computes the
    heavier per-host detail only for the page's hosts."""
    plan = await _plan(ctx, CONVERSATIONS_1H)
    sub, params = _union(plan, {"x_s": SRC, "x_d": DST, "x_b": "bytes", "x_p": "packets", "x_n": FLOWS}, ctx)
    metric = {"bytes": "b", "packets": "p", "flows": "n"}[sort]
    side = {"src": "s", "dst": "d", "total": ""}[by]
    rank = f"{side}{metric}"
    having = f"HAVING {side}n > 0" if side else ""
    tot = side or "s"      # every flow has exactly one source and one destination
    params.update(nf_limit=limit, nf_offset=offset)
    res = await run_query(
        f"""
        SELECT r.1 AS ip,
               sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n,
               sumIf(x_b, r.2 = 1) AS sb, sumIf(x_p, r.2 = 1) AS sp, sumIf(x_n, r.2 = 1) AS sn,
               sumIf(x_b, r.2 = 0) AS db, sumIf(x_p, r.2 = 0) AS dp, sumIf(x_n, r.2 = 0) AS dn,
               count() OVER () AS n_items,
               sum({tot}b) OVER () AS all_b, sum({tot}p) OVER () AS all_p, sum({tot}n) OVER () AS all_n
        FROM {sub} AS src
        ARRAY JOIN [(x_s, toUInt8(1)), (x_d, toUInt8(0))] AS r
        GROUP BY ip
        {having}
        ORDER BY {rank} DESC, ip ASC
        LIMIT %(nf_limit)s OFFSET %(nf_offset)s
        """,
        params,
        label="top talkers",
    )
    rows = res.result_rows
    items: list[dict] = []
    total = distinct = None
    for r in rows:
        ip = _ip(r[0])
        b, p, n = _i(r[1]), _i(r[2]), _i(r[3])
        item = {
            "ip": ip,
            "bytes": b, "packets": p, "flows": n,
            "src_bytes": _i(r[4]), "src_packets": _i(r[5]), "src_flows": _i(r[6]),
            "dst_bytes": _i(r[7]), "dst_packets": _i(r[8]), "dst_flows": _i(r[9]),
            "rank_by": by, "rank_metric": sort,
        }
        item["rank_value"] = item[{"src": "src_", "dst": "dst_", "total": ""}[by] + sort]
        items.append(item)
        distinct, total = _i(r[10]), _totals(r[11], r[12], r[13])
    if not rows and offset == 0:
        distinct, total = 0, _totals(0, 0, 0)
    if total and total["bytes"]:
        for it in items:
            it["share_pct"] = _pct(it["rank_value"], total[sort])
    await _talker_detail(ctx, plan, items)
    await _enrich(items, "ip", resolve)
    _truncation(limit, offset, len(items), distinct)
    if not with_totals:
        return items
    side_key = {"src": "src_", "dst": "dst_", "total": ""}[by]
    return _paged(
        items, total=total, distinct=distinct, limit=limit, offset=offset,
        basis=f"{_BY_LABEL[by]}_{sort}", additive=by != "total",
        measure={sort: side_key + sort} if by != "total" else None,
        rank_by=by, rank_metric=sort,
    )


async def _talker_detail(ctx: Ctx, plan: SourcePlan, items: list[dict]) -> None:
    """First/last seen, exporters, protocols and top services for the hosts
    on the page (only rows touching them are read)."""
    for it in items:
        it.update(first_seen=None, last_seen=None, exporters=[], protocols=[], ports=[], services=[])
    if not items:
        return
    ips = [ip_param(it["ip"]) for it in items]
    where = f"({SRC} IN %(nf_ips)s OR {DST} IN %(nf_ips)s)"
    cols = {"x_s": SRC, "x_d": DST, "x_first": FIRST, "x_last": LAST, "x_exp": EXPORTER, "x_proto": "protocol"}
    sub1, p1 = _union(plan, cols, ctx, where=where, params={"nf_ips": ips})
    cols2 = {"x_s": SRC, "x_d": DST, "x_proto": "protocol", "x_dport": "dst_port", **_classifier_columns(),
             "x_b": "bytes", "x_p": "packets", "x_n": FLOWS}
    sub2, p2 = _union(plan, cols2, ctx, where=where, params={"nf_ips": ips})
    detail, services = await asyncio.gather(
        run_query(
            f"""
            SELECT r.1 AS ip, min(x_first), max(x_last), groupUniqArray(8)(x_exp), groupUniqArray(8)(x_proto)
            FROM {sub1} AS src
            ARRAY JOIN [(x_s, toUInt8(1)), (x_d, toUInt8(0))] AS r
            WHERE r.1 IN %(nf_ips)s
            GROUP BY ip
            """,
            p1, label="top talkers detail",
        ),
        run_query(
            f"""
            SELECT r.1 AS ip, x_proto, {_SVC} AS svc, sum(x_b) AS b, sum(x_p), sum(x_n), argMax(x_app, x_b)
            FROM {sub2} AS src
            ARRAY JOIN [(x_s, toUInt8(1)), (x_d, toUInt8(0))] AS r
            WHERE r.1 IN %(nf_ips)s
            GROUP BY ip, x_proto, svc
            ORDER BY ip, b DESC, x_proto, svc
            LIMIT 8 BY ip
            """,
            p2, label="top talkers services",
        ),
    )
    by_ip = {it["ip"]: it for it in items}
    for r in detail.result_rows:
        it = by_ip.get(_ip(r[0]))
        if it is None:
            continue
        it["first_seen"] = _to_iso_utc(r[1])
        it["last_seen"] = _to_iso_utc(r[2])
        it["exporters"] = [{"ip": _ip(e)} for e in (r[3] or [])]
        it["protocols"] = [{"protocol": _i(p), "name": nc.protocol_name(_i(p))} for p in sorted(r[4] or [])]
    for r in services.result_rows:
        it = by_ip.get(_ip(r[0]))
        if it is None:
            continue
        info = _service_info(r[1], r[2])
        svc = {"protocol": _i(r[1]), **info, "application": str(r[6] or nc.APP_OTHER),
               "bytes": _i(r[3]), "packets": _i(r[4]), "flows": _i(r[5])}
        it["services"].append(svc)
        # Legacy `ports` list: service ports only (a client's ephemeral ports
        # are not services).
        if info["service_port"]:
            it["ports"].append({"port": info["service_port"], "service": info["service"],
                                "application": svc["application"], "bytes": svc["bytes"],
                                "packets": svc["packets"], "flows": svc["flows"]})


def _validate_choice(value: str, allowed: Iterable[str], key: str) -> str:
    allowed = tuple(allowed)
    if value not in allowed:
        raise C.invalid_filter(f"{key} must be one of {', '.join(allowed)}", {key: "Unknown value"})
    return value


@router.get("/top-talkers")
async def netflow_top_talkers(
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    by: str = Query(default="total", description="Ranking: bytes sent (src), received (dst) or both (total)"),
    sort: str = Query(default="bytes", description="Ranked metric: bytes, packets or flows"),
    resolve: int = Query(default=0, description="1 = reverse-DNS resolve the listed hosts"),
    with_totals: int = Query(default=0, description="1 = {items, total, distinct, ...} with whole-scope denominators"),
    ctx: Ctx = Depends(_ctx(24)),
):
    by = _validate_choice(by, ("total", "src", "dst"), "by")
    sort = _validate_choice(sort, ("bytes", "packets", "flows"), "sort")
    return await _talkers(ctx, by=by, sort=sort, limit=limit, offset=offset, resolve=bool(resolve),
                          with_totals=bool(with_totals))


@router.get("/top-endpoints")
async def netflow_top_endpoints(
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    by: str = Query(default="total"),
    sort: str = Query(default="bytes"),
    resolve: int = Query(default=0),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(1)),
):
    """Hosts by the traffic they took part in, with the sent/received split.
    Involvement overlaps between hosts (non-additive)."""
    by = _validate_choice(by, ("total", "src", "dst"), "by")
    sort = _validate_choice(sort, ("bytes", "packets", "flows"), "sort")
    return await _talkers(ctx, by=by, sort=sort, limit=limit, offset=offset, resolve=bool(resolve),
                          with_totals=bool(with_totals))


# ── Conversations (NF-20, X-29, X-30) ─────────────────────────────────────────

# Rows whose direction is known orient to client -> server; the rest (portless
# protocols, two ephemeral ports, equal ports) to the lower -> higher address,
# so both halves of an exchange land in one conversation either way.
_CLIENT = "multiIf(x_dir = 'request', x_s, x_dir = 'response', x_d, least(x_s, x_d))"
_SERVER = "multiIf(x_dir = 'request', x_d, x_dir = 'response', x_s, greatest(x_s, x_d))"
_C2S = "(x_dir = 'request' OR (x_dir NOT IN ('request', 'response') AND x_s <= x_d))"


@router.get("/top-conversations")
async def netflow_top_conversations(
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    sort: str = Query(default="bytes", description="Ranked metric: bytes, packets or flows"),
    resolve: int = Query(default=0, description="1 = reverse-DNS resolve client and server"),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(1)),
    db: AsyncSession = Depends(get_db),
):
    """Conversations normalised to client -> server with the classifier's
    service port: the request and response halves of one exchange are one
    row (with client->server and server->client bytes), not one row per
    ephemeral client port. ``src``/``dst``/``dst_port`` keep their names for
    existing consumers and mean client/server/service port."""
    sort = _validate_choice(sort, ("bytes", "packets", "flows"), "sort")
    plan = await _plan(ctx, CONVERSATIONS_1H)
    cls = _classifier_columns()
    cols = {"x_s": SRC, "x_d": DST, "x_proto": "protocol", "x_dport": "dst_port", **cls,
            "x_b": "bytes", "x_p": "packets", "x_n": FLOWS}
    sub, params = _union(plan, cols, ctx)
    metric = {"bytes": "b", "packets": "p", "flows": "n"}[sort]
    params.update(nf_limit=limit, nf_offset=offset)
    res = await run_query(
        f"""
        SELECT {_CLIENT} AS c, {_SERVER} AS sv, x_proto AS proto, x_sp AS sp,
               sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n,
               sumIf(x_b, {_C2S}) AS b_c2s, sumIf(x_p, {_C2S}) AS p_c2s, sumIf(x_n, {_C2S}) AS n_c2s,
               countIf(x_dir IN ('request', 'response')) AS oriented, count() AS parts,
               argMax(x_app, x_b) AS app, argMax(x_conf, x_b) AS conf, argMax(x_basis, x_b) AS basis,
               argMax(x_dport, x_b) AS top_dport,
               count() OVER () AS n_items,
               sum(b) OVER () AS all_b, sum(p) OVER () AS all_p, sum(n) OVER () AS all_n
        FROM {sub} AS src
        GROUP BY c, sv, proto, sp
        ORDER BY {metric} DESC, c ASC, sv ASC, proto ASC, sp ASC
        LIMIT %(nf_limit)s OFFSET %(nf_offset)s
        """,
        params,
        label="top conversations",
    )
    items: list[dict] = []
    total = distinct = None
    keys = []
    for r in res.result_rows:
        client, server, proto, sp = _ip(r[0]), _ip(r[1]), _i(r[2]), _i(r[3])
        svc_key = _i(r[15]) if proto in nc.ICMP_PROTOCOLS else sp
        info = _service_info(proto, svc_key)
        b, p, n = _i(r[4]), _i(r[5]), _i(r[6])
        oriented, parts = _i(r[10]), _i(r[11])
        orientation = "client_server" if oriented == parts else ("address_order" if oriented == 0 else "mixed")
        cls_info = _classification(r[12], r[13], r[14], proto)
        item = {
            # Legacy names (maps widget, device page): src = client, dst = server.
            "src": client, "dst": server,
            "client": client, "server": server, "orientation": orientation,
            "protocol": proto, "protocol_name": nc.protocol_name(proto),
            "dst_port": info["service_port"] or 0,
            **info,
            "service": _service_label(info),
            **cls_info,
            "port_class": _port_class(info["service_port"]),
            "bytes": b, "packets": p, "flows": n,
            "bytes_c2s": _i(r[7]), "bytes_s2c": max(0, b - _i(r[7])),
            "packets_c2s": _i(r[8]), "packets_s2c": max(0, p - _i(r[8])),
            "flows_c2s": _i(r[9]), "flows_s2c": max(0, n - _i(r[9])),
        }
        items.append(item)
        keys.append((ip_param(client), ip_param(server), proto, sp))
        distinct, total = _i(r[16]), _totals(r[17], r[18], r[19])
    if not res.result_rows and offset == 0:
        distinct, total = 0, _totals(0, 0, 0)
    if total and total[sort]:
        for it in items:
            it["share_pct"] = _pct(it[sort], total[sort])
    await _conversation_detail(ctx, plan, items, keys, db)
    if items:
        await _enrich(items, "client", resolve)
        await _enrich(items, "server", resolve)
    _truncation(limit, offset, len(items), distinct)
    if not with_totals:
        return items
    return _paged(items, total=total, distinct=distinct, limit=limit, offset=offset,
                  basis=f"conversation_{sort}", additive=True, measure={sort: sort}, rank_metric=sort)


def _port_class(port: int | None) -> str:
    if not port:
        return "No service port"
    if 1 <= port <= 1023:
        return "Well-known system port"
    if port <= 49151:
        return "Registered/user port"
    return "Dynamic/private port"


async def _conversation_detail(ctx: Ctx, plan: SourcePlan, items: list[dict], keys: list[tuple], db) -> None:
    """Client ports, exporter + interface pairs (as observed, never a cross
    product of exporters and ifIndexes), times, durations and TCP flags."""
    for it in items:
        it.update(src_ports=[], client_ports=[], exporters=[], input_snmp=[], output_snmp=[],
                  input_interfaces=[], output_interfaces=[], client_side_interfaces=[],
                  server_side_interfaces=[], first_seen=None, last_seen=None, received_at=None,
                  avg_duration_ms=None, duration_valid_flows=0, tcp_flags=None, tcp_flags_known=False,
                  flagged_flows=0)
        it["avg_bytes"] = (it["bytes"] / it["flows"]) if it["flows"] else None
        it["avg_packets"] = (it["packets"] / it["flows"]) if it["flows"] else None
    if not items:
        return
    hosts = sorted({k[0] for k in keys} | {k[1] for k in keys})
    in_pairs = ("arrayFilter(t -> t.2 != 0, arrayMap(i -> (" + EXPORTER + ", i), "
                "if(empty(finalizeAggregation(input_ifs)), arrayMap(x -> toUInt32(x), "
                "finalizeAggregation(input_snmp)), finalizeAggregation(input_ifs))))",
                f"arrayFilter(t -> t.2 != 0, [({EXPORTER}, input_if)])")
    out_pairs = ("arrayFilter(t -> t.2 != 0, arrayMap(i -> (" + EXPORTER + ", i), "
                 "if(empty(finalizeAggregation(output_ifs)), arrayMap(x -> toUInt32(x), "
                 "finalizeAggregation(output_snmp)), finalizeAggregation(output_ifs))))",
                 f"arrayFilter(t -> t.2 != 0, [({EXPORTER}, output_if)])")
    cls = _classifier_columns()
    cols = {
        "x_s": SRC, "x_d": DST, "x_proto": "protocol", "x_dport": "dst_port",
        "x_sp": cls["x_sp"], "x_dir": cls["x_dir"],
        "x_sports": ("finalizeAggregation(src_ports)", "[src_port]"),
        "x_exp": EXPORTER, "x_in": in_pairs, "x_out": out_pairs,
        "x_first": FIRST, "x_last": LAST, "x_recv": ("received_at", "received_at"),
        "x_dvf": ("duration_valid_flows", f"toUInt64({_DURATION_VALID})"),
        "x_dvms": ("duration_valid_ms",
                   f"if({_DURATION_VALID}, toInt64(last_switched_ms) - toInt64(first_switched_ms), toInt64(0))"),
        "x_flags": ("if(flagged_flows > 0, tcp_flags, toUInt64(0))", f"if({_FLAGGED_RAW}, toUInt64(tcp_flags), toUInt64(0))"),
        "x_flagged": ("flagged_flows", f"toUInt64({_FLAGGED_RAW})"),
    }
    where = f"({SRC} IN %(nf_hosts)s AND {DST} IN %(nf_hosts)s)"
    sub, params = _union(plan, cols, ctx, where=where, params={"nf_hosts": hosts, "nf_keys": keys})
    res = await run_query(
        f"""
        SELECT {_CLIENT} AS c, {_SERVER} AS sv, x_proto AS proto, x_sp AS sp,
               groupUniqArrayArray(10)(if(x_dir = 'response', [x_dport], x_sports)) AS client_ports,
               groupUniqArray(10)(x_exp) AS exporters,
               groupUniqArrayArray(20)(x_in) AS ins,
               groupUniqArrayArray(20)(x_out) AS outs,
               groupUniqArrayArray(20)(if({_C2S}, x_in, x_out)) AS client_side,
               groupUniqArrayArray(20)(if({_C2S}, x_out, x_in)) AS server_side,
               min(x_first), max(x_last), max(x_recv),
               sum(x_dvf) AS dvf, sum(x_dvms) AS dvms,
               groupBitOr(x_flags) AS flags, sum(x_flagged) AS flagged
        FROM {sub} AS src
        WHERE (c, sv, proto, sp) IN %(nf_keys)s
        GROUP BY c, sv, proto, sp
        """,
        params,
        label="top conversations detail",
    )
    by_key = {(it["client"], it["server"], it["protocol"], k[3]): it for it, k in zip(items, keys)}
    iface_keys: set[tuple[str, int]] = set()
    exporter_ips: set[str] = set()
    rows = []
    for r in res.result_rows:
        it = by_key.get((_ip(r[0]), _ip(r[1]), _i(r[2]), _i(r[3])))
        if it is None:
            continue
        rows.append((it, r))
        for col in (6, 7, 8, 9):
            iface_keys.update(_pairs(r[col]))
        exporter_ips.update(_ip(e) for e in (r[5] or []) if e is not None)
    names = await _exporter_names(db, exporter_ips)
    meta = await _interface_meta(db, iface_keys)
    for it, r in rows:
        ports = sorted({_i(p) for p in (r[4] or []) if _i(p)})
        it["client_ports"] = ports[:10]
        it["src_ports"] = ports[:10]
        it["exporters"] = [{"ip": _ip(e), "hostname": names.get(_ip(e))} for e in (r[5] or [])]

        def ifs(values):
            return [_iface_payload(e, idx, meta.get((e, idx))) for e, idx in _pairs(values)]

        it["input_interfaces"] = ifs(r[6])
        it["output_interfaces"] = ifs(r[7])
        it["input_snmp"] = sorted({x["ifindex"] for x in it["input_interfaces"]})
        it["output_snmp"] = sorted({x["ifindex"] for x in it["output_interfaces"]})
        it["client_side_interfaces"] = ifs(r[8])
        it["server_side_interfaces"] = ifs(r[9])
        it["first_seen"], it["last_seen"], it["received_at"] = _to_iso_utc(r[10]), _to_iso_utc(r[11]), _to_iso_utc(r[12])
        dvf, dvms = _i(r[13]), _i(r[14])
        it["duration_valid_flows"] = dvf
        it["avg_duration_ms"] = round(dvms / dvf, 1) if dvf else None
        flagged = _i(r[16])
        it["flagged_flows"] = flagged
        it["tcp_flags_known"] = it["protocol"] == 6 and flagged > 0
        it["tcp_flags"] = _i(r[15]) if it["tcp_flags_known"] else None


# ── Services: protocols, ports, applications (NF-20) ──────────────────────────

def _service_columns() -> dict[str, Column]:
    return {"x_proto": "protocol", "x_dport": "dst_port", **_classifier_columns(),
            "x_b": "bytes", "x_p": "packets", "x_n": FLOWS, "x_first": FIRST, "x_last": LAST}


def _service_row(r, offset: int = 0) -> dict:
    """proto, svc, b, p, n, request_b, response_b, app, conf, basis (from ``offset``)."""
    proto, svc = _i(r[offset]), _i(r[offset + 1])
    info = _service_info(proto, svc)
    return {
        "protocol": proto, "protocol_name": nc.protocol_name(proto),
        **info,
        "port": info["service_port"] or 0,
        "bytes": _i(r[offset + 2]), "packets": _i(r[offset + 3]), "flows": _i(r[offset + 4]),
        "request_bytes": _i(r[offset + 5]), "response_bytes": _i(r[offset + 6]),
        **_classification(r[offset + 7], r[offset + 8], r[offset + 9], proto),
    }


_SERVICE_AGGS = (
    "sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n,"
    " sumIf(x_b, x_dir = 'request') AS rb, sumIf(x_b, x_dir = 'response') AS sb,"
    " argMax(x_app, x_b) AS app, argMax(x_conf, x_b) AS conf, argMax(x_basis, x_b) AS basis"
)


@router.get("/protocols")
async def netflow_protocols(
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Every IP protocol in scope with its top services (request and
    response halves of a service counted under the service port)."""
    plan = await _plan(ctx, CONVERSATIONS_1H)
    sub, params = _union(plan, _service_columns(), ctx)
    res = await run_query(
        f"""
        SELECT proto, svc, b, p, n, rb, sb, app, conf, basis, fs, ls,
               pb, pp, pn, prb, psb, pfirst, plast
        FROM (
            SELECT x_proto AS proto, {_SVC} AS svc, {_SERVICE_AGGS},
                   min(x_first) AS fs, max(x_last) AS ls,
                   sum(b) OVER w AS pb, sum(p) OVER w AS pp, sum(n) OVER w AS pn,
                   sum(rb) OVER w AS prb, sum(sb) OVER w AS psb,
                   min(fs) OVER w AS pfirst, max(ls) OVER w AS plast,
                   row_number() OVER (PARTITION BY proto ORDER BY b DESC, svc ASC) AS rn
            FROM {sub} AS src
            GROUP BY proto, svc
            WINDOW w AS (PARTITION BY proto)
        )
        WHERE rn <= 10
        ORDER BY pb DESC, proto ASC, b DESC, svc ASC
        """,
        params,
        label="protocols",
    )
    protos: dict[int, dict] = {}
    for r in res.result_rows:
        proto = _i(r[0])
        entry = protos.get(proto)
        if entry is None:
            entry = protos[proto] = {
                "protocol": proto, "name": nc.protocol_name(proto),
                "bytes": _i(r[12]), "packets": _i(r[13]), "flows": _i(r[14]),
                "request_bytes": _i(r[15]), "response_bytes": _i(r[16]),
                "first_seen": _to_iso_utc(r[17]), "last_seen": _to_iso_utc(r[18]),
                "port_applicable": proto in nc.PORT_PROTOCOLS,
                "ports": [], "services": [],
            }
        svc = _service_row(r)
        entry["services"].append(svc)
        entry["ports"].append({"port": svc["port"], "service": _service_label(svc),
                               "application": svc["application"], "bytes": svc["bytes"],
                               "packets": svc["packets"], "flows": svc["flows"]})
    items = sorted(protos.values(), key=lambda x: (-x["bytes"], x["protocol"]))
    total = _totals(sum(x["bytes"] for x in items), sum(x["packets"] for x in items), sum(x["flows"] for x in items))
    for it in items:
        it["share_pct"] = _pct(it["bytes"], total["bytes"])
    if not with_totals:
        return items
    return _paged(items, total=total, distinct=len(items), limit=len(items), offset=0,
                  basis="protocol_bytes", additive=True, measure={"bytes": "bytes"})


@router.get("/ports")
async def netflow_ports(
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    sort: str = Query(default="bytes"),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Services ranked by traffic: (protocol, service port) with both
    directions of the service, ICMP by type/code, portless protocols once."""
    sort = _validate_choice(sort, ("bytes", "packets", "flows"), "sort")
    plan = await _plan(ctx, CONVERSATIONS_1H)
    sub, params = _union(plan, _service_columns(), ctx)
    metric = {"bytes": "b", "packets": "p", "flows": "n"}[sort]
    params.update(nf_limit=limit, nf_offset=offset)
    res = await run_query(
        f"""
        SELECT x_proto AS proto, {_SVC} AS svc, {_SERVICE_AGGS},
               count() OVER () AS n_items, sum(b) OVER () AS all_b, sum(p) OVER () AS all_p,
               sum(n) OVER () AS all_n
        FROM {sub} AS src
        GROUP BY proto, svc
        ORDER BY {metric} DESC, proto ASC, svc ASC
        LIMIT %(nf_limit)s OFFSET %(nf_offset)s
        """,
        params,
        label="services",
    )
    items, total, distinct = [], None, None
    for r in res.result_rows:
        svc = _service_row(r)
        svc["service"] = _service_label(svc)
        items.append(svc)
        distinct, total = _i(r[10]), _totals(r[11], r[12], r[13])
    if not res.result_rows and offset == 0:
        distinct, total = 0, _totals(0, 0, 0)
    if total and total[sort]:
        for it in items:
            it["share_pct"] = _pct(it[sort], total[sort])
    _truncation(limit, offset, len(items), distinct)
    if not with_totals:
        return items
    return _paged(items, total=total, distinct=distinct, limit=limit, offset=offset,
                  basis=f"service_{sort}", additive=True, measure={sort: sort})


@router.get("/applications")
async def netflow_applications(
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Traffic by inferred application (exporter App-ID, operator rules,
    protocol and service-port rules, in that order), with the services that
    make it up and how confidently the bytes were classified. The ``app``
    filter is not applied to its own breakdown (every application stays
    visible; the selected one is flagged)."""
    plan = await _plan(ctx, CONVERSATIONS_1H, ("app",))
    sub, params = _union(plan, _service_columns(), ctx, ("app",))
    res = await run_query(
        f"""
        SELECT app, proto, svc, b, p, n, rb, sb, conf, basis,
               ab, ap, an, arb, asb, hb, mb, lb
        FROM (
            SELECT x_app AS app, x_proto AS proto, {_SVC} AS svc,
                   sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n,
                   sumIf(x_b, x_dir = 'request') AS rb, sumIf(x_b, x_dir = 'response') AS sb,
                   argMax(x_conf, x_b) AS conf, argMax(x_basis, x_b) AS basis,
                   sumIf(x_b, x_conf = 'high') AS h, sumIf(x_b, x_conf = 'medium') AS m,
                   sumIf(x_b, x_conf = 'low') AS l,
                   sum(b) OVER w AS ab, sum(p) OVER w AS ap, sum(n) OVER w AS an,
                   sum(rb) OVER w AS arb, sum(sb) OVER w AS asb,
                   sum(h) OVER w AS hb, sum(m) OVER w AS mb, sum(l) OVER w AS lb,
                   row_number() OVER (PARTITION BY app ORDER BY b DESC, proto ASC, svc ASC) AS rn
            FROM {sub} AS src
            GROUP BY app, proto, svc
            WINDOW w AS (PARTITION BY app)
        )
        WHERE rn <= 8
        ORDER BY ab DESC, app ASC, b DESC
        """,
        params,
        label="applications",
    )
    apps: dict[str, dict] = {}
    selected = ctx.f.app
    for r in res.result_rows:
        name = str(r[0] or nc.APP_OTHER)
        entry = apps.get(name)
        if entry is None:
            ab = _i(r[10])
            entry = apps[name] = {
                "name": name, "category": nc.category_for(name),
                "bytes": ab, "packets": _i(r[11]), "flows": _i(r[12]),
                "request_bytes": _i(r[13]), "response_bytes": _i(r[14]),
                "confidence_bytes": {"high": _i(r[15]), "medium": _i(r[16]), "low": _i(r[17]),
                                     "n/a": max(0, ab - _i(r[15]) - _i(r[16]) - _i(r[17]))},
                "selected": selected == name,
                "ports": [], "services": [],
            }
        proto, svc_key = _i(r[1]), _i(r[2])
        info = _service_info(proto, svc_key)
        svc = {"protocol": proto, "protocol_name": nc.protocol_name(proto), **info,
               "port": info["service_port"] or 0,
               "bytes": _i(r[3]), "packets": _i(r[4]), "flows": _i(r[5]),
               "request_bytes": _i(r[6]), "response_bytes": _i(r[7]),
               "confidence": str(r[8] or "n/a"), "basis": str(r[9] or "none"),
               "basis_text": _BASIS_TEXT.get(str(r[9] or "none"), "")}
        entry["services"].append(svc)
        if info["service_port"]:
            entry["ports"].append({"port": info["service_port"], "service": info["service"],
                                   "application": name, "bytes": svc["bytes"], "packets": svc["packets"],
                                   "flows": svc["flows"]})
    items = sorted(apps.values(), key=lambda x: (-x["bytes"], x["name"]))
    total = _totals(sum(x["bytes"] for x in items), sum(x["packets"] for x in items), sum(x["flows"] for x in items))
    for it in items:
        it["share_pct"] = _pct(it["bytes"], total["bytes"])
        cb = it["confidence_bytes"]
        it["confidence"] = ("high" if cb["high"] >= it["bytes"] * 0.9 else
                            "n/a" if cb["n/a"] >= it["bytes"] * 0.9 else
                            "low" if cb["low"] >= it["bytes"] * 0.5 else "medium")
        vpn_portless = it["name"] == nc.APP_VPN and any(not s["port_applicable"] for s in it["services"])
        it["visibility_note"] = ("Encrypted tunnels: the applications inside them are not visible to flow records"
                                 if vpn_portless else None)
    if not with_totals:
        return items
    return _paged(items, total=total, distinct=len(items), limit=len(items), offset=0,
                  basis="application_bytes", additive=True, measure={"bytes": "bytes"})


# ── Interfaces (NF-27, X-19, X-36) ────────────────────────────────────────────

@router.get("/interfaces")
async def netflow_interfaces(
    limit: int = Query(default=5, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
    db: AsyncSession = Depends(get_db),
):
    """Interfaces by the traffic that crossed them, identified by exporter and
    ifIndex (the same index on two exporters is two interfaces). Every page
    filter applies except the interface selection itself, so the picker keeps
    the other interfaces for comparison and flags the selected one. Served
    from the 5-minute interface rollup when the filter allows it (exporter,
    interface, direction, counting mode), else from raw records."""
    drop = ("iface", "iface_dir")
    plan = await _plan(ctx, INTERFACE_5M, drop)
    cols = {
        "x_exp": EXPORTER,
        "x_if": ("ifindex", "d.1"),
        "x_dir": ("direction", "d.2"),
        "x_b": ("bytes", "toFloat64(bytes)"),
        "x_p": ("packets", "toFloat64(packets)"),
        "x_n": ("flows", "toUInt64(1)"),
    }
    sub, params = _union(plan, cols, ctx, drop, where=("", "d.1 != 0"),
                         raw_join="ARRAY JOIN [(input_if, toUInt8(0)), (output_if, toUInt8(1))] AS d")
    params.update(nf_limit=limit, nf_offset=offset)

    def _iface_sql(where: str, tail: str) -> str:
        return f"""
        SELECT x_exp, x_if,
               sumIf(x_b, x_dir = 0) AS ib, sumIf(x_b, x_dir = 1) AS ob,
               sumIf(x_p, x_dir = 0) AS ip_, sumIf(x_p, x_dir = 1) AS op_,
               sumIf(x_n, x_dir = 0) AS in_, sumIf(x_n, x_dir = 1) AS on_,
               count() OVER () AS n_items
        FROM {sub} AS src
        {where}
        GROUP BY x_exp, x_if
        ORDER BY (ib + ob) DESC, x_exp ASC, x_if ASC
        {tail}
        """

    res = await run_query(_iface_sql("", "LIMIT %(nf_limit)s OFFSET %(nf_offset)s"), params, label="interfaces")
    rows = [(_ip(r[0]), _i(r[1]), r) for r in res.result_rows]
    sel = ctx.f.iface
    sel_exporter = canonical_ip(sel.exporter) if sel and sel.exporter else (
        canonical_ip(ctx.f.exporter) if ctx.f.exporter else None)

    def _is_sel(exporter: str | None, idx: int) -> bool:
        return bool(sel and sel.ifindex == idx and (sel_exporter is None or sel_exporter == exporter))

    # The selected interface stays visible on the first page even when it is
    # not among the busiest (it is the one the page is filtered to); it is
    # appended after the ranked rows and flagged `pinned`.
    pinned: set[tuple[str | None, int]] = set()
    if sel is not None and offset == 0 and not any(_is_sel(e, i) for e, i, _ in rows):
        pparams = dict(params, nf_sel_if=sel.ifindex)
        pwhere = "WHERE x_if = %(nf_sel_if)s"
        if sel_exporter:
            pwhere += " AND x_exp = toIPv6(%(nf_sel_exp)s)"
            pparams["nf_sel_exp"] = ip_param(sel_exporter)
        pres = await run_query(_iface_sql(pwhere, "LIMIT 20"), pparams, label="interfaces-selected")
        for r in pres.result_rows:
            key = (_ip(r[0]), _i(r[1]))
            pinned.add(key)
            rows.append((key[0], key[1], (*r[:8], None)))
    meta = await _interface_meta(db, [(e, i) for e, i, _ in rows])
    seconds = max(1.0, (ctx.win.end - ctx.win.start).total_seconds())
    items, distinct = [], None
    for exporter, idx, r in rows:
        m = meta.get((exporter, idx), {})
        ib, ob = _f(r[2]), _f(r[3])
        item = {
            **_iface_payload(exporter, idx, m),
            "device_id": m.get("device_id"),
            "role": m.get("role"),
            "in_bytes": int(round(ib)), "out_bytes": int(round(ob)), "bytes": int(round(ib + ob)),
            "in_packets": int(round(_f(r[4]))), "out_packets": int(round(_f(r[5]))),
            "packets": int(round(_f(r[4]) + _f(r[5]))),
            "in_flows": _i(r[6]), "out_flows": _i(r[7]), "flows": _i(r[6]) + _i(r[7]),
            "in_bps_avg": ib * 8 / seconds, "out_bps_avg": ob * 8 / seconds,
            "speed_in_bps": m.get("speed_in_bps"), "speed_out_bps": m.get("speed_out_bps"),
            "speed_source": m.get("speed_source", "unknown"),
            "selected": _is_sel(exporter, idx),
            "pinned": (exporter, idx) in pinned,
        }
        # No utilization percentage here: this ranking counts records the
        # page's way (count=all adds an exporter's ingress and egress copies
        # of the same packets) and divides by the whole window (an exporter's
        # collection gaps read as idle). Capacity is the one utilization
        # definition (per-exporter direction de-duplication, gaps excluded,
        # per-direction percentiles); the client links there instead.
        items.append(item)
        if r[8] is not None:
            distinct = _i(r[8])
    if not res.result_rows and offset == 0:
        distinct = 0
    _truncation(limit, offset, len(res.result_rows), distinct)
    if not with_totals:
        return items
    scope = {
        "interface_excluded": sel is not None,
        "selected": sel.text if sel is not None else None,
        "note": ("Other interfaces are shown for comparison; the selected interface is highlighted"
                 if sel is not None else None),
    }
    # Interface volumes are not additive: one flow crosses an ingress and an
    # egress interface, on every exporter that saw it.
    return _paged(items, total=None, distinct=distinct, limit=limit, offset=offset,
                  basis="interface_bytes", additive=False, scope=scope,
                  window_seconds=int(seconds),
                  utilization={"source": "capacity", "path": "/netflow/capacity",
                               "note": "Utilization is measured on the Capacity page: per direction, over "
                                       "complete 5-minute samples, with exporter collection gaps left out "
                                       "and duplicate ingress and egress export counted once"})


# ── DSCP ──────────────────────────────────────────────────────────────────────

@router.get("/dscp")
async def netflow_dscp(
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Traffic by DSCP code point (top 6 bits of ToS / traffic class). The
    ``dscp`` filter is not applied to its own chart.

    Records whose exporter did not send the ToS field store 0, which is not
    DSCP 0 (Best Effort): they are reported apart as ``not_exported`` (and in
    the whole-scope ``total``), never as a class, with a ``dscp_not_exported``
    warning -- as the TCP-flags card counts flagged records only."""
    drop = ("dscp",)
    plan = await _plan(ctx, QOS_1H, drop)
    sub, params = _union(plan, {"x_dscp": "bitShiftRight(tos, 2)", "x_known": ("tos_known != 0", TOS_KNOWN_RAW),
                                "x_b": "bytes", "x_p": "packets", "x_n": FLOWS}, ctx, drop)
    res = await run_query(
        f"""
        SELECT if(x_known, toInt16(x_dscp), toInt16(-1)) AS dscp, sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n
        FROM {sub} AS src
        GROUP BY dscp
        ORDER BY b DESC, dscp ASC
        """,
        params,
        label="dscp",
    )
    items, unknown = [], {"bytes": 0, "packets": 0, "flows": 0}
    for r in res.result_rows:
        code = _i(r[0])
        if code < 0:
            unknown = {"bytes": _i(r[1]), "packets": _i(r[2]), "flows": _i(r[3])}
            continue
        items.append({"dscp": code, "label": _dscp_label(code), "bytes": _i(r[1]), "packets": _i(r[2]),
                      "flows": _i(r[3]), "selected": ctx.f.dscp == code})
    total = _totals(sum(x["bytes"] for x in items) + unknown["bytes"],
                    sum(x["packets"] for x in items) + unknown["packets"],
                    sum(x["flows"] for x in items) + unknown["flows"])
    for it in items:
        it["share_pct"] = _pct(it["bytes"], total["bytes"])
    not_exported = None
    if unknown["flows"] or unknown["bytes"]:
        not_exported = {**unknown, "share_pct": _pct(unknown["bytes"], total["bytes"])}
        share = not_exported["share_pct"]
        current_meta().warn(
            "dscp_not_exported",
            (f"{share:.1f}% of the traffic" if share is not None else "Some traffic")
            + " comes from records whose exporter did not send the ToS field: its DSCP is unknown, "
              "not Best Effort, and a DSCP filter does not match it",
        )
    if not with_totals:
        return items
    known_bytes = total["bytes"] - unknown["bytes"]
    return _paged(items, total=total, distinct=len(items), limit=len(items), offset=0,
                  basis="dscp_bytes", additive=True, measure={"bytes": "bytes"},
                  not_exported=not_exported,
                  tos_coverage_pct=_pct(known_bytes, total["bytes"]) if total["bytes"] else None)


# ── TCP flags (NF-07 display side) ────────────────────────────────────────────

# The flag conditions and the RST ratio are overview's (principle 5: one RST
# definition for /device-status, the KPI and this card), over the subquery's
# x_f alias. no_flags is computed separately below (exported and zero).
_FLAG_CONDS = {
    name: cond.replace("tcp_flags", "x_f")
    for name, cond in OVERVIEW_FLAG_CONDS.items() if name != "no_flags"
}


@router.get("/tcp-flags")
async def netflow_tcp_flags(
    ctx: Ctx = Depends(_ctx(24)),
):
    """TCP flows by the control bits they carried, counted only over flows
    whose flags are observations: a template without the TCP-flags field
    stores zeros, which are not "no flags". ``flagged_tcp`` is the
    denominator; ``unknown_tcp`` the TCP flows whose flags were not exported.
    The ``tcpflag`` filter is not applied to its own card. ``rst_ratio`` is
    the one RST share (overview.rst_payload, as /device-status reports it):
    null with a reason when the sample is too small or flags are missing."""
    drop = ("tcpflag",)
    plan = await _plan(ctx, QOS_1H, drop)
    cols = {
        "x_f": "tcp_flags",
        "x_present": ("flags_known = 1", "bitTest(fields_present, 0)"),
        "x_legacy": ("flags_known = 2", "fields_present = 0"),
        "x_n": FLOWS,
        "x_b": "bytes",
    }
    sub, params = _union(plan, cols, ctx, drop, where="protocol = 6")
    known = "(x_present OR (x_legacy AND x_f != 0))"
    counts = ", ".join(f"sumIf(x_n, {known} AND {cond}) AS {name}" for name, cond in _FLAG_CONDS.items())
    res = await run_query(
        f"""
        SELECT sum(x_n) AS total, sumIf(x_n, {known}) AS flagged, sumIf(x_n, x_legacy) AS legacy,
               {counts},
               sumIf(x_n, x_present AND x_f = 0) AS no_flags,
               sum(x_b) AS bytes, sumIf(x_b, {known}) AS flagged_bytes
        FROM {sub} AS src
        """,
        params,
        label="tcp flags",
    )
    r = _row(res, 12)
    total, flagged, legacy = _i(r[0]), _i(r[1]), _i(r[2])
    out = {
        "total_tcp": total,
        **{name: _i(r[3 + i]) for i, name in enumerate(_FLAG_CONDS)},
        "no_flags": _i(r[9]),
        "flagged_tcp": flagged,
        "unknown_tcp": max(0, total - flagged),
        "legacy_tcp": legacy,
        "total_bytes": _i(r[10]),
        "flagged_bytes": _i(r[11]),
        "flag_coverage_pct": _pct(flagged, total),
    }
    rst_ratio = rst_payload(total, flagged, out["rst"], [k for k in drop if k in ctx.f.active_keys()])
    out["rst_ratio_pct"] = rst_ratio["pct"]
    out["rst_ratio"] = rst_ratio
    if ctx.f.protocol is not None and ctx.f.protocol != 6:
        out["support"], out["reason"] = "not_applicable", "The protocol filter excludes TCP"
    elif total == 0:
        out["support"], out["reason"] = "no_tcp", "No TCP flows in scope"
    elif flagged == 0:
        out["support"] = "unsupported"
        out["reason"] = "The exporters in scope do not export TCP flags; SYN/RST/FIN counts are unknown, not zero"
    elif flagged < total:
        out["support"] = "partial"
        out["reason"] = (f"TCP flags were exported for {out['flag_coverage_pct']:.1f}% of TCP flows; "
                         "counts cover only those")
    else:
        out["support"], out["reason"] = "supported", None
    return out


# ── Network classes ───────────────────────────────────────────────────────────

@router.get("/network-classes")
async def netflow_network_classes(
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Address classes (private ranges, CGNAT, public, multicast, link-local,
    loopback, IPv6 unique local, documentation / special-use) per side. Each
    side partitions the traffic; the legacy ``bytes`` is source + destination
    (attributed twice). The
    ``netclass`` filter is not applied to its own breakdown; the classes are
    the ones the filter uses."""
    drop = ("netclass",)
    plan = await _plan(ctx, CONVERSATIONS_1H, drop)
    sub, params = _union(plan, {"x_sc": netclass_expr("src"), "x_dc": netclass_expr("dst"),
                                "x_b": "bytes", "x_p": "packets", "x_n": FLOWS}, ctx, drop)
    res = await run_query(
        f"""
        SELECT x_sc, x_dc, sum(x_b) AS b, sum(x_p), sum(x_n)
        FROM {sub} AS src
        GROUP BY x_sc, x_dc
        ORDER BY b DESC, x_sc, x_dc
        """,
        params,
        label="network classes",
    )
    classes: dict[str, dict] = {}
    pairs = []
    total = {"bytes": 0, "packets": 0, "flows": 0}

    def entry(name: str) -> dict:
        return classes.setdefault(name, {"name": name, "src_bytes": 0, "src_packets": 0, "src_flows": 0,
                                         "dst_bytes": 0, "dst_packets": 0, "dst_flows": 0,
                                         "selected": ctx.f.netclass == name})

    for sc, dc, b, p, n in res.result_rows:
        b, p, n = _i(b), _i(p), _i(n)
        pairs.append({"src": sc, "dst": dc, "bytes": b, "packets": p, "flows": n})
        total["bytes"] += b
        total["packets"] += p
        total["flows"] += n
        for name, side in ((sc, "src"), (dc, "dst")):
            e = entry(name)
            e[f"{side}_bytes"] += b
            e[f"{side}_packets"] += p
            e[f"{side}_flows"] += n
    items = []
    for e in classes.values():
        e["bytes"] = e["src_bytes"] + e["dst_bytes"]
        e["packets"] = e["src_packets"] + e["dst_packets"]
        e["flows"] = e["src_flows"] + e["dst_flows"]
        e["src_share_pct"] = _pct(e["src_bytes"], total["bytes"])
        e["dst_share_pct"] = _pct(e["dst_bytes"], total["bytes"])
        items.append(e)
    items.sort(key=lambda x: (-x["bytes"], x["name"]))
    if not with_totals:
        return items
    return _paged(items, total=total, distinct=len(items), limit=len(items), offset=0,
                  basis="per_side_bytes", additive=False, pairs=pairs[:50])


# ── Countries and ASNs (CR-20, IPv6) ──────────────────────────────────────────

_GEO_PREFIX_LIMIT = 5000


def _geoip_status() -> dict:
    """Whether the GeoIP databases are installed, and their build stamps."""
    def stamp(name: str) -> str | None:
        try:
            with open(os.path.join(geoip.GEOIP_DIR, f"{name}.stamp"), encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    return {
        "country_db": geoip._reader_for(geoip._COUNTRY_DB) is not None,
        "asn_db": geoip._reader_for(geoip._ASN_DB) is not None,
        "country_db_version": stamp(geoip._COUNTRY_DB),
        "asn_db_version": stamp(geoip._ASN_DB),
    }


_LOCAL_NETS = tuple(ipaddress.ip_network(c) for name, cidrs in NET_CLASSES.items()
                    if name != SPECIAL_USE_CLASS for c in cidrs)


def _address_kind(ip: str) -> str:
    """'private' for the network classes the netclass filter names (RFC 1918,
    CGNAT, loopback, link-local, multicast, IPv6 ULA), 'reserved' for other
    non-global space (documentation, benchmarking, ...), else 'public'."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "public"
    if any(a.version == n.version and a in n for n in _LOCAL_NETS):
        return "private"
    return "public" if a.is_global else "reserved"


_KIND_ROWS = {
    "private": {"name": "Private / local", "hint": "RFC 1918, CGNAT, loopback, link-local, multicast or IPv6 ULA"},
    "reserved": {"name": "Reserved / documentation", "hint": "Special-purpose address space no registry places"},
    "unknown": {"name": "Not in GeoIP database", "hint": "Public address the installed database has no entry for"},
}


async def _prefix_sides(ctx: Ctx, *, extra_cols: Mapping[str, Column] | None = None,
                        key_sql: str | None = None, drop: Iterable[str] = (), label: str) -> tuple[list, dict]:
    """Per-side traffic by /24 (IPv4) or /48 (IPv6) prefix, the top
    ``_GEO_PREFIX_LIMIT`` prefixes by volume, plus whole-scope totals. With
    ``key_sql`` rows are keyed by (key, prefix) instead (flow AS numbers)."""
    plan = await _plan(ctx, CONVERSATIONS_1H, drop)
    cols = {"x_s": SRC, "x_d": DST, **(extra_cols or {}), "x_b": "bytes", "x_p": "packets", "x_n": FLOWS}
    sub, params = _union(plan, cols, ctx, drop)
    params["nf_limit"] = _GEO_PREFIX_LIMIT
    if key_sql:
        array = "[(x_s, x_sa, toUInt8(1)), (x_d, x_da, toUInt8(0))]"
        key = f"r.2 AS k, if(r.2 != 0, toIPv6('::'), {_prefix('r.1')}) AS pfx, any(r.1) AS sample"
        group = "k, pfx"
        role = "r.3"
    else:
        array = "[(x_s, toUInt8(1)), (x_d, toUInt8(0))]"
        key = f"toUInt32(0) AS k, {_prefix('r.1')} AS pfx, any(r.1) AS sample"
        group = "pfx"
        role = "r.2"
    res = await run_query(
        f"""
        SELECT {key},
               sumIf(x_b, {role} = 1) AS sb, sumIf(x_p, {role} = 1) AS sp, sumIf(x_n, {role} = 1) AS sn,
               sumIf(x_b, {role} = 0) AS db, sumIf(x_p, {role} = 0) AS dp, sumIf(x_n, {role} = 0) AS dn,
               count() OVER () AS n_items,
               sum(sb) OVER () AS all_b, sum(sp) OVER () AS all_p, sum(sn) OVER () AS all_n
        FROM {sub} AS src
        ARRAY JOIN {array} AS r
        GROUP BY {group}
        ORDER BY (sb + db) DESC, pfx ASC, k ASC
        LIMIT %(nf_limit)s
        """,
        params,
        label=label,
    )
    rows = res.result_rows
    totals = {"bytes": 0, "packets": 0, "flows": 0, "prefixes": 0}
    if rows:
        totals = {**_totals(rows[0][10], rows[0][11], rows[0][12]), "prefixes": _i(rows[0][9])}
    return rows, totals


def _side_entry(store: dict, key: Any, base: dict) -> dict:
    e = store.get(key)
    if e is None:
        e = store[key] = {**base, "src_bytes": 0, "src_packets": 0, "src_flows": 0,
                          "dst_bytes": 0, "dst_packets": 0, "dst_flows": 0}
    return e


def _add_sides(e: dict, r) -> None:
    for i, k in enumerate(("src_bytes", "src_packets", "src_flows", "dst_bytes", "dst_packets", "dst_flows")):
        e[k] += _i(r[3 + i])


def _finish_sides(store: dict, totals: dict) -> tuple[list[dict], dict]:
    items = []
    covered = {"src_bytes": 0, "dst_bytes": 0, "src_flows": 0, "dst_flows": 0}
    for e in store.values():
        e["bytes"] = e["src_bytes"] + e["dst_bytes"]
        e["packets"] = e["src_packets"] + e["dst_packets"]
        e["flows"] = e["src_flows"] + e["dst_flows"]
        e["src_share_pct"] = _pct(e["src_bytes"], totals["bytes"])
        e["dst_share_pct"] = _pct(e["dst_bytes"], totals["bytes"])
        for k in covered:
            covered[k] += e[k]
        items.append(e)
    items.sort(key=lambda x: (-x["bytes"], str(x.get("name") or "")))
    remainder = {
        "src_bytes": max(0, totals["bytes"] - covered["src_bytes"]),
        "dst_bytes": max(0, totals["bytes"] - covered["dst_bytes"]),
        "src_flows": max(0, totals["flows"] - covered["src_flows"]),
        "dst_flows": max(0, totals["flows"] - covered["dst_flows"]),
    }
    return items, remainder


@router.get("/countries")
async def netflow_countries(
    limit: int = Query(default=600, ge=1, le=2000),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Traffic by country per side (source / destination), IPv4 and IPv6.

    Addresses are aggregated by /24 (IPv4) or /48 (IPv6) network in
    ClickHouse and each network is placed with the GeoIP country database;
    the long tail beyond the top 5,000 networks is reported as a remainder,
    not dropped silently. Private and reserved addresses form their own row.
    ``limit`` caps the number of countries returned."""
    status = _geoip_status()
    if not status["country_db"]:
        if not with_totals:
            return []
        return {**_paged([], total=None, distinct=0, limit=limit, offset=0, basis="per_side_bytes",
                         additive=False), "available": False, "geoip": status,
                "reason": "No GeoIP country database is installed (see Settings → Enrichment)"}
    rows, totals = await _prefix_sides(ctx, label="countries")
    store: dict = {}
    for r in rows:
        ip = _ip(r[2]) or ""
        kind = _address_kind(ip)
        cc, name = geoip.country_of(ip) if kind == "public" else (None, None)
        if cc:
            e = _side_entry(store, cc, {"country": cc, "country_name": name or cc, "kind": "country"})
        else:
            kind = kind if kind != "public" else "unknown"
            e = _side_entry(store, f"~{kind}", {"country": None, "country_name": _KIND_ROWS[kind]["name"],
                                                "kind": kind, "hint": _KIND_ROWS[kind]["hint"]})
        _add_sides(e, r)
    items, remainder = _finish_sides(store, totals)
    for it in items:
        it["name"] = it["country_name"]
        it["selected"] = False
    items = items[:limit]
    if not with_totals:
        return items
    return {**_paged(items, total={k: totals[k] for k in ("bytes", "packets", "flows")}, distinct=len(store),
                     limit=limit, offset=0, basis="per_side_bytes", additive=False),
            "remainder": remainder, "available": True, "geoip": status,
            "prefixes": {"resolved": len(rows), "total": totals["prefixes"]}}


@router.get("/asns")
async def netflow_asns(
    limit: int = Query(default=50, ge=1, le=2000),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Top source and destination autonomous systems (CR-20).

    The AS number is the one the exporter put in the flow (``src_as`` /
    ``dst_as``, from its BGP table) when present, else the GeoIP ASN
    database's answer for the address; names come from the GeoIP ASN
    database. ``basis_bytes`` tells how much was attributed each way. The
    ``asn`` filter is not applied to its own breakdown."""
    status = _geoip_status()
    drop = ("asn",)
    rows, totals = await _prefix_sides(
        ctx, extra_cols={"x_sa": ("toUInt32(0)", "src_as"), "x_da": ("toUInt32(0)", "dst_as")},
        key_sql="asn", drop=drop, label="autonomous systems")
    plan_source_kind = current_meta().source
    if plan_source_kind in ("rollup", "hybrid"):
        current_meta().warn("asn_rollup_geoip",
                            "Hourly rollups do not keep the exporter's AS numbers: that part of the range "
                            "is attributed with the GeoIP ASN database")
    store: dict = {}
    names: dict[int, str] = {}
    basis = {"flow": 0, "geoip": 0, "none": 0}
    for r in rows:
        flow_asn = _i(r[0])
        ip = _ip(r[2]) or ""
        side_bytes = _i(r[3]) + _i(r[6])
        if flow_asn:
            g_asn, g_name = geoip.asn_of(ip) if status["asn_db"] else (None, None)
            if g_asn == flow_asn and g_name:
                names[flow_asn] = g_name
            e = _side_entry(store, flow_asn, {"asn": flow_asn, "kind": "asn", "source": "flow"})
            if e.get("source") == "geoip":
                e["source"] = "flow+geoip"
            basis["flow"] += side_bytes
        else:
            kind = _address_kind(ip)
            g_asn, g_name = geoip.asn_of(ip) if (status["asn_db"] and kind == "public") else (None, None)
            if g_asn:
                if g_name:
                    names.setdefault(int(g_asn), g_name)
                e = _side_entry(store, int(g_asn), {"asn": int(g_asn), "kind": "asn", "source": "geoip"})
                if e.get("source") == "flow":
                    e["source"] = "flow+geoip"
                basis["geoip"] += side_bytes
            else:
                kind = kind if kind != "public" else "unknown"
                label = "Private / unrouted" if kind == "private" else (
                    "Reserved / documentation" if kind == "reserved" else "Unknown AS")
                e = _side_entry(store, f"~{kind}", {"asn": None, "kind": kind, "source": None, "as_name": label,
                                                    "hint": _KIND_ROWS[kind]["hint"]})
                basis["none"] += side_bytes
        _add_sides(e, r)
    for key, e in store.items():
        if isinstance(key, int):
            e["as_name"] = names.get(key)
            e["name"] = f"AS{key}" + (f" {names[key]}" if names.get(key) else "")
        else:
            e["name"] = e["as_name"]
        e["selected"] = isinstance(key, int) and ctx.f.asn == key
    items, remainder = _finish_sides(store, totals)
    items = items[:limit]
    if not with_totals:
        return items
    return {**_paged(items, total={k: totals[k] for k in ("bytes", "packets", "flows")}, distinct=len(store),
                     limit=limit, offset=0, basis="per_side_bytes", additive=False),
            "remainder": remainder, "basis_bytes": basis, "geoip": status,
            "available": True, "prefixes": {"resolved": len(rows), "total": totals["prefixes"]}}


# ── Next hops (CR-20) ─────────────────────────────────────────────────────────

_NH_KIND = (
    "multiIf(x_nh != toIPv6('::') AND x_nh != toIPv6('::ffff:0.0.0.0'), 'hop',"
    " bitTest(x_fp, 8), 'connected', x_fp = 0, 'legacy', 'not_exported')"
)


@router.get("/next-hops")
async def netflow_next_hops(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    resolve: int = Query(default=0),
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
    db: AsyncSession = Depends(get_db),
):
    """Traffic by BGP/IGP next hop as exported, with the egress interfaces it
    left through. Records without a next hop are split into "no next hop"
    (exported as zero: local/connected), "not exported" and legacy records,
    never shown as a 0.0.0.0 next hop. Raw records only (the rollups do not
    keep next hops)."""
    plan = await _plan(ctx, None)
    cols = {"x_nh": NEXT_HOP, "x_fp": "fields_present", "x_exp": EXPORTER, "x_oif": "output_if",
            "x_b": "bytes", "x_p": "packets", "x_n": "toUInt64(1)"}
    sub, params = _union(plan, cols, ctx)
    params.update(nf_limit=limit, nf_offset=offset)
    hops_q = run_query(
        f"""
        SELECT x_nh AS nh, sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n,
               groupUniqArray(8)(x_exp) AS exporters, groupUniqArray(16)((x_exp, x_oif)) AS outs,
               count() OVER () AS n_items
        FROM {sub} AS src
        WHERE {_NH_KIND} = 'hop'
        GROUP BY nh
        ORDER BY b DESC, nh ASC
        LIMIT %(nf_limit)s OFFSET %(nf_offset)s
        """,
        params,
        label="next hops",
    )
    kinds_q = run_query(
        f"""
        SELECT {_NH_KIND} AS kind, sum(x_b), sum(x_p), sum(x_n)
        FROM {sub} AS src
        GROUP BY kind
        """,
        dict(params),
        label="next hop coverage",
    )
    hops, kinds = await asyncio.gather(hops_q, kinds_q)
    by_kind = {str(r[0]): _totals(r[1], r[2], r[3]) for r in kinds.result_rows}
    total = _totals(sum(v["bytes"] for v in by_kind.values()), sum(v["packets"] for v in by_kind.values()),
                    sum(v["flows"] for v in by_kind.values()))
    iface_keys: set[tuple[str, int]] = set()
    for r in hops.result_rows:
        iface_keys.update(_pairs(r[5]))
    meta = await _interface_meta(db, iface_keys)
    items, distinct = [], None
    for r in hops.result_rows:
        items.append({
            "next_hop": _ip(r[0]), "ip": _ip(r[0]),
            "bytes": _i(r[1]), "packets": _i(r[2]), "flows": _i(r[3]),
            "share_pct": _pct(_i(r[1]), total["bytes"]),
            "exporters": [{"ip": _ip(e)} for e in (r[4] or [])],
            "out_interfaces": [_iface_payload(e, i, meta.get((e, i))) for e, i in _pairs(r[5])],
        })
        distinct = _i(r[6])
    if not items and offset == 0:
        distinct = 0
    await _enrich(items, "ip", bool(resolve))
    _truncation(limit, offset, len(items), distinct)
    unknown = {k: by_kind.get(k, _totals(0, 0, 0)) for k in ("connected", "not_exported", "legacy")}
    if not with_totals:
        return items
    body = _paged(items, total=total, distinct=distinct, limit=limit, offset=offset, basis="next_hop_bytes",
                  additive=True, measure={"bytes": "bytes"})
    if body["remainder"] is not None:
        # What the listed next hops leave is other next hops; traffic without
        # a next hop is reported on its own (without_next_hop).
        hop_bytes = by_kind.get("hop", {}).get("bytes", 0)
        body["remainder"] = {"bytes": max(0, hop_bytes - sum(it["bytes"] for it in items))}
    return {**body,
            "without_next_hop": unknown,
            "next_hop_coverage_pct": _pct(by_kind.get("hop", {}).get("bytes", 0), total["bytes"])}


# ── IP groups ─────────────────────────────────────────────────────────────────

_MAX_GROUPS = 200


@router.get("/ip-groups")
async def netflow_ip_groups(
    with_totals: int = Query(default=0),
    ctx: Ctx = Depends(_ctx(24)),
):
    """Traffic by managed IP group / site: sent (source in the group),
    received (destination in the group), internal (both) and involved
    (either). Groups can overlap and a flow between two groups counts for
    both, so involvement shares do not add up; ``ungrouped`` is the traffic
    touching no group at all. Sub-groups count toward their parent. The
    ``group`` filter is not applied to its own breakdown."""
    groups = netflow_groups.groups_snapshot()[:_MAX_GROUPS]
    if not groups:
        if not with_totals:
            return []
        return {**_paged([], total=None, distinct=0, limit=0, offset=0, basis="group_bytes", additive=False),
                "configured": False}
    drop = ("group",)
    cols: dict[str, Column] = {"x_b": "bytes", "x_p": "packets", "x_n": FLOWS}
    selected = []
    for idx, g in enumerate(groups):
        src = netflow_groups.networks_sql(g.effective or g.cidrs, "src_addr") or "0"
        dst = netflow_groups.networks_sql(g.effective or g.cidrs, "dst_addr") or "0"
        cols[f"x_g{idx}s"] = f"toUInt8({src})"
        cols[f"x_g{idx}d"] = f"toUInt8({dst})"
        selected.append(g)
    plan = await _plan(ctx, CONVERSATIONS_1H, drop)
    sub, params = _union(plan, cols, ctx, drop)
    aggs = ["sum(x_b)", "sum(x_p)", "sum(x_n)"]
    anyg = " OR ".join(f"x_g{i}s OR x_g{i}d" for i in range(len(selected)))
    aggs += [f"sumIf(x_b, NOT ({anyg}))", f"sumIf(x_n, NOT ({anyg}))"]
    for i in range(len(selected)):
        s, d = f"x_g{i}s", f"x_g{i}d"
        aggs += [f"sumIf(x_b, {s})", f"sumIf(x_b, {d})", f"sumIf(x_b, {s} AND {d})",
                 f"sumIf(x_b, {s} OR {d})", f"sumIf(x_p, {s} OR {d})", f"sumIf(x_n, {s} OR {d})"]
    res = await run_query(f"SELECT {', '.join(aggs)} FROM {sub} AS src", params, label="ip groups")
    row = _row(res, len(aggs))
    total = _totals(row[0], row[1], row[2])
    items = []
    for i, g in enumerate(selected):
        base = 5 + i * 6
        involved = _i(row[base + 3])
        items.append({
            "group": g.name, "name": g.name, "description": g.description, "color": g.color,
            "cidrs": [str(n) for n in g.cidrs],
            "src_bytes": _i(row[base]), "dst_bytes": _i(row[base + 1]), "internal_bytes": _i(row[base + 2]),
            "bytes": involved, "packets": _i(row[base + 4]), "flows": _i(row[base + 5]),
            "share_pct": _pct(involved, total["bytes"]),
            "selected": ctx.f.group == g.name,
        })
    items.sort(key=lambda x: (-x["bytes"], x["name"].lower()))
    if not with_totals:
        return items
    return {**_paged(items, total=total, distinct=len(items), limit=len(items), offset=0,
                     basis="group_involvement_bytes", additive=False),
            "ungrouped": {"bytes": _i(row[3]), "flows": _i(row[4]), "share_pct": _pct(_i(row[3]), total["bytes"])},
            "configured": True}
