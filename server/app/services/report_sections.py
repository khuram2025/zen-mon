"""Section-based report engine.

A *section* is a self-contained unit of report content: it fetches its data
for a time window and emits a neutral ``SectionOutput`` dict::

    {
      "id": "availability_trend",
      "title": "Availability Trend",
      "description": "...",
      "kpis":   [{"label", "value", "accent", "subtitle"?}],
      "charts": [{"title", "png": bytes}],          # matplotlib PNG bytes
      "tables": [{"title"?, "headers": [...], "rows": [[...]], "widths"?}],
      "notes":  [str],
    }

Three renderers consume that model:

- :func:`sections_to_json` — for the dashboard's generic on-screen viewer
  (chart PNGs become base64 data URIs);
- :func:`render_html` — a self-contained, print-ready branded HTML document
  (also the artifact stored in ``report_runs`` for share links);
- :func:`render_pdf` — fpdf2 via the existing ``ZenPlusReport`` primitives.

Report *types* are named presets of section ids (``REPORT_PRESETS``); custom
reports are user-picked section lists stored in ``custom_reports``. Legacy
fpdf2 report types (executive_summary/device_health/service_health/
alert_analysis/full_report) keep their original pipeline in
report_service.py — this engine only serves the new types.

Sections reuse the heavy lifting already done for the on-screen report tabs
(report_data_service builders) through a per-render cache, so a render calls
each underlying builder at most once no matter how many sections consume it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.report_service import (
    COLOR_FAINT,
    COLOR_HAIRLINE,
    COLOR_MUTED,
    COLOR_TEXT,
    CONTENT_W,
    HEX_INFO,
    HEX_PRIMARY,
    HEX_SUCCESS,
    HEX_WARNING,
    MARGIN_L,
    MARGIN_T,
    ZenPlusReport,
    _fetch_company_info,
    _make_bar_chart,
    _make_donut,
    _make_line_chart,
    _make_time_bar_chart,
    _safe,
)
from app.services.report_data_service import (
    build_business,
    build_executive,
    build_inventory,
    build_technical,
    device_outage_episodes,
)

logger = logging.getLogger("zenplus.reports")

ENTRY_KINDS = "('SERVER','CONSUMER')"

ACCENT_HEX = {
    "primary": "#4F6BF6",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "danger": "#EF4444",
    "info": "#22D3EE",
}
ACCENT_RGB = {
    "primary": (79, 107, 246),
    "success": (34, 197, 94),
    "warning": (245, 158, 11),
    "danger": (239, 68, 68),
    "info": (34, 211, 238),
}


def _fmt_bps(v: float) -> str:
    for unit in ("bps", "Kbps", "Mbps", "Gbps", "Tbps"):
        if abs(v) < 1000:
            return f"{v:,.1f} {unit}"
        v /= 1000
    return f"{v:,.1f} Pbps"


def _fmt_bytes(v: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(v) < 1024:
            return f"{v:,.1f} {unit}"
        v /= 1024
    return f"{v:,.1f} PB"


def _fmt_ms(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def _finite(v: Any) -> Optional[float]:
    """float(v), or None for missing, NaN or infinite values (an empty
    ClickHouse quantile or average comes back as nan)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _aware_utc(dt: datetime) -> datetime:
    """Aware UTC; naive values are UTC throughout the report engine."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _naive_utc(dt: datetime) -> datetime:
    """Naive UTC, the form ClickHouse DateTime parameters are bound in."""
    return _aware_utc(dt).replace(tzinfo=None)


def _top_n(ctx: "SectionCtx", default: int) -> int:
    """The running section's validated ``top_n`` option (its registry
    default when the template sets none; ``default`` outside a render)."""
    try:
        return max(1, int(ctx.opt("top_n", default)))
    except (TypeError, ValueError):
        return default


def _shown_note(shown: int, total: int, what: str) -> list[str]:
    """Disclose a list cut to the section's row limit (never cut silently)."""
    if total > shown:
        return [f"Showing {shown:,} of {total:,} {what}. Raise the section's “Rows” option to "
                "list more."]
    return []


def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _coerce_dt(value: Any) -> Optional[datetime]:
    """datetime | ISO string | None -> aware-or-naive datetime (naive = UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return _parse_ts(value)
        except ValueError:
            return None
    return None


# ─── Section context with builder cache ─────────────────────────────────────

class SectionCtx:
    """Carries the window + db handle and memoises shared dataset builders."""

    def __init__(self, db: AsyncSession, frm: datetime, to: datetime,
                 filters: Optional[dict] = None, requester: Any = None):
        self.db = db
        self.frm = frm
        self.to = to
        self.filters = filters or {}
        # Who the report is for (the viewer, or a schedule's owner): NetFlow
        # sections are restricted to the flow exporters this user may see.
        # None = generated by the appliance itself (unrestricted).
        self.requester = requester
        self._cache: dict[str, Any] = {}
        # Organisation branding (timezone, date/time format); set by
        # build_sections. Sections format every timestamp through fmt_*.
        self.branding = None
        # The running section's validated options (see SECTION_REGISTRY
        # "options") and its template entry.
        self.section_options: dict[str, Any] = {}
        self.section_id: str = ""

    # --- per-section options -------------------------------------------------

    def opt(self, key: str, default: Any = None) -> Any:
        """The running section's option ``key`` (validated, defaulted)."""
        if key in self.section_options:
            return self.section_options[key]
        spec = SECTION_REGISTRY.get(self.section_id) or {}
        for o in spec.get("options") or ():
            if o["key"] == key:
                return o.get("default", default)
        return default

    # --- organisation time formatting ---------------------------------------

    def _brand(self):
        if self.branding is None:
            from app.services.branding import Branding
            self.branding = Branding()
        return self.branding

    @property
    def tz_name(self) -> str:
        return self._brand().timezone

    def fmt_dt(self, value: Any, seconds: bool = False) -> str:
        """Date + time in the organisation timezone and format ('—' if unset)."""
        dt = _coerce_dt(value)
        return self._brand().fmt_datetime(dt, seconds=seconds) if dt else "—"

    def fmt_date(self, value: Any) -> str:
        dt = _coerce_dt(value)
        return self._brand().fmt_date(dt) if dt else "—"

    def fmt_short(self, value: Any) -> str:
        """Compact 'Oct 07 14:05' in the organisation timezone for dense tables."""
        dt = _coerce_dt(value)
        return self._brand().fmt_short(dt) if dt else "—"

    def tz_label(self) -> str:
        return self._brand().tz_label(self.to)

    # --- shared query helpers for section packs ------------------------------

    def pg_device_clause(self, column: str, key: str = "dev_ids") -> tuple[str, dict]:
        """`` AND <column> = ANY(ids)`` for Postgres when a device filter is set.

        A visibility-scoped account with no visible devices carries
        scoping.NO_MATCH_ID, which matches nothing (fail closed)."""
        ids = self.device_ids
        if not ids:
            return "", {}
        return f" AND {column}::text = ANY(:{key})", {key: ids}

    def ch_device_clause(self, column: str = "device_id", key: str = "dev_ids") -> tuple[str, dict]:
        """`` AND <column> IN ids`` for ClickHouse when a device filter is set."""
        ids = self.device_ids
        if not ids:
            return "", {}
        return f" AND {column} IN %({key})s", {key: ids}

    async def ch_rows(self, sql: str, params: Optional[dict] = None) -> list:
        """Run a ClickHouse query off the event loop and return result rows."""
        client = self.ch()
        result = await asyncio.to_thread(client.query, sql, parameters=params or {})
        return list(result.result_rows)

    def iso(self, value: Any) -> Optional[str]:
        """Chart point timestamp: ISO-8601 UTC with 'Z' (the viewer formats it
        in the organisation timezone; naive values are UTC)."""
        dt = _coerce_dt(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def ch_bucket(self, column: str = "timestamp", interval: Optional[str] = None) -> tuple[str, dict]:
        """``toStartOfInterval(column, INTERVAL …, tz)`` aligned to the
        organisation timezone (daily buckets start at local midnight)."""
        return (f"toStartOfInterval({column}, INTERVAL {interval or self.bucket()}, %(org_tz)s)",
                {"org_tz": self.tz_name})

    def prior_window(self) -> tuple[datetime, datetime]:
        """The equally long window immediately before this one (for deltas)."""
        span = self.to - self.frm
        return self.frm - span, self.frm

    def window_note(self, retention_days: int, what: str) -> Optional[str]:
        """A disclosure when the window reaches past ``what``'s retention."""
        reach = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention_days)
        frm = self.frm.replace(tzinfo=None) if self.frm.tzinfo else self.frm
        if frm < reach:
            return (f"{what} is kept for {retention_days} days; this section covers "
                    f"{self.fmt_date(reach)} onwards.")
        return None

    async def flow_visibility(self):
        """The requester's NetFlow visibility (cached for the render)."""
        if "_flow_visibility" not in self._cache:
            from app.services.netflow_report import flow_visibility_for
            self._cache["_flow_visibility"] = await flow_visibility_for(self.db, self.requester)
        return self._cache["_flow_visibility"]

    async def withholds_netflow_alerts(self) -> bool:
        """Whether NetFlow alert messages must be withheld from this report
        (X-01): its requester lacks netflow.view. The appliance itself (no
        requester) sees them; an orphaned schedule owner does not."""
        from app.api.v1.netflow.access import VIEW_PERMISSION
        return not (await self.flow_visibility()).can(VIEW_PERMISSION)

    @property
    def device_ids(self) -> Optional[list[str]]:
        ids = self.filters.get("device_ids")
        return [str(i) for i in ids] if ids else None

    @property
    def application_id(self) -> Optional[str]:
        """RUM application to scope browser sections to; None means every application."""
        app = self.filters.get("application_id")
        return str(app) if app else None

    def rum_app_clause(self) -> str:
        """SQL fragment scoping a RUM query to the selected application."""
        return " AND application_id = %(app)s" if self.application_id else ""

    def rum_params(self, **extra: Any) -> dict:
        p: dict[str, Any] = {"f": self.frm, "t": self.to}
        if self.application_id:
            p["app"] = self.application_id
        p.update(extra)
        return p

    async def dataset(self, name: str) -> dict:
        if name not in self._cache:
            if name == "executive":
                self._cache[name] = await build_executive(self.db, self.frm, self.to)
            elif name == "technical":
                self._cache[name] = await build_technical(
                    self.db, self.frm, self.to, redact_netflow=await self.withholds_netflow_alerts())
            elif name == "business":
                self._cache[name] = await build_business(self.db, self.frm, self.to)
            elif name == "inventory":
                self._cache[name] = await build_inventory(self.db)
            else:
                raise KeyError(name)
        return self._cache[name]

    def ch(self):
        from app.core.database import get_clickhouse_client
        return get_clickhouse_client()

    @property
    def hours(self) -> float:
        return max((self.to - self.frm).total_seconds() / 3600.0, 0.01)

    def bucket(self) -> str:
        h = self.hours
        if h <= 6:
            return "5 MINUTE"
        if h <= 48:
            return "30 MINUTE"
        if h <= 21 * 24:
            return "2 HOUR"
        return "1 DAY"


def _section(id: str, title: str, **parts: Any) -> dict:
    out = {"id": id, "title": title, "kpis": [], "charts": [], "tables": [], "notes": []}
    out.update(parts)
    return out


def _local_bucket_starts(ctx: SectionCtx, unit: str) -> list[datetime]:
    """Aware-UTC starts of every organisation-local hour or day overlapping
    the window (what Postgres ``date_trunc(unit, ts AT TIME ZONE tz)``
    produces). Wall-clock stepping keeps local midnights across DST."""
    zone = ctx._brand().tz
    frm, to = _aware_utc(ctx.frm), _aware_utc(ctx.to)
    local = frm.astimezone(zone).replace(tzinfo=None)
    if unit == "day":
        wall, step = local.replace(hour=0, minute=0, second=0, microsecond=0), timedelta(days=1)
    else:
        wall, step = local.replace(minute=0, second=0, microsecond=0), timedelta(hours=1)
    out: list[datetime] = []
    while len(out) < 10_000:
        start = wall.replace(tzinfo=zone).astimezone(timezone.utc)
        if start >= to:
            break
        if not out or start > out[-1]:      # a skipped DST hour maps onto the next one
            out.append(start)
        wall += step
    return out


async def _retention_days(ctx: SectionCtx, table: str, default: int) -> int:
    """Days a ClickHouse table currently keeps (its TTL, which Storage settings
    and the RUM settings page may change), cached per render; ``default``
    when the TTL cannot be read."""
    key = f"_ttl_days:{table}"
    if key not in ctx._cache:
        days = default
        try:
            from app.services.storage_service import _parse_ttl_days
            rows = await ctx.ch_rows(
                "SELECT engine_full FROM system.tables WHERE database = 'zenplus' AND name = %(ttl_table)s",
                {"ttl_table": table})
            parsed = _parse_ttl_days(str(rows[0][0])) if rows else None
            if parsed and parsed > 0:
                days = parsed
        except Exception:
            logger.debug("retention of %s unavailable", table, exc_info=True)
        ctx._cache[key] = days
    return ctx._cache[key]


# ─── Availability sections ──────────────────────────────────────────────────

SLA_TARGET_PCT = 99.9  # fallback only; the configured policy default wins


async def _node_availability(ctx: SectionCtx) -> dict:
    """Per-node availability over the window, honouring the device filter.

    Read from the ping table that covers a window this long
    (``ping_rollups.ping_table_for_hours``: raw up to 6 h, 5-minute up to 7 d,
    hourly beyond) and sample-weighted (up samples / samples; the rollups'
    ``uptime_pct`` is a 0..1 fraction), with samples inside planned
    maintenance excluded -- the arithmetic of the Availability page
    (report_data_service.build_executive), so the report's headline agrees
    with it. Outage episodes come from device_status_log, device identity and
    status from Postgres. Cached per render: the KPIs, trend, node table,
    lowest-availability list, latency table and longest outages share it.
    """
    if "node_avail" in ctx._cache:
        return ctx._cache["node_avail"]

    from app.services.ping_rollups import is_rollup, ping_table_for_hours, rtt_agg_sql, uptime_agg_sql
    from app.services.report_data_service import _maintenance_exclusion_sql
    from app.services.report_service import _fetch_device_maintenance_windows

    ids = ctx.device_ids
    pg_sql = ("SELECT id::text, hostname, ip_address::text, COALESCE(location, ''), "
              "COALESCE(status, 'unknown') FROM devices")
    pg_params: dict[str, Any] = {}
    if ids:
        pg_sql += " WHERE id::text = ANY(:ids)"
        pg_params["ids"] = ids
    devices = [
        {"id": r[0], "hostname": r[1] or r[2] or r[0][:8],
         "ip": (r[2] or "").split("/")[0],
         "location": r[3], "status": r[4]}
        for r in (await ctx.db.execute(text(pg_sql + " ORDER BY hostname"), pg_params)).fetchall()
    ]
    names = {d["id"]: d["hostname"] for d in devices}

    # Planned maintenance windows (device_id -> [(start, end)], naive UTC):
    # samples inside them never dent availability, the trend or the RTT.
    try:
        maint = await _fetch_device_maintenance_windows(ctx.db, ctx.frm, ctx.to)
    except Exception:
        maint = {}
    maint = {did: wins for did, wins in maint.items() if did in names}

    table = ping_table_for_hours(ctx.hours)
    stats: dict[str, dict] = {}
    trend: list[dict] = []
    if names:
        maint_sql, maint_params = _maintenance_exclusion_sql(maint)
        # Only current inventory is counted (ids come from Postgres, so the
        # primary-key filter is always well formed): history of deleted
        # devices must not move the headline.
        params: dict[str, Any] = {"f": _naive_utc(ctx.frm), "t": _naive_utc(ctx.to),
                                  "ids": list(names), **maint_params}
        where = (f" FROM zenplus.{table} WHERE timestamp >= %(f)s AND timestamp <= %(t)s"
                 f" AND device_id IN %(ids)s {maint_sql}")
        # Packet loss while the node answered: a down probe records 100 % loss,
        # which is availability, not loss. On the rollups the down samples'
        # loss (1.0 each) is subtracted from the bucket total.
        loss_sql = ("greatest(sum(avg_packet_loss * sample_count) - sum((1 - uptime_pct) * sample_count), 0)"
                    if is_rollup(table) else "sumIf(packet_loss, is_up = 1)")
        try:
            rows = await ctx.ch_rows(
                f"SELECT toString(device_id) AS did, {uptime_agg_sql(table)}, {rtt_agg_sql(table)}, "
                f"{loss_sql} AS loss_up{where} GROUP BY did", params)
            for did, up, total, avg_rtt, p95_rtt, rtt_n, loss_up in rows:
                did, up, total = str(did), float(up or 0), float(total or 0)
                if did not in names or total <= 0:
                    continue
                has_rtt = float(rtt_n or 0) > 0
                stats[did] = {
                    "avail": up / total * 100, "up": up, "samples": total,
                    "rtt": _finite(avg_rtt) if has_rtt else None,
                    "p95_rtt": _finite(p95_rtt) if has_rtt else None,
                    "loss_pct": (min(max(float(loss_up or 0) / up, 0.0), 1.0) * 100) if up > 0 else None,
                }
        except Exception:
            logger.exception("node availability query failed")
        try:
            bucket_sql, bucket_params = ctx.ch_bucket("timestamp")
            trows = await ctx.ch_rows(
                f"SELECT {bucket_sql} AS b, {uptime_agg_sql(table)}{where} GROUP BY b ORDER BY b",
                {**params, **bucket_params})
            trend = [{"t": ctx.iso(b), "v": round(float(up or 0) / float(total) * 100, 2)}
                     for b, up, total in trows if total]
        except Exception:
            logger.exception("node availability trend query failed")

    # Outage episodes pair each down transition with its recovery; the log's
    # duration_sec column is always 0, so it cannot supply durations.
    outages: list[dict] = []
    try:
        if names:
            episodes = await asyncio.to_thread(
                device_outage_episodes, ctx.frm, ctx.to, list(names), maint)
            for e in episodes:
                s = stats.setdefault(e["entity_id"], {})
                s["outages"] = s.get("outages", 0) + 1
                s["logged_down_s"] = s.get("logged_down_s", 0) + e["in_window_s"]
                outages.append({
                    "device_id": e["entity_id"], "hostname": names.get(e["entity_id"], "Unknown"),
                    "started_at": e["started_at"] or e["clipped_start"],
                    "start_known": e["started_at"] is not None,
                    "duration_s": e["duration_s"], "in_window_s": e["in_window_s"],
                    "ongoing": e["ongoing"],
                })
    except Exception:
        logger.exception("node outage episodes query failed")

    from app.services.availability_policy import load_default_target
    sla_target, _source = await load_default_target(ctx.db)

    window_min = ctx.hours * 60
    nodes = []
    for d in devices:
        s = stats.get(d["id"], {})
        avail = s.get("avail")
        # SLA-relevant minutes for this node exclude its own maintenance time.
        maint_min = sum(max(0.0, (e - st).total_seconds() / 60)
                        for st, e in maint.get(d["id"], []))
        sla_min = max(window_min - maint_min, 0.0)
        nodes.append({
            **d,
            "avail": round(avail, 3) if avail is not None else None,
            "downtime_min": round((100 - avail) / 100 * sla_min, 1) if avail is not None else None,
            "outages": s.get("outages", 0),
            "rtt": s.get("rtt"),
            "p95_rtt": s.get("p95_rtt"),
            "loss_pct": s.get("loss_pct"),
            "maintenance_min": round(maint_min, 1) if maint_min else 0,
        })
    # Problems first; devices with no data at the bottom.
    nodes.sort(key=lambda n: (n["avail"] is None, n["avail"] if n["avail"] is not None else 0))

    reporting = [n for n in nodes if n["avail"] is not None]
    # Sample-weighted over every measured node (not a mean of per-node
    # percentages): a node polled twice as often weighs twice as much, exactly
    # like the Availability page's headline.
    up_sum = sum(s.get("up", 0.0) for s in stats.values())
    sample_sum = sum(s.get("samples", 0.0) for s in stats.values())
    overall = up_sum / sample_sum * 100 if sample_sum else None
    result = {
        "nodes": nodes,
        "trend": trend,
        "overall": round(overall, 3) if overall is not None else None,
        "up": sum(1 for n in nodes if n["status"] == "up"),
        "below_sla": sum(1 for n in reporting if n["avail"] < sla_target),
        "sla_target": sla_target,
        "downtime_min": round(sum(n["downtime_min"] or 0 for n in nodes), 1),
        "outage_events": sum(n["outages"] for n in nodes),
        "outage_list": outages,
        "filtered": bool(ids),
        "maint_excluded_min": round(sum(n["maintenance_min"] for n in nodes), 1),
        "measured": len(reporting),
        "table": table,
    }
    ctx._cache["node_avail"] = result
    return result


def _ping_method_note(na: dict) -> list[str]:
    """How long windows were measured (the rollups keep no raw probes)."""
    if na.get("table") == "ping_metrics_1h":
        return ["Windows longer than 7 days are measured from hourly ping rollups; latency "
                "percentiles are taken across hourly averages and read slightly low."]
    if na.get("table") == "ping_metrics_5m":
        return ["Latency percentiles are taken across 5-minute averages (raw probes are kept "
                "only for short windows) and read slightly low."]
    return []


