"""NetFlow sections of the report engine (NF-04 report side, NF-25, FE-07).

``report_sections`` registers these as the ``traffic_*`` sections and the
``netflow_executive_summary`` section (preset ``netflow_executive``). Every
query goes through the NetFlow foundation, so reports answer exactly what the
NetFlow pages answer:

* **Scope** -- the requester's flow visibility (``FlowVisibility``: exporter
  ownership + tag scope; a scheduled report uses its owner's) restricts every
  query before the report's own NetFlow scope (``filters["netflow"]``, a
  FlowFilter in canonical keys) narrows it. A requester without
  ``netflow.view`` gets a section that says so, never numbers.
* **Windows** are half-open ``[from, to)``; hour rollups serve complete hours
  and raw records the partial edges (``plan_source``); the part of a period
  older than retention is left out and disclosed, never extrapolated.
* **Denominators** (NF-25) are the whole scope: every share is of the total
  traffic in scope, with an explicit "Other" remainder; host rankings say that
  a host's contribution counts both directions of its flows.
* **Unknown is not zero**: a total with no coverage is "—" with the reason.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.netflow.access import (
    UNRESTRICTED_SYSTEM,
    VIEW_PERMISSION,
    FlowVisibility,
    canonical_ip,
    resolve_exporter_owners,
    visibility_for_user,
)
from app.api.v1.netflow.common import (
    BUDGET_BACKGROUND,
    CONVERSATIONS_1H,
    EXPORTER_MINUTES,
    MAX_SPAN_ROLLUP,
    QOS_1H,
    TRAFFIC_5M,
    NetflowError,
    Window,
    get_retention,
    now_utc,
    run_query,
    table_ref,
)
from app.api.v1.netflow.filters import (
    FlowFilter,
    compile_filter,
    load_counting_policy,
    parse_flow_filter,
    parse_flow_filter_strict,
    reject_unknown_filter_keys,
)
from app.api.v1.netflow.sql import DST, SRC, display, floor_to, plan_source

logger = logging.getLogger("zenplus.netflow.report")

REPORT_BUDGET_S = BUDGET_BACKGROUND
_PROTO = {1: "ICMP", 2: "IGMP", 6: "TCP", 17: "UDP", 41: "IPv6", 47: "GRE", 50: "ESP", 51: "AH",
          58: "ICMPv6", 88: "EIGRP", 89: "OSPF", 103: "PIM", 112: "VRRP", 115: "L2TP", 132: "SCTP"}
_SEVERITY_RANK = {"critical": 4, "high": 3, "warning": 2, "medium": 2, "low": 1, "info": 0}


# ── Formatting (decimal SI, like the NetFlow pages) ──────────────────────────

def _si(value: float, unit: str) -> str:
    prefixes = ("", "k", "M", "G", "T", "P", "E")
    v = float(value)
    i = 0
    while abs(v) >= 1000 and i < len(prefixes) - 1:
        v /= 1000.0
        i += 1
    digits = 0 if abs(v) >= 100 or i == 0 else 1 if abs(v) >= 10 else 2
    return f"{v:,.{digits}f} {prefixes[i]}{unit}"


def fmt_bytes(v: float | None) -> str:
    return "—" if v is None else _si(v, "B")


def fmt_bps(v: float | None) -> str:
    if v is None:
        return "—"
    if 0 < v < 1:
        return "<1 bps"
    return _si(v, "bps")


def fmt_count(v: float | None) -> str:
    return "—" if v is None else f"{int(v):,}"


def fmt_pct(v: float | None, digits: int = 1) -> str:
    """Percent with ``digits`` decimals; a non-zero share too small to show is
    "<0.1%", never a fabricated "0.0%"."""
    if v is None:
        return "—"
    floor = 10 ** -digits
    if 0 < abs(v) < floor / 2:
        return f"<{floor:.{digits}f}%" if v > 0 else f"-<{floor:.{digits}f}%"
    return f"{v:.{digits}f}%"


def fmt_change(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) < 0.05:
        return "±0%"
    return f"{'+' if v > 0 else '−'}{abs(v):.1f}%"


def gb_value(b: float) -> float:
    """Bytes as a chart value in GB with sensible precision (1,210 / 46.5 / 0.35)."""
    v = b / 1e9
    return round(v, 0 if v >= 100 else 1 if v >= 10 else 2 if v >= 0.01 else 4)


def share(part: float | None, whole: float | None) -> float | None:
    """Percent of the whole scope; None when the whole is unknown or zero."""
    if part is None or not whole:
        return None
    return part / whole * 100.0


def change_pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev <= 0:
        return None
    return (cur - prev) / prev * 100.0


def _aware(dt: datetime) -> datetime:
    # The report engine hands over UTC instants; naive = UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _aware(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label_time(dt: datetime | None, branding: Any = None) -> str:
    """A human timestamp in the organisation's time zone, date and time format
    with its zone label ('2026-10-07 14:05 GMT+3'); UTC without branding."""
    if dt is None:
        return "—"
    if branding is not None:
        try:
            return branding.fmt_datetime(_aware(dt), with_tz=True)
        except Exception:
            logger.debug("netflow report: branding time format failed", exc_info=True)
    return _aware(dt).strftime("%Y-%m-%d %H:%M UTC")


def _ctx_branding(ctx) -> Any:
    """The report's organisation branding (SectionCtx._brand(): the loaded
    branding, or the defaults)."""
    brand = getattr(ctx, "_brand", None)
    if callable(brand):
        try:
            return brand()
        except Exception:
            return None
    return getattr(ctx, "branding", None)


def _ctx_tz(ctx) -> str:
    return getattr(_ctx_branding(ctx), "timezone", None) or "UTC"


def _point_time(ctx, dt: datetime) -> str | None:
    """Chart point timestamp: aware UTC ISO-8601 with 'Z'."""
    iso = getattr(ctx, "iso", None)
    return iso(dt) if callable(iso) else _iso(dt)


def _section(id: str, title: str, **parts: Any) -> dict:
    out = {"id": id, "title": title, "kpis": [], "charts": [], "tables": [], "notes": []}
    out.update(parts)
    return out


# ── Scope ────────────────────────────────────────────────────────────────────

class ScopeError(Exception):
    """The report cannot show NetFlow data (permission, invalid scope)."""


@dataclass
class NfScope:
    f: FlowFilter
    vis: FlowVisibility
    win: Window
    scope: dict
    label: str
    notes: list[str] = field(default_factory=list)
    # Organisation branding: every human timestamp is in its time zone.
    branding: Any = None


def describe(scope: Mapping | None) -> str:
    from app.services.netflow_alert_service import describe_scope

    return describe_scope(scope)


async def resolve_scope(ctx) -> NfScope:
    """The requester's visibility, the report's NetFlow scope and the window.
    Raises ScopeError with a user-facing message."""
    key = "_netflow_scope"
    cached = ctx._cache.get(key)
    if isinstance(cached, NfScope):
        return cached
    if isinstance(cached, ScopeError):
        raise cached
    try:
        vis = await ctx.flow_visibility()
        if getattr(ctx, "requester", None) is ORPHANED_OWNER:
            raise ScopeError("NetFlow data is withheld: the user who owns this scheduled report no longer "
                             "exists or is deactivated. Recreate the schedule from an account with NetFlow "
                             "access.")
        if not vis.can(VIEW_PERMISSION):
            raise ScopeError("NetFlow data is not shown: the report's requester does not have the "
                             "netflow.view permission.")
        raw = ctx.filters.get("netflow") or {}
        if not isinstance(raw, Mapping):
            raise ScopeError("The NetFlow scope saved with this report is not valid; NetFlow sections "
                             "were not generated.")
        try:
            from app.services import netflow_classify, netflow_groups

            await netflow_classify.load_rules()
            await netflow_groups.load_groups()
            f = parse_flow_filter_strict(raw)
        except NetflowError as exc:
            raise ScopeError(f"The NetFlow scope of this report is not valid ({exc.detail}); "
                             "NetFlow sections were not generated.") from None
        if f.count == "edge":
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                f = replace(f, counting=await load_counting_policy(db))
        for exp in (f.exporter, f.iface.exporter if f.iface else None):
            if exp and not vis.allows(exp):
                raise ScopeError("The NetFlow scope names an exporter the report's requester cannot see; "
                                 "NetFlow sections were not generated.")
        start, end = _aware(ctx.frm), _aware(ctx.to)
        end = min(end, now_utc() + timedelta(seconds=60))
        if end <= start:
            raise ScopeError("The report period is empty.")
        if end - start > MAX_SPAN_ROLLUP:
            start = end - MAX_SPAN_ROLLUP
        win = Window(start=start, end=end, mode="fixed", tz="UTC")
        scope = f.canonical()
        notes = []
        if vis.restricted:
            # Count exporters that sent flows, not every management address
            # of a visible device (a device that never exported is not one).
            from app.api.v1.netflow.access import flow_exporter_count, flow_exporters

            n = flow_exporter_count(vis, await flow_exporters())
            if n is None:
                notes.append("Restricted to the flow exporters the report's requester may see (tag scope).")
            elif n == 0:
                notes.append("The report's requester has no flow exporter in scope (tag scope); "
                             "NetFlow figures are empty for that reason, not for lack of traffic.")
            else:
                notes.append(f"Restricted to the {n} flow exporter{'s' if n != 1 else ''} the report's "
                             "requester may see (tag scope).")
        nf = NfScope(f=f, vis=vis, win=win, scope=scope, label=describe(scope), notes=notes,
                     branding=_ctx_branding(ctx))
    except ScopeError as exc:
        ctx._cache[key] = exc
        raise
    ctx._cache[key] = nf
    return nf


def _blocked(section_id: str, title: str, message: str, **parts) -> dict:
    return _section(section_id, title, notes=[message], **parts)


def _plan_notes(plan, branding: Any = None) -> list[str]:
    notes = []
    for code, message in plan.warnings:
        if code in ("raw_retention", "rollup_retention"):
            notes.append(message + f" (data from {_label_time(plan.effective_from, branding)} to "
                         f"{_label_time(plan.effective_to, branding)}).")
    return notes


def _scope_description(nf: NfScope, base: str) -> str:
    return f"{base} Scope: {nf.label}." if nf.scope else base


# ── Shared queries ───────────────────────────────────────────────────────────

@dataclass
class Totals:
    bytes: int | None
    packets: int | None
    flows: int | None
    seconds: float
    source: str
    effective_from: datetime
    effective_to: datetime
    notes: list[str]

    @property
    def avg_bps(self) -> float | None:
        if self.bytes is None or self.seconds <= 0:
            return None
        return self.bytes * 8 / self.seconds


async def totals(win: Window, f: FlowFilter, vis: FlowVisibility, *, label: str = "report-totals",
                 branding: Any = None) -> Totals:
    """Bytes/packets/flows over the window (hybrid: rollup hours + raw edges).
    ``bytes`` is None when no retained table covers any of the window.
    ``branding`` formats the retention notes' timestamps."""
    plan = await plan_source(win, f, rollup=CONVERSATIONS_1H, record=False)
    sub, params = plan.from_sql({"b": "bytes", "p": "packets", "n": ("flows", "toUInt64(1)")}, f, vis)
    res = await run_query(f"SELECT sum(b), sum(p), sum(n) FROM {sub} AS src", params,
                          budget_s=REPORT_BUDGET_S, label=label)
    row = res.result_rows[0] if res.result_rows else (0, 0, 0)
    seconds = max(0.0, (plan.effective_to - plan.effective_from).total_seconds())
    if plan.empty:
        return Totals(None, None, None, 0.0, plan.source, plan.effective_from, plan.effective_to,
                      _plan_notes(plan, branding) or ["No retained flow data covers this period."])
    return Totals(int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), seconds, plan.source,
                  plan.effective_from, plan.effective_to, _plan_notes(plan, branding))