async def _sec_availability_kpis(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    avail = na["overall"]
    target = na.get("sla_target", SLA_TARGET_PCT)
    scope = f"{len(na['nodes'])} selected node(s)" if na["filtered"] else "all monitored nodes"
    maint_note = ([f"Planned maintenance excluded: {na['maint_excluded_min']:,.0f} "
                   "device-minutes fell inside maintenance windows and do not "
                   "count against availability."]
                  if na.get("maint_excluded_min") else [])
    return _section(
        "availability_kpis", "Availability Summary",
        notes=maint_note,
        description=f"Computed over {scope} for the reporting window.",
        kpis=[
            {"label": "Availability", "value": _fmt_pct(avail),
             "accent": ("neutral" if avail is None else
                        "success" if avail >= target else "danger")},
            {"label": f"Nodes up (of {len(na['nodes'])})", "value": str(na["up"]),
             "accent": "success" if na["up"] == len(na["nodes"]) else "warning"},
            {"label": f"Below configured target ({target:g}%)", "value": str(na["below_sla"]),
             "accent": "danger" if na["below_sla"] else "success"},
            {"label": "Total downtime", "value": f"{na['downtime_min']:,.0f} min",
             "accent": "warning" if na["downtime_min"] else "success"},
            {"label": "Outage events", "value": str(na["outage_events"]), "accent": "info"},
        ],
    )


async def _sec_availability_trend(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    trend = na["trend"]
    scope = "selected nodes" if na["filtered"] else "all monitored nodes"
    return _section(
        "availability_trend", "Availability Trend",
        description=f"Ping availability across {scope} over the reporting window.",
        charts=([{"title": "",
                  "series": {"kind": "area", "unit": "%", "color": "success",
                             "y_domain": [80, 100], "points": trend}}] if trend else []),
        notes=[] if trend else ["No ping samples in this window (not measured)."],
    )


async def _sec_device_availability(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    top = _top_n(ctx, 200)
    nodes = na["nodes"]
    rows = [
        [n["hostname"], n["ip"], n["location"] or "—", n["status"],
         _fmt_pct(n["avail"], 2) if n["avail"] is not None else "—",
         f"{n['downtime_min']:,.0f} min" if n["downtime_min"] is not None else "—",
         str(n["outages"]),
         _fmt_ms(n["rtt"]) if n["rtt"] else "—"]
        for n in nodes[:top]
    ]
    scope = "the selected nodes" if na["filtered"] else "every monitored node"
    return _section(
        "device_availability", "Node Availability",
        description=f"Availability, downtime and outages for {scope} — worst first.",
        tables=[{"headers": ["Node", "IP address", "Location", "Status",
                             "Availability", "Downtime", "Outages", "Avg RTT"],
                 "styles": ["text", "mono", "text", "status", "pct-bar", "num", "num", "num"],
                 "rows": rows}],
        notes=_shown_note(len(rows), len(nodes), "nodes (worst first)"),
    )


async def _sec_device_uptime(ctx: SectionCtx) -> dict:
    # The same computation as Node Availability (one ping table per window,
    # sample-weighted), so the two sections of one report never disagree.
    na = await _node_availability(ctx)
    measured = sorted((n for n in na["nodes"] if n["avail"] is not None), key=lambda n: n["avail"])
    top = _top_n(ctx, 10)
    rows = [
        [n["hostname"] or "—", n["ip"] or "—", _fmt_pct(n["avail"]), str(n["outages"]),
         _fmt_ms(n["rtt"]), _fmt_ms(n["p95_rtt"])]
        for n in measured[:top]
    ]
    return _section(
        "device_uptime", "Lowest-Availability Devices",
        description="Devices with the poorest availability in the window — start remediation here.",
        tables=[{"headers": ["Device", "IP", "Availability", "Outages", "Avg RTT", "P95 RTT"],
                 "styles": ["text", "mono", "pct-bar", "num", "num", "num"], "rows": rows}],
        notes=[] if measured else ["No ping samples in this window (not measured)."],
    )


async def _sec_top_outages(ctx: SectionCtx) -> dict:
    na = await _node_availability(ctx)
    outages = sorted(na.get("outage_list") or [],
                     key=lambda o: -(o.get("duration_s") or o.get("in_window_s") or 0))
    top = _top_n(ctx, 15)

    def _duration(o: dict) -> str:
        secs = o.get("duration_s")
        text_ = "unknown" if secs is None else f"{secs / 60:,.0f} min"
        return f"{text_} (ongoing)" if o.get("ongoing") else text_

    rows = [[o.get("hostname") or "—", ctx.fmt_short(o.get("started_at")), _duration(o)]
            for o in outages[:top]]
    return _section(
        "top_outages", "Longest Outages", half=True,
        tables=[{"headers": ["Device", "Started", "Duration"],
                 "styles": ["text", "num", "num"], "rows": rows}],
        notes=_shown_note(len(rows), len(outages), "outage episodes"),
    )


async def _sec_service_availability(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("business")
    services = d.get("service_availability") or []
    top = _top_n(ctx, 25)
    rows = [
        [s.get("name") or "—", (s.get("type") or "—").upper(), s.get("status") or "—",
         _fmt_pct(s.get("availability_pct")),
         f"{s.get('checks_failed') or 0}/{s.get('checks_total') or 0}"]
        for s in services[:top]
    ]
    tls = [[w.get("name") or "—", str(w.get("days_remaining")), w.get("severity") or ""]
           for w in (d.get("tls_warnings") or [])]
    tables = [{"title": "Service check availability",
               "headers": ["Service", "Type", "Status", "Availability", "Failed checks"],
               "styles": ["text", "text", "status", "pct-bar", "num"], "rows": rows}]
    if tls:
        tables.append({"title": "TLS certificates expiring within 30 days",
                       "headers": ["Service", "Days remaining", "Severity"],
                       "styles": ["text", "num", "severity"], "rows": tls})
    return _section("service_availability", "Service Availability", tables=tables,
                    notes=_shown_note(len(rows), len(services), "service checks"))


# ─── Performance sections ───────────────────────────────────────────────────

async def _sec_network_performance(ctx: SectionCtx) -> dict:
    # Latency and loss from the ping rollups (the same cached computation as
    # the availability sections), ranked by p95 round-trip time.
    na = await _node_availability(ctx)
    measured = [n for n in na["nodes"] if n.get("rtt") is not None or n.get("p95_rtt") is not None]

    def _rank(n: dict) -> float:
        v = n.get("p95_rtt") if n.get("p95_rtt") is not None else n.get("rtt")
        return -(v or 0.0)

    ranked = sorted(measured, key=_rank)
    top = _top_n(ctx, 10)
    p95s = [n["p95_rtt"] for n in measured if n.get("p95_rtt") is not None]
    lossy = sum(1 for n in measured if (n.get("loss_pct") or 0) >= 1)
    kpis = [
        {"label": "Devices measured", "value": str(len(measured)), "accent": "primary"},
        {"label": "Worst p95 latency", "value": _fmt_ms(max(p95s)) if p95s else "—", "accent": "warning"},
        {"label": "Devices with ≥1% loss", "value": str(lossy) if measured else "—",
         "accent": "danger" if lossy else ("success" if measured else "neutral")},
    ]
    rows = [[n["hostname"] or "—", _fmt_ms(n.get("rtt")), _fmt_ms(n.get("p95_rtt")),
             _fmt_pct(n.get("loss_pct"), 2), _fmt_pct(n.get("avail"))]
            for n in ranked[:top]]
    notes = [] if measured else ["No latency samples in this window (not measured)."]
    return _section(
        "network_performance", "Network Device Performance",
        description=("Devices with the highest round-trip latency (95th percentile) in the window, "
                     "with packet loss while they answered; outages count toward availability."),
        kpis=kpis,
        tables=[{"headers": ["Device", "Avg RTT", "P95 RTT", "Packet loss", "Availability"],
                 "styles": ["text", "num", "num", "num", "pct-bar"], "rows": rows}],
        notes=notes + (_ping_method_note(na) if measured else []),
    )


IF_RAW_RETENTION_DAYS = 30       # zenplus.snmp_if_metrics default TTL
IF_ROLLUP_RETENTION_DAYS = 90    # zenplus.snmp_if_metrics_5m default TTL


async def _interface_stats(ctx: SectionCtx) -> dict:
    """Per-interface throughput for the window (cached per render).

    Raw ``snmp_if_metrics`` while the whole window is within its retention
    (30 days by default), else the ``snmp_if_metrics_5m`` rollup (sample-weighted
    averages; percentiles over 5-minute averages). Utilisation is against the
    effective interface speed (``interface_speed.effective_speed``: configured
    circuit speed, else SNMP ifSpeed); an unknown speed is never guessed.
    Interfaces of deleted devices are left out."""
    if "if_stats" in ctx._cache:
        return ctx._cache["if_stats"]
    from app.services.interface_speed import effective_speed

    raw_days = await _retention_days(ctx, "snmp_if_metrics", IF_RAW_RETENTION_DAYS)
    raw = _aware_utc(ctx.frm) >= datetime.now(timezone.utc) - timedelta(days=raw_days)
    clause, params = ctx.ch_device_clause("device_id")
    params.update({"f": _naive_utc(ctx.frm), "t": _naive_utc(ctx.to)})
    if raw:
        sql = f"""
            SELECT toString(device_id) AS did, if_index,
                   avg(in_bps), avg(out_bps),
                   quantile(0.95)(in_bps), quantile(0.95)(out_bps),
                   max(in_bps), max(out_bps), count()
            FROM zenplus.snmp_if_metrics
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s{clause}
            GROUP BY device_id, if_index
        """
    else:
        sql = f"""
            SELECT did, if_index,
                   sum(i * n) / sum(n), sum(o * n) / sum(n),
                   quantile(0.95)(i), quantile(0.95)(o),
                   max(mi), max(mo), sum(n)
            FROM (
                SELECT toString(device_id) AS did, if_index, timestamp,
                       sum(avg_in_bps * sample_count) / sum(sample_count) AS i,
                       sum(avg_out_bps * sample_count) / sum(sample_count) AS o,
                       max(max_in_bps) AS mi, max(max_out_bps) AS mo,
                       sum(sample_count) AS n
                FROM zenplus.snmp_if_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{clause}
                GROUP BY device_id, if_index, timestamp
                HAVING n > 0
            )
            GROUP BY did, if_index
        """
    out: dict[str, Any] = {"rows": [], "raw": raw, "raw_days": raw_days, "speed_unknown": 0,
                           "rollup_note": None if raw else ctx.window_note(
                               await _retention_days(ctx, "snmp_if_metrics_5m", IF_ROLLUP_RETENTION_DAYS),
                               "Interface 5-minute history")}
    try:
        stats = await ctx.ch_rows(sql, params)
        dids = sorted({str(r[0]) for r in stats})
        meta: dict[tuple[str, int], tuple] = {}
        hosts: dict[str, str] = {}
        if dids:
            res = await ctx.db.execute(text(
                "SELECT d.id::text, d.hostname, di.if_index, di.if_name, di.if_speed, "
                "di.configured_speed_bps "
                "FROM devices d LEFT JOIN device_interfaces di ON di.device_id = d.id "
                "WHERE d.id::text = ANY(:dids)"), {"dids": dids})
            for r in res.fetchall():
                hosts[str(r[0])] = r[1] or str(r[0])[:8]
                if r[2] is not None:
                    meta[(str(r[0]), int(r[2]))] = (r[3], r[4], r[5])
        for did, idx, ia, oa, ip95, op95, imax, omax, n in stats:
            did, idx = str(did), int(idx)
            if did not in hosts:
                continue
            if_name, if_speed, configured = meta.get((did, idx), (None, None, None))
            speed = effective_speed(configured, None, if_speed)
            ia, oa = _finite(ia) or 0.0, _finite(oa) or 0.0
            ip95, op95 = _finite(ip95) or 0.0, _finite(op95) or 0.0
            out["rows"].append({
                "device_id": did, "if_index": idx,
                "label": f"{hosts[did]} · {if_name or f'#{idx}'}",
                "in_avg": ia, "out_avg": oa, "in_p95": ip95, "out_p95": op95,
                "in_max": _finite(imax) or 0.0, "out_max": _finite(omax) or 0.0,
                "samples": float(n or 0), "speed": speed,
                "util_avg": (max(ia, oa) / speed * 100) if speed else None,
                "util_p95": (max(ip95, op95) / speed * 100) if speed else None,
            })
            if not speed:
                out["speed_unknown"] += 1
    except Exception:
        logger.debug("interface statistics unavailable", exc_info=True)
        out["error"] = True
    ctx._cache["if_stats"] = out
    return out


def _if_method_note(st: dict) -> list[str]:
    if st.get("raw"):
        return []
    notes = [f"Interface samples are kept for {st.get('raw_days') or IF_RAW_RETENTION_DAYS} days; this "
             "period is read from 5-minute averages (percentiles are taken across them)."]
    if st.get("rollup_note"):
        notes.append(st["rollup_note"])
    return notes


async def _sec_interface_utilization(ctx: SectionCtx) -> dict:
    st = await _interface_stats(ctx)
    busiest = sorted(st["rows"], key=lambda r: -(r["in_avg"] + r["out_avg"]))
    top = _top_n(ctx, 10)
    rows = [
        [r["label"], _fmt_bps(r["in_avg"]), _fmt_bps(r["out_avg"]),
         _fmt_pct(r["util_avg"], 1) if r["util_avg"] is not None else "—"]
        for r in busiest[:top]
    ]
    unknown = sum(1 for r in busiest[:top] if r["util_avg"] is None)
    notes = _if_method_note(st)
    if unknown:
        notes.append(f"{unknown} of the listed interfaces have no known speed; their utilization "
                     "is not shown (set the circuit speed on the interface to assess it).")
    return _section(
        "interface_utilization", "Busiest Interfaces",
        description=("Top interfaces by average throughput over the window; utilization is the busier "
                     "direction against the effective interface speed."),
        tables=[{"headers": ["Interface", "Avg In", "Avg Out", "Utilization"],
                 "styles": ["text", "num", "num", "pct-bar"], "rows": rows}],
        notes=notes,
    )


async def _sec_server_performance(ctx: SectionCtx) -> dict:
    """Agent servers ranked by sample-weighted average CPU (5-minute rollups);
    disabled servers are left out and a server without samples is "not
    measured", never 0 %."""
    rows_out: list[list[str]] = []
    unmeasured = 0
    total_ranked = 0
    try:
        servers = {r[0]: r[1] for r in (await ctx.db.execute(text(
            "SELECT id::text, COALESCE(display_name, hostname) FROM servers "
            "WHERE COALESCE(status, '') <> 'disabled'"))).fetchall()}
        if servers:
            params = {"f": _naive_utc(ctx.frm), "t": _naive_utc(ctx.to), "ids": list(servers)}
            cpu = {str(r[0]): r for r in await ctx.ch_rows(
                """
                SELECT toString(server_id) AS sid,
                       sum(avg_total_pct * sample_count) / nullIf(sum(sample_count), 0) AS avg_cpu,
                       max(max_total_pct) AS max_cpu, sum(sample_count) AS n
                FROM zenplus.host_cpu_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND server_id IN %(ids)s
                GROUP BY server_id
                """, params)}
            mem = {str(r[0]): r for r in await ctx.ch_rows(
                """
                SELECT toString(server_id) AS sid,
                       sum(avg_used_pct * sample_count) / nullIf(sum(sample_count), 0) AS avg_mem,
                       max(max_used_pct) AS max_mem, sum(sample_count) AS n
                FROM zenplus.host_memory_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND server_id IN %(ids)s
                GROUP BY server_id
                """, params)}
            ranked = []
            for sid, name in servers.items():
                c, m = cpu.get(sid), mem.get(sid)
                c_avg = _finite(c[1]) if c and int(c[3] or 0) > 0 else None
                c_max = _finite(c[2]) if c and int(c[3] or 0) > 0 else None
                m_avg = _finite(m[1]) if m and int(m[3] or 0) > 0 else None
                m_max = _finite(m[2]) if m and int(m[3] or 0) > 0 else None
                if c_avg is None and m_avg is None:
                    unmeasured += 1
                    continue
                ranked.append((name or sid[:8], c_avg, c_max, m_avg, m_max))
            ranked.sort(key=lambda r: (r[1] is None, -(r[1] or 0.0), -(r[3] or 0.0)))
            total_ranked = len(ranked)
            for name, c_avg, c_max, m_avg, m_max in ranked[:_top_n(ctx, 15)]:
                rows_out.append([name, _fmt_pct(c_avg, 1), _fmt_pct(c_max, 1),
                                 _fmt_pct(m_avg, 1), _fmt_pct(m_max, 1)])
    except Exception:
        logger.debug("server performance section: host metrics unavailable", exc_info=True)
    notes: list[str] = []
    if not rows_out:
        notes.append("No agent-monitored servers reported in this window.")
    notes += _shown_note(len(rows_out), total_ranked, "servers")
    if unmeasured and rows_out:
        notes.append(f"{unmeasured} enabled server(s) reported no CPU or memory samples in this window "
                     "(not measured).")
    retention = ctx.window_note(90, "Server 5-minute metrics")
    if retention:
        notes.append(retention)
    return _section(
        "server_performance", "Server Performance",
        description=("Agent-monitored servers ranked by average CPU load (sample-weighted 5-minute "
                     "averages; disabled servers excluded)."),
        tables=[{"headers": ["Server", "Avg CPU", "Peak CPU", "Avg Mem", "Peak Mem"],
                 "styles": ["text", "pct-bar", "num", "pct-bar", "num"], "rows": rows_out}],
        notes=notes,
    )

# ─── Traffic (NetFlow) sections ─────────────────────────────────────────────
# Implemented in netflow_report on the NetFlow foundation: requester
# visibility (NF-04), the report's FlowFilter scope, half-open hybrid windows,
# whole-scope denominators with remainders (NF-25) and retention disclosure.

async def _sec_traffic_kpis(ctx: SectionCtx) -> dict:
    from app.services import netflow_report
    return await netflow_report.sec_traffic_kpis(ctx)


async def _sec_traffic_trend(ctx: SectionCtx) -> dict:
    from app.services import netflow_report
    return await netflow_report.sec_traffic_trend(ctx)


async def _sec_traffic_protocols(ctx: SectionCtx) -> dict:
    from app.services import netflow_report
    return await netflow_report.sec_traffic_protocols(ctx)


async def _sec_traffic_ports(ctx: SectionCtx) -> dict:
    from app.services import netflow_report
    return await netflow_report.sec_traffic_ports(ctx)


async def _sec_traffic_talkers(ctx: SectionCtx) -> dict:
    from app.services import netflow_report
    return await netflow_report.sec_traffic_talkers(ctx)


async def _sec_netflow_executive(ctx: SectionCtx) -> dict:
    from app.services import netflow_report
    return await netflow_report.sec_executive_summary(ctx)


# ─── APM sections ───────────────────────────────────────────────────────────

def _apm_health(err_rate: float, p95_ms: float, reqs: int) -> str:
    """Same thresholds as the APM Services page (apm_services._health)."""
    if reqs <= 0:
        return "no data"
    if err_rate >= 0.05 or p95_ms >= 1000:
        return "critical"
    if err_rate >= 0.01 or p95_ms >= 500:
        return "degraded"
    return "healthy"


async def _sec_apm_services(ctx: SectionCtx) -> dict:
    """RED metrics per service from the RED rollup that serves this window
    (apm_rollup.table_for: 5-minute up to 6 h, hourly beyond -- the hourly
    rollup keeps 395 days), with the window start snapped down to a bucket
    boundary (apm_rollup.align_ms_window) so no bucket is silently dropped."""
    from app.services.apm_rollup import align_ms_window, table_for

    rows_out: list[list[str]] = []
    healthy = degraded = 0
    total = 0
    try:
        frm_ms = int(_aware_utc(ctx.frm).timestamp() * 1000)
        to_ms = int(_aware_utc(ctx.to).timestamp() * 1000)
        a_from, a_to, _covered = align_ms_window(frm_ms, to_ms)
        # The table must match the bucket the start was aligned to (both follow
        # the requested span), or an hourly bucket straddling the start drops out.
        table = table_for(int(max((to_ms - frm_ms) / 1000.0, 1.0)))
        params = {"f": datetime.fromtimestamp(a_from / 1000, tz=timezone.utc).replace(tzinfo=None),
                  "t": datetime.fromtimestamp(a_to / 1000, tz=timezone.utc).replace(tzinfo=None)}
        rows = await ctx.ch_rows(
            f"""
            SELECT service_name,
                   sum(request_count) AS reqs,
                   sum(error_count)   AS errs,
                   arrayElement(quantilesTDigestMerge(0.5,0.95)(duration_state),1) AS p50,
                   arrayElement(quantilesTDigestMerge(0.5,0.95)(duration_state),2) AS p95,
                   sum(satisfied_count) AS sat, sum(tolerating_count) AS tol
            FROM zenplus.{table}
            WHERE timestamp >= %(f)s AND timestamp < %(t)s AND span_kind IN {ENTRY_KINDS}
            GROUP BY service_name ORDER BY reqs DESC
            """, params)
        total = len(rows)
        top = _top_n(ctx, 25)
        for i, r in enumerate(rows):
            reqs, errs = int(r[1] or 0), int(r[2] or 0)
            err_rate = errs / reqs if reqs else 0
            p50, p95 = _finite(r[3]), _finite(r[4])
            apdex = (int(r[5] or 0) + int(r[6] or 0) / 2) / reqs if reqs else None
            state = _apm_health(err_rate, p95 or 0.0, reqs)
            healthy += state == "healthy"
            degraded += state in ("degraded", "critical")
            if i < top:
                rows_out.append([r[0], f"{reqs:,}", _fmt_pct(err_rate * 100) if reqs else "—",
                                 _fmt_ms(p50) if reqs else "—", _fmt_ms(p95) if reqs else "—",
                                 f"{apdex:.2f}" if apdex is not None else "—", state])
    except Exception:
        logger.debug("apm services section unavailable", exc_info=True)
    return _section(
        "apm_services", "Application Service Health",
        description="Golden signals per instrumented service (entry spans).",
        kpis=[
            {"label": "Services reporting", "value": str(healthy + degraded), "accent": "primary"},
            {"label": "Healthy", "value": str(healthy), "accent": "success"},
            {"label": "Degraded / critical", "value": str(degraded),
             "accent": "danger" if degraded else "success"},
        ],
        tables=[{"headers": ["Service", "Requests", "Error rate", "P50", "P95", "Apdex", "Health"],
                 "styles": ["text", "num", "num", "num", "num", "num", "status"],
                 "rows": rows_out}],
        notes=_shown_note(len(rows_out), total, "services"),
    )


async def _apm_ignored_groups(ctx: SectionCtx) -> set[str]:
    """Exception groups triaged as Ignored in APM → Errors. A group seen in
    several services has one triage row per service; the most recently
    touched row wins, exactly like the Errors inbox (apm_errors._triage_map)."""
    try:
        rows = (await ctx.db.execute(text(
            "SELECT group_id FROM ("
            " SELECT DISTINCT ON (group_id) group_id, status FROM apm_error_issues"
            " ORDER BY group_id, updated_at DESC NULLS LAST, last_seen_at DESC NULLS LAST"
            ") latest WHERE status = 'ignored'"))).fetchall()
    except Exception:
        logger.debug("apm error triage unavailable", exc_info=True)
        return set()
    return {str(r[0]) for r in rows}


def _group_key(v: Any) -> str:
    """ClickHouse FixedString group ids arrive as bytes."""
    return v.decode(errors="replace") if isinstance(v, (bytes, bytearray)) else str(v)


async def _sec_apm_errors(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    top = _top_n(ctx, 12)
    ignored = await _apm_ignored_groups(ctx)
    excluded = 0
    try:
        # Ask for enough groups that dropping the ignored ones still fills the table.
        rows = await ctx.ch_rows(
            f"""
            SELECT group_id, anyHeavy(exception_type), anyHeavy(exception_message),
                   anyHeavy(service_name), count() AS occurrences, max(timestamp)
            FROM zenplus.apm_exceptions
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
            GROUP BY group_id ORDER BY occurrences DESC LIMIT {top + len(ignored)}
            """, {"f": _naive_utc(ctx.frm), "t": _naive_utc(ctx.to)})
        for r in rows:
            if _group_key(r[0]) in ignored:
                excluded += 1
                continue
            if len(rows_out) >= top:
                break
            rows_out.append([str(r[1] or "Error"), (str(r[2] or ""))[:60], str(r[3] or "—"),
                             f"{int(r[4]):,}", ctx.fmt_short(r[5])])
    except Exception:
        logger.debug("apm errors section unavailable", exc_info=True)
    notes = ([f"{excluded} exception group(s) marked Ignored in APM → Errors are not listed."]
             if excluded else [])
    return _section("apm_errors", "Top Application Errors",
                    tables=[{"headers": ["Type", "Message", "Service", "Count", "Last seen"],
                             "rows": rows_out}],
                    notes=notes)


def _slo_budget(slo: dict) -> tuple[Optional[float], int]:
    """(error budget consumed as a fraction, requests) over the SLO's own
    trailing window; consumed is None when the window has no data. Blocking
    ClickHouse -- run via asyncio.to_thread."""
    from app.services.apm_slo_service import _bad_fraction

    budget = max(1.0 - float(slo["target"]) / 100.0, 1e-9)
    r = _bad_fraction(slo["service_name"], slo.get("env"), slo.get("operation"), slo["sli_type"],
                      slo.get("latency_threshold_ms"), int(slo["window_days"]) * 86_400)
    if not r:
        return None, 0
    return r[0] / budget, int(r[1] or 0)


async def _sec_slo_attainment(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        from app.services.apm_slo_service import _load_slos
        slos = await _load_slos(ctx.db)
        for slo in slos:
            objective = f"{slo['target']}% / {slo['window_days']}d"
            if slo.get("sli_type") == "custom":
                rows_out.append([slo["name"], slo["service_name"], objective, "—", "Not evaluated"])
                continue
            try:
                consumed, _requests = await asyncio.to_thread(_slo_budget, slo)
            except Exception:
                logger.debug("slo budget unavailable for %s", slo.get("name"), exc_info=True)
                consumed = None
            if consumed is None:
                # No requests in the SLO window: nothing was measured, so the
                # budget is unknown -- never "100% remaining / on track".
                rows_out.append([slo["name"], slo["service_name"], objective, "—", "No data"])
                continue
            remaining = max(0.0, 1.0 - consumed) * 100
            rows_out.append([
                slo["name"], slo["service_name"], objective, f"{remaining:.0f}%",
                "BREACHING" if consumed >= 1 else "AT RISK" if remaining < 25 else "ON TRACK",
            ])
    except Exception:
        logger.debug("slo section unavailable", exc_info=True)
    notes = (["Error budgets are measured over each SLO's own trailing window ending when the report "
              "was generated, not over the report period."] if rows_out else ["No SLOs defined."])
    return _section(
        "slo_attainment", "SLO Attainment",
        tables=[{"headers": ["SLO", "Service", "Objective", "Budget remaining", "Status"],
                 "styles": ["text", "text", "text", "pct-bar", "status"], "rows": rows_out}],
        notes=notes,
    )


async def _sec_synthetics(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    try:
        mons = (await ctx.db.execute(text(
            "SELECT id::text, name, status FROM apm_synthetic_monitors ORDER BY name"))).fetchall()
        if mons:
            import uuid as _uuid
            stats = await ctx.ch_rows(
                """
                SELECT monitor_id, count(), countIf(success = 1), avg(total_ms)
                FROM zenplus.apm_synthetic_results
                WHERE monitor_id IN %(ids)s AND timestamp >= %(f)s AND timestamp <= %(t)s
                GROUP BY monitor_id
                """,
                {"ids": [_uuid.UUID(m[0]) for m in mons], "f": ctx.frm, "t": ctx.to},
            )
            smap = {str(r[0]): r for r in stats}
            for m in mons:
                s = smap.get(m[0])
                uptime = f"{int(s[2]) / int(s[1]) * 100:.2f}%" if s and s[1] else "—"
                rows_out.append([m[1], (m[2] or "unknown").upper(), uptime,
                                 f"{int(s[1]):,}" if s else "0",
                                 _fmt_ms(float(s[3])) if s else "—"])
    except Exception:
        logger.debug("synthetics section unavailable", exc_info=True)
    return _section(
        "synthetics", "Synthetic Scenario Uptime",
        description="Scripted user-journey checks run from the appliance.",
        tables=[{"headers": ["Scenario", "Status", "Uptime", "Runs", "Avg duration"],
                 "styles": ["text", "status", "pct-bar", "num", "num"], "rows": rows_out}],
        notes=[] if rows_out else ["No synthetic scenarios configured."],
    )


# ─── Usage sections ─────────────────────────────────────────────────────────

USER_EXPR = ("if(attributes_string['enduser.id'] != '', "
             "attributes_string['enduser.id'], attributes_string['user.id'])")

# zenplus.apm_spans keeps request-level spans for 14 days by default
# (migrate-098; Storage settings may change it). The usage sections read them,
# so every one of them is clamped to the retained part and says so.
SPAN_RETENTION_DAYS = 14


async def _span_window(ctx: SectionCtx) -> tuple[Optional[datetime], Optional[datetime], list[str]]:
    """(from, to, notes) clamped to raw span retention (naive UTC); from is
    None when nothing of the period is retained."""
    days = await _retention_days(ctx, "apm_spans", SPAN_RETENTION_DAYS)
    reach = datetime.now(timezone.utc) - timedelta(days=days)
    frm, to = _aware_utc(ctx.frm), _aware_utc(ctx.to)
    if to <= reach:
        return None, None, [f"Request-level span data is kept for {days} days; none of this period "
                            "is retained."]
    if frm < reach:
        return (_naive_utc(reach), _naive_utc(to),
                [f"Request-level span data is kept for {days} days; this section covers "
                 f"{ctx.fmt_dt(reach)} onwards."])
    return _naive_utc(frm), _naive_utc(to), []


async def _sec_usage_kpis(ctx: SectionCtx) -> dict:
    reqs = users = pages = errors = 0
    p95: Optional[float] = None
    points: list[dict] = []
    frm, to, notes = await _span_window(ctx)
    try:
        if frm is not None:
            t = (await ctx.ch_rows(
                f"""
                SELECT count(), uniqIf({USER_EXPR}, {USER_EXPR} != ''),
                       uniqIf(http_route, http_route != ''), countIf(has_error = 1),
                       quantile(0.95)(duration_nano) / 1e6
                FROM zenplus.apm_spans
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND span_kind_str IN {ENTRY_KINDS}
                """, {"f": frm, "t": to}))[0]
            reqs, users, pages, errors = int(t[0]), int(t[1]), int(t[2]), int(t[3])
            p95 = _finite(t[4]) if reqs else None
            bucket_sql, bucket_params = ctx.ch_bucket("timestamp")
            series = await ctx.ch_rows(
                f"""
                SELECT {bucket_sql} AS b, count()
                FROM zenplus.apm_spans
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND span_kind_str IN {ENTRY_KINDS}
                GROUP BY b ORDER BY b
                """, {"f": frm, "t": to, **bucket_params})
            points = [{"t": ctx.iso(r[0]), "v": float(r[1])} for r in series]
    except Exception:
        logger.debug("usage kpis unavailable", exc_info=True)
    err_rate = errors / reqs * 100 if reqs else None
    return _section(
        "usage_kpis", "Usage Summary",
        description="Application traffic and audience from request-level spans.",
        kpis=[
            {"label": "Requests", "value": f"{reqs:,}", "accent": "primary"},
            {"label": "Unique users", "value": f"{users:,}", "accent": "info"},
            {"label": "Pages", "value": f"{pages:,}", "accent": "primary"},
            {"label": "Error rate", "value": _fmt_pct(err_rate),
             "accent": "neutral" if err_rate is None else "danger" if err_rate > 2 else "success"},
            {"label": "P95 latency", "value": _fmt_ms(p95), "accent": "warning"},
        ],
        charts=([{"title": "Request volume",
                  "series": {"kind": "area", "unit": "requests", "color": "primary",
                             "points": points}}] if points else []),
        notes=notes,
    )


async def _sec_usage_pages(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    frm, to, notes = await _span_window(ctx)
    try:
        if frm is not None:
            rows = await ctx.ch_rows(
                f"""
                SELECT http_route, anyHeavy(service_name), count() AS hits,
                       uniqIf({USER_EXPR}, {USER_EXPR} != ''), countIf(has_error = 1),
                       quantile(0.95)(duration_nano) / 1e6
                FROM zenplus.apm_spans
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s
                  AND span_kind_str IN {ENTRY_KINDS} AND http_route != ''
                GROUP BY http_route ORDER BY hits DESC LIMIT {_top_n(ctx, 15)}
                """, {"f": frm, "t": to})
            for r in rows:
                hits = int(r[2])
                rows_out.append([r[0], str(r[1]), f"{hits:,}", f"{int(r[3]):,}",
                                 _fmt_pct(int(r[4]) / hits * 100 if hits else None), _fmt_ms(_finite(r[5]))])
    except Exception:
        logger.debug("usage pages unavailable", exc_info=True)
    return _section("usage_pages", "Top Pages",
                    tables=[{"headers": ["Route", "Service", "Hits", "Users", "Error rate", "P95"],
                             "styles": ["mono", "text", "num", "num", "num", "num"],
                             "rows": rows_out}],
                    notes=notes)


async def _sec_usage_users(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    frm, to, notes = await _span_window(ctx)
    try:
        if frm is not None:
            rows = await ctx.ch_rows(
                f"""
                SELECT {USER_EXPR} AS uid, count() AS reqs, countIf(has_error = 1),
                       uniqIf(http_route, http_route != ''), max(timestamp)
                FROM zenplus.apm_spans
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s
                  AND span_kind_str IN {ENTRY_KINDS} AND {USER_EXPR} != ''
                GROUP BY uid ORDER BY reqs DESC LIMIT {_top_n(ctx, 15)}
                """, {"f": frm, "t": to})
            for r in rows:
                rows_out.append([r[0], f"{int(r[1]):,}", f"{int(r[2]):,}", f"{int(r[3]):,}",
                                 ctx.fmt_short(r[4])])
    except Exception:
        logger.debug("usage users unavailable", exc_info=True)
    if not rows_out and frm is not None:
        notes = notes + ["No user attribution — set the enduser.id span attribute to unlock "
                         "per-user analytics."]
    return _section(
        "usage_users", "Top Users",
        tables=[{"headers": ["User", "Requests", "Errors", "Pages", "Last seen"],
                 "styles": ["mono", "num", "num", "num", "num"], "rows": rows_out}],
        notes=notes,
    )

# ─── Browser RUM sections ───────────────────────────────────────────────────
#
# These read zenplus.apm_rum_events — what real browsers reported — rather than
# server spans, so they answer the question a business owner actually asks:
# how many people used the product, what did they use, and was it fast and
# reliable for them. Google's Core Web Vitals thresholds are used verbatim so
# the verdicts match what Search Console and PageSpeed report.
#
# Counting follows the RUM pages (app/api/v1/rum.py) exactly: a page view is
# counted once (its view_start record; legacy SDKs send one record per view),
# sessions and views are keyed by application + environment, Web Vitals take
# one finalized, sampled sample per view, and a period older than raw-event
# retention is read from the 5-minute rollup (apm_rum_metrics_5m), which keeps
# no individual users or error details.

# metric -> (good_max, poor_min, unit) using the p75 the Web Vitals spec scores on.
_VITAL_THRESHOLDS = {
    "LCP": (2500.0, 4000.0, "ms"),
    "INP": (200.0, 500.0, "ms"),
    "CLS": (0.10, 0.25, ""),
    "FCP": (1800.0, 3000.0, "ms"),
    "TTFB": (800.0, 1800.0, "ms"),
}
_VITAL_ORDER = ("LCP", "INP", "CLS", "FCP", "TTFB")

RUM_VIEW_ONCE = ("sampled = 1 AND event_type = 'view' "
                 "AND (sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start'))")
RUM_VIEWS_SQL = f"uniqExactIf((application_id, env, view_id), {RUM_VIEW_ONCE})"
RUM_SESSIONS_SQL = "uniqExactIf((application_id, env, session_id), sampled = 1)"
RUM_RAW_RETENTION_DAYS = 14      # defaults; the RUM settings page may change both
RUM_ROLLUP_RETENTION_DAYS = 90


def _vital_verdict(metric: str, value: Optional[float]) -> tuple[str, str]:
    """(verdict, accent) for a Core Web Vital p75."""
    if value is None:
        return "No data", "info"
    good, poor, _ = _VITAL_THRESHOLDS[metric]
    if value <= good:
        return "Good", "success"
    if value <= poor:
        return "Needs work", "warning"
    return "Poor", "danger"


def _fmt_vital(metric: str, value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}" if metric == "CLS" else _fmt_ms(value)


def _rum_raw_days() -> int:
    """Raw RUM event retention as the RUM API last read it (default 14 days)."""
    try:
        from app.api.v1.rum import _RETENTION_CACHE
        return max(1, int(_RETENTION_CACHE.get("raw", RUM_RAW_RETENTION_DAYS)))
    except Exception:
        return RUM_RAW_RETENTION_DAYS


async def _rum_source(ctx: SectionCtx) -> dict:
    """Which RUM table answers this window: raw events while the whole period
    is within raw retention (the table's TTL, which the RUM settings page may
    change), else the 5-minute rollup (cached per render)."""
    if "rum_source" not in ctx._cache:
        days = await _retention_days(ctx, "apm_rum_events", _rum_raw_days())
        reach = datetime.now(timezone.utc) - timedelta(days=days)
        rollup = _aware_utc(ctx.frm) < reach
        note = None
        if rollup:
            note = (f"Browser events are kept for {days} days; this period is read from 5-minute "
                    "aggregates, which do not keep individual users or error details.")
            older = ctx.window_note(await _retention_days(ctx, "apm_rum_metrics_5m", RUM_ROLLUP_RETENTION_DAYS),
                                    "Browser 5-minute aggregates")
            if older:
                note += " " + older
        ctx._cache["rum_source"] = {"rollup": rollup, "raw_days": days, "reach": reach, "note": note}
    return ctx._cache["rum_source"]


def _rum_scope(ctx: SectionCtx, extra: str = "") -> str:
    return f"timestamp >= %(f)s AND timestamp <= %(t)s{extra}{ctx.rum_app_clause()}"


def _per_view_sql(ctx: SectionCtx, extra: str = "", group_by: str = "application_id, env, view_id") -> str:
    """One finalized, sampled Web Vitals sample per page view (rum.py's
    per-view collapse), over this section's raw window."""
    from app.api.v1.rum import _per_view_vitals_sql
    return _per_view_vitals_sql(_rum_scope(ctx, extra), group_by)


def _p75(value: Any, samples: Any) -> Optional[float]:
    return _finite(value) if int(samples or 0) > 0 else None


async def _sec_rum_audience(ctx: SectionCtx) -> dict:
    sessions = views = actions = 0
    users: Optional[int] = 0
    per_session = 0.0
    points: list[dict] = []
    src = await _rum_source(ctx)
    try:
        app = ctx.rum_app_clause()
        bucket_sql, bucket_params = ctx.ch_bucket("timestamp")
        if src["rollup"]:
            t = (await ctx.ch_rows(
                f"""
                SELECT uniqCombined64Merge(sessions), uniqCombined64Merge(views), sum(actions)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                """, ctx.rum_params()))[0]
            sessions, views, actions = int(t[0] or 0), int(t[1] or 0), int(t[2] or 0)
            users = None
            rows = await ctx.ch_rows(
                f"""
                SELECT {bucket_sql} AS b, uniqCombined64Merge(sessions)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                GROUP BY b ORDER BY b
                """, ctx.rum_params(**bucket_params))
        else:
            t = (await ctx.ch_rows(
                f"""
                SELECT {RUM_SESSIONS_SQL},
                       uniqIf(user_id, user_id != ''),
                       {RUM_VIEWS_SQL},
                       countIf(event_type = 'action' AND sampled = 1)
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                """, ctx.rum_params()))[0]
            sessions, users, views, actions = int(t[0]), int(t[1]), int(t[2]), int(t[3])
            rows = await ctx.ch_rows(
                f"""
                SELECT {bucket_sql} AS b, {RUM_SESSIONS_SQL}
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                GROUP BY b ORDER BY b
                """, ctx.rum_params(**bucket_params))
        per_session = views / sessions if sessions else 0.0
        points = [{"t": ctx.iso(r[0]), "v": float(r[1] or 0)} for r in rows]
    except Exception:
        logger.debug("rum audience unavailable", exc_info=True)
    if users is None:
        user_kpi = {"label": "Unique users", "value": "—", "accent": "info",
                    "subtitle": f"not kept beyond {src['raw_days']} days"}
    else:
        user_kpi = {"label": "Unique users", "value": f"{users:,}", "accent": "info",
                    "subtitle": "signed-in identities" if users else "no user attribution"}
    notes = ([src["note"]] if src["note"] else [])
    if not sessions:
        notes.append("No browser telemetry in this window — install a Browser RUM key from APM "
                     "Settings → Ingest keys.")
    return _section(
        "rum_audience", "Audience & Engagement",
        description="Real people who opened the application in a browser during this period.",
        kpis=[
            {"label": "Sessions", "value": f"{sessions:,}", "accent": "primary"},
            user_kpi,
            {"label": "Page views", "value": f"{views:,}", "accent": "primary"},
            {"label": "Views / session", "value": f"{per_session:.1f}", "accent": "info",
             "subtitle": "engagement depth"},
            {"label": "User actions", "value": f"{actions:,}", "accent": "success",
             "subtitle": "clicks and interactions"},
        ],
        charts=([{"title": "Sessions over time",
                  "series": {"kind": "area", "unit": "sessions", "color": "primary",
                             "points": points}}]
                if len(points) >= 2 else []),
        notes=notes,
    )


async def _sec_rum_experience(ctx: SectionCtx) -> dict:
    vitals: dict[str, Optional[float]] = {k: None for k in _VITAL_THRESHOLDS}
    good = needs = poor = 0
    src = await _rum_source(ctx)
    lcp_good, lcp_poor, _ = _VITAL_THRESHOLDS["LCP"]
    try:
        app = ctx.rum_app_clause()
        if src["rollup"]:
            r = (await ctx.ch_rows(
                f"""
                SELECT quantileTDigestMerge(0.75)(lcp), sum(lcp_samples),
                       quantileTDigestMerge(0.75)(inp), sum(inp_samples),
                       quantileTDigestMerge(0.75)(cls), sum(cls_samples),
                       quantileTDigestMerge(0.75)(fcp), sum(fcp_samples),
                       quantileTDigestMerge(0.75)(ttfb), sum(ttfb_samples),
                       sum(lcp_good), sum(lcp_poor), sum(lcp_rated)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                """, ctx.rum_params()))[0]
            good, poor, rated = int(r[10] or 0), int(r[11] or 0), int(r[12] or 0)
            needs = max(rated - good - poor, 0)
        else:
            r = (await ctx.ch_rows(
                f"""
                SELECT quantileTDigestIf(0.75)(lcp_value, lcp_present = 1), countIf(lcp_present = 1),
                       quantileTDigestIf(0.75)(inp_value, inp_present = 1), countIf(inp_present = 1),
                       quantileTDigestIf(0.75)(cls_value, cls_present = 1), countIf(cls_present = 1),
                       quantileTDigestIf(0.75)(fcp_value, fcp_present = 1), countIf(fcp_present = 1),
                       quantileTDigestIf(0.75)(ttfb_value, ttfb_present = 1), countIf(ttfb_present = 1),
                       countIf(lcp_present = 1 AND lcp_value <= {lcp_good}),
                       countIf(lcp_present = 1 AND lcp_value > {lcp_good} AND lcp_value <= {lcp_poor}),
                       countIf(lcp_present = 1 AND lcp_value > {lcp_poor})
                FROM ({_per_view_sql(ctx)})
                """, ctx.rum_params()))[0]
            good, needs, poor = int(r[10] or 0), int(r[11] or 0), int(r[12] or 0)
        for i, k in enumerate(_VITAL_ORDER):
            vitals[k] = _p75(r[2 * i], r[2 * i + 1])
    except Exception:
        logger.debug("rum experience unavailable", exc_info=True)

    kpis = []
    for k in _VITAL_ORDER:
        verdict, accent = _vital_verdict(k, vitals[k])
        kpis.append({"label": f"{k} (p75)", "value": _fmt_vital(k, vitals[k]),
                     "accent": accent, "subtitle": verdict})
    total = good + needs + poor
    notes = [src["note"]] if src["note"] else []
    notes.append(f"{good / total * 100:.0f}% of page loads met the “Good” bar for LCP." if total else
                 "No Core Web Vitals collected yet — vitals arrive once real visitors load an "
                 "instrumented page.")
    return _section(
        "rum_experience", "Real-User Experience",
        description=("Core Web Vitals at the 75th percentile — the score Google applies to "
                     "search ranking, measured on your real visitors' devices."),
        kpis=kpis,
        charts=([{"title": "Page-load experience (LCP)",
                  "series": {"kind": "donut", "unit": "views",
                             "points": [{"label": "Good", "value": good},
                                        {"label": "Needs work", "value": needs},
                                        {"label": "Poor", "value": poor}]}}]
                if total else []),
        notes=notes,
    )


async def _sec_rum_top_pages(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    ranked: list[tuple[str, int]] = []
    src = await _rum_source(ctx)
    top = _top_n(ctx, 15)
    try:
        app = ctx.rum_app_clause()
        if src["rollup"]:
            rows = await ctx.ch_rows(
                f"""
                SELECT view_name, uniqCombined64Merge(views) AS v, uniqCombined64Merge(sessions),
                       quantileTDigestMerge(0.75)(lcp), sum(lcp_samples),
                       quantileTDigestMerge(0.75)(load_ms), sum(load_samples), sum(errors)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND view_name != ''{app}
                GROUP BY view_name ORDER BY v DESC LIMIT {top}
                """, ctx.rum_params())
            for r in rows:
                lcp, load = _p75(r[3], r[4]), _p75(r[5], r[6])
                rows_out.append([r[0], f"{int(r[1]):,}", f"{int(r[2]):,}", "—",
                                 _fmt_vital("LCP", lcp), _fmt_ms(load), f"{int(r[7] or 0):,}"])
                ranked.append((r[0], int(r[1])))
        else:
            rows = await ctx.ch_rows(
                f"""
                SELECT view_name, {RUM_VIEWS_SQL} AS v, {RUM_SESSIONS_SQL},
                       uniqIf(user_id, user_id != ''), countIf(event_type = 'error')
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND view_name != ''{app}
                GROUP BY view_name ORDER BY v DESC LIMIT {top}
                """, ctx.rum_params())
            vit: dict[str, tuple] = {}
            names = [r[0] for r in rows]
            if names:
                vrows = await ctx.ch_rows(
                    f"""
                    SELECT view_name,
                           quantileTDigestIf(0.75)(lcp_value, lcp_present = 1), countIf(lcp_present = 1),
                           quantileTDigestIf(0.75)(load_value, load_present = 1), countIf(load_present = 1)
                    FROM ({_per_view_sql(ctx, " AND view_name IN %(names)s",
                                         "application_id, env, view_name, view_id")})
                    GROUP BY view_name
                    """, ctx.rum_params(names=names))
                vit = {v[0]: v for v in vrows}
            for r in rows:
                v = vit.get(r[0])
                lcp = _p75(v[1], v[2]) if v else None
                load = _p75(v[3], v[4]) if v else None
                rows_out.append([r[0], f"{int(r[1]):,}", f"{int(r[2]):,}", f"{int(r[3]):,}",
                                 _fmt_vital("LCP", lcp), _fmt_ms(load), f"{int(r[4]):,}"])
                ranked.append((r[0], int(r[1])))
    except Exception:
        logger.debug("rum top pages unavailable", exc_info=True)
    return _section(
        "rum_top_pages", "Most-Visited Pages",
        description="Where visitors actually spend their attention, and how each page performs.",
        charts=([{"title": "Views by page",
                  "series": {"kind": "rank", "unit": "views", "color": "primary",
                             "points": [{"label": name, "value": n} for name, n in ranked[:10]]}}]
                if ranked else []),
        tables=[{"headers": ["Page", "Views", "Sessions", "Users", "LCP p75", "Load p75", "Errors"],
                 "styles": ["text", "num", "num", "num", "num", "num", "num"],
                 "rows": rows_out}],
        notes=[src["note"]] if src["note"] else [],
    )


async def _sec_rum_segments(ctx: SectionCtx) -> dict:
    device_rows: list[list[str]] = []
    browser_rows: list[list[str]] = []
    device_points: list[dict] = []
    src = await _rum_source(ctx)
    try:
        app = ctx.rum_app_clause()
        if src["rollup"]:
            drows = await ctx.ch_rows(
                f"""
                SELECT device_type, uniqCombined64Merge(sessions) AS s, uniqCombined64Merge(views),
                       quantileTDigestMerge(0.75)(lcp), sum(lcp_samples)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND device_type != ''{app}
                GROUP BY device_type ORDER BY s DESC LIMIT 10
                """, ctx.rum_params())
            lcp_by_device = {r[0]: _p75(r[3], r[4]) for r in drows}
            brows = await ctx.ch_rows(
                f"""
                SELECT browser, anyHeavy(os), uniqCombined64Merge(sessions) AS s,
                       uniqCombined64Merge(views), sum(errors)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND browser != ''{app}
                GROUP BY browser ORDER BY s DESC LIMIT 10
                """, ctx.rum_params())
        else:
            drows = await ctx.ch_rows(
                f"""
                SELECT device_type, {RUM_SESSIONS_SQL} AS s, {RUM_VIEWS_SQL}
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND device_type != ''{app}
                GROUP BY device_type ORDER BY s DESC LIMIT 10
                """, ctx.rum_params())
            lcp_by_device = {}
            if drows:
                vrows = await ctx.ch_rows(
                    f"""
                    SELECT device_type,
                           quantileTDigestIf(0.75)(lcp_value, lcp_present = 1), countIf(lcp_present = 1)
                    FROM ({_per_view_sql(ctx, " AND device_type != ''",
                                         "application_id, env, device_type, view_id")})
                    GROUP BY device_type
                    """, ctx.rum_params())
                lcp_by_device = {v[0]: _p75(v[1], v[2]) for v in vrows}
            brows = await ctx.ch_rows(
                f"""
                SELECT browser, anyHeavy(os), {RUM_SESSIONS_SQL} AS s,
                       {RUM_VIEWS_SQL}, countIf(event_type = 'error')
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s AND browser != ''{app}
                GROUP BY browser ORDER BY s DESC LIMIT 10
                """, ctx.rum_params())
        total_sessions = sum(int(r[1]) for r in drows) or 1
        for r in drows:
            label = str(r[0]).title()
            device_rows.append([label, f"{int(r[1]):,}",
                                _fmt_pct(int(r[1]) / total_sessions * 100, 1),
                                f"{int(r[2]):,}", _fmt_vital("LCP", lcp_by_device.get(r[0]))])
            device_points.append({"label": label, "value": int(r[1])})
        for r in brows:
            browser_rows.append([r[0], str(r[1] or "—"), f"{int(r[2]):,}",
                                 f"{int(r[3]):,}", f"{int(r[4] or 0):,}"])
    except Exception:
        logger.debug("rum segments unavailable", exc_info=True)
    return _section(
        "rum_segments", "Audience Segments",
        description="Which devices and browsers your visitors use — and whether any of them get a worse experience.",
        charts=([{"title": "Sessions by device",
                  "series": {"kind": "donut", "unit": "sessions", "points": device_points}}]
                if device_points else []),
        tables=[
            {"title": "By device", "headers": ["Device", "Sessions", "Share", "Views", "LCP p75"],
             "styles": ["text", "num", "num", "num", "num"], "rows": device_rows},
            {"title": "By browser", "headers": ["Browser", "OS", "Sessions", "Views", "Errors"],
             "styles": ["text", "text", "num", "num", "num"], "rows": browser_rows},
        ],
        notes=[src["note"]] if src["note"] else [],
    )


async def _sec_rum_reliability(ctx: SectionCtx) -> dict:
    errors = affected_sessions = views = 0
    distinct: Optional[int] = 0
    affected_users: Optional[int] = 0
    rows_out: list[list[str]] = []
    src = await _rum_source(ctx)
    notes: list[str] = []
    try:
        app = ctx.rum_app_clause()
        raw_params = ctx.rum_params()
        if src["rollup"]:
            t = (await ctx.ch_rows(
                f"""
                SELECT sum(errors), uniqCombined64Merge(error_sessions), uniqCombined64Merge(views)
                FROM zenplus.apm_rum_metrics_5m
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                """, ctx.rum_params()))[0]
            errors, affected_sessions, views = int(t[0] or 0), int(t[1] or 0), int(t[2] or 0)
            # Issue-level detail only exists in raw events: the retained part.
            raw_params["f"] = _naive_utc(src["reach"])
            detail = _aware_utc(ctx.to) > src["reach"]
            if detail:
                notes.append(f"Error details are kept for {src['raw_days']} days; distinct issues, users "
                             f"affected and the error list cover {ctx.fmt_dt(src['reach'])} onwards.")
                d = (await ctx.ch_rows(
                    f"""
                    SELECT uniqIf(error_fingerprint, event_type = 'error' AND error_fingerprint != ''),
                           uniqIf(user_id, event_type = 'error' AND user_id != '')
                    FROM zenplus.apm_rum_events
                    WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                    """, raw_params))[0]
                distinct, affected_users = int(d[0] or 0), int(d[1] or 0)
            else:
                # Not measured, never "0 issues".
                distinct = affected_users = None
                notes.append(f"Error details are kept for {src['raw_days']} days; distinct issues, users "
                             "affected and the error list are not available for this period.")
        else:
            t = (await ctx.ch_rows(
                f"""
                SELECT countIf(event_type = 'error'),
                       uniqIf(error_fingerprint, event_type = 'error' AND error_fingerprint != ''),
                       uniqExactIf((application_id, env, session_id), event_type = 'error' AND sampled = 1),
                       uniqIf(user_id, event_type = 'error' AND user_id != ''),
                       {RUM_VIEWS_SQL}
                FROM zenplus.apm_rum_events
                WHERE timestamp >= %(f)s AND timestamp <= %(t)s{app}
                """, raw_params))[0]
            errors, distinct, affected_sessions, affected_users, views = (
                int(t[0]), int(t[1]), int(t[2]), int(t[3]), int(t[4]))
        rows = [] if distinct is None else await ctx.ch_rows(
            f"""
            SELECT anyHeavy(error_message), anyHeavy(error_type), anyHeavy(view_name),
                   count() AS n, uniqExact((application_id, env, session_id)), max(timestamp)
            FROM zenplus.apm_rum_events
            WHERE timestamp >= %(f)s AND timestamp <= %(t)s
              AND event_type = 'error' AND error_fingerprint != ''{app}
            GROUP BY error_fingerprint ORDER BY n DESC LIMIT 12
            """, raw_params)
        for r in rows:
            msg = str(r[0] or "Unknown error")
            rows_out.append([msg if len(msg) <= 90 else msg[:89] + "…",
                             str(r[1] or "—"), str(r[2] or "—"),
                             f"{int(r[3]):,}", f"{int(r[4]):,}", ctx.fmt_short(r[5])])
    except Exception:
        logger.debug("rum reliability unavailable", exc_info=True)
    err_rate = errors / views * 100 if views else None
    if not errors:
        notes.append("No browser errors reported in this period.")
    return _section(
        "rum_reliability", "Reliability in the Browser",
        description="JavaScript failures real visitors hit — each one is a person who could not finish what they came to do.",
        kpis=[
            {"label": "Errors", "value": f"{errors:,}",
             "accent": "danger" if errors else "success"},
            {"label": "Distinct issues", "value": "—" if distinct is None else f"{distinct:,}",
             "accent": "neutral" if distinct is None else "warning"},
            {"label": "Error rate", "value": _fmt_pct(err_rate),
             "accent": "danger" if (err_rate or 0) > 1 else "success", "subtitle": "of page views"},
            {"label": "Sessions affected", "value": f"{affected_sessions:,}", "accent": "warning"},
            {"label": "Users affected", "value": "—" if affected_users is None else f"{affected_users:,}",
             "accent": ("neutral" if affected_users is None else
                        "danger" if affected_users else "success")},
        ],
        tables=[{"headers": ["Error", "Type", "Page", "Count", "Sessions", "Last seen"],
                 "styles": ["text", "text", "text", "num", "num", "num"], "rows": rows_out}],
        notes=notes,
    )

# ─── Capacity sections ──────────────────────────────────────────────────────

# Agents refresh every filesystem row at least every 10 minutes (unchanged
# rows included) and the Servers page marks a row stale after 15; a row not
# refreshed for an hour belongs to a volume or agent that no longer reports.
FS_FRESH_HOURS = 1


async def _sec_capacity_filesystems(ctx: SectionCtx) -> dict:
    """Fullest monitorable filesystems right now: the capacity filter of the
    server health and alert rules (no install media, tmpfs, overlays …),
    disabled servers left out, stale inventory disclosed instead of shown."""
    from app.services.filesystem_monitoring import pg_capacity_filter

    rows_out: list[list[str]] = []
    critical: Optional[int] = None
    total = stale = 0
    try:
        rows = (await ctx.db.execute(text(
            f"""
            SELECT COALESCE(s.display_name, s.hostname), f.mount, f.used_pct,
                   f.total_bytes, f.free_bytes,
                   f.updated_at >= NOW() - make_interval(hours => :fresh_h) AS fresh
            FROM server_filesystem_inventory f
            JOIN servers s ON s.id = f.server_id
            WHERE f.used_pct IS NOT NULL
              AND COALESCE(s.status, '') <> 'disabled'
              AND {pg_capacity_filter('f.fs_type')}
            ORDER BY f.used_pct DESC
            """), {"fresh_h": FS_FRESH_HOURS})).fetchall()
        fresh = [r for r in rows if r[5]]
        stale = len(rows) - len(fresh)
        total = len(fresh)
        critical = sum(1 for r in fresh if float(r[2]) >= 90)
        for r in fresh[:_top_n(ctx, 20)]:
            rows_out.append([r[0] or "—", r[1] or "—", _fmt_pct(float(r[2]), 1),
                             _fmt_bytes(float(r[3])) if r[3] is not None else "—",
                             _fmt_bytes(float(r[4])) if r[4] is not None else "—"])
    except Exception:
        logger.debug("capacity filesystems unavailable", exc_info=True)
    notes = _shown_note(len(rows_out), total, "filesystems")
    if stale:
        notes.append(f"{stale} filesystem(s) not reported by their agent in the last "
                     f"{FS_FRESH_HOURS} hour(s) are left out (stale inventory).")
    return _section(
        "capacity_filesystems", "Filesystem Capacity",
        description="Fullest filesystems across agent-monitored servers.",
        kpis=[{"label": "Filesystems ≥90% full", "value": "—" if critical is None else str(critical),
               "accent": "neutral" if critical is None else "danger" if critical else "success"}],
        tables=[{"headers": ["Server", "Mount", "Used", "Size", "Free"],
                 "styles": ["text", "mono", "pct-bar", "num", "num"], "rows": rows_out}],
        notes=notes,
    )


async def _sec_capacity_interfaces(ctx: SectionCtx) -> dict:
    """Interfaces truly closest to saturation: 95th-percentile utilisation of
    the busier direction against the effective interface speed."""
    st = await _interface_stats(ctx)
    assessable = [r for r in st["rows"] if r["util_p95"] is not None]
    assessable.sort(key=lambda r: -r["util_p95"])
    top = _top_n(ctx, 15)
    rows = [
        [r["label"], _fmt_bps(r["speed"]), _fmt_pct(r["util_p95"], 1),
         _fmt_pct(r["util_avg"], 1), _fmt_bps(max(r["in_p95"], r["out_p95"]))]
        for r in assessable[:top]
    ]
    notes = _if_method_note(st) + _shown_note(len(rows), len(assessable), "interfaces with a known speed")
    if st.get("speed_unknown"):
        notes.append(f"{st['speed_unknown']} interface(s) with traffic have no known speed and cannot be "
                     "assessed (set the circuit speed on the interface).")
    return _section(
        "capacity_interfaces", "Link Capacity Headroom",
        description=("Interfaces closest to saturation: 95th-percentile utilization of the busier "
                     "direction against the effective interface speed (configured circuit speed, "
                     "else SNMP ifSpeed)."),
        tables=[{"headers": ["Interface", "Speed", "P95 utilization", "Avg utilization", "P95 rate"],
                 "styles": ["text", "num", "pct-bar", "num", "num"], "rows": rows}],
        notes=notes,
    )


# ─── Alerting sections ──────────────────────────────────────────────────────

def _fmt_minutes(m: Optional[float]) -> str:
    if m is None:
        return "—"
    if m < 1:
        return "<1 min"
    return f"{m:,.0f} min" if m < 600 else f"{m / 60:,.1f} h"


async def _sec_alert_kpis(ctx: SectionCtx) -> dict:
    """Alert incidents raised in the window by severity, and MTTR, with the
    Alert Center's semantics: recovery notices, dependency-suppressed and
    hold-cancelled rows are notices, not alerts (alert_workflow.notice_sql),
    and MTTR is the mean time to recover of incidents that recovered in the
    window (alert_service.summary)."""
    from app.services import alert_workflow as wf

    counts = {"critical": 0, "warning": 0, "info": 0}
    total = notices = 0
    mttr_min: Optional[float] = None
    recovered = 0
    try:
        notice = wf.notice_sql("a")
        rows = (await ctx.db.execute(text(
            f"SELECT a.severity, count(*) FILTER (WHERE NOT {notice}) AS incidents, "
            f"count(*) FILTER (WHERE {notice}) AS notices "
            "FROM alerts a WHERE a.triggered_at >= :f AND a.triggered_at <= :t "
            "GROUP BY a.severity"), {"f": ctx.frm, "t": ctx.to})).fetchall()
        for sev, n_incidents, n_notices in rows:
            key = str(sev or "info").lower()
            counts[key] = counts.get(key, 0) + int(n_incidents or 0)
            total += int(n_incidents or 0)
            notices += int(n_notices or 0)
    except Exception:
        logger.debug("alert kpis unavailable", exc_info=True)
    try:
        from app.services import alert_service
        summ = await alert_service.summary(ctx.db, alert_service.AlertFilters(
            from_time=_aware_utc(ctx.frm), to_time=_aware_utc(ctx.to)))
        ttr = (summ.get("window") or {}).get("time_to_recover") or {}
        if ttr.get("mean_s") is not None:
            mttr_min = float(ttr["mean_s"]) / 60
        recovered = int(ttr.get("count") or 0)
    except Exception:
        logger.debug("alert MTTR unavailable", exc_info=True)
    notes = ([f"{notices:,} recovery or suppression notice(s) are not counted as alerts."]
             if notices else [])
    return _section(
        "alert_kpis", "Alert Summary",
        kpis=[
            {"label": "Total alerts", "value": f"{total:,}", "accent": "primary"},
            {"label": "Critical", "value": str(counts.get("critical", 0)),
             "accent": "danger" if counts.get("critical") else "success"},
            {"label": "Warning", "value": str(counts.get("warning", 0)), "accent": "warning"},
            {"label": "MTTR", "value": _fmt_minutes(mttr_min), "accent": "info",
             "subtitle": (f"mean time to recover · {recovered:,} incident(s)" if recovered
                          else "no incident recovered in this period")},
        ],
        notes=notes,
    )


async def _sec_alert_trend(ctx: SectionCtx) -> dict:
    """Alert incidents raised per hour (windows up to 2 days) or per day,
    bucketed in the organisation's time zone; notices excluded like the
    Alert Summary."""
    from app.services import alert_workflow as wf

    unit = "hour" if (ctx.to - ctx.frm) <= timedelta(days=2) else "day"
    points: list[dict] = []
    critical = 0
    try:
        rows = (await ctx.db.execute(text(
            f"SELECT date_trunc('{unit}', a.triggered_at AT TIME ZONE CAST(:tz AS text))"
            " AT TIME ZONE CAST(:tz AS text) AS b, "
            "count(*) AS n, count(*) FILTER (WHERE lower(a.severity) = 'critical') AS crit "
            "FROM alerts a WHERE a.triggered_at >= :f AND a.triggered_at <= :t "
            f"AND NOT {wf.notice_sql('a')} GROUP BY 1 ORDER BY 1"),
            {"f": ctx.frm, "t": ctx.to, "tz": ctx.tz_name})).fetchall()
        # Every local hour/day of the window gets a bar: a quiet day is 0
        # alerts (a fact), not a missing bar.
        counts = {ctx.iso(start): 0 for start in _local_bucket_starts(ctx, unit)}
        for r in rows:
            key = ctx.iso(r[0])
            counts[key] = counts.get(key, 0) + int(r[1] or 0)
        points = [{"t": k, "v": v} for k, v in sorted(counts.items()) if k]
        critical = sum(int(r[2] or 0) for r in rows)
    except Exception:
        logger.debug("alert trend unavailable", exc_info=True)
    sec = _section("alert_trend", "Alert Volume Over Time", half=True,
                   description=f"Alerts raised per {unit} ({ctx.tz_label()}); recovery notices excluded.",
                   charts=[{"title": "",
                            "series": {"kind": "bars", "unit": "alerts", "color": "warning",
                                       "points": points}}])
    if critical:
        sec["notes"].append(f"{critical} critical alert(s) in the window.")
    return sec


async def _sec_noisy_sources(ctx: SectionCtx) -> dict:
    # The technical dataset withholds NetFlow alert messages from a requester
    # without netflow.view (SectionCtx.withholds_netflow_alerts). It ranks the
    # ten noisiest sources, so the section lists at most ten.
    d = await ctx.dataset("technical")
    rows = [[n.get("hostname") or "—", (n.get("sample_message") or "")[:60],
             (n.get("severity") or "").upper(), str(n.get("alert_count") or 0)]
            for n in (d.get("noisy_alerts") or [])][:_top_n(ctx, 10)]
    return _section("noisy_sources", "Noisiest Alert Sources", half=True,
                    tables=[{"headers": ["Device", "Sample message", "Severity", "Count"],
                             "styles": ["text", "text", "severity", "num"], "rows": rows}])


async def _sec_recent_alerts(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    total = 0
    try:
        # A requester without netflow.view gets NetFlow alerts without their
        # flow-derived message (X-01), redacted like the Alert Center does.
        from app.services.netflow_alert_service import redact_alert_payload

        redact = await ctx.withholds_netflow_alerts()
        rows = (await ctx.db.execute(text(
            """
            SELECT a.severity, a.message,
                   COALESCE(d.hostname, a.metadata->>'service', '—') AS source,
                   a.status, a.triggered_at,
                   jsonb_build_object('source', a.metadata->'source') AS metadata,
                   COUNT(*) OVER () AS total
            FROM alerts a LEFT JOIN devices d ON d.id = a.device_id
            WHERE a.triggered_at >= :f AND a.triggered_at <= :t
            ORDER BY a.triggered_at DESC LIMIT :lim
            """), {"f": ctx.frm, "t": ctx.to, "lim": _top_n(ctx, 25)})).fetchall()
        for row in rows:
            r = dict(row._mapping)
            total = max(total, int(r.get("total") or 0))
            if redact:
                r = redact_alert_payload(r)
            rows_out.append([(r["severity"] or "").upper(), (r["message"] or "")[:70], r["source"],
                             r["status"] or "—", ctx.fmt_short(r["triggered_at"])])
    except Exception:
        logger.debug("recent alerts unavailable", exc_info=True)
    return _section("recent_alerts", "Recent Alerts",
                    tables=[{"headers": ["Severity", "Message", "Source", "Status", "Triggered"],
                             "styles": ["severity", "text", "text", "status", "num"],
                             "rows": rows_out}],
                    notes=_shown_note(len(rows_out), total, "alerts (latest first)"))


# ─── Inventory sections ─────────────────────────────────────────────────────

async def _sec_inventory_summary(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("inventory")
    totals = d.get("totals") or {}
    return _section(
        "inventory_summary", "Asset Summary",
        kpis=[
            {"label": "Network devices", "value": str(totals.get("devices") or 0), "accent": "primary"},
            {"label": "Monitored interfaces", "value": str((d.get("interface_totals") or {}).get("monitored") or 0),
             "accent": "info"},
            {"label": "Remote sensors", "value": str(totals.get("sensors") or 0), "accent": "primary"},
        ],
    )


async def _sec_inventory_breakdown(ctx: SectionCtx) -> dict:
    d = await ctx.dataset("inventory")
    by_type = d.get("devices_by_type") or []
    vendor_rows = [[v.get("vendor") or "unknown", str(v.get("count") or 0)]
                   for v in (d.get("devices_by_vendor") or [])[:10]]
    loc_rows = [[l.get("location") or "unknown", str(l.get("count") or 0)]
                for l in (d.get("devices_by_location") or [])[:10]]
    return _section(
        "inventory_breakdown", "Device Breakdown",
        charts=[{"title": "Devices by type",
                 "series": {"kind": "donut", "unit": "devices",
                            "points": [{"label": t.get("type") or "unknown",
                                        "value": t.get("count") or 0}
                                       for t in by_type[:6]]}}],
        tables=[{"title": "By vendor", "headers": ["Vendor", "Devices"], "rows": vendor_rows},
                {"title": "By location", "headers": ["Location", "Devices"], "rows": loc_rows}],
    )


async def _sec_inventory_servers(ctx: SectionCtx) -> dict:
    rows_out: list[list[str]] = []
    total = 0
    try:
        rows = (await ctx.db.execute(text(
            """
            SELECT COALESCE(display_name, hostname), os_name, os_version, status,
                   environment, collection_mode, COUNT(*) OVER () AS total
            FROM servers ORDER BY hostname LIMIT :lim
            """), {"lim": _top_n(ctx, 100)})).fetchall()
        total = int(rows[0][6] or 0) if rows else 0
        for r in rows:
            rows_out.append([r[0] or "—", f"{r[1] or '—'} {r[2] or ''}".strip(),
                             (r[3] or "—"), r[4] or "—", r[5] or "—"])
    except Exception:
        logger.debug("inventory servers unavailable", exc_info=True)
    return _section("inventory_servers", "Server Inventory",
                    tables=[{"headers": ["Server", "Operating system", "Status", "Environment", "Mode"],
                             "rows": rows_out}],
                    notes=_shown_note(len(rows_out), total, "servers"))

# ─── NCCM (configuration protection) sections ──────────────────────────────

async def _ncm_devices(ctx: SectionCtx) -> list[dict]:
    """Current per-device NCCM state (same computation as the NCCM overview)."""
    if "ncm_devices" not in ctx._cache:
        from app.api.v1.ncm import _overview_rows, _device_row
        now = datetime.now(timezone.utc)
        rows = [_device_row(r, now) for r in await _overview_rows(ctx.db, None)]
        ids = set(ctx.device_ids or [])
        ctx._cache["ncm_devices"] = [d for d in rows if not ids or d["device_id"] in ids]
    return ctx._cache["ncm_devices"]


def _ncm_when(ctx: SectionCtx, iso: Optional[str]) -> str:
    """An NCCM timestamp in the organisation's time zone and format."""
    return ctx.fmt_dt(iso) if iso else "never"


async def _sec_ncm_protection(ctx: SectionCtx) -> dict:
    devices = await _ncm_devices(ctx)
    eligible = [d for d in devices if d["eligible"]]
    fresh = sum(d["fresh"] for d in eligible)
    pct = f"{fresh / len(eligible) * 100:.0f}%" if eligible else "N/A"
    failed = sum(d["backup_state"] in ("failed", "blocked") for d in eligible)
    unsupported = sum(d["support"]["state"] == "unsupported" for d in eligible)
    restored = sum(bool(d["last_restore_test"] and d["last_restore_test"]["result"] == "passed") for d in eligible)
    sec = _section(
        "ncm_protection", "Configuration Protection",
        kpis=[
            {"label": "Protected (fresh & validated)", "value": f"{fresh} of {len(eligible)}",
             "accent": "success" if eligible and fresh == len(eligible) else "danger" if not fresh else "warning"},
            {"label": "Coverage", "value": pct, "accent": "primary"},
            {"label": "Failed / blocked", "value": str(failed), "accent": "danger" if failed else "success"},
            {"label": "Eligible without collector", "value": str(unsupported), "accent": "warning" if unsupported else "success"},
            {"label": "Restore-tested", "value": f"{restored} of {len(eligible)}", "accent": "info"},
        ])
    sec["notes"].append("Coverage = eligible devices with fresh, format-validated captures of every required "
                        "configuration type, divided by all eligible devices (unsupported devices stay in the denominator). "
                        "Validated means complete and format-checked; only a recorded restore test demonstrates recovery.")
    if not eligible:
        sec["notes"].append("No devices are in the automatic backup scope; coverage is not measurable.")
    return sec


async def _sec_ncm_breakdown(ctx: SectionCtx) -> dict:
    devices = [d for d in await _ncm_devices(ctx) if d["eligible"]]

    def table(key, label):
        groups: dict[str, list] = {}
        for d in devices:
            groups.setdefault(d.get(key) or "unassigned", []).append(d)
        rows = []
        for name, members in sorted(groups.items(), key=lambda i: -len(i[1])):
            ok = sum(m["fresh"] for m in members)
            rows.append([name, str(len(members)), str(ok), f"{ok / len(members) * 100:.0f}%"])
        return {"title": f"By {label}", "headers": [label.capitalize(), "Eligible", "Protected", "Coverage"],
                "styles": ["text", "num", "num", "num"], "rows": rows}
    return _section("ncm_breakdown", "Coverage by Vendor and Site",
                    tables=[table("vendor", "vendor"), table("location", "site")],
                    notes=["Each row shows its own denominator; site percentages are never averaged."])


async def _sec_ncm_outcomes(ctx: SectionCtx) -> dict:
    ids = ctx.device_ids
    cond = " AND device_id::text = ANY(:ids)" if ids else ""
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to}
    if ids:
        params["ids"] = ids
    # Calendar days of the organisation's time zone (not the database's).
    rows = (await ctx.db.execute(text(f"""
        SELECT date_trunc('day', COALESCE(finished_at, started_at) AT TIME ZONE CAST(:tz AS text))
                 AT TIME ZONE CAST(:tz AS text) AS day,
               count(*) FILTER (WHERE status='success' AND validated) AS ok,
               count(*) FILTER (WHERE status='failed') AS failed
        FROM ncm_backup_runs WHERE COALESCE(finished_at, started_at) BETWEEN :f AND :t{cond}
        GROUP BY 1 ORDER BY 1"""), {**params, "tz": ctx.tz_name})).fetchall()
    ts = [ctx.iso(r.day) for r in rows]
    oks = [r.ok for r in rows]
    fails = [r.failed for r in rows]
    cats = (await ctx.db.execute(text(f"""
        SELECT COALESCE(failure_category, 'unknown') AS cat, count(*) AS n FROM ncm_backup_runs
        WHERE status='failed' AND COALESCE(finished_at, started_at) BETWEEN :f AND :t{cond}
        GROUP BY 1 ORDER BY 2 DESC"""), params)).fetchall()
    charts = [
        {"title": "Validated captures per day",
         "series": {"kind": "bars", "unit": "captures", "color": "success",
                    "points": [{"t": t, "v": v} for t, v in zip(ts, oks)]}},
        {"title": "Failed attempts per day",
         "series": {"kind": "bars", "unit": "attempts", "color": "warning",
                    "points": [{"t": t, "v": v} for t, v in zip(ts, fails)]}},
    ] if rows else []
    sec = _section("ncm_outcomes", "Backup Outcomes", charts=charts,
                   tables=[{"title": "Failed attempts by cause", "headers": ["Cause", "Attempts"],
                            "styles": ["text", "num"], "rows": [[r.cat.replace("_", " "), str(r.n)] for r in cats]}])
    if not rows:
        sec["notes"].append("No backup attempts were recorded in this period.")
    return sec


async def _sec_ncm_at_risk(ctx: SectionCtx) -> dict:
    order = ["failed", "blocked", "stale", "unverified", "pending", "unconfigured"]
    risky = [d for d in await _ncm_devices(ctx) if d["eligible"] and d["backup_state"] in order]
    risky.sort(key=lambda d: (order.index(d["backup_state"]), d["hostname"] or ""))
    rows = [[d["hostname"] or d["ip"], d["backup_state"].replace("_", " "), _ncm_when(ctx, d["validated_at"]),
             (d["last_error"] or d["support"]["message"] if d["backup_state"] in ("failed", "blocked", "unconfigured") else "")[:80],
             _ncm_when(ctx, d["next_due"]) if d["next_due"] else "not scheduled"]
            for d in risky[:_top_n(ctx, 50)]]
    return _section("ncm_at_risk", "Devices Needing Action",
                    tables=[{"headers": ["Device", "State", "Last valid capture", "Reason", "Next due"],
                             "styles": ["text", "status", "num", "text", "num"], "rows": rows}],
                    notes=(_shown_note(len(rows), len(risky), "devices needing action") if rows else
                           ["Every eligible device has a fresh validated capture."]))


async def _sec_ncm_compliance(ctx: SectionCtx) -> dict:
    ids = ctx.device_ids
    cond = " AND r.device_id::text = ANY(:ids)" if ids else ""
    params: dict[str, Any] = {"ids": ids} if ids else {}
    policies = (await ctx.db.execute(text(f"""
        SELECT p.name, count(DISTINCT r.device_id) AS devices,
               count(*) FILTER (WHERE r.status='pass') AS pass, count(*) FILTER (WHERE r.status='fail') AS fail,
               count(*) FILTER (WHERE r.status='unknown') AS unknown, count(*) FILTER (WHERE r.status='excepted') AS excepted
        FROM ncm_compliance_results r JOIN ncm_policies p ON p.id=r.policy_id WHERE TRUE{cond}
        GROUP BY p.name ORDER BY fail DESC, p.name"""), params)).fetchall()
    top = (await ctx.db.execute(text(f"""
        SELECT ru.title, ru.severity, count(*) AS n FROM ncm_compliance_results r JOIN ncm_policy_rules ru ON ru.id=r.rule_id
        WHERE r.status='fail'{cond} GROUP BY ru.title, ru.severity
        ORDER BY array_position(ARRAY['critical','high','medium','low','info'], ru.severity), n DESC LIMIT 10"""), params)).fetchall()
    fails = sum(p.fail for p in policies)
    sec = _section("ncm_compliance", "Configuration Compliance",
                   kpis=[{"label": "Failed checks", "value": str(fails), "accent": "danger" if fails else "success"},
                         {"label": "Excepted checks", "value": str(sum(p.excepted for p in policies)), "accent": "warning"},
                         {"label": "Not assessable", "value": str(sum(p.unknown for p in policies)), "accent": "info"}],
                   tables=[{"title": "By policy", "headers": ["Policy", "Devices", "Pass", "Fail", "Unknown", "Excepted"],
                            "styles": ["text", "num", "num", "num", "num", "num"],
                            "rows": [[p.name, str(p.devices), str(p[2]), str(p.fail), str(p.unknown), str(p.excepted)] for p in policies]},
                           {"title": "Most frequent failures", "headers": ["Rule", "Severity", "Devices"],
                            "styles": ["text", "severity", "num"], "rows": [[t.title, t.severity.upper(), str(t.n)] for t in top]}])
    sec["notes"].append("Results reflect the latest validated running configuration of each device. "
                        "Unknown means no validated configuration was available to assess.")
    return sec


async def _sec_ncm_changes(ctx: SectionCtx) -> dict:
    ids = ctx.device_ids
    cond = " AND c.device_id::text = ANY(:ids)" if ids else ""
    params: dict[str, Any] = {"f": ctx.frm, "t": ctx.to}
    if ids:
        params["ids"] = ids
    changes = (await ctx.db.execute(text(f"""
        SELECT d.hostname, count(*) AS n, max(c.captured_at) AS last FROM device_configs c JOIN devices d ON d.id=c.device_id
        WHERE c.is_change AND c.validated AND c.captured_at BETWEEN :f AND :t{cond}
        GROUP BY d.hostname ORDER BY n DESC LIMIT 20"""), params)).fetchall()
    base = (await ctx.db.execute(text("""
        SELECT b.device_id::text AS device_id, d.hostname, b.name, b.config_type,
               (SELECT norm_hash FROM device_configs l WHERE l.device_id=b.device_id AND l.config_type=b.config_type
                ORDER BY l.captured_at DESC, l.id DESC LIMIT 1) <> c.norm_hash AS drifted
        FROM ncm_baselines b JOIN device_configs c ON c.id=b.version_id JOIN devices d ON d.id=b.device_id
        WHERE b.retired_at IS NULL ORDER BY d.hostname"""))).fetchall()
    allowed = {d["device_id"] for d in await _ncm_devices(ctx)}
    base = [b for b in base if b.device_id in allowed]
    return _section("ncm_changes", "Configuration Changes and Baseline Drift",
                    kpis=[{"label": "Changed configurations", "value": str(sum(c.n for c in changes)), "accent": "primary"},
                          {"label": "Baselines drifted", "value": f"{sum(bool(b.drifted) for b in base)} of {len(base)}",
                           "accent": "warning" if any(b.drifted for b in base) else "success"}],
                    tables=[{"title": "Most changed devices", "headers": ["Device", "Changes", "Last change"],
                             "styles": ["text", "num", "num"],
                             "rows": [[c.hostname, str(c.n), ctx.fmt_dt(c.last)] for c in changes]},
                            {"title": "Active baselines", "headers": ["Device", "Baseline", "Type", "State"],
                             "styles": ["text", "text", "text", "status"],
                             "rows": [[b.hostname, b.name, b.config_type, "drifted" if b.drifted else "in sync"] for b in base]}])


# ─── Registry + presets ─────────────────────────────────────────────────────

SectionFn = Callable[[SectionCtx], Awaitable[dict]]

# Sections that honour ``filters['device_ids']`` end to end. Accounts limited to
# a visibility scope may render only these (the API pins the filter to the
# devices they can see); every other section reads fleet-wide data.
DEVICE_SCOPABLE_SECTIONS = frozenset({
    "availability_kpis", "availability_trend", "device_availability",
    "ncm_protection", "ncm_breakdown", "ncm_outcomes", "ncm_at_risk",
    "ncm_compliance", "ncm_changes",
})

# NetFlow sections do not read device_ids: they are confined by the requester's
# own flow visibility (netflow.view plus the exporters their tag scope owns, see
# netflow_report.resolve_scope), and are withheld outright without netflow.view.
# That makes them safe for accounts limited to a visibility scope even though
# they are not device-filtered.
NETFLOW_SCOPED_SECTIONS = frozenset({
    "netflow_executive_summary", "traffic_kpis", "traffic_trend", "traffic_protocols",
    "traffic_ports", "traffic_talkers",
})

# What an account limited to a visibility scope may render at all.
SCOPE_SAFE_SECTIONS = DEVICE_SCOPABLE_SECTIONS | NETFLOW_SCOPED_SECTIONS

def _rows_option(default: int, maximum: int, help_: str = "How many rows to list.") -> dict:
    """The ``top_n`` option of a built-in list section: the schema of
    ``top_n_option`` (defined with the registry v2 helpers below this table)."""
    return {"key": "top_n", "label": "Rows", "type": "int", "default": default,
            "min": 1, "max": maximum, "help": help_}


SECTION_REGISTRY: dict[str, dict[str, Any]] = {
    # NCCM
    "ncm_protection": {"fn": _sec_ncm_protection, "title": "Configuration Protection", "category": "Configuration",
                       "description": "Fresh validated coverage of eligible devices, failures and restore evidence."},
    "ncm_breakdown": {"fn": _sec_ncm_breakdown, "title": "Coverage by Vendor and Site", "category": "Configuration",
                      "description": "Protected vs eligible devices per vendor and site, with denominators."},
    "ncm_outcomes": {"fn": _sec_ncm_outcomes, "title": "Backup Outcomes", "category": "Configuration",
                     "description": "Validated captures and failed attempts per day, failures by cause."},
    "ncm_at_risk": {"fn": _sec_ncm_at_risk, "title": "Devices Needing Action", "category": "Configuration",
                    "description": "Failed, blocked, stale and never-captured eligible devices.",
                    "options": [_rows_option(50, 500)]},
    "ncm_compliance": {"fn": _sec_ncm_compliance, "title": "Configuration Compliance", "category": "Configuration",
                       "description": "Policy pass/fail/exception status and most frequent failures."},
    "ncm_changes": {"fn": _sec_ncm_changes, "title": "Configuration Changes and Baseline Drift", "category": "Configuration",
                    "description": "Devices with the most changes and drift from approved baselines."},
    # Availability
    "availability_kpis": {"fn": _sec_availability_kpis, "title": "Availability Summary",
                          "category": "Availability",
                          "description": "Uptime %, SLA attainment, incidents and MTTR."},
    "availability_trend": {"fn": _sec_availability_trend, "title": "Availability Trend",
                           "category": "Availability",
                           "description": "Network-wide availability chart over the window."},
    "device_availability": {"fn": _sec_device_availability, "title": "Node Availability",
                            "category": "Availability",
                            "description": "Per-node availability, downtime and outages "
                                           "(supports node selection).",
                            "options": [_rows_option(200, 1000)]},
    "device_uptime": {"fn": _sec_device_uptime, "title": "Lowest-Availability Devices",
                      "category": "Availability",
                      "description": "Devices with the worst uptime and their latency.",
                      "options": [_rows_option(10, 100)]},
    "top_outages": {"fn": _sec_top_outages, "title": "Longest Outages", "category": "Availability",
                    "description": "Outage episodes ranked by duration.",
                    "options": [_rows_option(15, 100)]},
    "service_availability": {"fn": _sec_service_availability, "title": "Service Availability",
                             "category": "Availability",
                             "description": "Service checks uptime and expiring TLS certificates.",
                             "options": [_rows_option(25, 500)]},
    # Performance
    "network_performance": {"fn": _sec_network_performance, "title": "Network Device Performance",
                            "category": "Performance",
                            "description": "Highest p95 round-trip latency and packet loss per device.",
                            "options": [_rows_option(10, 100)]},
    "interface_utilization": {"fn": _sec_interface_utilization, "title": "Busiest Interfaces",
                              "category": "Performance",
                              "description": "Top interfaces by average throughput.",
                              "options": [_rows_option(10, 100)]},
    "server_performance": {"fn": _sec_server_performance, "title": "Server Performance",
                           "category": "Performance",
                           "description": "Agent-monitored servers ranked by CPU and memory load.",
                           "options": [_rows_option(15, 200)]},
    # Traffic
    "netflow_executive_summary": {"fn": _sec_netflow_executive, "title": "NetFlow Executive Summary",
                                  "category": "Traffic",
                                  "description": "Volume vs the previous period, capacity risks, coverage "
                                                 "limits, notable findings and recommended actions."},
    "traffic_kpis": {"fn": _sec_traffic_kpis, "title": "Traffic Summary", "category": "Traffic",
                     "description": "Total NetFlow volume, rate, packets and flow records."},
    "traffic_trend": {"fn": _sec_traffic_trend, "title": "Traffic Trend", "category": "Traffic",
                      "description": "Throughput over time from NetFlow, with collection gaps shown."},
    "traffic_protocols": {"fn": _sec_traffic_protocols, "title": "Traffic by Protocol",
                          "category": "Traffic", "description": "Protocol share of all bytes in scope."},
    "traffic_ports": {"fn": _sec_traffic_ports, "title": "Top Applications / Ports",
                      "category": "Traffic",
                      "description": "Applications ranked by volume, as a share of all traffic in scope."},
    "traffic_talkers": {"fn": _sec_traffic_talkers, "title": "Top Talkers",
                        "category": "Traffic",
                        "description": "Top sources and destinations by volume, with whole-scope shares."},
    # Applications
    "apm_services": {"fn": _sec_apm_services, "title": "Application Service Health",
                     "category": "Applications",
                     "description": "RED metrics and apdex per instrumented service.",
                     "options": [_rows_option(25, 200)]},
    "apm_errors": {"fn": _sec_apm_errors, "title": "Top Application Errors",
                   "category": "Applications", "description": "Most frequent exception groups.",
                   "options": [_rows_option(12, 100)]},
    "slo_attainment": {"fn": _sec_slo_attainment, "title": "SLO Attainment",
                       "category": "Applications",
                       "description": "Error-budget status for every SLO."},
    "synthetics": {"fn": _sec_synthetics, "title": "Synthetic Scenario Uptime",
                   "category": "Applications",
                   "description": "Scripted user-journey check results."},
    "usage_kpis": {"fn": _sec_usage_kpis, "title": "Usage Summary", "category": "Applications",
                   "description": "Requests, unique users, pages and error rate."},
    "usage_pages": {"fn": _sec_usage_pages, "title": "Top Pages", "category": "Applications",
                    "description": "Most visited routes with audience and latency.",
                    "options": [_rows_option(15, 100)]},
    "rum_audience": {"fn": _sec_rum_audience, "title": "Audience & Engagement",
                     "category": "Applications",
                     "description": "Browser sessions, users, page views and engagement depth."},
    "rum_experience": {"fn": _sec_rum_experience, "title": "Real-User Experience",
                       "category": "Applications",
                       "description": "Core Web Vitals (LCP/INP/CLS) at p75 with Google's verdicts."},
    "rum_top_pages": {"fn": _sec_rum_top_pages, "title": "Most-Visited Pages",
                      "category": "Applications",
                      "description": "Top pages by views with per-page speed and errors.",
                      "options": [_rows_option(15, 100)]},
    "rum_segments": {"fn": _sec_rum_segments, "title": "Audience Segments",
                     "category": "Applications",
                     "description": "Device and browser mix, and the experience each segment gets."},
    "rum_reliability": {"fn": _sec_rum_reliability, "title": "Reliability in the Browser",
                        "category": "Applications",
                        "description": "JavaScript errors, distinct issues and affected users."},
    "usage_users": {"fn": _sec_usage_users, "title": "Top Users", "category": "Applications",
                    "description": "Most active users by request volume.",
                    "options": [_rows_option(15, 100)]},
    # Capacity
    "capacity_filesystems": {"fn": _sec_capacity_filesystems, "title": "Filesystem Capacity",
                             "category": "Capacity",
                             "description": "Fullest filesystems across servers.",
                             "options": [_rows_option(20, 500)]},
    "capacity_interfaces": {"fn": _sec_capacity_interfaces, "title": "Link Capacity Headroom",
                            "category": "Capacity",
                            "description": "Interfaces closest to saturation by p95 utilization of known speed.",
                            "options": [_rows_option(15, 100)]},
    # Alerting
    "alert_kpis": {"fn": _sec_alert_kpis, "title": "Alert Summary", "category": "Alerting",
                   "description": "Alert counts by severity and MTTR."},
    "alert_trend": {"fn": _sec_alert_trend, "title": "Alert Volume Over Time",
                    "category": "Alerting", "description": "Alert volume chart."},
    "noisy_sources": {"fn": _sec_noisy_sources, "title": "Noisiest Alert Sources",
                      "category": "Alerting", "description": "Devices/rules generating the most alerts.",
                      "options": [_rows_option(10, 10, "How many sources to list (the ten noisiest are ranked).")]},
    "recent_alerts": {"fn": _sec_recent_alerts, "title": "Recent Alerts", "category": "Alerting",
                      "description": "Latest alerts in the window.",
                      "options": [_rows_option(25, 200)]},
    # Inventory
    "inventory_summary": {"fn": _sec_inventory_summary, "title": "Asset Summary",
                          "category": "Inventory", "description": "Fleet totals."},
    "inventory_breakdown": {"fn": _sec_inventory_breakdown, "title": "Device Breakdown",
                            "category": "Inventory",
                            "description": "Devices by type, vendor and location."},
    "inventory_servers": {"fn": _sec_inventory_servers, "title": "Server Inventory",
                          "category": "Inventory", "description": "Agent-monitored server list.",
                          "options": [_rows_option(100, 1000)]},
}

# ─── Registry v2: section metadata, options and registration ────────────────
#
# Every registry entry carries, besides fn/title/category/description:
#   scope     "device"  honours filters['device_ids'] end to end (safe for
#                       accounts limited to a visibility scope);
#             "netflow" confined by the requester's own flow visibility;
#             "fleet"   reads fleet-wide data (omitted for scoped accounts).
#   requires  module permissions the requester must ALL hold to see it
#             (system.admin passes); a section the requester may not see is
#             replaced by a 'restricted' stub, never rendered.
#   options   per-section parameters a template may set (top_n, metric, …):
#             [{key, label, type: int|number|select|bool, default, min?, max?,
#               choices?: [[value, label]], help?}]
#   semantics "window" (honours the report period) or "snapshot" (current state).
#   half      lays the section out at half width when paired.
# Section packs (app.services.report_packs) add entries with @register_section.

SCOPES = ("device", "netflow", "fleet")
OPTION_TYPES = ("int", "number", "select", "bool")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


def re_key(key: str) -> bool:
    """Template/section keys: lower_snake_case, at most 50 characters."""
    return isinstance(key, str) and bool(_KEY_RE.match(key))


def top_n_option(default: int = 10, maximum: int = 100) -> dict:
    return {"key": "top_n", "label": "Rows", "type": "int", "default": default,
            "min": 1, "max": maximum, "help": "How many rows to list."}


_LEGACY_META: dict[str, dict[str, Any]] = {
    **{sid: {"requires": ("ncm.view",)} for sid in (
        "ncm_protection", "ncm_breakdown", "ncm_at_risk", "ncm_compliance")},
    "ncm_outcomes": {"requires": ("ncm.view",)},
    "ncm_changes": {"requires": ("ncm.view",)},
    "availability_kpis": {"requires": ("devices.view",)},
    "availability_trend": {"requires": ("devices.view",)},
    "device_availability": {"requires": ("devices.view",)},
    "device_uptime": {"requires": ("devices.view",)},
    "top_outages": {"requires": ("devices.view",)},
    "service_availability": {"requires": ("service_checks.view",)},
    "network_performance": {"requires": ("devices.view",)},
    "interface_utilization": {"requires": ("devices.view",)},
    "server_performance": {"requires": ("servers.view",)},
    **{sid: {"requires": ("netflow.view",)} for sid in (
        "netflow_executive_summary", "traffic_kpis", "traffic_trend", "traffic_protocols",
        "traffic_ports", "traffic_talkers")},
    **{sid: {"requires": ("apm.view",)} for sid in (
        "apm_services", "apm_errors", "slo_attainment", "synthetics", "usage_kpis", "usage_pages",
        "rum_audience", "rum_experience", "rum_top_pages", "rum_segments", "rum_reliability",
        "usage_users")},
    "capacity_filesystems": {"requires": ("servers.view",), "semantics": "snapshot"},
    "capacity_interfaces": {"requires": ("devices.view",)},
    **{sid: {"requires": ("alerts.view",)} for sid in (
        "alert_kpis", "alert_trend", "noisy_sources", "recent_alerts")},
    "inventory_summary": {"requires": ("devices.view",), "semantics": "snapshot"},
    "inventory_breakdown": {"requires": ("devices.view",), "semantics": "snapshot"},
    "inventory_servers": {"requires": ("servers.view",), "semantics": "snapshot"},
}
for _sid in ("ncm_protection", "ncm_breakdown", "ncm_at_risk", "ncm_compliance"):
    _LEGACY_META[_sid]["semantics"] = "snapshot"


def _finalise_entry(sid: str, entry: dict[str, Any]) -> dict[str, Any]:
    meta = _LEGACY_META.get(sid, {})
    if "scope" not in entry:
        entry["scope"] = ("device" if sid in DEVICE_SCOPABLE_SECTIONS else
                          "netflow" if sid in NETFLOW_SCOPED_SECTIONS else "fleet")
    entry.setdefault("requires", tuple(meta.get("requires", ())))
    entry.setdefault("options", [])
    entry.setdefault("semantics", meta.get("semantics", "window"))
    entry.setdefault("half", False)
    entry["requires"] = tuple(entry["requires"])
    if entry["scope"] not in SCOPES:
        raise ValueError(f"section {sid}: unknown scope {entry['scope']!r}")
    for o in entry["options"]:
        if o.get("type") not in OPTION_TYPES or not o.get("key"):
            raise ValueError(f"section {sid}: invalid option {o!r}")
    return entry


for _sid, _entry in SECTION_REGISTRY.items():
    _finalise_entry(_sid, _entry)


def _rebuild_scope_sets() -> None:
    global DEVICE_SCOPABLE_SECTIONS, NETFLOW_SCOPED_SECTIONS, SCOPE_SAFE_SECTIONS
    DEVICE_SCOPABLE_SECTIONS = frozenset(s for s, e in SECTION_REGISTRY.items() if e["scope"] == "device")
    NETFLOW_SCOPED_SECTIONS = frozenset(s for s, e in SECTION_REGISTRY.items() if e["scope"] == "netflow")
    SCOPE_SAFE_SECTIONS = DEVICE_SCOPABLE_SECTIONS | NETFLOW_SCOPED_SECTIONS


def register_section(sid: str, *, title: str, category: str, description: str,
                     scope: str = "fleet", requires: tuple[str, ...] | list[str] = (),
                     options: list[dict] | None = None, semantics: str = "window",
                     half: bool = False):
    """Decorator for section packs: ``@register_section("cpu_top", ...)``.

    A device-scoped section must read ``ctx.device_ids`` in every query and
    treat scoping.NO_MATCH_ID as matching nothing; a netflow-scoped section
    must go through netflow_report.resolve_scope(ctx)."""
    def deco(fn: SectionFn) -> SectionFn:
        if sid in SECTION_REGISTRY:
            raise ValueError(f"duplicate report section id {sid!r}")
        SECTION_REGISTRY[sid] = _finalise_entry(sid, {
            "fn": fn, "title": title, "category": category, "description": description,
            "scope": scope, "requires": tuple(requires), "options": list(options or []),
            "semantics": semantics, "half": half,
        })
        _rebuild_scope_sets()
        return fn
    return deco


def normalize_options(sid: str, raw: Optional[dict]) -> dict[str, Any]:
    """Validate a template's options for section ``sid`` against its schema:
    unknown keys are dropped, numbers clamped, selects checked; missing keys
    take their defaults. Never raises."""
    spec = SECTION_REGISTRY.get(sid) or {}
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for o in spec.get("options") or ():
        key, typ, default = o["key"], o["type"], o.get("default")
        val = raw.get(key, default)
        try:
            if typ == "int":
                val = int(val)
            elif typ == "number":
                val = float(val)
            elif typ == "bool":
                val = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")
            elif typ == "select":
                allowed = [c[0] for c in o.get("choices") or ()]
                val = val if val in allowed else default
        except (TypeError, ValueError):
            val = default
        if typ in ("int", "number") and val is not None:
            if o.get("min") is not None:
                val = max(o["min"], val)
            if o.get("max") is not None:
                val = min(o["max"], val)
        out[key] = val
    return out


def option_errors(sid: str, raw: Optional[dict]) -> list[str]:
    """Human-readable problems with a template's options (for save-time validation)."""
    spec = SECTION_REGISTRY.get(sid)
    if spec is None:
        return [f"Unknown section {sid!r}"]
    errs: list[str] = []
    known = {o["key"]: o for o in spec.get("options") or ()}
    for key, val in (raw or {}).items():
        o = known.get(key)
        if o is None:
            errs.append(f"{spec['title']}: unknown option {key!r}")
            continue
        typ = o["type"]
        if typ == "select" and val not in [c[0] for c in o.get("choices") or ()]:
            errs.append(f"{spec['title']}: {o['label']} must be one of "
                        f"{', '.join(str(c[0]) for c in o.get('choices') or ())}")
        elif typ in ("int", "number"):
            try:
                num = float(val)
            except (TypeError, ValueError):
                errs.append(f"{spec['title']}: {o['label']} must be a number")
                continue
            if (o.get("min") is not None and num < o["min"]) or (o.get("max") is not None and num > o["max"]):
                errs.append(f"{spec['title']}: {o['label']} must be between {o.get('min')} and {o.get('max')}")
        elif typ == "bool" and not isinstance(val, bool):
            errs.append(f"{spec['title']}: {o['label']} must be true or false")
    return errs


def section_entries(items: list) -> list[dict[str, Any]]:
    """Normalise a template's section list (ids or {id, title?, options?})."""
    out = []
    for it in items or ():
        if isinstance(it, str):
            out.append({"id": it, "title": None, "options": {}})
        elif isinstance(it, dict) and isinstance(it.get("id"), str):
            title = it.get("title")
            out.append({"id": it["id"],
                        "title": title.strip()[:120] if isinstance(title, str) and title.strip() else None,
                        "options": it.get("options") if isinstance(it.get("options"), dict) else {}})
    return out


REPORT_PRESETS: dict[str, dict[str, Any]] = {
    "availability": {
        "title": "Availability & SLA Report",
        "description": "Uptime, SLA attainment, outages and service availability.",
        "category": "Operations",
        "sections": ["availability_kpis", "availability_trend", "device_availability",
                     "top_outages", "service_availability"],
    },
    "performance": {
        "title": "Performance Report",
        "description": "Network latency, interface throughput and server load.",
        "category": "Operations",
        "sections": ["network_performance", "interface_utilization", "server_performance"],
    },
    "traffic": {
        "title": "Traffic Analysis Report",
        "description": "NetFlow volumes, protocols and top applications.",
        "category": "Operations",
        "sections": ["traffic_kpis", "traffic_trend", "traffic_protocols", "traffic_ports",
                     "traffic_talkers"],
    },
    "netflow_executive": {
        "title": "NetFlow Executive Summary",
        "description": ("Management view of flow traffic: volume against the previous period, top "
                        "capacity risks, coverage and data-quality limits, notable findings, top "
                        "applications and talkers, and recommended actions."),
        "category": "Operations",
        "sections": ["netflow_executive_summary", "traffic_trend", "traffic_ports", "traffic_talkers"],
    },
    "alerts": {
        "title": "Alert Insights Report",
        "description": "Alert volumes, noisiest sources and response times.",
        "category": "Operations",
        "sections": ["alert_kpis", "alert_trend", "noisy_sources", "recent_alerts"],
    },
    "capacity": {
        "title": "Capacity Planning Report",
        "description": "Filesystem and link capacity headroom.",
        "category": "Operations",
        "sections": ["capacity_filesystems", "capacity_interfaces", "server_performance"],
    },
    "apm_performance": {
        "title": "Application Performance Report",
        "description": "Service golden signals, SLOs, errors and synthetic checks.",
        "category": "Applications",
        "sections": ["apm_services", "slo_attainment", "apm_errors", "synthetics"],
    },
    "usage": {
        "title": "Usage Analytics Report",
        "description": "Application traffic, top pages and most active users.",
        "category": "Applications",
        "sections": ["usage_kpis", "usage_pages", "usage_users"],
    },
    "rum_usage": {
        "title": "Digital Experience Report",
        "description": ("How real people experienced the application in their browser — audience, "
                        "engagement, Core Web Vitals and reliability."),
        "category": "Applications",
        "sections": ["rum_audience", "rum_experience", "rum_top_pages",
                     "rum_segments", "rum_reliability"],
    },
    "ncm_protection": {
        "title": "Configuration Protection Report",
        "description": "NCCM backup coverage, outcomes, devices needing action, compliance and baseline drift.",
        "category": "Operations",
        "sections": ["ncm_protection", "ncm_breakdown", "ncm_outcomes", "ncm_at_risk", "ncm_compliance", "ncm_changes"],
    },
    "inventory": {
        "title": "Inventory Report",
        "description": "Devices, interfaces, sensors and servers on record.",
        "category": "Assets",
        "sections": ["inventory_summary", "inventory_breakdown", "inventory_servers"],
    },
}


ORPHANED_NOTE = ("Not included: this report's owner no longer exists or is deactivated. "
                 "Recreate the schedule under an active account.")


async def requester_permissions(db: AsyncSession, requester: Any) -> Optional[frozenset[str]]:
    """The requester's effective permissions, or None for "unrestricted" (the
    appliance itself). A schedule whose owner no longer exists, or any
    requester without a role, fails closed: no module permissions."""
    if requester is None:
        return None
    role = getattr(requester, "role", None)
    if not role:
        return frozenset()
    try:
        from app.core.security import get_role_permissions
        perms = await get_role_permissions(db, role)
    except Exception:
        logger.exception("report permission lookup failed")
        return frozenset()
    return frozenset(perms or ())


def may_view_section(perms: Optional[frozenset[str]], sid: str) -> bool:
    if perms is None:
        return True
    from app.core.permissions import SUPERUSER_PERMISSION
    if SUPERUSER_PERMISSION in perms:
        return True
    spec = SECTION_REGISTRY.get(sid) or {}
    return all(p in perms for p in spec.get("requires") or ())


def restricted_section(sid: str, title: Optional[str] = None, orphaned: bool = False) -> dict:
    spec = SECTION_REGISTRY.get(sid) or {}
    need = ", ".join(spec.get("requires") or ())
    note = ORPHANED_NOTE if orphaned else \
        f"Not included: viewing this section requires the {need} permission."
    sec = _section(sid, title or spec.get("title") or sid, notes=[note])
    sec["restricted"] = True
    sec["category"] = spec.get("category")
    return sec


def _failure_note(exc: BaseException) -> str:
    """Reader-facing reason a section failed — never the raw exception text
    (it can carry SQL or connection details into shared report links)."""
    mod = type(exc).__module__ or ""
    if mod.startswith("clickhouse_connect") or getattr(exc, "status_code", None) == 503:
        return ("Metrics for this section are temporarily unavailable: the time-series database did "
                "not respond. Generate the report again shortly; other sections are unaffected.")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "This section took too long to generate and was skipped. Try a shorter period."
    return (f"This section could not be generated ({type(exc).__name__}). Other sections are "
            "unaffected; the appliance log has details.")


async def build_sections(db: AsyncSession, section_ids: list,
                         frm: datetime, to: datetime,
                         filters: Optional[dict] = None,
                         requester: Any = None,
                         branding: Any = None) -> list[dict]:
    """Run the given sections; a failing section becomes an error note, never
    a failed report.

    ``section_ids`` holds section ids or template entries ``{id, title?,
    options?}`` (a section may appear more than once with different options).
    ``requester`` scopes NetFlow sections to what that user may see and hides
    sections whose module permission the user lacks (None = the appliance
    itself). Each section runs in its own savepoint so one failing query can
    not blank the sections after it."""
    ctx = SectionCtx(db, frm, to, filters, requester=requester)
    if branding is None:
        try:
            from app.services.branding import load_branding
            branding = await load_branding(db, with_logos=False)
        except Exception:
            logger.debug("branding unavailable for report sections", exc_info=True)
            branding = None
    ctx.branding = branding
    perms = await requester_permissions(db, requester)
    out: list[dict] = []
    for item in section_entries(section_ids):
        sid = item["id"]
        entry = SECTION_REGISTRY.get(sid)
        if not entry:
            continue
        if not may_view_section(perms, sid):
            out.append(restricted_section(sid, item["title"],
                                          orphaned=requester is not None and not getattr(requester, "role", None)))
            continue
        ctx.section_id = sid
        ctx.section_options = normalize_options(sid, item["options"])
        nested = getattr(db, "begin_nested", None)
        try:
            if nested is not None:
                async with nested():
                    sec = await entry["fn"](ctx)
            else:
                sec = await entry["fn"](ctx)
        except Exception as exc:
            logger.exception("report section %s failed", sid)
            sec = _section(sid, item["title"] or entry["title"],
                           notes=[_failure_note(exc)])
            sec["error"] = True
        if item["title"]:
            sec["title"] = item["title"]
        sec.setdefault("category", entry.get("category"))
        sec["section_id"] = sid
        if ctx.section_options:
            sec["options"] = dict(ctx.section_options)
        out.append(sec)
    ctx.section_id, ctx.section_options = "", {}
    return out


# ─── Renderers ──────────────────────────────────────────────────────────────

def sections_to_json(sections: list[dict]) -> list[dict]:
    out = []
    for s in sections:
        j = dict(s)
        charts = []
        for c in s.get("charts") or []:
            entry: dict[str, Any] = {"title": c.get("title") or ""}
            if c.get("series"):
                # Native series render client-side; skip the heavy PNG payload.
                entry["series"] = c["series"]
            elif c.get("png"):
                entry["data_uri"] = ("data:image/png;base64,"
                                     + base64.b64encode(c["png"]).decode())
            charts.append(entry)
        j["charts"] = charts
        out.append(j)
    return out


from html import escape as _html_escape  # noqa: E402

from app.services.report_service import (  # noqa: E402
    COLOR_PRIMARY,
    _png_size,
    brand_tokens,
    classification_rgb,
    infer_column_styles,
    pill_kind,
    rgb_to_hex,
)

# Rows a table shows in the HTML document (beyond this an explicit note
# points to the XLSX export) and in the PDF (full width / half width).
HTML_MAX_ROWS = 500
PDF_MAX_ROWS = 500
PDF_MAX_ROWS_HALF = 40
# Chart image sizes (mm). One size per layout, identical for HTML, PDF, A4
# and Letter, so a chart drawn for the shared HTML and the PDF attachment of
# the same run is rendered once (report_charts caches by content and size).
CHART_W_FULL = 178.0
CHART_W_HALF = 85.0

_ACCENT_CLASSES = ("primary", "success", "warning", "danger", "info", "neutral")
_NOT_INCLUDED = re.compile(r"^\s*not included:\s*", re.IGNORECASE)


def _meta_branding(meta: dict):
    """The meta's Branding, or one rebuilt from its plain values."""
    from app.services.branding import Branding
    b = meta.get("branding")
    if isinstance(b, Branding):
        return b
    return Branding(company_name=meta.get("company_name") or "",
                    timezone=meta.get("timezone") or "UTC",
                    brand_color=meta.get("brand_color") or "#4F6BF6",
                    paper_size=meta.get("paper_size") or "A4")


def _meta_rgb(meta: dict) -> tuple:
    rgb = meta.get("brand_rgb")
    if rgb and len(rgb) == 3:
        return tuple(int(v) for v in rgb)
    if meta.get("brand_color"):
        from app.services.branding import hex_to_rgb
        return hex_to_rgb(meta["brand_color"])
    return COLOR_PRIMARY


def _chart_height(series: dict, half: bool) -> float:
    kind = (series or {}).get("kind")
    if kind == "donut":
        return 50.0 if half else 58.0
    return 50.0 if half else 56.0


def _chart_image(c: dict, branding: Any, half: bool) -> Optional[bytes]:
    """A chart's PNG: the section's own image when it has one, else the
    branded rendering of its series (None when neither is usable)."""
    if c.get("png"):
        return c["png"]
    series = c.get("series")
    if isinstance(series, dict):
        try:
            from app.services.report_charts import chart_png
            return chart_png(series, branding=branding,
                             width_mm=CHART_W_HALF if half else CHART_W_FULL,
                             height_mm=_chart_height(series, half))
        except Exception:
            logger.exception("report chart rendering failed")
    return None


def _restricted_text(s: dict) -> str:
    notes = [n for n in (s.get("notes") or []) if n]
    text_ = " ".join(_NOT_INCLUDED.sub("", str(n)) for n in notes) or \
        "You do not have permission to view this section."
    return text_[:1].upper() + text_[1:]


def _is_error_note(s: dict, note: Any) -> bool:
    return bool(s.get("error")) or "could not be generated" in str(note)


# ─── HTML ───────────────────────────────────────────────────────────────────

_HTML_CSS = """
:root{--ink:#0F172A;--body:#334155;--muted:#64748B;--faint:#94A3B8;--line:#E2E8F0;
--line-soft:#EBF0F6;--panel:#F7F9FC;--ok:#16803D;--ok-bg:#E8F6EE;--bad:#B91C1C;--bad-bg:#FDECEC;
--warn:#B45309;--warn-bg:#FEF3E2;--info:#075985;--info-bg:#E8F4FB;--neutral:#64748B;
--neutral-bg:#EEF2F7;--k-success:#16A34A;--k-warning:#D97706;--k-danger:#DC2626;
--k-info:#0891B2;--k-neutral:#64748B;color-scheme:light}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#EEF1F6;color:var(--ink);
font:14px/1.5 "Segoe UI",system-ui,-apple-system,"Liberation Sans","Helvetica Neue",Arial,sans-serif}
.doc{max-width:980px;margin:24px auto;background:#fff;border-radius:12px;overflow:hidden;
box-shadow:0 1px 3px rgba(15,23,42,.06),0 8px 28px rgba(15,23,42,.08)}
.cover{background:var(--ground);color:#fff;padding:26px 40px 30px;border-top:4px solid var(--brand)}
.cover-top{display:flex;align-items:center;justify-content:space-between;gap:16px;
padding-bottom:18px;border-bottom:1px solid var(--ground-panel)}
.brand{display:flex;align-items:center;min-height:40px;min-width:0}
.brand img{display:block;height:44px;width:auto;max-width:220px;object-fit:contain;
object-position:left center}
.brand .tile{background:#fff;border-radius:8px;padding:6px 10px;display:inline-flex}
.brand .tile img{height:32px;max-width:200px}
.brand .name{font-size:18px;font-weight:700;overflow-wrap:anywhere}
.doc-label{font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
color:var(--ground-label);text-align:right}
.cover-body{padding:34px 0 4px}
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.12em;
text-transform:uppercase;padding:3px 11px;border-radius:999px;background:var(--class-bg);
color:#fff;margin:0 0 14px}
.cover h1{font-size:30px;line-height:1.2;margin:0 0 10px;font-weight:700;letter-spacing:-.01em;
overflow-wrap:anywhere}
.cover .lede{color:var(--ground-text);font-size:15px;max-width:660px;margin:0}
.facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 28px;margin:28px 0 0;
padding-top:18px;border-top:1px solid var(--ground-panel)}
.facts dt{font-size:10.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:var(--ground-label);margin:0 0 3px}
.facts dd{margin:0;font-size:13.5px;color:#fff;overflow-wrap:anywhere}
.toc{padding:26px 40px 4px}
.toc h2,.disclaimer h2{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:var(--muted);margin:0 0 8px}
.toc ol{list-style:none;margin:0;padding:0;columns:2;column-gap:32px}
.toc li{break-inside:avoid}
.toc a{display:flex;gap:10px;padding:5px 0;color:var(--ink);text-decoration:none;
border-bottom:1px solid var(--line-soft);font-size:13.5px}
.toc a:hover{color:var(--accent)}
.toc .n{font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums}
.toc .off{color:var(--faint)}
.content{padding:4px 40px 30px}
.section{margin-top:32px;min-width:0}
.sec-head{margin-bottom:12px;padding-bottom:9px;border-bottom:1px solid var(--line);position:relative}
.sec-head:after{content:"";position:absolute;left:0;bottom:-1px;width:36px;height:2px;
background:var(--accent)}
.eyebrow{font-size:10.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
color:var(--faint);margin:0 0 3px}
.section h2{font-size:19px;line-height:1.3;margin:0;display:flex;gap:10px;align-items:center}
.sec-num{flex:none;font-size:12px;font-weight:700;line-height:1;padding:5px 7px;border-radius:6px;
background:var(--accent);color:var(--on-accent);font-variant-numeric:tabular-nums}
.desc{color:var(--muted);font-size:13.5px;margin:6px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:16px 0}
.pair .kpis{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}
.kpi{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fff;
break-inside:avoid;min-width:0}
.kpi-label{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted)}
.kpi-value{font-size:23px;font-weight:700;line-height:1.25;margin-top:4px;overflow-wrap:anywhere}
.kpi-bar{width:22px;height:3px;border-radius:2px;margin-top:8px;background:var(--accent)}
.kpi--success .kpi-bar{background:var(--k-success)}.kpi--warning .kpi-bar{background:var(--k-warning)}
.kpi--danger .kpi-bar{background:var(--k-danger)}.kpi--info .kpi-bar{background:var(--k-info)}
.kpi--neutral .kpi-bar{background:var(--k-neutral)}
.kpi-sub{font-size:12px;color:var(--muted);margin-top:6px}
figure.chart{margin:16px 0;break-inside:avoid}
figure.chart figcaption{font-size:12.5px;font-weight:600;margin:0 0 6px}
figure.chart img{display:block;width:100%;height:auto}
.table-block{margin:16px 0}
.table-title{font-size:12.5px;font-weight:600;margin:0 0 6px}
.table-scroll{overflow-x:auto}
table.data{width:100%;border-collapse:collapse;font-size:12.5px}
table.data th{text-align:left;font-size:10.5px;font-weight:700;letter-spacing:.06em;
text-transform:uppercase;color:#475569;padding:7px 10px;border-bottom:1.5px solid #475569;
white-space:nowrap}
table.data td{padding:6px 10px;border-bottom:1px solid var(--line-soft);vertical-align:middle;
color:var(--body)}
table.data tbody tr:nth-child(even) td{background:#FBFCFE}
table.data .r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.data .mono{font-family:"DejaVu Sans Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:11.5px}
table.data .dash,table.data .empty{color:var(--faint)}
table.data .empty{text-align:center;padding:14px}
.pill{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.06em;
text-transform:uppercase;padding:2px 8px;border-radius:999px;white-space:nowrap}
.pill--success{color:var(--ok);background:var(--ok-bg)}.pill--danger{color:var(--bad);background:var(--bad-bg)}
.pill--warning{color:var(--warn);background:var(--warn-bg)}.pill--info{color:var(--info);background:var(--info-bg)}
.pill--neutral{color:var(--neutral);background:var(--neutral-bg)}
.pct{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end}
.pct .bar{display:inline-block;width:56px;height:5px;background:var(--line-soft);border-radius:3px;
overflow:hidden}
.pct .bar i{display:block;height:100%;background:var(--accent);border-radius:3px}
.cap-note{font-size:12px;color:var(--muted);margin:6px 0 0}
.callout{border-radius:8px;padding:10px 14px;margin:10px 0;font-size:13px;break-inside:avoid}
.callout--info{background:var(--panel);border-left:3px solid var(--accent);color:var(--body)}
.callout--error{background:var(--bad-bg);border-left:3px solid var(--bad);color:#7F1D1D}
.callout strong{display:block;font-size:12px;letter-spacing:.02em}
.locked{display:flex;gap:12px;align-items:flex-start;border:1.5px dashed #CBD5E1;
background:var(--panel);border-radius:10px;padding:14px 16px;color:var(--muted);font-size:13px;
margin-top:12px;break-inside:avoid}
.locked svg{flex:none;margin-top:2px}
.locked .t{font-size:10.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.locked p{margin:2px 0 0}
.section--restricted .sec-head h2{color:var(--muted)}
.section--restricted .sec-num{background:var(--neutral-bg);color:var(--muted)}
.pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 32px}
.pair>.section+.section{border-left:1px solid var(--line);padding-left:32px;margin-left:-16px}
.pair>.section:first-child{padding-right:16px}
.empty-state{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:14px;
text-align:center;color:var(--muted);font-size:13px;margin:12px 0}
.disclaimer{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);
font-size:12px}
.disclaimer p{margin:0 0 6px}
.doc-foot{display:flex;justify-content:space-between;align-items:flex-end;gap:8px 16px;flex-wrap:wrap;
border-top:1px solid var(--line);padding:14px 40px;color:var(--muted);font-size:12px}
.doc-foot .sub{color:var(--faint);font-size:11px}
.doc-foot .credit{color:var(--faint);font-size:11px;white-space:nowrap}
@media (max-width:720px){
.doc{margin:0;border-radius:0}
.cover,.toc,.content,.doc-foot{padding-left:16px;padding-right:16px}
.cover-top{flex-wrap:wrap}
.facts{grid-template-columns:1fr}
.toc ol{columns:1}
.pair{grid-template-columns:1fr}
.pair>.section+.section{border-left:0;padding-left:0;margin-left:0}
.pair>.section:first-child{padding-right:0}
.cover h1{font-size:24px}
}
@media print{
body{background:#fff;font-size:10pt}
.doc{margin:0;max-width:none;box-shadow:none;border-radius:0;overflow:visible}
.cover,.badge,.sec-num,.kpi-bar,.pill,.pct .bar,.pct .bar i,.callout,.locked,.sec-head:after,
table.data tbody tr:nth-child(even) td{-webkit-print-color-adjust:exact;print-color-adjust:exact}
.cover{padding:20px 24px 24px}
.toc,.content,.doc-foot{padding-left:0;padding-right:0}
.toc{padding-top:18px}
.section{margin-top:22px}
.sec-head{break-inside:avoid;break-after:avoid}
.section h2,.table-title,figure.chart figcaption,.kpis{break-after:avoid}
.kpis{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))}
.kpis,.kpi,figure.chart,.callout,.locked,.empty-state,.disclaimer,.pair{break-inside:avoid}
thead{display:table-header-group}
tr{break-inside:avoid}
.table-scroll{overflow:visible}
figure.chart img{max-width:178mm;margin:0 auto}
.pair{gap:0 20px}
.pair>.section+.section{padding-left:20px;margin-left:-10px}
.pair>.section:first-child{padding-right:10px}
.pair figure.chart img{max-width:85mm}
.toc a{border-bottom-color:var(--line)}
a{color:inherit;text-decoration:none}
.doc-foot{break-inside:avoid;padding-top:10px}
}
"""

_LOCK_SVG = ('<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
             '<rect x="3" y="7" width="10" height="7.5" rx="1.6" fill="#94A3B8"/>'
             '<path d="M5.2 7V5.2a2.8 2.8 0 0 1 5.6 0V7" fill="none" stroke="#94A3B8" '
             'stroke-width="1.6"/></svg>')


def _esc(v: Any) -> str:
    """HTML-escape text and attribute values (quotes included)."""
    return _html_escape("" if v is None else str(v), quote=True)


def _css_str(v: Any) -> str:
    """A CSS string literal: quotes, backslashes, newlines and angle
    brackets escaped so the value can never close the string or the
    <style> element."""
    out = []
    for ch in str(v or ""):
        if ch in '"\\<>&' or ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\{ord(ch):X} ")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _logo_uri(meta: dict, uri_key: str, bytes_key: str) -> Optional[str]:
    """A data: URI for a logo with its real (sniffed) MIME type."""
    uri = meta.get(uri_key)
    if isinstance(uri, str) and re.match(r"^data:image/(png|jpeg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$",
                                         uri):
        return uri
    data = meta.get(bytes_key)
    if isinstance(data, (bytes, bytearray)) and data:
        from app.services.branding import sniff_logo
        mime = sniff_logo(bytes(data))
        if mime:
            return f"data:{mime};base64,{base64.b64encode(bytes(data)).decode()}"
    return None


def _html_root_vars(meta: dict) -> str:
    t = brand_tokens(_meta_rgb(meta))
    pairs = {
        "--brand": t["brand"], "--accent": t["accent"], "--on-accent": t["on_accent"],
        "--on-brand": t["on_brand"], "--tint": t["tint"], "--ground": t["ground"],
        "--ground-panel": t["ground_panel"], "--ground-label": t["ground_label"],
        "--ground-text": t["ground_text"],
        "--class-bg": classification_rgb(meta.get("confidentiality")),
    }
    return ":root{" + ";".join(f"{k}:{rgb_to_hex(v)}" for k, v in pairs.items()) + "}"


def _html_page_css(meta: dict) -> str:
    """@page rules: paper size, margins, running header/footer, page counter."""
    size = "letter" if str(meta.get("paper_size") or "").lower() == "letter" else "A4"
    company = meta.get("company_name") or "ZenPlus"
    head = f"{company} · {meta.get('title') or 'Report'}"
    foot = meta.get("footer_text") or company
    tz = _tz_note(meta)
    cls = (meta.get("confidentiality") or "").upper()
    box = "font-family:\"Liberation Sans\",Arial,sans-serif;font-size:7.5pt;color:#64748B"
    rules = [
        f"@top-left{{content:{_css_str(head)};{box}}}",
        f"@bottom-left{{content:{_css_str(foot)};{box}}}",
        f"@bottom-center{{content:{_css_str(tz)};{box}}}",
        f"@bottom-right{{content:\"Page \" counter(page) \" of \" counter(pages);{box}}}",
    ]
    if cls:
        rgb = rgb_to_hex(classification_rgb(meta.get("confidentiality")))
        rules.append(f"@top-right{{content:{_css_str(cls)};{box};color:{rgb};font-weight:700}}")
    return (f"@page{{size:{size};margin:16mm 14mm 18mm 14mm;{''.join(rules)}}}"
            "@page:first{@top-left{content:none}}")


def _tz_note(meta: dict) -> str:
    tz_label = meta.get("tz_label") or "UTC"
    zone = meta.get("timezone") or "UTC"
    if zone in ("UTC", "Etc/UTC") or zone == tz_label:
        return f"All times {tz_label}"
    return f"All times {tz_label} ({zone})"


def _html_cell(val: Any, style: str) -> tuple[str, str]:
    """(td class, inner html) for one cell."""
    raw = "" if val is None else str(val)
    stripped = raw.strip()
    if style in ("status", "severity"):
        if stripped.lower() in ("", "-", "—", "–", "n/a", "none"):
            return "", '<span class="dash">—</span>'
        return "", f'<span class="pill pill--{pill_kind(stripped)}">{_esc(stripped)}</span>'
    if not stripped:
        return ("r" if style in ("num", "pct", "pct-bar") else ""), '<span class="dash">—</span>'
    if style == "pct-bar":
        m = re.match(r"^\s*(-?\d[\d,]*(?:\.\d+)?)\s*%", stripped)
        if m:
            try:
                pct = min(max(float(m.group(1).replace(",", "")), 0.0), 100.0)
            except ValueError:
                pct = None
            if pct is not None:
                return "r", (f'<span class="pct"><span>{_esc(stripped)}</span>'
                             f'<span class="bar"><i style="width:{pct:.1f}%"></i></span></span>')
        return "r", _esc(stripped)
    if style in ("num", "pct"):
        return "r", _esc(stripped)
    if style == "mono":
        return "mono", _esc(stripped)
    return "", _esc(raw)


def _html_table(t: dict) -> str:
    rows = [r if isinstance(r, (list, tuple)) else [r] for r in (t.get("rows") or [])]
    headers = [str(h) for h in (t.get("headers") or [])]
    if not headers and rows:
        headers = [""] * max(len(r) for r in rows)  # header-less table: size from the rows
    styles = infer_column_styles(headers, rows, t.get("styles"))
    title = f'<div class="table-title">{_esc(t["title"])}</div>' if t.get("title") else ""
    right = ' class="r"'
    head = "".join(
        f'<th scope="col"{right if styles[i] in ("num", "pct", "pct-bar") else ""}>'
        f"{_esc(h)}</th>" for i, h in enumerate(headers))
    out: list[str] = []
    for row in rows[:HTML_MAX_ROWS]:
        cells = []
        for i in range(len(headers)):
            cls, inner = _html_cell(row[i] if i < len(row) else None, styles[i])
            cells.append(f'<td class="{cls}">{inner}</td>' if cls else f"<td>{inner}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    if not rows:
        out.append(f'<tr><td class="empty" colspan="{max(len(headers), 1)}">'
                   "No records in this window.</td></tr>")
    cap = ""
    if len(rows) > HTML_MAX_ROWS:
        cap = (f'<p class="cap-note">Showing the first {HTML_MAX_ROWS:,} of {len(rows):,} rows. '
               "The Excel (XLSX) export lists every row.</p>")
    thead = f"<thead><tr>{head}</tr></thead>" if any(headers) else ""
    return (f'<div class="table-block">{title}<div class="table-scroll"><table class="data">'
            f"{thead}<tbody>{''.join(out)}</tbody></table></div>{cap}</div>")


def _html_section(s: dict, number: int, branding: Any, half: bool) -> str:
    sid = f"s-{number:02d}"
    restricted = bool(s.get("restricted"))
    classes = "section" + (" section--restricted" if restricted else "") + \
        (" section--error" if s.get("error") else "")
    parts = [f'<section class="{classes}" id="{sid}" aria-labelledby="{sid}-h">'
             '<div class="sec-head">']
    if s.get("category"):
        parts.append(f'<div class="eyebrow">{_esc(s["category"])}</div>')
    parts.append(f'<h2 id="{sid}-h"><span class="sec-num">{number:02d}</span>'
                 f'<span>{_esc(s.get("title") or "Section")}</span></h2>')
    if s.get("description") and not restricted:
        parts.append(f'<p class="desc">{_esc(s["description"])}</p>')
    parts.append("</div>")
    if restricted:
        parts.append(f'<div class="locked" role="note">{_LOCK_SVG}<div><div class="t">Not included</div>'
                     f"<p>{_esc(_restricted_text(s))}</p></div></div></section>")
        return "".join(parts)

    kpis = s.get("kpis") or []
    charts = s.get("charts") or []
    tables = s.get("tables") or []
    notes = s.get("notes") or []
    if kpis:
        parts.append('<div class="kpis">')
        for k in kpis:
            acc = k.get("accent") if k.get("accent") in _ACCENT_CLASSES else "primary"
            sub = f'<div class="kpi-sub">{_esc(k["subtitle"])}</div>' if k.get("subtitle") else ""
            parts.append(f'<div class="kpi kpi--{acc}"><div class="kpi-label">{_esc(k.get("label"))}</div>'
                         f'<div class="kpi-value">{_esc(k.get("value") if k.get("value") not in (None, "") else "—")}</div>'
                         f'<div class="kpi-bar"></div>{sub}</div>')
        parts.append("</div>")
    for c in charts:
        uri = c.get("data_uri") if isinstance(c.get("data_uri"), str) and \
            c["data_uri"].startswith("data:image/png;base64,") else None
        if uri is None:
            png = _chart_image(c, branding, half)
            uri = "data:image/png;base64," + base64.b64encode(png).decode() if png else None
        cap = f"<figcaption>{_esc(c['title'])}</figcaption>" if c.get("title") else ""
        alt = _esc(c.get("title") or f"{s.get('title') or 'Section'} chart")
        if uri:
            parts.append(f'<figure class="chart">{cap}<img src="{uri}" alt="{alt}"></figure>')
        else:
            parts.append(f'<figure class="chart">{cap}<div class="empty-state">Chart unavailable.</div></figure>')
    for t in tables:
        if not t.get("rows") and notes:
            continue  # the section's note already explains the absence of data
        parts.append(_html_table(t))
    for n in notes:
        if _is_error_note(s, n):
            parts.append(f'<div class="callout callout--error" role="note"><strong>Section unavailable'
                         f"</strong>{_esc(n)}</div>")
        else:
            parts.append(f'<div class="callout callout--info" role="note">{_esc(n)}</div>')
    if not kpis and not charts and not tables and not notes:
        parts.append('<div class="empty-state">No data available for this period.</div>')
    parts.append("</section>")
    return "".join(parts)


def _layout(sections: list[dict]) -> list[tuple[dict, ...]]:
    """Group consecutive half-width sections into side-by-side pairs."""
    out: list[tuple[dict, ...]] = []
    i = 0
    while i < len(sections):
        s = sections[i]
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        if s.get("half") and nxt is not None and nxt.get("half"):
            out.append((s, nxt))
            i += 2
        else:
            out.append((s,))
            i += 1
    return out


def render_html(meta: dict, sections: list[dict]) -> str:
    """Self-contained, print-ready branded HTML document (no external
    resources: logo and charts are data URIs, no scripts), used for the
    shared-link artifact and the HTML download."""
    branding = _meta_branding(meta)
    title = meta.get("title") or "Report"
    company = meta.get("company_name") or "ZenPlus"
    on_dark = bool(getattr(branding, "logo_dark", None))
    cover_uri = _logo_uri(meta, "cover_logo_data_uri", "cover_logo_bytes")
    if cover_uri and on_dark:
        brand = f'<img src="{_esc(cover_uri)}" alt="{_esc(company)}">'
    elif cover_uri:
        brand = f'<span class="tile"><img src="{_esc(cover_uri)}" alt="{_esc(company)}"></span>'
    else:
        brand = f'<span class="name">{_esc(company)}</span>'
    doc_label = meta.get("category") or "Report"
    badge = (f'<div class="badge">{_esc(meta["confidentiality"])}</div>'
             if meta.get("confidentiality") else "")
    lede = f'<p class="lede">{_esc(meta["description"])}</p>' if meta.get("description") else ""
    facts = [
        ("Reporting period", meta.get("period_label") or "—"),
        ("Generated", meta.get("generated_label") or "—"),
        ("Scope", meta.get("scope_label") or "All monitored infrastructure"),
        ("Time zone", _tz_note(meta)),
    ]
    facts_html = "".join(f"<div><dt>{_esc(k)}</dt><dd>{_esc(v)}</dd></div>" for k, v in facts)

    parts: list[str] = [
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light"><meta name="generator" content="ZenPlus">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{_esc(title)} · {_esc(company)}</title>"
        f"<style>{_html_root_vars(meta)}{_HTML_CSS}{_html_page_css(meta)}</style></head>"
        '<body><main class="doc">'
        f'<header class="cover"><div class="cover-top"><div class="brand">{brand}</div>'
        f'<div class="doc-label">{_esc(doc_label)}</div></div>'
        f'<div class="cover-body">{badge}<h1>{_esc(title)}</h1>{lede}'
        f'<dl class="facts">{facts_html}</dl></div></header>'
    ]

    if len(sections) >= 2:
        items = []
        for n, s in enumerate(sections, 1):
            off = ' <span class="off">(not included)</span>' if s.get("restricted") else ""
            items.append(f'<li><a href="#s-{n:02d}"><span class="n">{n:02d}</span>'
                         f'<span>{_esc(s.get("title") or "Section")}{off}</span></a></li>')
        parts.append(f'<nav class="toc" aria-label="Contents"><h2>Contents</h2><ol>{"".join(items)}</ol></nav>')

    parts.append('<div class="content">')
    number = 0
    for group in _layout(sections):
        if len(group) == 2:
            parts.append('<div class="pair">')
            for s in group:
                number += 1
                parts.append(_html_section(s, number, branding, half=True))
            parts.append("</div>")
        else:
            number += 1
            parts.append(_html_section(group[0], number, branding, half=False))
    if not sections:
        parts.append('<div class="empty-state">This report has no sections.</div>')
    if meta.get("disclaimer"):
        parts.append(f'<aside class="disclaimer"><h2>Disclaimer</h2><p>{_esc(meta["disclaimer"])}</p></aside>')
    parts.append("</div>")

    foot_main = " · ".join(_esc(p) for p in (meta.get("footer_text") or company,
                                              meta.get("contact_line")) if p)
    address = f'<div class="sub">{_esc(meta["company_address"])}</div>' if meta.get("company_address") else ""
    parts.append(f'<footer class="doc-foot"><div><div>{foot_main}</div>{address}</div>'
                 '<div class="credit">Generated by ZenPlus</div></footer></main></body></html>')
    return "".join(parts)


# ─── PDF ────────────────────────────────────────────────────────────────────

_COL_GAP = 8


def _pdf_section_body(pdf: ZenPlusReport, s: dict, half: bool = False) -> None:
    """Render a section's KPIs, charts, tables and notes with the current
    margins (full page or a half-width column)."""
    if s.get("restricted"):
        pdf.restricted_card(_restricted_text(s))
        return
    kpis = s.get("kpis") or []
    charts = s.get("charts") or []
    tables = s.get("tables") or []
    notes = s.get("notes") or []
    if kpis:
        pdf.kpi_row(kpis, per_row=2 if half else None)
    for c in charts:
        png = _chart_image(c, getattr(pdf, "branding", None), half)
        if png:
            pdf.add_chart(png, title=c.get("title") or "")
        elif c.get("series") is not None or c.get("png") is not None:
            pdf.empty_state("Chart unavailable.")
    for t in tables:
        rows = [["" if c is None else str(c) for c in (row if isinstance(row, (list, tuple)) else [row])]
                for row in (t.get("rows") or [])]
        if not rows and notes:
            continue  # the section's note already explains the absence of data
        if t.get("title"):
            pdf.sub_heading(t["title"])
        headers = list(t.get("headers") or []) or ([""] * max((len(r) for r in rows), default=0))
        pdf.data_table(headers, rows, col_styles=t.get("styles"),
                       max_rows=PDF_MAX_ROWS_HALF if half else PDF_MAX_ROWS)
    for n in notes:
        if _is_error_note(s, n):
            pdf.note(n, kind="error", label="Section unavailable")
        else:
            pdf.note(n, kind="info")
    if not kpis and not charts and not tables and not notes:
        pdf.empty_state()


def _first_block_mm(pdf: ZenPlusReport, s: dict) -> float:
    """Height (mm) of the first block a full-width section body draws, so the
    section heading moves to the next page with it instead of being left
    orphaned at the bottom of a page."""
    if s.get("restricted"):
        return 24.0
    kpis = s.get("kpis") or []
    if kpis:
        first_row = kpis[:len(kpis) if len(kpis) <= 5 else 4]
        return (23.0 if any(k.get("subtitle") for k in first_row) else 19.6) + 4.0
    charts = s.get("charts") or []
    if charts:
        c = charts[0]
        png = _chart_image(c, getattr(pdf, "branding", None), False)
        if not png:
            return 15.0
        px_w, px_h = _png_size(png)
        w = min(px_w / 200 * 25.4, pdf.content_w)
        return min(w * px_h / max(px_w, 1) + (6.2 if c.get("title") else 0) + 2.0, 150.0)
    notes = s.get("notes") or []
    for t in s.get("tables") or []:
        rows = t.get("rows") or []
        if not rows and notes:
            continue
        body = 6.2 + 0.9 + 6.35 * min(len(rows), 3) + 6.0 if rows else 15.0
        return body + (9.2 if t.get("title") else 0.0)
    return 16.0 if notes else 15.0


def _half_heading(pdf: ZenPlusReport, s: dict, number: int) -> None:
    """Compact heading used when two sections sit side by side."""
    x = pdf.l_margin
    y = pdf.get_y()
    if s.get("category"):
        pdf._eyebrow(x, y, s["category"], COLOR_FAINT, size=5.8)
        y += 4.2
    pdf._sans("B", 8.8)
    pdf.set_text_color(*(COLOR_MUTED if s.get("restricted") else pdf.accent))
    ns = f"{number:02d}"
    pdf.set_xy(x, y + 1.1)
    pdf.cell(pdf.get_string_width(ns) + 1, 5, ns)
    tx = x + pdf.get_string_width(ns) + 2.8
    pdf._sans("B", 11)
    pdf.set_text_color(*(COLOR_MUTED if s.get("restricted") else COLOR_TEXT))
    pdf.set_xy(tx, y)
    pdf.cell(pdf.content_w - (tx - x), 6.6,
             pdf._fit(_safe(s["title"]), pdf.content_w - (tx - x)))
    y2 = y + 7.8
    pdf.set_fill_color(*pdf.accent)
    pdf.rect(x, y2, 9, 0.8, "F")
    pdf._hairline(x + 11, y2 + 0.4, x + pdf.content_w)
    pdf.set_y(y2 + 3)
    if s.get("description") and not s.get("restricted"):
        pdf._sans("", 7.4)
        pdf.set_text_color(*COLOR_MUTED)
        pdf.set_x(x)
        pdf.multi_cell(pdf.content_w, 3.9, _safe(s["description"]))
        pdf.set_y(pdf.get_y() + 1.4)


def _measure_half(s: dict, col_w: float, pdf: Optional[ZenPlusReport] = None) -> float | None:
    """Height of a section rendered at column width, or None if it cannot fit
    on a single page. Measures on one reusable probe document per report
    (same paper size, accent and branding as ``pdf``)."""
    try:
        probe = getattr(pdf, "_half_probe", None)
        if probe is None:
            probe = ZenPlusReport(title="probe",
                                  paper_size=getattr(pdf, "paper_size", "A4"),
                                  accent=getattr(pdf, "brand", COLOR_PRIMARY),
                                  branding=getattr(pdf, "branding", None))
            probe._decor = False
            probe._outline_on = False
            if pdf is not None:
                pdf._half_probe = probe
        probe.add_page()
        start = probe.page_no()
        probe.begin_column(MARGIN_L, col_w)
        probe.set_y(MARGIN_T)
        _half_heading(probe, s, 99)
        _pdf_section_body(probe, s, half=True)
        h = probe.get_y() - MARGIN_T
        probe.end_column()
        if probe.page_no() > start:
            return None
        return h
    except Exception:
        logger.debug("half-section measurement failed", exc_info=True)
        return None


def render_pdf(meta: dict, sections: list[dict]) -> bytes:
    """Enterprise PDF in the organisation's branding: cover, contents (4+
    sections), numbered sections with side-by-side placement of compact
    half-width sections, running header/footer, closing disclaimer.
    Paper size (A4/Letter), accent, logos, timezone and labels come from
    ``meta`` (build_report_meta)."""
    pdf = ZenPlusReport.from_meta(meta)
    if pdf.branding is None:
        pdf.branding = _meta_branding(meta)
    pdf._toc_muted = {n for n, s in enumerate(sections, 1) if s.get("restricted")}
    pdf.cover()
    if len(sections) >= 4:
        pdf.toc(expected_entries=len(sections))
    else:
        pdf.add_page()

    for group in _layout(sections):
        if len(group) == 2:
            s, nxt = group
            col_w = (pdf.body_w - _COL_GAP) / 2
            h1 = _measure_half(s, col_w, pdf)
            h2 = _measure_half(nxt, col_w, pdf)
            page_capacity = pdf.page_break_trigger - MARGIN_T
            if h1 is not None and h2 is not None and max(h1, h2) <= page_capacity:
                if max(h1, h2) > pdf.page_break_trigger - pdf.get_y() - 3:
                    pdf.add_page()
                n1 = pdf._section_n = pdf._section_n + 1
                n2 = pdf._section_n = pdf._section_n + 1
                if pdf._outline_on:
                    try:
                        pdf.start_section(_safe(s["title"]), level=0)
                        pdf.start_section(_safe(nxt["title"]), level=0)
                    except Exception:
                        pass
                y0 = pdf.get_y() + 2
                x_left = MARGIN_L
                x_right = MARGIN_L + col_w + _COL_GAP
                ends = []
                for sec, num, x in ((s, n1, x_left), (nxt, n2, x_right)):
                    pdf.begin_column(x, col_w)
                    pdf.set_y(y0)
                    _half_heading(pdf, sec, num)
                    _pdf_section_body(pdf, sec, half=True)
                    ends.append(pdf.get_y())
                    pdf.end_column()
                # vertical divider between the columns
                divider_x = MARGIN_L + col_w + _COL_GAP / 2
                pdf.set_draw_color(*COLOR_HAIRLINE)
                pdf.set_line_width(0.2)
                pdf.line(divider_x, y0 + 1, divider_x, max(ends) - 2)
                pdf.set_y(max(ends) + 7)
                continue
        for s in group:
            pdf.section_title(s["title"], description=None if s.get("restricted") else s.get("description"),
                              category=s.get("category"), keep_with=_first_block_mm(pdf, s))
            _pdf_section_body(pdf, s, half=False)
            pdf.ln(4)
    if pdf.disclaimer:
        pdf.disclaimer_block(pdf.disclaimer, contact=pdf.contact_line,
                             address=meta.get("company_address") or "")
    return bytes(pdf.output())


# ─── Top-level: resolve + render ────────────────────────────────────────────

async def resolve_report(db: AsyncSession, key: str,
                         custom_id: Optional[str] = None) -> tuple[str, str, list[str]]:
    """Return (title, description, section_ids) for a preset key or custom id."""
    if key == "custom":
        row = (await db.execute(text(
            "SELECT name, description, sections FROM custom_reports WHERE id = :id"),
            {"id": custom_id})).first()
        if not row:
            raise KeyError("custom report not found")
        return row[0], row[1] or "", list(row[2] or [])
    preset = REPORT_PRESETS.get(key)
    if not preset:
        raise KeyError(key)
    return preset["title"], preset["description"], preset["sections"]


async def build_report_meta(db: AsyncSession, title: str, frm: datetime, to: datetime,
                            description: str | None = None,
                            category: str | None = None,
                            scope_label: str | None = None,
                            branding: Any = None) -> dict:
    """Cover/header facts for every renderer, in the organisation's branding,
    timezone and date/time format (Settings → General → Company)."""
    if branding is None:
        from app.services.branding import load_branding
        branding = await load_branding(db)
    now = datetime.now(timezone.utc)
    logo = branding.logo
    cover_logo = branding.cover_logo()
    return {
        "title": title,
        "company_name": branding.display_name,
        "branding": branding,
        "logo_bytes": logo.data if logo else None,
        "logo_mime": logo.content_type if logo else None,
        "logo_data_uri": logo.data_uri if logo else None,
        "cover_logo_bytes": cover_logo.data if cover_logo else None,
        "cover_logo_data_uri": cover_logo.data_uri if cover_logo else None,
        "period_label": branding.period_label(frm, to),
        "period_from_label": branding.fmt_datetime(frm),
        "period_to_label": branding.fmt_datetime(to),
        "generated_label": branding.fmt_datetime(now, with_tz=True),
        "generated_at": now,
        "timezone": branding.timezone,
        "tz_label": branding.tz_label(to),
        "brand_color": branding.brand_color,
        "brand_rgb": branding.brand_rgb,
        "footer_text": branding.report_footer,
        "disclaimer": branding.report_disclaimer,
        "confidentiality": branding.confidentiality,
        "paper_size": branding.paper_size,
        "contact_line": branding.contact_line,
        "company_address": branding.company_address,
        "description": description or "",
        "category": category or "",
        "scope_label": scope_label or "",
    }


# ─── Email summary ──────────────────────────────────────────────────────────
# Email clients are not browsers: Outlook for Windows renders with Word, Gmail
# blocks data: URIs and clips bodies over ~102 KB, nothing reliably renders
# SVG. The summary is therefore a 600px table layout with inline CSS plus
# bgcolor attributes, the logo travels as a CID attachment (PNG/JPEG only,
# see email_logo_attachment) with the company name as alt text and as live
# text, and the full report is the attachment or the "Open full report" link.

EMAIL_LOGO_CID = "report-logo@zenplus"
EMAIL_MAX_KPIS = 6
EMAIL_MAX_FINDINGS = 5
_EMAIL_IMAGE_TYPES = ("image/png", "image/jpeg")
_EMAIL_FONT = "'Segoe UI',Helvetica,Arial,sans-serif"


def _email_esc(v: Any) -> str:
    import html as _html
    return _html.escape("" if v is None else str(v), quote=True)


def _email_clip(v: Any, limit: int) -> str:
    s = " ".join(("" if v is None else str(v)).split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _email_logo(meta: dict) -> Optional[tuple[bytes, str, str]]:
    """(bytes, mime, 'light'|'dark') of the logo an email can carry: the
    light-background logo when it is PNG/JPEG, else a PNG/JPEG dark logo."""
    from app.services.branding import sniff_logo
    light = meta.get("logo_bytes")
    if light:
        mime = sniff_logo(bytes(light))
        if mime in _EMAIL_IMAGE_TYPES:
            return bytes(light), mime, "light"
    dark = meta.get("cover_logo_bytes")
    branding = meta.get("branding")
    if getattr(branding, "logo_dark", None) is not None:
        dark = branding.logo_dark.data
    if dark and dark != light:
        mime = sniff_logo(bytes(dark))
        if mime in _EMAIL_IMAGE_TYPES:
            return bytes(dark), mime, "dark"
    return None


def email_logo_attachment(meta: dict) -> Optional[tuple[str, bytes, str]]:
    """``(content_id, bytes, mime)`` for the inline logo of the report email,
    or None when the organisation has no PNG/JPEG logo (SVG and WebP are not
    rendered reliably by email clients). Reference it as ``cid:<content_id>``
    and send it with ``Content-ID: <content_id>``."""
    logo = _email_logo(meta or {})
    if logo is None:
        return None
    return EMAIL_LOGO_CID, logo[0], logo[1]


def _email_logo_size(data: Optional[bytes], height: int = 40, max_width: int = 180) -> tuple[Optional[int], int]:
    """Explicit pixel size for the logo (Outlook ignores CSS sizing)."""
    if not data:
        return None, height
    try:
        import io as _io
        from PIL import Image
        with Image.open(_io.BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        return None, height
    if not w or not h:
        return None, height
    width = round(w * height / h)
    if width > max_width:
        return max_width, max(1, round(h * max_width / w))
    return max(1, width), height


def _email_safe_url(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    return url if re.match(r"^https?://[^\s\"'<>]+$", url, re.IGNORECASE) else None


def _email_kpis(sections: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in sections:
        if s.get("restricted") or s.get("error"):
            continue
        for k in s.get("kpis") or []:
            if isinstance(k, dict) and k.get("label") is not None:
                out.append(k)
                if len(out) >= EMAIL_MAX_KPIS:
                    return out
    return out


def _email_findings(sections: list[dict]) -> list[tuple[str, str, str]]:
    """(severity, section title, finding) for sections whose headline figures
    are flagged danger/warning: the section's first note, else the flagged
    figure itself. Danger first, report order within a severity."""
    found: list[tuple[int, int, str, str, str]] = []
    for i, s in enumerate(sections):
        if s.get("restricted") or s.get("error"):
            continue
        flagged = [k for k in (s.get("kpis") or []) if isinstance(k, dict)
                   and k.get("accent") in ("danger", "warning")]
        if not flagged:
            continue
        severity = "danger" if any(k.get("accent") == "danger" for k in flagged) else "warning"
        notes = [n for n in (s.get("notes") or []) if n]
        if notes:
            text_ = _email_clip(notes[0], 280)
        else:
            k = next(k for k in flagged if k.get("accent") == severity)
            text_ = _email_clip(f"{k.get('label')}: {k.get('value')}", 280)
        found.append((0 if severity == "danger" else 1, i, severity,
                      _email_clip(s.get("title") or "", 90), text_))
    found.sort(key=lambda f: (f[0], f[1]))
    return [(sev, title, txt) for _, _, sev, title, txt in found[:EMAIL_MAX_FINDINGS]]


def _email_omitted(sections: list[dict]) -> list[str]:
    restricted = sum(1 for s in sections if s.get("restricted"))
    failed = sum(1 for s in sections if s.get("error"))
    out = []
    if restricted:
        out.append(f"{restricted} section{'s' if restricted != 1 else ''} not included "
                   f"(requires additional permissions)")
    if failed:
        out.append(f"{failed} section{'s' if failed != 1 else ''} could not be generated")
    return out


def email_summary_html(meta: dict, sections: list[dict], view_url: Optional[str],
                       attached: bool, logo_cid: Optional[str] = None) -> str:
    """Branded, email-safe report summary (600px, tables + inline CSS +
    bgcolor, no SVG or data: URIs): classification banner, header in the
    brand colour with the logo (``cid:``) or company name, title, period with
    timezone, scope, up to six KPIs, top findings, "Open full report" button,
    footer text, contact line, disclaimer and generated time."""
    from app.services import report_colors as rc

    meta = meta or {}
    esc = _email_esc
    brand = rc.normalize(meta.get("brand_color") or getattr(meta.get("branding"), "brand_color", None))
    on_brand = rc.text_on(brand)
    company = str(meta.get("company_name") or "ZenPlus")
    title = str(meta.get("title") or "Report")
    font = f"font-family:{_EMAIL_FONT}"
    kpis = _email_kpis(sections)
    findings = _email_findings(sections)
    url = _email_safe_url(view_url)
    rows: list[str] = []

    classification = str(meta.get("confidentiality") or "").strip()
    if classification:
        bg = rc.classification_bg(classification)
        rows.append(
            f'<tr><td bgcolor="{bg}" align="center" style="background:{bg};color:{rc.text_on(bg)};'
            f'{font};font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;'
            f'padding:7px 16px">{esc(classification)}</td></tr>')

    if logo_cid:
        logo = _email_logo(meta)
        cell_bg = "#1F2937" if logo is not None and logo[2] == "dark" else "#FFFFFF"
        w, h = _email_logo_size(logo[0] if logo else None)
        size = (f' width="{w}"' if w else "") + f' height="{h}"'
        css_w = f"width:{w}px;" if w else ""
        brand_block = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td bgcolor="{cell_bg}" style="background:{cell_bg};padding:8px 12px">'
            f'<img src="cid:{esc(logo_cid)}" alt="{esc(company)}"{size} border="0" '
            f'style="display:block;{css_w}height:{h}px;border:0;outline:none;text-decoration:none;'
            f'{font};font-size:14px;font-weight:700;color:#111827"></td>'
            f'<td style="padding-left:14px;{font};font-size:13px;font-weight:600;color:{on_brand}">'
            f'{esc(company)}</td></tr></table>')
    else:
        brand_block = (f'<div style="{font};font-size:15px;font-weight:700;letter-spacing:.5px;'
                       f'color:{on_brand}">{esc(company)}</div>')

    category = meta.get("category")
    rows.append(
        f'<tr><td bgcolor="{brand}" style="background:{brand};padding:22px 28px 24px">'
        f'{brand_block}'
        + (f'<div style="{font};font-size:11px;letter-spacing:1.5px;text-transform:uppercase;'
           f'color:{on_brand};margin-top:18px">{esc(category)}</div>' if category else
           '<div style="height:16px;line-height:16px;font-size:0">&nbsp;</div>')
        + f'<div style="{font};font-size:22px;line-height:1.25;font-weight:700;color:{on_brand};'
        f'margin-top:4px">{esc(title)}</div>'
        f'<div style="{font};font-size:13px;color:{on_brand};margin-top:6px">'
        f'{esc(meta.get("period_label") or "")}</div>'
        + (f'<div style="{font};font-size:12px;color:{on_brand};margin-top:3px">Scope: '
           f'{esc(meta.get("scope_label"))}</div>' if meta.get("scope_label") else "")
        + "</td></tr>")

    body: list[str] = []
    if kpis:
        cols = 2 if len(kpis) in (1, 2, 4) else 3
        pct = 100 // cols
        trs = []
        for i in range(0, len(kpis), cols):
            cells = []
            row_has_sub = any(k.get("subtitle") for k in kpis[i:i + cols])
            for k in kpis[i:i + cols]:
                color = rc.accent_ink(k.get("accent"), brand)
                sub = (f'<div style="{font};font-size:11px;color:#6B7280;margin-top:3px">'
                       f'{esc(_email_clip(k.get("subtitle"), 80)) if k.get("subtitle") else "&nbsp;"}'
                       f'</div>') if row_has_sub else ""
                cells.append(
                    f'<td width="{pct}%" valign="top" style="padding:5px">'
                    f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                    f'<td bgcolor="#F5F7FE" style="background:#F5F7FE;border-top:3px solid {color};'
                    f'padding:12px 14px">'
                    f'<div style="{font};font-size:20px;font-weight:700;line-height:1.2;color:{color}">'
                    f'{esc(_email_clip(k.get("value"), 40))}</div>'
                    f'<div style="{font};font-size:11px;color:#4B5563;text-transform:uppercase;'
                    f'letter-spacing:.5px;margin-top:4px">{esc(_email_clip(k.get("label"), 60))}</div>'
                    f'{sub}</td></tr></table></td>')
            cells += [f'<td width="{pct}%" style="padding:5px">&nbsp;</td>'] * (cols - len(cells))
            trs.append("<tr>" + "".join(cells) + "</tr>")
        body.append(f'<tr><td style="padding:18px 23px 6px">'
                    f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                    f'{"".join(trs)}</table></td></tr>')

    if findings:
        items = "".join(
            f'<tr><td valign="top" width="18" style="{font};font-size:13px;line-height:20px;'
            f'color:{rc.ACCENT_INK[sev]};padding:3px 0">&#9679;</td>'
            f'<td style="{font};font-size:13px;line-height:20px;color:#1F2937;padding:3px 0">'
            f'<strong>{esc(stitle)}</strong> — {esc(txt)}</td></tr>'
            for sev, stitle, txt in findings)
        body.append(
            f'<tr><td style="padding:14px 28px 4px">'
            f'<div style="{font};font-size:14px;font-weight:700;color:#111827;margin-bottom:6px">'
            f'Top findings</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'{items}</table></td></tr>')

    omitted = _email_omitted(sections)
    if omitted:
        body.append(f'<tr><td style="padding:8px 28px 0;{font};font-size:12px;color:#6B7280">'
                    f'{esc(" · ".join(omitted))}</td></tr>')

    if url:
        body.append(
            f'<tr><td style="padding:20px 28px 4px">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td bgcolor="{brand}" style="background:{brand}">'
            f'<a href="{esc(url)}" target="_blank" style="display:inline-block;padding:12px 24px;'
            f'{font};font-size:14px;font-weight:600;color:{on_brand};text-decoration:none">'
            f'Open full report</a></td></tr></table></td></tr>')
    if attached:
        body.append(f'<tr><td style="padding:10px 28px 0;{font};font-size:12px;color:#4B5563">'
                    f'The full report is attached to this email.</td></tr>')
    if not body:
        body.append(f'<tr><td style="padding:18px 28px;{font};font-size:13px;color:#4B5563">'
                    f'This report has no headline figures for the period.</td></tr>')
    rows.append("".join(body) + '<tr><td style="height:22px;line-height:22px;font-size:0">&nbsp;</td></tr>')

    foot: list[str] = []
    first = " · ".join(_email_clip(p, 200) for p in (meta.get("footer_text"), meta.get("contact_line")) if p)
    if first:
        foot.append(f'<div style="margin-bottom:6px">{esc(first)}</div>')
    if meta.get("disclaimer"):
        foot.append(f'<div style="font-size:10px;line-height:1.45;color:#6B7280;margin-bottom:6px">'
                    f'{esc(_email_clip(meta.get("disclaimer"), 1000))}</div>')
    foot.append(f'<div>Generated {esc(meta.get("generated_label") or "")} · {esc(company)}</div>')
    rows.append(
        f'<tr><td bgcolor="#F9FAFB" style="background:#F9FAFB;border-top:1px solid #E5E7EB;'
        f'padding:16px 28px;{font};font-size:11px;line-height:1.5;color:#4B5563">'
        f'{"".join(foot)}</td></tr>')

    pre_bits = [f"{_email_clip(k.get('label'), 40)} {_email_clip(k.get('value'), 30)}" for k in kpis[:3]]
    preheader = (
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;'
        f'line-height:1px;color:#EDF0F7">{esc(" · ".join(pre_bits))}&nbsp;&zwnj;&nbsp;&zwnj;</div>'
        if pre_bits else "")
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
        f'<title>{esc(title)}</title></head>'
        '<body bgcolor="#EDF0F7" style="margin:0;padding:0;background:#EDF0F7">'
        f'{preheader}'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#EDF0F7" style="background:#EDF0F7"><tr><td align="center" style="padding:24px 10px">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#FFFFFF" style="width:600px;max-width:600px;background:#FFFFFF;'
        'border:1px solid #E5E7EB">'
        + "".join(rows)
        + '</table></td></tr></table></body></html>')


def email_summary_text(meta: dict, sections: list[dict], view_url: Optional[str]) -> str:
    """Plain-text alternative of :func:`email_summary_html`."""
    meta = meta or {}
    company = str(meta.get("company_name") or "ZenPlus")
    lines: list[str] = []
    classification = str(meta.get("confidentiality") or "").strip()
    if classification:
        lines += [f"[{classification.upper()}]", ""]
    lines += [company, str(meta.get("title") or "Report")]
    if meta.get("category"):
        lines.append(str(meta["category"]))
    if meta.get("period_label"):
        lines.append(f"Period: {meta['period_label']}")
    if meta.get("scope_label"):
        lines.append(f"Scope: {meta['scope_label']}")
    kpis = _email_kpis(sections)
    if kpis:
        lines += ["", "Key figures"]
        for k in kpis:
            sub = f" ({_email_clip(k.get('subtitle'), 80)})" if k.get("subtitle") else ""
            lines.append(f"- {_email_clip(k.get('label'), 60)}: {_email_clip(k.get('value'), 40)}{sub}")
    findings = _email_findings(sections)
    if findings:
        lines += ["", "Top findings"]
        for sev, stitle, txt in findings:
            lines.append(f"- [{'!' if sev == 'danger' else '~'}] {stitle} — {txt}")
    omitted = _email_omitted(sections)
    if omitted:
        lines += [""] + omitted
    url = _email_safe_url(view_url)
    if url:
        lines += ["", f"Open the full report: {url}"]
    footer = [p for p in (meta.get("footer_text"), meta.get("contact_line")) if p]
    lines += ["", "--"]
    if footer:
        lines.append(" · ".join(str(p) for p in footer))
    if meta.get("disclaimer"):
        lines.append(str(meta["disclaimer"]))
    lines.append(f"Generated {meta.get('generated_label') or ''} · {company}".rstrip())
    return "\n".join(lines)


# ─── Extensions ─────────────────────────────────────────────────────────────
# Imported last (they use the names above): the template catalog's categories
# and core templates, the section packs (which register sections and their
# domain templates), the cross-domain catalog, then every built-in template is
# published into REPORT_PRESETS.
from app.services import report_templates as _report_templates  # noqa: E402
from app.services import report_packs as _report_packs  # noqa: E402,F401
from app.services import report_catalog as _report_catalog  # noqa: E402,F401

_report_templates.publish_presets()