async def ranked(win: Window, f: FlowFilter, vis: FlowVisibility, key: str | tuple[str, str], *,
                 limit: int, rollup: str = CONVERSATIONS_1H, label: str = "report-ranked",
                 display_key: bool = False) -> list[tuple[Any, int, int]]:
    """Top ``limit`` values of ``key`` by bytes: [(key, bytes, flows)]."""
    plan = await plan_source(win, f, rollup=rollup, record=False)
    flows = ("flows", "toUInt64(1)")
    sub, params = plan.from_sql({"k": key, "b": "bytes", "n": flows}, f, vis)
    params["nf_rank_limit"] = limit
    k = display("k") if display_key else "k"
    res = await run_query(
        f"SELECT {k} AS kk, sum(b) AS bb, sum(n) AS nn FROM {sub} AS src GROUP BY k ORDER BY bb DESC "
        "LIMIT %(nf_rank_limit)s", params, budget_s=REPORT_BUDGET_S, label=label)
    return [(r[0], int(r[1] or 0), int(r[2] or 0)) for r in res.result_rows]


def application_exprs() -> tuple[str, str]:
    """(rollup, raw) classifier expressions for the application name."""
    from app.services import netflow_classify

    kw = dict(app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr")
    roll = netflow_classify.classification_sql(netflow_classify.ROLLUP_SRC_PORT_SQL, "dst_port", "protocol",
                                               **kw)["application"]
    raw = netflow_classify.classification_sql("src_port", "dst_port", "protocol", **kw)["application"]
    return roll, raw


def with_remainder(rows: list[tuple[str, int, int]], total: int | None, limit: int,
                   other_label: str = "Other") -> list[tuple[str, int, int | None]]:
    """The top ``limit`` rows plus an explicit remainder so the table adds up
    to the whole-scope total (NF-25)."""
    top = rows[:limit]
    if total is None:
        return top
    rest = total - sum(r[1] for r in top)
    if rest > 0:
        top = top + [(other_label, rest, None)]
    return top


# ── traffic_kpis ─────────────────────────────────────────────────────────────

async def sec_traffic_kpis(ctx) -> dict:
    sid, title = "traffic_kpis", "Traffic Summary"
    try:
        nf = await resolve_scope(ctx)
    except ScopeError as exc:
        return _blocked(sid, title, str(exc))
    t = await totals(nf.win, nf.f, nf.vis, branding=nf.branding)
    unknown = t.bytes is None
    kpis = [
        {"label": "Total volume", "value": fmt_bytes(t.bytes), "accent": "primary",
         "subtitle": "no retained data" if unknown else f"{t.source} data"},
        {"label": "Average rate", "value": fmt_bps(t.avg_bps), "accent": "info",
         "subtitle": None if unknown else f"over {_span_text(t.seconds)}"},
        {"label": "Packets", "value": fmt_count(t.packets), "accent": "primary"},
        {"label": "Flow records", "value": fmt_count(t.flows), "accent": "info"},
    ]
    return _section(sid, title, kpis=[{k: v for k, v in x.items() if v is not None} for x in kpis],
                    description=_scope_description(nf, "Total NetFlow volume and average rate."),
                    notes=nf.notes + t.notes)


def _span_text(seconds: float) -> str:
    if seconds >= 2 * 86400:
        return f"{seconds / 86400:.0f} days"
    if seconds >= 2 * 3600:
        return f"{seconds / 3600:.0f} hours"
    return f"{max(1, round(seconds / 60))} min"


# ── traffic_trend ────────────────────────────────────────────────────────────

_BUCKETS = {"5 MINUTE": 300, "30 MINUTE": 1800, "2 HOUR": 7200, "1 DAY": 86400}


def _local_buckets(tz: str, secs: int) -> bool:
    """Whether buckets of this width follow the organisation's local day.
    Hour-and-longer buckets do (a "day" starts at local midnight, as ClickHouse
    counts hours from local midnight); minute buckets are identical in every
    whole- or half-hour zone and stay on UTC."""
    return secs >= 3600 and tz not in ("UTC", "Etc/UTC")


def _bucket_starts(frm: datetime, to: datetime, secs: int, tz: str) -> list[datetime]:
    """Aware-UTC starts of every bucket overlapping [frm, to). Local buckets
    step through wall-clock time, so a DST day keeps local-midnight days."""
    if not _local_buckets(tz, secs):
        out, cursor = [], floor_to(frm, secs)
        while cursor < to:
            out.append(cursor)
            cursor = cursor + timedelta(seconds=secs)
        return out
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    local = _aware(frm).astimezone(zone).replace(tzinfo=None)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(seconds=secs)
    wall = midnight if secs >= 86400 else midnight + step * int((local - midnight) / step)
    out = []
    while True:
        start = wall.replace(tzinfo=zone).astimezone(timezone.utc)
        if start >= to:
            return out
        out.append(start)
        nxt = wall + step
        if secs < 86400 and nxt.date() != wall.date():
            nxt = nxt.replace(hour=0, minute=0)   # ClickHouse restarts hour buckets at midnight
        wall = nxt


async def trend_series(win: Window, f: FlowFilter, vis: FlowVisibility, bucket: str, *,
                       tz: str = "UTC", branding: Any = None) -> tuple[list, list, list[str]]:
    """[(bucket start, bps | None)] over the window. A bucket in which no
    exporter in scope delivered anything is a gap (None), not zero; partial
    buckets at the edges are divided by the seconds they actually cover.
    Hour and day buckets start at the organisation's local midnight (``tz``)."""
    secs = _BUCKETS[bucket]
    local = _local_buckets(tz, secs)
    rollup = TRAFFIC_5M if secs < 3600 else CONVERSATIONS_1H
    plan = await plan_source(win, f, rollup=rollup, record=False, min_rollup_span=timedelta(0))
    sub, params = plan.from_sql({"t": "timestamp", "b": "bytes"}, f, vis)
    zone_sql = "%(nf_trend_tz)s" if local else "'UTC'"
    if local:
        params["nf_trend_tz"] = tz
    res = await run_query(
        f"SELECT toStartOfInterval(t, INTERVAL {bucket}, {zone_sql}) AS k, sum(b) FROM {sub} AS src "
        "GROUP BY k ORDER BY k", params, budget_s=REPORT_BUDGET_S, label="report-trend")
    volume = {_aware(r[0]): int(r[1] or 0) for r in res.result_rows}
    ef = FlowFilter(exporter=f.exporter or (f.iface.exporter if f.iface else None), count=f.count,
                    counting=f.counting)
    where, cparams = compile_filter(ef, EXPORTER_MINUTES, vis)
    cparams.update({"nf_cv_from": floor_to(plan.effective_from, 60), "nf_cv_to": plan.effective_to})
    if local:
        cparams["nf_trend_tz"] = tz
    cov = await run_query(
        f"SELECT toStartOfInterval(minute, INTERVAL {bucket}, {zone_sql}) AS k, count() "
        f"FROM {table_ref(EXPORTER_MINUTES)} WHERE minute >= %(nf_cv_from)s AND minute < %(nf_cv_to)s "
        f"AND {where} GROUP BY k", cparams, budget_s=REPORT_BUDGET_S, label="report-trend-coverage")
    covered = {_aware(r[0]) for r in cov.result_rows}
    ts, vals = [], []
    starts = _bucket_starts(plan.effective_from, plan.effective_to, secs, tz)
    for i, cursor in enumerate(starts):
        b_end = starts[i + 1] if i + 1 < len(starts) else cursor + timedelta(seconds=secs)
        span = (min(b_end, plan.effective_to) - max(cursor, plan.effective_from)).total_seconds()
        if cursor in volume and span > 0:
            vals.append(volume[cursor] * 8 / span)
        elif cursor in covered:
            vals.append(0.0)       # exporters delivered, nothing matched: genuinely idle
        else:
            vals.append(None)      # collection gap
        ts.append(cursor)
    return ts, vals, _plan_notes(plan, branding)


async def sec_traffic_trend(ctx) -> dict:
    sid, title = "traffic_trend", "Traffic Trend"
    try:
        nf = await resolve_scope(ctx)
    except ScopeError as exc:
        return _blocked(sid, title, str(exc))
    bucket = ctx.bucket()
    ts, vals, notes = await trend_series(nf.win, nf.f, nf.vis, bucket, tz=_ctx_tz(ctx), branding=nf.branding)
    mbps = [None if v is None else v / 1e6 for v in vals]
    gaps = sum(1 for v in vals if v is None)
    if gaps:
        notes = notes + [f"{gaps} of {len(vals)} intervals have no flow data from the exporters in scope "
                         "(collection gap, shown as a break, not as zero)."]
    # Collection gaps stay nulls so every renderer breaks the line there.
    return _section(sid, title,
                    description=_scope_description(nf, f"Average throughput per {bucket.lower()} interval."),
                    charts=[{"title": "",
                             "series": {"kind": "area", "unit": "Mbps", "color": "info",
                                        "points": [{"t": _point_time(ctx, t),
                                                    "v": None if v is None else round(v, 3)}
                                                   for t, v in zip(ts, mbps)]}}],
                    notes=nf.notes + notes)


# ── traffic_protocols ────────────────────────────────────────────────────────

async def sec_traffic_protocols(ctx) -> dict:
    sid, title = "traffic_protocols", "Traffic by Protocol"
    try:
        nf = await resolve_scope(ctx)
    except ScopeError as exc:
        return _blocked(sid, title, str(exc), half=True)
    t, rows = await asyncio.gather(
        totals(nf.win, nf.f, nf.vis, label="report-proto-total", branding=nf.branding),
        ranked(nf.win, nf.f, nf.vis, "protocol", limit=6, rollup=QOS_1H, label="report-protocols"),
    )
    items = [(_PROTO.get(int(p), f"Protocol {int(p)}"), b, n) for p, b, n in rows]
    items = with_remainder(items, t.bytes, 5, "Other protocols")
    labels = [i[0] for i in items]
    gb = [gb_value(i[1]) for i in items]
    table_rows = [[lbl, fmt_bytes(b), fmt_pct(share(b, t.bytes))] for lbl, b, _ in items]
    return _section(sid, title, half=True,
                    description=_scope_description(nf, "Share of all bytes in scope, per IP protocol."),
                    charts=[{"title": "Share of total bytes",
                             "series": {"kind": "donut", "unit": "GB",
                                        "points": [{"label": l, "value": v} for l, v in zip(labels, gb)]}}],
                    tables=[{"headers": ["Protocol", "Volume", "Share of total"],
                             "styles": ["text", "num", "pct-bar"], "rows": table_rows}],
                    notes=nf.notes + t.notes)


# ── traffic_ports (applications) ─────────────────────────────────────────────

async def sec_traffic_ports(ctx) -> dict:
    sid, title = "traffic_ports", "Top Applications / Ports"
    try:
        nf = await resolve_scope(ctx)
    except ScopeError as exc:
        return _blocked(sid, title, str(exc), half=True)
    t, rows = await asyncio.gather(
        totals(nf.win, nf.f, nf.vis, label="report-apps-total", branding=nf.branding),
        ranked(nf.win, nf.f, nf.vis, application_exprs(), limit=11, label="report-apps"),
    )
    items = with_remainder([(a or "Unclassified", b, n) for a, b, n in rows], t.bytes, 10,
                           "All other applications")
    table_rows = [[a, fmt_bytes(b), fmt_pct(share(b, t.bytes)), fmt_count(n) if n is not None else "—"]
                  for a, b, n in items]
    return _section(sid, title, half=True,
                    description=_scope_description(
                        nf, "Applications (NBAR/App-ID, operator rules, then well-known service ports) "
                            "ranked by volume; shares are of all traffic in scope."),
                    tables=[{"headers": ["Application", "Volume", "Share of total", "Flows"],
                             "styles": ["text", "num", "pct-bar", "num"], "rows": table_rows}],
                    notes=nf.notes + t.notes)


# ── traffic_talkers ──────────────────────────────────────────────────────────

async def sec_traffic_talkers(ctx) -> dict:
    sid, title = "traffic_talkers", "Top Talkers"
    try:
        nf = await resolve_scope(ctx)
    except ScopeError as exc:
        return _blocked(sid, title, str(exc))
    t, src_rows, dst_rows = await asyncio.gather(
        totals(nf.win, nf.f, nf.vis, label="report-talkers-total", branding=nf.branding),
        ranked(nf.win, nf.f, nf.vis, SRC, limit=10, label="report-top-src", display_key=True),
        ranked(nf.win, nf.f, nf.vis, DST, limit=10, label="report-top-dst", display_key=True),
    )
    tables = []
    for heading, rows, other in (("Top sources (bytes sent)", src_rows, "All other sources"),
                                 ("Top destinations (bytes received)", dst_rows, "All other destinations")):
        items = with_remainder([(r[0], r[1], r[2]) for r in rows], t.bytes, 10, other)
        tables.append({"title": heading, "headers": ["Host", "Volume", "Share of total", "Flows"],
                       "styles": ["mono", "num", "pct-bar", "num"],
                       "rows": [[h, fmt_bytes(b), fmt_pct(share(b, t.bytes)),
                                 fmt_count(n) if n is not None else "—"] for h, b, n in items]})
    return _section(sid, title,
                    description=_scope_description(
                        nf, "Hosts sending and receiving the most traffic. Each ranking covers every "
                            "flow once, so its shares add up to 100% of the traffic in scope."),
                    tables=tables, notes=nf.notes + t.notes)


# ── Executive summary (FE-07) ────────────────────────────────────────────────

async def coverage(nf: NfScope, win: Window) -> dict | None:
    """/netflow/coverage for the window (per exporter + totals), or None."""
    from app.api.v1.netflow.meta import netflow_coverage

    ef = FlowFilter(exporter=nf.f.exporter or (nf.f.iface.exporter if nf.f.iface else None),
                    count=nf.f.count, counting=nf.f.counting)
    try:
        return await netflow_coverage(win=win, f=ef, vis=nf.vis)
    except Exception:
        logger.warning("netflow report: coverage unavailable", exc_info=True)
        return None


async def known_exporters(nf: NfScope, end: datetime, days: int = 30) -> dict[str, datetime | None]:
    """Exporters in scope that delivered anything in the ``days`` before ``end``
    (the "expected" set), with their last receipt."""
    from app.services.netflow_alert_service import exporter_receipts

    ef = FlowFilter(exporter=nf.f.exporter or (nf.f.iface.exporter if nf.f.iface else None))
    rec = await exporter_receipts(ef, nf.vis, end - timedelta(days=days), end + timedelta(minutes=1),
                                  label="report-known-exporters")
    return {ip: r["last_received_at"] for ip, r in rec.items()}


async def capacity_risks(db: AsyncSession, nf: NfScope, *, limit: int = 5) -> dict:
    """Top interfaces by utilisation risk from the capacity service (B4).
    ``{"available": False, "reason": ...}`` when it cannot answer."""
    try:
        from app.services import netflow_capacity as cap
    except ImportError:
        return {"available": False, "reason": "The capacity service is not installed on this appliance."}
    fn = getattr(cap, "compute_capacity", None)
    if fn is None:
        return {"available": False, "reason": "The capacity service does not offer interface statistics."}
    f = nf.f
    try:
        unsupported = cap.unsupported_keys(f) if hasattr(cap, "unsupported_keys") else frozenset()
        if unsupported:
            f = f.without(*unsupported)
        result = await fn(nf.win, f, nf.vis, db, previous=True, record_meta=False)
    except Exception:
        logger.warning("netflow report: capacity statistics failed", exc_info=True)
        return {"available": False, "reason": "Capacity statistics could not be computed for this period."}
    rows = list(result.rows)
    key = getattr(cap, "risk_key", None)
    if key:
        rows.sort(key=key)
    summary = cap.summarize(rows) if hasattr(cap, "summarize") else {}
    risky = [r for r in rows if r.get("headline", {}).get("state") in ("over", "critical", "warning")]
    return {"available": True, "rows": risky[:limit], "risky_total": len(risky), "summary": summary,
            "method": getattr(result, "method", {}), "interfaces": len(rows),
            "filtered_keys": sorted(unsupported) if f is not nf.f else []}


async def notable_findings(db: AsyncSession, nf: NfScope, *, limit: int = 8) -> dict:
    """Open/acknowledged detection findings active in the window, most severe
    first, restricted to visible exporters and the report scope."""
    try:
        async with db.begin_nested():
            rows = (await db.execute(text("""
                SELECT id::text, kind, severity, confidence, status, title, host(src_ip), host(dst_ip),
                       host(exporter_ip), first_seen, last_seen, occurrences
                FROM netflow_findings
                WHERE last_seen >= :f AND first_seen < :t
                ORDER BY last_seen DESC LIMIT 2000
            """), {"f": nf.win.start, "t": nf.win.end})).all()
    except Exception:
        return {"available": False, "reason": "Detection findings are not available on this appliance."}
    pinned = canonical_ip(nf.f.exporter) if nf.f.exporter else None
    items = []
    for r in rows:
        exp = canonical_ip(r[8])
        if exp and not nf.vis.allows(exp):
            continue
        if exp is None and nf.vis.restricted:
            continue
        if pinned and exp != pinned:
            continue
        items.append({"id": r[0], "kind": r[1], "severity": (r[2] or "info").lower(), "confidence": r[3],
                      "status": r[4], "title": r[5], "src": r[6], "dst": r[7], "exporter": exp,
                      "first_seen": r[9], "last_seen": r[10], "occurrences": r[11]})
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    active = [i for i in items if i["status"] in ("open", "acknowledged")]
    by_sev: dict[str, int] = {}
    for it in active:
        by_sev[it["severity"]] = by_sev.get(it["severity"], 0) + 1
    groups = group_findings(active)
    return {"available": True, "items": groups[:limit], "groups": len(groups), "active": len(active),
            "by_severity": by_sev, "by_status": counts}


def group_findings(items: list[dict]) -> list[dict]:
    """One row per (kind, title): the same detection on many hosts is one
    management item, with the hosts involved (in order of appearance), how
    many findings it stands for, its worst severity and its latest sighting.
    Most severe and most recent first."""
    groups: dict[tuple, dict] = {}
    for it in items:
        key = (it["kind"], it["title"])
        host = it["src"] or it["dst"]
        g = groups.get(key)
        if g is None:
            groups[key] = {**it, "findings": 1, "hosts": [host] if host else []}
            continue
        g["findings"] += 1
        if host and host not in g["hosts"]:
            g["hosts"].append(host)
        g["occurrences"] = (g.get("occurrences") or 0) + (it.get("occurrences") or 0)
        if _SEVERITY_RANK.get(it["severity"], 0) > _SEVERITY_RANK.get(g["severity"], 0):
            g["severity"] = it["severity"]
        if it["last_seen"] and (g["last_seen"] is None or it["last_seen"] > g["last_seen"]):
            g["last_seen"] = it["last_seen"]
        if it["first_seen"] and (g["first_seen"] is None or it["first_seen"] < g["first_seen"]):
            g["first_seen"] = it["first_seen"]
        if it["status"] == "open":          # any still-open member keeps the item open
            g["status"] = "open"
    out = list(groups.values())
    out.sort(key=lambda i: (-_SEVERITY_RANK.get(i["severity"], 0),
                            -(i["last_seen"].timestamp() if i["last_seen"] else 0)))
    return out


@dataclass
class ExecInputs:
    """What the recommendations are derived from (pure data, for testing)."""

    total_change_pct: float | None = None
    top_growth: tuple[str, float] | None = None
    capacity: dict | None = None
    coverage_pct: float | None = None
    low_coverage_exporters: list[tuple[str, float]] = field(default_factory=list)
    silent_exporters: list[str] = field(default_factory=list)
    no_flag_exporters: list[str] = field(default_factory=list)
    unmapped_exporters: list[str] = field(default_factory=list)
    findings: dict | None = None
    sampled_share: float | None = None
    max_sampling: int | None = None
    retention_limited: bool = False
    raw_days: int | None = None


def recommend(x: ExecInputs, *, limit: int = 8) -> list[tuple[str, str, str]]:
    """[(priority, action, why)] -- concrete, evidence-backed next steps."""
    out: list[tuple[int, str, str, str]] = []
    cap = x.capacity or {}
    for row in (cap.get("rows") or [])[:3]:
        h = row.get("headline", {})
        name = _iface_label(row)
        util = h.get("utilization_pct")
        stat = h.get("statistic", "p95")
        sev = 0 if h.get("state") in ("over", "critical") else 1
        out.append((sev, "High" if sev == 0 else "Medium",
                    f"Plan capacity for {name} ({h.get('direction', '')})",
                    f"{stat} utilisation {fmt_pct(util)} of {fmt_bps(_speed_of(row, h.get('direction')))}; "
                    f"headroom {fmt_bps(h.get('headroom_bps'))}."))
    if x.silent_exporters:
        out.append((0, "High", f"Restore flow export from {_list(x.silent_exporters)}",
                    "No flow records were received from them at the end of the period; their traffic "
                    "is missing from every figure in this report."))
    for exp, pct in x.low_coverage_exporters[:3]:
        out.append((1, "Medium", f"Investigate collection gaps from {exp}",
                    f"Flow records cover only {fmt_pct(pct)} of the period's minutes."))
    f = x.findings or {}
    crit = sum(n for s, n in (f.get("by_severity") or {}).items() if _SEVERITY_RANK.get(s, 0) >= 3)
    if crit:
        out.append((0, "High", f"Review {crit} high-severity detection finding{'s' if crit != 1 else ''}",
                    "They are still open or acknowledged; see Notable findings."))
    if x.no_flag_exporters:
        out.append((2, "Low", f"Export TCP flags from {_list(x.no_flag_exporters)}",
                    "Their templates omit TCP flags, so SYN-scan and RST detections cannot run on "
                    "their traffic."))
    if x.unmapped_exporters:
        one = len(x.unmapped_exporters) == 1
        out.append((2, "Low", f"Map {_list(x.unmapped_exporters)} to {'its device' if one else 'their devices'}",
                    "Unmapped exporters are hidden from tag-scoped users and their alerts cannot be "
                    "routed by device."))
    if x.total_change_pct is not None and abs(x.total_change_pct) >= 50:
        grow = x.total_change_pct > 0
        why = f"Traffic {'grew' if grow else 'fell'} {fmt_change(x.total_change_pct)} against the previous period"
        if x.top_growth and grow:
            why += f", led by {x.top_growth[0]} ({fmt_change(x.top_growth[1])})"
        out.append((1, "Medium", "Confirm the change in traffic volume is expected", why + "."))
    if x.retention_limited and x.raw_days:
        out.append((2, "Low", "Review flow retention",
                    f"Part of the period is older than the {x.raw_days}-day raw retention and is "
                    "answered from hourly rollups or not at all."))
    out.sort(key=lambda r: r[0])
    return [(p, a, w) for _, p, a, w in out[:limit]]


def _list(items: list[str], n: int = 3) -> str:
    items = list(items)
    if len(items) <= n:
        return ", ".join(items)
    return ", ".join(items[:n]) + f" and {len(items) - n} more"


def _iface_label(row: dict) -> str:
    dev = row.get("device_hostname") or row.get("exporter")
    name = row.get("display_name") or row.get("if_name") or f"ifIndex {row.get('ifindex')}"
    return f"{dev} / {name}"


def _speed_of(row: dict, direction: str | None) -> float | None:
    sp = row.get("speed") or {}
    return sp.get("in_bps" if direction == "in" else "out_bps")


async def sec_executive_summary(ctx) -> dict:
    """FE-07: a management view of the period -- volume with prior-period
    change, top capacity risks, coverage and data-quality limits, notable
    findings, top applications and talkers with whole-scope shares, and
    recommended actions."""
    sid, title = "netflow_executive_summary", "NetFlow Executive Summary"
    try:
        nf = await resolve_scope(ctx)
    except ScopeError as exc:
        return _blocked(sid, title, str(exc))
    win = nf.win
    brand = nf.branding
    prior = win.shifted(-(win.end - win.start))
    app_expr = application_exprs()
    (cur, prev, apps, prev_apps, talkers, cov, known, retention) = await asyncio.gather(
        totals(win, nf.f, nf.vis, label="exec-total", branding=nf.branding),
        totals(prior, nf.f, nf.vis, label="exec-prior", branding=nf.branding),
        ranked(win, nf.f, nf.vis, app_expr, limit=6, label="exec-apps"),
        ranked(prior, nf.f, nf.vis, app_expr, limit=50, label="exec-prior-apps"),
        # Each flow counted once for each endpoint: exact sent + received.
        ranked(win, nf.f, nf.vis, f"arrayJoin([{SRC}, {DST}])", limit=5, label="exec-talkers",
               display_key=True),
        coverage(nf, win),
        known_exporters(nf, win.end),
        get_retention(),
    )
    cap = await capacity_risks(ctx.db, nf)
    findings = await notable_findings(ctx.db, nf)
    owners = await resolve_exporter_owners(ctx.db, list(known))

    # ── Volume and change ──
    # A previous period without any flow record in scope is "no data", not zero traffic: the
    # exporter may simply not have been exporting yet.
    prev_has_data = bool(prev.bytes)
    prior_complete = not prev.notes and prev_has_data
    change = change_pct(cur.bytes, prev.bytes) if prev.bytes else None
    prev_by_app = {a: b for a, b, _ in prev_apps}
    growth = None
    for a, b, _ in apps:
        c = change_pct(b, prev_by_app.get(a))
        if c is not None and (growth is None or c > growth[1]) and b >= 0.05 * (cur.bytes or 1):
            growth = (a or "Unclassified", c)

    # ── Coverage ──
    cov_totals = (cov or {}).get("totals") or {}
    per_exp = {e["exporter"]: e for e in (cov or {}).get("exporters") or []}
    receiving = [e for e, v in per_exp.items() if (v.get("minutes_with_data") or 0) > 0]
    end_is_now = win.end >= now_utc() - timedelta(minutes=10)
    silent = []
    if end_is_now:
        for exp, last in known.items():
            last = _aware(last) if isinstance(last, datetime) else None
            if last is None or win.end - last > timedelta(minutes=15):
                silent.append(_exp_name(exp, owners))
    low_cov = sorted(((_exp_name(e, owners), v["coverage_pct"]) for e, v in per_exp.items()
                      if v.get("coverage_pct") is not None and v["coverage_pct"] < 90), key=lambda x: x[1])
    no_flags = [_exp_name(e, owners) for e, v in per_exp.items()
                if (v.get("tcp_records") or 0) > 0 and (v.get("flag_coverage_pct") or 0) < 50]
    unmapped = [e for e in known if e not in owners]

    x = ExecInputs(total_change_pct=change, top_growth=growth, capacity=cap if cap.get("available") else None,
                   coverage_pct=cov_totals.get("coverage_pct"), low_coverage_exporters=low_cov,
                   silent_exporters=silent, no_flag_exporters=no_flags,
                   unmapped_exporters=[] if nf.vis.restricted else unmapped,
                   findings=findings if findings.get("available") else None,
                   sampled_share=cov_totals.get("sampled_share"), max_sampling=cov_totals.get("max_sampling"),
                   retention_limited=bool(cur.notes), raw_days=retention.raw_days)
    actions = recommend(x)

    # ── KPIs ──
    cap_summary = (cap.get("summary") or {}) if cap.get("available") else {}
    assessable = (cap_summary.get("speed_known") if "speed_known" in cap_summary
                  else (cap.get("interfaces") if cap.get("available") else None))
    risky = cap.get("risky_total") if cap.get("available") and assessable else None
    crit_findings = sum(n for s, n in (findings.get("by_severity") or {}).items()
                        if _SEVERITY_RANK.get(s, 0) >= 3) if findings.get("available") else None
    kpis = [
        {"label": "Traffic volume", "value": fmt_bytes(cur.bytes), "accent": "primary",
         "subtitle": (f"{fmt_change(change)} vs previous period" if change is not None
                      else "previous period: no comparable data")},
        {"label": "Average rate", "value": fmt_bps(cur.avg_bps), "accent": "info",
         "subtitle": f"previous {fmt_bps(prev.avg_bps)}" if prev_has_data else "previous period: no data"},
        {"label": "Exporters reporting", "value": f"{len(receiving)} of {len(known)}" if known else "—",
         "accent": "success" if known and len(receiving) == len(known) and not silent else "warning",
         "subtitle": f"coverage {fmt_pct(cov_totals.get('coverage_pct'))}"},
        {"label": "Capacity risks", "value": "—" if risky is None else str(risky),
         "accent": "danger" if risky else ("success" if risky == 0 else "warning"),
         "subtitle": (f"of {assessable} interface{'s' if assessable != 1 else ''} with a known speed"
                      if risky is not None else
                      "no interface traffic in scope" if cap.get("available") and not cap.get("interfaces")
                      else "no interface in scope has a known speed" if cap.get("available")
                      else "capacity data unavailable")},
        {"label": "Severe findings", "value": "—" if crit_findings is None else str(crit_findings),
         "accent": "danger" if crit_findings else ("success" if crit_findings == 0 else "warning"),
         "subtitle": (f"high or critical; {findings.get('active', 0)} open or acknowledged in total"
                      if findings.get("available") else "detection data unavailable")},
    ]

    tables: list[dict] = []
    notes: list[str] = list(nf.notes)

    # Recommended actions first: what management should do.
    tables.append({"title": "Recommended actions", "headers": ["Priority", "Action", "Why"],
                   "styles": ["severity", "text", "text"],
                   "rows": [[p, a, w] for p, a, w in actions] or
                   [["Info", "No action required", "No capacity, coverage or security issue crossed a "
                                                   "threshold in this period."]]})

    # Capacity.
    if cap.get("available"):
        rows = []
        for row in cap["rows"]:
            h = row["headline"]
            prev_b = row.get("previous") or {}
            direction = h.get("direction")
            rows.append([f"{_iface_label(row)} ({direction})" if direction else _iface_label(row),
                         f"{h.get('statistic')} {fmt_pct(h.get('utilization_pct'))}",
                         f"{fmt_bps(h.get('value_bps'))} of {fmt_bps(_speed_of(row, direction))}",
                         fmt_bps(h.get("headroom_bps")), fmt_change(prev_b.get("change_pct"))])
        tables.append({"title": "Top capacity risks",
                       "headers": ["Interface (direction)", "Utilisation", "Rate of speed", "Headroom",
                                   "Change"],
                       "styles": ["text", "num", "text", "num", "num"],
                       "rows": rows or [[
                           "No interface at warning or above" if assessable else
                           "No interface could be assessed: none has a known speed", "—", "—", "—", "—"]]})
        summ = cap.get("summary") or {}
        if summ.get("speed_unknown"):
            notes.append(f"{summ['speed_unknown']} of {summ.get('interfaces_total')} interfaces with flow "
                         "traffic have no known speed; their utilisation cannot be assessed.")
    else:
        notes.append(f"Capacity risks are not included: {cap.get('reason')}")

    # Findings.
    if findings.get("available"):
        frows = [[i["severity"].capitalize(),
                  (i["title"] or i["kind"] or "Finding") + (" (acknowledged)" if i["status"] == "acknowledged" else ""),
                  _list(i["hosts"]) if i["hosts"] else "—", f"{i['findings']:,}", _label_time(i["last_seen"], brand)]
                 for i in findings["items"]]
        tables.append({"title": "Notable findings",
                       "headers": ["Severity", "Finding", "Hosts", "Findings", "Last seen"],
                       "styles": ["severity", "text", "text", "num", "text"],
                       "rows": frows or [["Info", "No open detection findings in this period", "—", "—", "—"]]})
        if findings.get("groups", 0) > len(findings["items"]):
            notes.append(f"Notable findings shows the {len(findings['items'])} most severe of "
                         f"{findings['groups']} kinds of open finding ({findings['active']} findings in total); "
                         "the NetFlow Anomalies page lists every one.")
    else:
        notes.append(findings.get("reason") or "Detection findings unavailable.")

    # Applications (whole-scope shares + change).
    app_items = with_remainder([(a or "Unclassified", b, n) for a, b, n in apps], cur.bytes, 5,
                               "All other applications")
    tables.append({"title": "Top applications",
                   "headers": ["Application", "Volume", "Share of total", "Change"],
                   "styles": ["text", "num", "pct-bar", "num"],
                   "rows": [[a, fmt_bytes(b), fmt_pct(share(b, cur.bytes)),
                             fmt_change(change_pct(b, prev_by_app.get(a))) if n is not None else "—"]
                            for a, b, n in app_items]})

    # Talkers: a host's contribution counts what it sent and received.
    top_hosts = [(h, b) for h, b, _ in talkers]
    tables.append({"title": "Top talkers (sent + received)",
                   "headers": ["Host", "Volume", "Share of total"],
                   "styles": ["mono", "num", "pct-bar"],
                   "rows": [[h, fmt_bytes(b), fmt_pct(share(b, cur.bytes))] for h, b in top_hosts]})

    # Coverage & data quality.
    dq = [
        ["Period", f"{_label_time(win.start, brand)} → {_label_time(win.end, brand)}",
         f"data {_label_time(cur.effective_from, brand)} → {_label_time(cur.effective_to, brand)} ({cur.source})"],
        ["Previous period", f"{_label_time(prior.start, brand)} → {_label_time(prior.end, brand)}",
         "complete" if prior_complete else
         "no flow records in scope" if prev.bytes == 0 and not prev.notes else
         "partly outside retention" if prev_has_data else "no retained data"],
        ["Scope", nf.label, "restricted to visible exporters" if nf.vis.restricted else "all exporters"],
        ["Collection coverage", fmt_pct(cov_totals.get("coverage_pct")),
         f"{cov_totals.get('minutes_with_data') or 0} of {cov_totals.get('minutes_in_window') or 0} minutes "
         "with flow records"],
        ["Sampling", "sampled" if (cov_totals.get("sampled_share") or 0) > 0 else "unsampled",
         (f"{fmt_pct((cov_totals.get('sampled_share') or 0) * 100)} of records sampled "
          f"(up to 1:{cov_totals.get('max_sampling')}); volumes are estimates")
         if (cov_totals.get("sampled_share") or 0) > 0 else "volumes are exact counts"],
        ["TCP flags", fmt_pct(cov_totals.get("flag_coverage_pct")),
         "share of TCP records with exported flags (security detections need them)"],
        ["Retention", f"raw {retention.raw_days or '—'} d · rollup {retention.rollup_days or '—'} d",
         "older detail is answered from hourly rollups"],
    ]
    cap_method = (cap.get("method") or {}) if cap.get("available") else {}
    if cap_method.get("statistic"):
        dq.append(["Capacity method", f"{cap_method['statistic']} per direction",
                   "of complete 5-minute samples (nearest rank); collection gaps are excluded, not counted "
                   "as idle"])
    dq.append(["Talker shares", "sent + received",
               "one flow counts for both of its endpoints, so talker shares can add up to more than 100%"])
    tables.append({"title": "Coverage and data quality", "headers": ["Item", "Value", "Note"],
                   "styles": ["text", "text", "text"], "rows": dq})
    notes.extend(cur.notes)
    if not prior_complete:
        notes.append("The previous period is not fully covered by retained data; its comparison is "
                     "indicative only." if prev_has_data else
                     "The previous period has no flow data in this scope, so no change is shown.")
    if (cov_totals.get("coverage_pct") or 100) < 95:
        notes.append(f"Flow collection covered {fmt_pct(cov_totals.get('coverage_pct'))} of the period; "
                     "volumes understate traffic during collection gaps.")

    charts = []
    bar_items = [(a, b) for a, b, n in app_items if n is not None][:5]
    if bar_items:
        charts.append({"title": "Top applications (GB)",
                       "series": {"kind": "rank", "unit": "GB", "color": "primary",
                                  "points": [{"label": a, "value": gb_value(b)} for a, b in bar_items]}})

    return _section(
        sid, title,
        description=(f"{_label_time(win.start, brand)} → {_label_time(win.end, brand)}, compared with the previous "
                     f"{_span_text((win.end - win.start).total_seconds())}. Scope: {nf.label}."),
        kpis=kpis, charts=charts, tables=tables, notes=list(dict.fromkeys(notes)),
    )


def _exp_name(exp: str, owners: Mapping) -> str:
    o = owners.get(exp)
    return f"{o.hostname} ({exp})" if o and getattr(o, "hostname", None) else exp


class _OrphanedOwner:
    """Requester of a scheduled report whose owner was deleted or deactivated."""

    id = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ORPHANED_OWNER"


ORPHANED_OWNER = _OrphanedOwner()
_NO_VISIBILITY = FlowVisibility(restricted=True, exporters=frozenset(), user_id=None, permissions=frozenset())


async def flow_visibility_for(db: AsyncSession, requester) -> FlowVisibility:
    """The visibility a report runs with: the requester's, the appliance's for
    system-generated reports with no requester (None), and nothing at all for
    a schedule whose owner no longer exists (fail closed)."""
    if requester is None:
        return UNRESTRICTED_SYSTEM
    if requester is ORPHANED_OWNER:
        return _NO_VISIBILITY
    return await visibility_for_user(db, requester)


def scope_from_query(raw: str | None) -> dict:
    """``netflow=`` report parameter (URL-encoded FlowFilter query string) ->
    canonical scope dict. Raises NetflowError (400) when invalid, including
    a key that is not a filter key (a typo must not widen the report to the
    whole scope)."""
    from urllib.parse import parse_qsl

    from starlette.datastructures import QueryParams

    if not raw:
        return {}
    pairs = parse_qsl(raw, keep_blank_values=False)
    reject_unknown_filter_keys([k for k, _v in pairs])
    return parse_flow_filter(QueryParams(pairs)).canonical()


def scope_from_mapping(raw: Any) -> dict:
    """A stored NetFlow report scope (``filters.netflow`` of a schedule, a
    JSON object of filter keys) -> canonical scope dict; NetflowError (400)
    when it is not an object, names an unknown key or holds an invalid
    value. Callers load the classifier rules and IP groups first."""
    from app.api.v1.netflow.common import invalid_filter

    if raw in (None, {}, ""):
        return {}
    if not isinstance(raw, Mapping):
        raise invalid_filter("filters.netflow must be an object of NetFlow filter keys",
                             {"filters.netflow": "Not an object"})
    return parse_flow_filter_strict(raw, prefix="filters.netflow.").canonical()


__all__ = [
    "sec_traffic_kpis", "sec_traffic_trend", "sec_traffic_protocols", "sec_traffic_ports",
    "sec_traffic_talkers", "sec_executive_summary", "resolve_scope", "ScopeError", "totals", "ranked",
    "trend_series", "with_remainder", "share", "change_pct", "recommend", "ExecInputs", "capacity_risks",
    "notable_findings", "group_findings", "flow_visibility_for", "ORPHANED_OWNER", "scope_from_query", "describe",
    "scope_from_mapping",
]
