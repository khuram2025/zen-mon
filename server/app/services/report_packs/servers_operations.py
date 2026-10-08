"""Server and agent operations sections: fleet health, telemetry coverage,
reboots and uptime, Windows services, event log errors, server alerts,
software baseline compliance, software and hardware inventory, and the agent
fleet's health and resource overhead.

Data sources:
- Postgres ``servers`` / ``agents`` hold the current state (status,
  status_reasons, boot_time, hardware, versions, certificates); the agent
  inventory tables hold the last snapshot each server uploaded and
  ``server_baseline_results`` the latest evaluation per (server, rule).
- ClickHouse host tables written by host_metric_service: CPU 5-minute rollup
  (90 days), Windows service state changes (60 days), event log summaries
  and agent self-telemetry (30 days).

Every section is fleet-scoped (servers are not network devices, so the
report's device filter does not apply) and requires servers.view; server
alerts additionally require alerts.view.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import cmp_to_key
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _coerce_dt,
    _fmt_bytes,
    _fmt_ms,
    _fmt_pct,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

# ─── Shared helpers ─────────────────────────────────────────────────────────

SERVER_NAME = "COALESCE(NULLIF(s.display_name, ''), NULLIF(s.hostname, ''), s.id::text)"
# Servers whose health is current (stale/unknown are not measured).
REPORTING_SQL = "s.status IN ('healthy', 'warning', 'critical')"
REPORTING = ("healthy", "warning", "critical")

HOST_ROLLUP_DAYS = 90      # host_cpu_metrics_5m TTL
SERVICE_STATE_DAYS = 60    # host_service_state TTL
EVENT_LOG_DAYS = 30        # host_event_log_summary TTL
AGENT_HEALTH_DAYS = 30     # agent_health_metrics TTL

STATUS_LABELS = {
    "critical": "Critical", "warning": "Warning", "stale": "Stale (not reporting)",
    "unknown": "Unknown (no data yet)", "healthy": "Healthy", "disabled": "Disabled",
}
STATUS_ORDER = ("critical", "warning", "stale", "unknown", "healthy", "disabled")
OS_FAMILY = {"windows": "Windows", "linux": "Linux", "macos": "macOS", "bsd": "BSD",
             "other": "Other", "unknown": "Unknown"}
SOURCE_LABELS = {"host_metric": "Alert rules", "health_threshold": "Health thresholds",
                 "agent_offline": "Agent offline", "baseline": "Software baselines"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    """datetime/ISO string -> aware UTC datetime (naive values are UTC)."""
    dt = _coerce_dt(value)
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _num(v: Any) -> Optional[float]:
    """A measured number, or None (ClickHouse returns nan for empty averages)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _count(n: Any) -> str:
    return f"{_int(n):,}"


def _share(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}% of {total:,}" if total else "—"


def _duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


def _json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            parsed = json.loads(v)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _clip(value: Any, n: int = 120) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _change(cur: int, prev: Optional[int]) -> Optional[str]:
    """'▲ 12.5% vs previous period' (None when the previous period is unknown)."""
    if prev is None:
        return None
    if prev == 0:
        return "▲ from none in the previous period" if cur else "None in the previous period either"
    pct = (cur - prev) / prev * 100
    if abs(pct) < 0.05:
        return "No change vs previous period"
    return f"{'▲' if pct > 0 else '▼'} {abs(pct):.1f}% vs previous period"


def _rows_title(base: str, shown: int, total: int) -> str:
    return f"{base} (top {shown:,} of {total:,})" if total > shown else base


def _os_label(os_name: Any, os_type: Any) -> str:
    name = str(os_name or "").strip()
    return name or OS_FAMILY.get(str(os_type or "unknown"), "—")


def _tz(ctx: SectionCtx) -> ZoneInfo:
    try:
        return ZoneInfo(ctx.tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _local_days(ctx: SectionCtx) -> list[int]:
    """Epoch seconds of each local midnight (organisation timezone) from the
    day containing the window start up to the window end or now, whichever
    is earlier: the keys of ClickHouse ``toUnixTimestamp(ctx.ch_bucket(..,
    '1 DAY'))`` and of the Postgres day buckets below."""
    tz = _tz(ctx)
    frm, end = _aware(ctx.frm), min(_aware(ctx.to), _utcnow())
    day = frm.astimezone(tz).date()
    out: list[int] = []
    while len(out) < 1000:
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        if midnight >= end:
            break
        out.append(int(midnight.timestamp()))
        day += timedelta(days=1)
    return out


def _iso_epoch(ctx: SectionCtx, epoch: int) -> Optional[str]:
    return ctx.iso(datetime.fromtimestamp(epoch, tz=timezone.utc))


def _prior_in_retention(ctx: SectionCtx, days: int) -> bool:
    """Whether the previous window is still inside ``days`` of retention."""
    pf, _pt = ctx.prior_window()
    return _aware(pf) >= _utcnow() - timedelta(days=days)


async def _pg(ctx: SectionCtx, sql: str, params: Optional[dict] = None) -> list[dict]:
    res = await ctx.db.execute(text(sql), params or {})
    return [dict(r) for r in res.mappings().all()]


async def _pg_one(ctx: SectionCtx, sql: str, params: Optional[dict] = None) -> dict:
    res = await ctx.db.execute(text(sql), params or {})
    row = res.mappings().first()
    return dict(row) if row else {}


async def _server_names(ctx: SectionCtx, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await _pg(ctx, f"SELECT s.id::text AS id, {SERVER_NAME} AS name FROM servers s "
                          "WHERE s.id::text = ANY(:ids)", {"ids": ids})
    return {str(r["id"]): r["name"] for r in rows}


def _fold(points: list[tuple[str, int]], keep: int = 7) -> list[dict]:
    """Donut points: the largest ``keep`` groups plus 'Other'."""
    pts = [p for p in points if p[1]]
    head, tail = pts[:keep], pts[keep:]
    out = [{"label": label, "value": n} for label, n in head]
    if tail:
        out.append({"label": "Other", "value": sum(n for _l, n in tail)})
    return out


# ─── Server fleet health ────────────────────────────────────────────────────

@register_section(
    "server_fleet_status", title="Server Fleet Health", category="Servers",
    description=("Servers by health status and operating system, with the servers that need "
                 "attention and why, as of report generation."),
    scope="fleet", requires=("servers.view",), semantics="snapshot",
    options=[
        top_n_option(25, 200),
        {"key": "os_group", "label": "Operating system breakdown", "type": "select",
         "default": "family", "choices": [["family", "OS family"], ["name", "OS name"]]},
    ],
)
async def server_fleet_status(ctx: SectionCtx) -> dict:
    sid, title = "server_fleet_status", "Server Fleet Health"
    counts = {str(r["status"] or "unknown"): _int(r["n"]) for r in await _pg(
        ctx, "SELECT COALESCE(s.status, 'unknown') AS status, COUNT(*) AS n FROM servers s GROUP BY 1")}
    total_all = sum(counts.values())
    if not total_all:
        return _section(sid, title, description="Health of the server fleet as of report generation.",
                        notes=["No servers are registered."])
    disabled = counts.get("disabled", 0)
    monitored = total_all - disabled
    healthy, warning, critical = (counts.get(k, 0) for k in ("healthy", "warning", "critical"))
    not_reporting = counts.get("stale", 0) + counts.get("unknown", 0)

    by_name = ctx.opt("os_group") == "name"
    os_expr = ("COALESCE(NULLIF(s.os_name, ''), 'Not reported')" if by_name
               else "COALESCE(NULLIF(s.os_type, ''), 'unknown')")
    os_rows = await _pg(ctx, f"SELECT {os_expr} AS os, COUNT(*) AS n FROM servers s "
                             "WHERE s.status <> 'disabled' GROUP BY 1 ORDER BY 2 DESC, 1")
    os_points = _fold([(r["os"] if by_name else OS_FAMILY.get(str(r["os"]), str(r["os"]).title()),
                        _int(r["n"])) for r in os_rows])

    top_n = int(ctx.opt("top_n"))
    attention_total = critical + warning + not_reporting
    rows = await _pg(ctx, f"""
        SELECT {SERVER_NAME} AS server, s.os_type, s.os_name, st.name AS site, s.status,
               s.status_reasons, s.last_seen
        FROM servers s LEFT JOIN sites st ON st.id = s.site_id
        WHERE s.status IN ('critical', 'warning', 'stale', 'unknown')
        ORDER BY CASE s.status WHEN 'critical' THEN 0 WHEN 'warning' THEN 1
                               WHEN 'stale' THEN 2 ELSE 3 END, server
        LIMIT :lim""", {"lim": top_n}) if attention_total else []

    def reasons(r: dict) -> str:
        items = [str(x) for x in _json_list(r.get("status_reasons")) if x]
        if items:
            return _clip("; ".join(items), 160)
        return {"stale": "No data received from the agent",
                "unknown": "No telemetry received yet"}.get(r.get("status"), "—")

    table_rows = [[r["server"], _os_label(r.get("os_name"), r.get("os_type")), r.get("site") or "—",
                   r.get("status") or "unknown", reasons(r), ctx.fmt_short(r.get("last_seen"))]
                  for r in rows]
    notes = []
    if not_reporting:
        notes.append(f"{not_reporting:,} server(s) are stale or have not reported yet: their health is "
                     "not measured, so they are not counted as healthy.")
    if disabled:
        notes.append(f"{disabled:,} disabled server(s) are excluded from the totals and the attention list.")
    if not attention_total and monitored:
        notes.append("Every monitored server is healthy.")
    return _section(
        sid, title,
        description=f"Health of {monitored:,} monitored server(s) as of report generation.",
        kpis=[
            {"label": "Monitored servers", "value": f"{monitored:,}", "accent": "primary",
             "subtitle": f"{disabled:,} disabled" if disabled else "All servers enabled"},
            {"label": "Healthy", "value": f"{healthy:,}", "accent": "success",
             "subtitle": _share(healthy, monitored)},
            {"label": "Warning", "value": f"{warning:,}", "accent": "warning" if warning else "neutral",
             "subtitle": _share(warning, monitored)},
            {"label": "Critical", "value": f"{critical:,}", "accent": "danger" if critical else "success",
             "subtitle": _share(critical, monitored)},
            {"label": "Not reporting", "value": f"{not_reporting:,}",
             "accent": "warning" if not_reporting else "neutral", "subtitle": "Stale or unknown — not measured"},
        ],
        charts=[
            {"title": "Servers by status",
             "series": {"kind": "donut", "unit": "servers",
                        "points": [{"label": STATUS_LABELS[k], "value": counts[k]}
                                   for k in STATUS_ORDER if counts.get(k)]}},
            {"title": "Servers by operating system" + (" (name)" if by_name else ""),
             "series": {"kind": "donut", "unit": "servers", "points": os_points}},
        ],
        tables=[{"title": _rows_title("Servers needing attention", len(table_rows), attention_total),
                 "headers": ["Server", "Operating system", "Site", "Status", "Reasons", "Last seen"],
                 "styles": ["text", "text", "text", "status", "text", "text"],
                 "rows": table_rows}] if table_rows else [],
        notes=notes,
    )


# ─── Telemetry coverage ─────────────────────────────────────────────────────

@register_section(
    "server_telemetry_coverage", title="Server Telemetry Coverage", category="Servers",
    description=("Metric samples received from each agent-monitored server over the period against "
                 "the number its agent policy collection interval implies."),
    scope="fleet", requires=("servers.view",), semantics="window",
    options=[
        top_n_option(25, 500),
        {"key": "threshold", "label": "Coverage target (%)", "type": "number", "default": 95,
         "min": 1, "max": 100, "help": "Servers below this share of expected samples are listed."},
    ],
)
async def server_telemetry_coverage(ctx: SectionCtx) -> dict:
    sid, title = "server_telemetry_coverage", "Server Telemetry Coverage"
    servers = await _pg(ctx, f"""
        SELECT s.id::text AS id, {SERVER_NAME} AS server, s.status, s.collection_mode, s.created_at,
               pol.metric_interval_s, ag.agents, ag.first_enrolled, ag.last_metric_at
        FROM servers s
        LEFT JOIN LATERAL (
            SELECT a.policy_id FROM agents a
            WHERE a.server_id = s.id AND a.revoked_at IS NULL
            ORDER BY a.last_heartbeat_at DESC NULLS LAST, a.created_at DESC LIMIT 1) cur ON TRUE
        LEFT JOIN agent_policies pol ON pol.id = cur.policy_id
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS agents, MIN(a.created_at) AS first_enrolled,
                   MAX(a.last_metric_at) AS last_metric_at
            FROM agents a WHERE a.server_id = s.id AND a.revoked_at IS NULL) ag ON TRUE
        WHERE s.status <> 'disabled'
        ORDER BY server""")
    if not servers:
        return _section(sid, title, description="Telemetry delivery from agent-monitored servers.",
                        notes=["No servers are registered."])

    samples = {str(r[0]): _int(r[1]) for r in await ctx.ch_rows(
        """SELECT toString(server_id) AS sid, sum(sample_count) AS samples
           FROM zenplus.host_cpu_metrics_5m
           WHERE timestamp >= %(f)s AND timestamp < %(t)s
           GROUP BY server_id""", {"f": ctx.frm, "t": ctx.to})}

    thr = float(ctx.opt("threshold"))
    frm = _aware(ctx.frm)
    end = min(_aware(ctx.to), _utcnow())
    measured: list[dict] = []
    other_mode = no_agent = not_expected = 0
    for s in servers:
        if (s.get("collection_mode") or "agent") != "agent":
            other_mode += 1
            continue
        if not _int(s.get("agents")):
            no_agent += 1
            continue
        start = max(d for d in (frm, _aware(s.get("first_enrolled")), _aware(s.get("created_at"))) if d)
        interval = max(5, min(_int(s.get("metric_interval_s")) or 30, 3600))
        secs = (end - start).total_seconds()
        expected = int(secs // interval) if secs > 0 else 0
        if expected <= 0:
            not_expected += 1
            continue
        received = samples.get(s["id"], 0)
        measured.append({**s, "interval": interval, "expected": expected, "received": received,
                         "coverage": min(100.0, received / expected * 100)})

    complete = [m for m in measured if m["coverage"] >= thr]
    silent = [m for m in measured if m["received"] == 0]
    partial = [m for m in measured if 0 < m["received"] and m["coverage"] < thr]
    below = sorted((m for m in measured if m["coverage"] < thr), key=lambda m: (m["coverage"], m["server"]))
    exp_total = sum(m["expected"] for m in measured)
    rec_total = sum(min(m["received"], m["expected"]) for m in measured)
    fleet = rec_total / exp_total * 100 if exp_total else None
    top_n = int(ctx.opt("top_n"))
    target = f"{thr:g}%"

    notes = ["Coverage compares the CPU samples stored for each agent-monitored server with the number "
             "its current agent policy collection interval implies, from enrolment or the start of the "
             "period until its end (or now). A policy change during the period skews the ratio."]
    if no_agent:
        notes.append(f"{no_agent:,} agent-mode server(s) have no enrolled agent: not measured.")
    if other_mode:
        notes.append(f"{other_mode:,} server(s) use another collection mode and are not rated.")
    if not_expected:
        notes.append(f"{not_expected:,} server(s) were enrolled after the period and are not rated.")
    retention = ctx.window_note(HOST_ROLLUP_DAYS, "Host metric rollup data")
    if retention:
        notes.append(retention)
    if measured and not below:
        notes.append(f"Every rated server delivered at least {target} of its expected samples.")
    if not measured:
        notes.append("No agent-monitored server could be rated for this period.")

    kpis = [
        {"label": "Fleet coverage", "value": _fmt_pct(fleet, 2),
         "accent": "neutral" if fleet is None else "success" if fleet >= thr else "warning",
         "subtitle": f"{rec_total:,} of {exp_total:,} expected samples" if exp_total else "Not measured"},
        {"label": f"Servers ≥ {target}", "value": f"{len(complete):,}", "accent": "success",
         "subtitle": _share(len(complete), len(measured))},
        {"label": "Servers with gaps", "value": f"{len(partial):,}",
         "accent": "warning" if partial else "neutral", "subtitle": f"Below {target}, still reporting"},
        {"label": "Silent servers", "value": f"{len(silent):,}",
         "accent": "danger" if silent else "success", "subtitle": "No samples in the period"},
    ]
    if no_agent:
        kpis.append({"label": "No agent", "value": f"{no_agent:,}", "accent": "neutral",
                     "subtitle": "Not measured"})
    bands = [("Meets target", len(complete)), ("Gaps", len(partial)), ("Silent", len(silent)),
             ("No agent", no_agent)]
    return _section(
        sid, title,
        description=f"Samples received against expected for {len(measured):,} agent-monitored server(s) "
                    f"over the period (target {target}).",
        kpis=kpis,
        charts=[{"title": "Servers by telemetry coverage",
                 "series": {"kind": "donut", "unit": "servers",
                            "points": [{"label": label, "value": n} for label, n in bands if n]}}]
        if measured or no_agent else [],
        tables=[{"title": _rows_title(f"Servers below {target} coverage", min(top_n, len(below)), len(below)),
                 "headers": ["Server", "Interval", "Samples received", "Samples expected", "Coverage",
                             "Last metric", "Status"],
                 "styles": ["text", "num", "num", "num", "pct-bar", "text", "status"],
                 "rows": [[m["server"], f"{m['interval']} s", f"{m['received']:,}", f"{m['expected']:,}",
                           _fmt_pct(m["coverage"], 2), ctx.fmt_short(m.get("last_metric_at")),
                           m.get("status") or "unknown"] for m in below[:top_n]]}] if below else [],
        notes=notes,
    )


# ─── Reboots and uptime ─────────────────────────────────────────────────────

@register_section(
    "server_uptime_reboots", title="Server Reboots and Uptime", category="Servers",
    description=("Servers whose last boot falls in the period and the longest-running servers, as of "
                 "report generation (only each server's latest boot is known)."),
    scope="fleet", requires=("servers.view",), semantics="snapshot",
    options=[
        top_n_option(15, 200),
        {"key": "long_uptime_days", "label": "Long uptime threshold (days)", "type": "int",
         "default": 90, "min": 1, "max": 3650,
         "help": "Servers running longer than this without a reboot are counted."},
    ],
)
async def server_uptime_reboots(ctx: SectionCtx) -> dict:
    sid, title = "server_uptime_reboots", "Server Reboots and Uptime"
    long_days = int(ctx.opt("long_uptime_days"))
    rep = f"{REPORTING_SQL} AND s.boot_time IS NOT NULL AND s.boot_time <= NOW()"
    agg = await _pg_one(ctx, f"""
        SELECT COUNT(*) AS servers,
               COUNT(*) FILTER (WHERE s.boot_time IS NULL) AS no_boot,
               COUNT(*) FILTER (WHERE s.boot_time >= :f AND s.boot_time < :t) AS rebooted,
               COUNT(*) FILTER (WHERE {rep}) AS reporting,
               COUNT(*) FILTER (WHERE {rep} AND s.boot_time > NOW() - INTERVAL '1 day') AS up_1d,
               COUNT(*) FILTER (WHERE {rep} AND s.boot_time <= NOW() - INTERVAL '1 day'
                                AND s.boot_time > NOW() - INTERVAL '7 days') AS up_7d,
               COUNT(*) FILTER (WHERE {rep} AND s.boot_time <= NOW() - INTERVAL '7 days'
                                AND s.boot_time > NOW() - INTERVAL '30 days') AS up_30d,
               COUNT(*) FILTER (WHERE {rep} AND s.boot_time <= NOW() - INTERVAL '30 days'
                                AND s.boot_time > NOW() - INTERVAL '90 days') AS up_90d,
               COUNT(*) FILTER (WHERE {rep} AND s.boot_time <= NOW() - INTERVAL '90 days') AS up_more,
               COUNT(*) FILTER (WHERE {rep} AND s.boot_time <= NOW() - make_interval(days => :long))
                   AS long_running
        FROM servers s WHERE s.status <> 'disabled'""",
        {"f": ctx.frm, "t": ctx.to, "long": long_days})
    servers = _int(agg.get("servers"))
    if not servers:
        return _section(sid, title, description="Server reboots and uptime as of report generation.",
                        notes=["No servers are registered."])
    top_n = int(ctx.opt("top_n"))
    rebooted_n = _int(agg.get("rebooted"))
    cols = f"{SERVER_NAME} AS server, s.os_type, s.os_name, s.status, s.boot_time"
    rebooted = await _pg(ctx, f"""
        SELECT {cols} FROM servers s
        WHERE s.status <> 'disabled' AND s.boot_time >= :f AND s.boot_time < :t
        ORDER BY s.boot_time DESC, server LIMIT :lim""",
        {"f": ctx.frm, "t": ctx.to, "lim": top_n}) if rebooted_n else []
    longest = await _pg(ctx, f"SELECT {cols} FROM servers s WHERE s.status <> 'disabled' AND {rep} "
                             "ORDER BY s.boot_time ASC, server LIMIT :lim", {"lim": top_n})
    now = _utcnow()

    def uptime(r: dict) -> Optional[float]:
        bt = _aware(r.get("boot_time"))
        if bt is None or r.get("status") not in REPORTING or bt > now:
            return None
        return (now - bt).total_seconds()

    long_running = _int(agg.get("long_running"))
    bands = [("Under 1 day", "up_1d"), ("1–7 days", "up_7d"), ("7–30 days", "up_30d"),
             ("30–90 days", "up_90d"), ("90 days or more", "up_more")]
    no_boot = _int(agg.get("no_boot"))
    notes = ["Only the most recent boot of each server is recorded: a server that restarted several "
             "times in the period is listed once, with its latest boot, and earlier reboots are not "
             "visible.",
             "Uptime runs from the last reported boot to report generation; servers that are stale or "
             "not yet reporting show — because their current uptime is unknown."]
    if not rebooted_n:
        notes.append("No server reported a boot in this period.")
    return _section(
        sid, title,
        description=f"Reboots in the period and current uptime of {servers:,} server(s), as of report "
                    "generation.",
        kpis=[
            {"label": "Rebooted in period", "value": f"{rebooted_n:,}", "accent": "info",
             "subtitle": f"of {servers:,} servers (latest boot)"},
            {"label": "Longest uptime", "value": _duration(uptime(longest[0])) if longest else "—",
             "accent": "primary", "subtitle": longest[0]["server"] if longest else "Not measured"},
            {"label": f"Up {long_days}+ days", "value": f"{long_running:,}",
             "accent": "warning" if long_running else "success",
             "subtitle": f"No reboot in {long_days} days or more"},
            {"label": "Boot time not reported", "value": f"{no_boot:,}", "accent": "neutral",
             "subtitle": "Not measured"},
        ],
        charts=[{"title": "Current uptime of reporting servers",
                 "series": {"kind": "donut", "unit": "servers",
                            "points": [{"label": label, "value": _int(agg.get(k))}
                                       for label, k in bands if _int(agg.get(k))]}}]
        if _int(agg.get("reporting")) else [],
        tables=([{"title": _rows_title("Servers rebooted in this period", len(rebooted), rebooted_n),
                  "headers": ["Server", "Operating system", "Status", "Last boot", "Uptime now"],
                  "styles": ["text", "text", "status", "text", "num"],
                  "rows": [[r["server"], _os_label(r.get("os_name"), r.get("os_type")),
                            r.get("status") or "unknown", ctx.fmt_dt(r.get("boot_time")),
                            _duration(uptime(r))] for r in rebooted]}] if rebooted else [])
        + ([{"title": f"Longest-running servers (top {len(longest):,})",
             "headers": ["Server", "Operating system", "Last boot", "Uptime"],
             "styles": ["text", "text", "text", "num"],
             "rows": [[r["server"], _os_label(r.get("os_name"), r.get("os_type")),
                       ctx.fmt_dt(r.get("boot_time")), _duration(uptime(r))] for r in longest]}]
           if longest else []),
        notes=notes,
    )


# ─── Windows service health ─────────────────────────────────────────────────

# Inventory rows older than the policy's freshness horizon (at least 15
# minutes, as on the server Services tab) describe a past state.
_SVC_CTE = f"""
WITH cur AS (
    SELECT DISTINCT ON (a.server_id) a.server_id, p.metric_interval_s, p.upload_interval_s,
           CASE WHEN jsonb_typeof(p.service_watchlist) = 'array'
                THEN p.service_watchlist ELSE '[]'::jsonb END AS watchlist
    FROM agents a LEFT JOIN agent_policies p ON p.id = a.policy_id
    WHERE a.server_id IS NOT NULL AND a.revoked_at IS NULL
    ORDER BY a.server_id, a.last_heartbeat_at DESC NULLS LAST, a.created_at DESC
), svc AS (
    SELECT i.server_id, {SERVER_NAME} AS server, i.service_name,
           COALESCE(NULLIF(i.display_name, ''), i.service_name) AS display_name,
           lower(COALESCE(i.start_mode, '')) AS start_mode,
           lower(COALESCE(NULLIF(i.state, ''), 'unknown')) AS state,
           i.updated_at,
           EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(cur.watchlist, '[]'::jsonb)) w
                   WHERE lower(w) = lower(i.service_name)) AS watched,
           i.updated_at < NOW() - make_interval(secs => GREATEST(900,
               LEAST(GREATEST(COALESCE(cur.metric_interval_s, 30), 5), 3600)
             + LEAST(GREATEST(COALESCE(cur.upload_interval_s, 60), 5), 3600) + 60)) AS is_stale
    FROM server_service_inventory i
    JOIN servers s ON s.id = i.server_id
    LEFT JOIN cur ON cur.server_id = i.server_id
    WHERE s.status <> 'disabled'
), down AS (
    SELECT * FROM svc WHERE (start_mode = 'auto' OR watched) AND state <> 'running'
)
"""

# Consecutive state rows per service (the agent records changes, and every
# sample for watched services); only services with more than one state in
# the window can have changed, so only those pay for the window function.
_SVC_CHANGES_INNER = """
    SELECT server_id, service_name, timestamp, CAST(state AS String) AS st,
           lagInFrame(CAST(state AS String), 1, '') OVER (
               PARTITION BY server_id, service_name ORDER BY timestamp ASC
               ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS prev
    FROM zenplus.host_service_state
    WHERE timestamp >= %(f)s AND timestamp < %(t)s
      AND (server_id, service_name) IN (
          SELECT server_id, service_name FROM zenplus.host_service_state
          WHERE timestamp >= %(f)s AND timestamp < %(t)s
          GROUP BY server_id, service_name
          HAVING uniqExact(state) > 1)
"""
_SVC_CHANGES_CAP = 5000


@register_section(
    "server_service_health", title="Windows Service Health", category="Servers",
    description=("Automatic-start and watched Windows services that are not running now, the most "
                 "common ones across the fleet, and the services that changed state most often in "
                 "the period."),
    scope="fleet", requires=("servers.view",), semantics="window",
    options=[top_n_option(25, 500)],
)
async def server_service_health(ctx: SectionCtx) -> dict:
    sid, title = "server_service_health", "Windows Service Health"
    top_n = int(ctx.opt("top_n"))
    summary = await _pg_one(ctx, _SVC_CTE + """
        SELECT (SELECT COUNT(DISTINCT server_id) FROM svc) AS inv_servers,
               COUNT(*) FILTER (WHERE NOT is_stale) AS down,
               COUNT(DISTINCT server_id) FILTER (WHERE NOT is_stale) AS servers,
               COUNT(*) FILTER (WHERE NOT is_stale AND watched) AS watched,
               COUNT(*) FILTER (WHERE is_stale) AS stale
        FROM down""")
    inv_servers = _int(summary.get("inv_servers"))
    down_n = _int(summary.get("down"))
    down_rows = await _pg(ctx, _SVC_CTE + """
        SELECT server, service_name, display_name, start_mode, state, watched, updated_at
        FROM down WHERE NOT is_stale
        ORDER BY watched DESC, server, display_name LIMIT :lim""", {"lim": top_n}) if down_n else []
    common = await _pg(ctx, _SVC_CTE + """
        SELECT service_name, MAX(display_name) AS display_name, COUNT(DISTINCT server_id) AS servers
        FROM down WHERE NOT is_stale
        GROUP BY service_name ORDER BY servers DESC, service_name LIMIT 10""") if down_n else []

    p = {"f": ctx.frm, "t": ctx.to}
    changes = await ctx.ch_rows(f"""
        SELECT toString(server_id) AS sid, service_name,
               countIf(prev != '' AND st != prev) AS changes,
               countIf(prev = 'running' AND st != 'running') AS stops,
               argMax(st, timestamp) AS last_state,
               maxIf(timestamp, prev != '' AND st != prev) AS last_change
        FROM ({_SVC_CHANGES_INNER})
        GROUP BY server_id, service_name
        HAVING changes > 0
        ORDER BY changes DESC, stops DESC
        LIMIT {_SVC_CHANGES_CAP}""", p)
    bucket, bparams = ctx.ch_bucket("timestamp", "1 DAY")
    daily = {_int(r[0]): _int(r[1]) for r in await ctx.ch_rows(f"""
        SELECT toUnixTimestamp({bucket}) AS d,
               countIf(prev = 'running' AND st != 'running') AS stops
        FROM ({_SVC_CHANGES_INNER})
        GROUP BY d ORDER BY d""", {**p, **bparams})}
    total_changes = sum(_int(r[2]) for r in changes)
    total_stops = sum(_int(r[3]) for r in changes)
    names = await _server_names(ctx, sorted({str(r[0]) for r in changes[:top_n]}))

    if not inv_servers and not changes:
        notes = ["No Windows service inventory or service state history is available for this period."]
        retention = ctx.window_note(SERVICE_STATE_DAYS, "Windows service state history")
        return _section(sid, title, description="Windows services on agent-monitored servers.",
                        notes=notes + ([retention] if retention else []))

    notes = []
    if not down_n:
        notes.append("Every automatic-start and watched service is running on servers with current data.")
    stale = _int(summary.get("stale"))
    if stale:
        notes.append(f"{stale:,} stopped service record(s) from servers whose service inventory is out of "
                     "date are excluded: their last known state is not current.")
    notes.append("Automatic-start services that stop by design (trigger-start update helpers, for "
                 "example) are included; the chart shows which are common across the fleet.")
    notes.append("Service state history records changes (and every sample of watched services); a "
                 "service that never changed state in the period is not listed.")
    if len(changes) >= _SVC_CHANGES_CAP:
        notes.append(f"Only the {_SVC_CHANGES_CAP:,} services with the most changes are counted.")
    retention = ctx.window_note(SERVICE_STATE_DAYS, "Windows service state history")
    if retention:
        notes.append(retention)

    tables = []
    if down_rows:
        tables.append({
            "title": _rows_title("Automatic-start or watched services not running", len(down_rows), down_n),
            "headers": ["Server", "Service", "Display name", "Start mode", "State", "Watched", "Updated"],
            "styles": ["text", "mono", "text", "text", "status", "text", "text"],
            "rows": [[r["server"], r["service_name"], _clip(r.get("display_name"), 60),
                      r.get("start_mode") or "—", r.get("state") or "unknown",
                      "Yes" if r.get("watched") else "No", ctx.fmt_short(r.get("updated_at"))]
                     for r in down_rows]})
    if changes:
        tables.append({
            "title": _rows_title("Services that changed state most often", min(top_n, len(changes)),
                                 len(changes)),
            "headers": ["Server", "Service", "State changes", "Stops", "Last state", "Last change"],
            "styles": ["text", "mono", "num", "num", "status", "text"],
            "rows": [[names.get(str(r[0]), str(r[0])[:8]), r[1], f"{_int(r[2]):,}", f"{_int(r[3]):,}",
                      r[4] or "unknown", ctx.fmt_short(r[5])] for r in changes[:top_n]]})
    charts = []
    if common:
        charts.append({"title": "Most common stopped services",
                       "series": {"kind": "rank", "unit": "servers",
                                  "points": [{"label": _clip(r.get("display_name") or r["service_name"], 48),
                                              "value": _int(r["servers"])} for r in common]}})
    days = _local_days(ctx)
    if days and (changes or daily):
        charts.append({"title": "Service stops per day",
                       "series": {"kind": "bars", "unit": "stops", "color": "warning",
                                  "points": [{"t": _iso_epoch(ctx, d), "v": daily.get(d, 0)} for d in days]}})
    return _section(
        sid, title,
        description=f"Windows services on {inv_servers:,} server(s) with service inventory: services not "
                    "running now and state changes over the period.",
        kpis=[
            {"label": "Services not running", "value": f"{down_n:,}",
             "accent": "warning" if down_n else "success",
             "subtitle": f"Auto-start or watched · on {_int(summary.get('servers')):,} of "
                         f"{inv_servers:,} servers"},
            {"label": "Watched services not running", "value": _count(summary.get("watched")),
             "accent": "danger" if _int(summary.get("watched")) else "success",
             "subtitle": "From agent policy watchlists"},
            {"label": "State changes in period", "value": f"{total_changes:,}", "accent": "info",
             "subtitle": f"{total_stops:,} stops"},
            {"label": "Services that changed", "value": f"{len(changes):,}", "accent": "neutral",
             "subtitle": "At least one state change"},
        ],
        charts=charts, tables=tables, notes=notes,
    )


# ─── Event log errors ───────────────────────────────────────────────────────

_EVT_LEVELS = {"error": ("critical", "error"), "warning": ("critical", "error", "warning")}


@register_section(
    "server_event_log_errors", title="Event Log Errors", category="Servers",
    description=("Critical and error events per day and the servers and logs that raised the most, "
                 "from the agents' Windows event log summaries."),
    scope="fleet", requires=("servers.view",), semantics="window",
    options=[
        top_n_option(20, 500),
        {"key": "level", "label": "Levels", "type": "select", "default": "error",
         "choices": [["error", "Critical and error"], ["warning", "Critical, error and warning"]]},
    ],
)
async def server_event_log_errors(ctx: SectionCtx) -> dict:
    sid = "server_event_log_errors"
    with_warn = ctx.opt("level") == "warning"
    title = "Event Log Errors and Warnings" if with_warn else "Event Log Errors"
    levels = _EVT_LEVELS["warning" if with_warn else "error"]
    top_n = int(ctx.opt("top_n"))
    p = {"f": ctx.frm, "t": ctx.to, "levels": levels}
    tot = await ctx.ch_rows("""
        SELECT sumIf(event_count, level = 'critical') AS crit,
               sumIf(event_count, level = 'error') AS err,
               sumIf(event_count, level = 'warning') AS warn,
               uniqExactIf(server_id, level IN %(levels)s AND event_count > 0) AS servers_hit,
               uniqExact(server_id) AS servers_reporting,
               uniqExactIf(server_id, log_name, level IN %(levels)s AND event_count > 0) AS pairs,
               count() AS summaries
        FROM zenplus.host_event_log_summary
        WHERE timestamp >= %(f)s AND timestamp < %(t)s""", p)
    t = tot[0] if tot else (0, 0, 0, 0, 0, 0, 0)
    crit, err, warn = _int(t[0]), _int(t[1]), _int(t[2])
    servers_hit, reporting, pairs, nrows = _int(t[3]), _int(t[4]), _int(t[5]), _int(t[6])
    retention = ctx.window_note(EVENT_LOG_DAYS, "Event log summary data")
    if not nrows:
        return _section(sid, title, description="Windows event log errors on agent-monitored servers.",
                        notes=["No event log summaries were received in this period."]
                        + ([retention] if retention else []))
    total = crit + err + (warn if with_warn else 0)

    prev_total = None
    if _prior_in_retention(ctx, EVENT_LOG_DAYS):
        pf, pt = ctx.prior_window()
        prev = await ctx.ch_rows("""
            SELECT sumIf(event_count, level IN %(levels)s) AS n, count() AS summaries
            FROM zenplus.host_event_log_summary
            WHERE timestamp >= %(f)s AND timestamp < %(t)s""", {"f": pf, "t": pt, "levels": levels})
        if prev and _int(prev[0][1]):
            prev_total = _int(prev[0][0])

    bucket, bparams = ctx.ch_bucket("timestamp", "1 DAY")
    daily = {_int(r[0]): r for r in await ctx.ch_rows(f"""
        SELECT toUnixTimestamp({bucket}) AS d,
               sumIf(event_count, level = 'critical') AS crit,
               sumIf(event_count, level = 'error') AS err,
               sumIf(event_count, level = 'warning') AS warn
        FROM zenplus.host_event_log_summary
        WHERE timestamp >= %(f)s AND timestamp < %(t)s
        GROUP BY d ORDER BY d""", {**p, **bparams})}
    top = await ctx.ch_rows("""
        SELECT toString(server_id) AS sid, log_name,
               sumIf(event_count, level = 'critical') AS crit,
               sumIf(event_count, level = 'error') AS err,
               sumIf(event_count, level = 'warning') AS warn,
               sum(event_count) AS total,
               max(timestamp) AS last_seen
        FROM zenplus.host_event_log_summary
        WHERE timestamp >= %(f)s AND timestamp < %(t)s
          AND level IN %(levels)s AND event_count > 0
        GROUP BY server_id, log_name
        ORDER BY total DESC, crit DESC
        LIMIT %(lim)s""", {**p, "lim": top_n})
    names = await _server_names(ctx, sorted({str(r[0]) for r in top}))

    days = _local_days(ctx)
    # A day with summaries but no matching events is 0; a day without any
    # summary is a gap (null), never 0.
    def series(idx: int) -> list[dict]:
        return [{"t": _iso_epoch(ctx, d), "v": _int(daily[d][idx]) if d in daily else None} for d in days]

    stacked = [{"name": "Critical", "color": "danger", "points": series(1)},
               {"name": "Error", "color": "warning", "points": series(2)}]
    if with_warn:
        stacked.append({"name": "Warning", "color": "info", "points": series(3)})
    headers = ["Server", "Log", "Critical", "Error"] + (["Warning"] if with_warn else []) + ["Total", "Last event"]
    rows = []
    for r in top:
        row = [names.get(str(r[0]), str(r[0])[:8]), r[1] or "—", f"{_int(r[2]):,}", f"{_int(r[3]):,}"]
        if with_warn:
            row.append(f"{_int(r[4]):,}")
        rows.append(row + [f"{_int(r[5]):,}", ctx.fmt_short(r[6])])
    kpis = [
        {"label": "Critical events", "value": f"{crit:,}", "accent": "danger" if crit else "success"},
        {"label": "Error events", "value": f"{err:,}", "accent": "warning" if err else "success"},
    ]
    if with_warn:
        kpis.append({"label": "Warning events", "value": f"{warn:,}", "accent": "info"})
    kpis += [
        {"label": "Total", "value": f"{total:,}", "accent": "primary",
         "subtitle": _change(total, prev_total) or "Previous period not available"},
        {"label": "Servers affected", "value": f"{servers_hit:,}",
         "accent": "warning" if servers_hit else "success",
         "subtitle": f"of {reporting:,} sending event log summaries"},
    ]
    notes = ["Counts cover the channels and levels each server's agent policy collects (System and "
             "Application at critical, error and warning by default)."]
    if retention:
        notes.append(retention)
    if not total:
        notes.append("No matching events were logged in this period.")
    return _section(
        sid, title,
        description=f"{'Critical, error and warning' if with_warn else 'Critical and error'} events per "
                    f"day and the server logs that raised the most, over the period.",
        kpis=kpis,
        charts=[{"title": "Events per day",
                 "series": {"kind": "stacked", "unit": "events", "points": [], "series": stacked}}]
        if days else [],
        tables=[{"title": _rows_title("Server logs with the most events", len(rows), pairs),
                 "headers": headers,
                 "styles": ["text", "text"] + ["num"] * (len(headers) - 3) + ["text"],
                 "rows": rows}] if rows else [],
        notes=notes,
    )


# ─── Server alerts ──────────────────────────────────────────────────────────

@register_section(
    "server_alerts", title="Server Alerts", category="Alerting",
    description=("Alerts raised on servers in the period by severity and source, the servers with the "
                 "most alerts, and time to resolve."),
    scope="fleet", requires=("servers.view", "alerts.view"), semantics="window",
    options=[top_n_option(15, 200)],
)
async def server_alerts(ctx: SectionCtx) -> dict:
    from app.services.alert_workflow import notice_sql

    sid, title = "server_alerts", "Server Alerts"
    base = (f"a.server_id IS NOT NULL AND a.status <> 'pending' AND NOT {notice_sql('a')} "
            "AND a.triggered_at >= :f AND a.triggered_at < :t")
    p = {"f": ctx.frm, "t": ctx.to}
    agg = await _pg_one(ctx, f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE a.severity = 'critical') AS critical,
               COUNT(*) FILTER (WHERE a.severity = 'warning') AS warning,
               COUNT(*) FILTER (WHERE a.severity NOT IN ('critical', 'warning')) AS info,
               COUNT(*) FILTER (WHERE a.status IN ('active', 'acknowledged')) AS open,
               COUNT(*) FILTER (WHERE a.resolved_at IS NOT NULL) AS resolved,
               COUNT(DISTINCT a.server_id) AS servers,
               AVG(EXTRACT(EPOCH FROM (a.resolved_at - a.triggered_at)))
                   FILTER (WHERE a.resolved_at IS NOT NULL AND a.resolved_at >= a.triggered_at) AS mttr_s
        FROM alerts a WHERE {base}""", p)
    total = _int(agg.get("total"))
    pf, pt = ctx.prior_window()
    prev = await _pg_one(ctx, f"SELECT COUNT(*) AS total FROM alerts a WHERE {base}", {"f": pf, "t": pt})
    prev_total = _int(prev.get("total")) if prev else None
    if not total:
        return _section(sid, title, description="Alerts raised on servers in the period.",
                        kpis=[{"label": "Server alerts", "value": "0", "accent": "success",
                               "subtitle": _change(0, prev_total)}],
                        notes=["No server alerts were raised in this period."])

    daily = {_int(r["d"]): r for r in await _pg(ctx, f"""
        SELECT EXTRACT(EPOCH FROM (date_trunc('day', a.triggered_at AT TIME ZONE :tz) AT TIME ZONE :tz))::bigint
                   AS d,
               COUNT(*) FILTER (WHERE a.severity = 'critical') AS critical,
               COUNT(*) FILTER (WHERE a.severity = 'warning') AS warning,
               COUNT(*) FILTER (WHERE a.severity NOT IN ('critical', 'warning')) AS info
        FROM alerts a WHERE {base}
        GROUP BY 1 ORDER BY 1""", {**p, "tz": ctx.tz_name})}
    sources = await _pg(ctx, f"""
        SELECT COALESCE(NULLIF(a.metadata->>'source', ''), 'other') AS source, COUNT(*) AS n
        FROM alerts a WHERE {base} GROUP BY 1 ORDER BY 2 DESC, 1""", p)
    top_n = int(ctx.opt("top_n"))
    top = await _pg(ctx, f"""
        SELECT {SERVER_NAME} AS server, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE a.severity = 'critical') AS critical,
               COUNT(*) FILTER (WHERE a.severity = 'warning') AS warning,
               COUNT(*) FILTER (WHERE a.status IN ('active', 'acknowledged')) AS open,
               AVG(EXTRACT(EPOCH FROM (a.resolved_at - a.triggered_at)))
                   FILTER (WHERE a.resolved_at IS NOT NULL AND a.resolved_at >= a.triggered_at) AS mttr_s,
               MAX(a.triggered_at) AS last_alert
        FROM alerts a JOIN servers s ON s.id = a.server_id
        WHERE {base}
        GROUP BY s.id ORDER BY total DESC, critical DESC, server LIMIT :lim""", {**p, "lim": top_n})

    days = _local_days(ctx)

    def series(key: str) -> list[dict]:
        # Alerts are complete in Postgres: a day without rows had none.
        return [{"t": _iso_epoch(ctx, d), "v": _int(daily[d][key]) if d in daily else 0} for d in days]

    critical, open_n = _int(agg.get("critical")), _int(agg.get("open"))
    mttr = _num(agg.get("mttr_s"))
    servers = _int(agg.get("servers"))
    charts = []
    if days:
        charts.append({"title": "Server alerts per day",
                       "series": {"kind": "stacked", "unit": "alerts", "points": [],
                                  "series": [{"name": "Critical", "color": "danger", "points": series("critical")},
                                             {"name": "Warning", "color": "warning", "points": series("warning")},
                                             {"name": "Info", "color": "info", "points": series("info")}]}})
    charts.append({"title": "Alerts by source",
                   "series": {"kind": "donut", "unit": "alerts",
                              "points": _fold([(SOURCE_LABELS.get(r["source"], str(r["source"]).replace("_", " ")
                                                                  .capitalize()), _int(r["n"])) for r in sources])}})
    return _section(
        sid, title,
        description=f"{total:,} alert(s) raised on {servers:,} server(s) in the period.",
        kpis=[
            {"label": "Server alerts", "value": f"{total:,}", "accent": "primary",
             "subtitle": _change(total, prev_total)},
            {"label": "Critical", "value": f"{critical:,}", "accent": "danger" if critical else "success",
             "subtitle": _share(critical, total)},
            {"label": "Still open", "value": f"{open_n:,}", "accent": "warning" if open_n else "success",
             "subtitle": "Raised in this period"},
            {"label": "Mean time to resolve", "value": _duration(mttr), "accent": "info",
             "subtitle": f"{_int(agg.get('resolved')):,} resolved" if mttr is not None else "None resolved yet"},
            {"label": "Servers alerting", "value": f"{servers:,}", "accent": "neutral"},
        ],
        charts=charts,
        tables=[{"title": _rows_title("Servers with the most alerts", len(top), servers),
                 "headers": ["Server", "Alerts", "Critical", "Warning", "Open", "Mean time to resolve",
                             "Last alert"],
                 "styles": ["text", "num", "num", "num", "num", "num", "text"],
                 "rows": [[r["server"], f"{_int(r['total']):,}", f"{_int(r['critical']):,}",
                           f"{_int(r['warning']):,}", f"{_int(r['open']):,}", _duration(_num(r.get("mttr_s"))),
                           ctx.fmt_short(r.get("last_alert"))] for r in top]}] if top else [],
        notes=["Informational notices and alerts cancelled during their hold period are not counted."],
    )


# ─── Software baseline compliance ───────────────────────────────────────────

_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}
_SCOPE_COLS = ("b.enabled AS _b_enabled, b.os_type AS _b_os_type, b.site_id AS _b_site_id, "
               "b.match_tags AS _b_match_tags, s.os_type AS _s_os_type, s.site_id AS _s_site_id, "
               "s.tags AS _s_tags, s.status AS _s_status")


def _in_scope(r: dict) -> bool:
    """baseline_in_scope on a joined result row (as the server Compliance tab
    does), so results for disabled or rescoped baselines are never counted."""
    from app.services.baseline_service import baseline_in_scope

    return baseline_in_scope(
        {"enabled": r.get("_b_enabled"), "os_type": r.get("_b_os_type"),
         "site_id": r.get("_b_site_id"), "match_tags": r.get("_b_match_tags")},
        {"os_type": r.get("_s_os_type"), "site_id": r.get("_s_site_id"),
         "tags": r.get("_s_tags"), "status": r.get("_s_status")})


@register_section(
    "server_software_compliance", title="Software Baseline Compliance", category="Configuration",
    description=("Servers compliant with their software baselines, violations by severity and type, "
                 "and the open violations, as of report generation."),
    scope="fleet", requires=("servers.view",), semantics="snapshot",
    options=[
        top_n_option(25, 500),
        {"key": "min_severity", "label": "Violations listed", "type": "select", "default": "all",
         "choices": [["all", "All severities"], ["warning", "Warning and critical"],
                     ["critical", "Critical only"]]},
    ],
)
async def server_software_compliance(ctx: SectionCtx) -> dict:
    sid, title = "server_software_compliance", "Software Baseline Compliance"
    # Pre-filters are a subset of baseline_in_scope (enabled baseline, enabled
    # server); the full check runs per row below.
    pairs = [r for r in await _pg(ctx, f"""
        SELECT b.id::text AS baseline_id, b.name AS baseline, s.id::text AS server_id,
               {SERVER_NAME} AS server, {_SCOPE_COLS},
               COUNT(*) AS results,
               COUNT(*) FILTER (WHERE res.status <> 'compliant') AS violations,
               COUNT(*) FILTER (WHERE res.status <> 'compliant' AND res.severity = 'critical') AS critical,
               COUNT(*) FILTER (WHERE res.status <> 'compliant' AND res.severity = 'warning') AS warning,
               COUNT(*) FILTER (WHERE res.status <> 'compliant'
                                AND res.severity NOT IN ('critical', 'warning')) AS info,
               COUNT(*) FILTER (WHERE res.status = 'missing') AS missing,
               COUNT(*) FILTER (WHERE res.status = 'outdated') AS outdated,
               COUNT(*) FILTER (WHERE res.status = 'prohibited') AS prohibited,
               MAX(res.evaluated_at) AS evaluated_at
        FROM server_baseline_results res
        JOIN software_baselines b ON b.id = res.baseline_id
        JOIN servers s ON s.id = res.server_id
        WHERE b.enabled AND s.status <> 'disabled'
        GROUP BY b.id, s.id""") if _in_scope(r)]
    if not pairs:
        return _section(sid, title, description="Software baseline compliance as of report generation.",
                        notes=["No software baseline results apply to any enabled server. Define baselines "
                               "under Servers → Baselines; servers are evaluated on each inventory upload."])

    servers: dict[str, bool] = {}
    baselines: dict[str, dict] = {}
    for r in pairs:
        ok = _int(r["violations"]) == 0
        servers[r["server_id"]] = servers.get(r["server_id"], True) and ok
        b = baselines.setdefault(r["baseline_id"], {"name": r["baseline"], "servers": 0, "compliant": 0,
                                                    "violations": 0, "critical": 0})
        b["servers"] += 1
        b["compliant"] += 1 if ok else 0
        b["violations"] += _int(r["violations"])
        b["critical"] += _int(r["critical"])
    n_servers = len(servers)
    n_ok = sum(1 for v in servers.values() if v)
    results = sum(_int(r["results"]) for r in pairs)
    sev = {k: sum(_int(r[k]) for r in pairs) for k in ("critical", "warning", "info")}
    kinds = {k: sum(_int(r[k]) for r in pairs) for k in ("missing", "outdated", "prohibited")}
    violations = sum(sev.values())
    bad_servers = n_servers - n_ok
    pct = n_ok / n_servers * 100 if n_servers else None

    min_sev = ctx.opt("min_severity")
    sev_filter = {"all": ("critical", "warning", "info"), "warning": ("critical", "warning"),
                  "critical": ("critical",)}[min_sev]
    detail = []
    if violations:
        detail = [r for r in await _pg(ctx, f"""
            SELECT {SERVER_NAME} AS server, b.name AS baseline, br.rule_type, br.package_match,
                   br.min_version, res.status, res.severity, res.found_package, res.found_version,
                   res.expected, res.first_failed_at, {_SCOPE_COLS}
            FROM server_baseline_results res
            JOIN software_baselines b ON b.id = res.baseline_id
            JOIN software_baseline_rules br ON br.id = res.rule_id
            JOIN servers s ON s.id = res.server_id
            WHERE b.enabled AND s.status <> 'disabled' AND res.status <> 'compliant'
              AND (CASE WHEN res.severity IN ('critical', 'warning') THEN res.severity ELSE 'info' END)
                  = ANY(:sevs)
            ORDER BY CASE res.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     res.first_failed_at NULLS LAST, server, b.name""", {"sevs": list(sev_filter)})
            if _in_scope(r)]
    top_n = int(ctx.opt("top_n"))

    def rule(r: dict) -> str:
        verb = "Prohibit" if r.get("rule_type") == "prohibited" else "Require"
        return f"{verb} {r.get('package_match') or '—'}"

    def found(r: dict) -> str:
        name = " ".join(str(x) for x in (r.get("found_package"), r.get("found_version")) if x)
        return _clip(name, 60) or "—"

    blist = sorted(baselines.values(), key=lambda b: (b["compliant"] / b["servers"] if b["servers"] else 1,
                                                       b["name"]))
    sev_label = {"all": "", "warning": " (warning and critical)", "critical": " (critical only)"}[min_sev]
    tables = [{"title": "Compliance by baseline",
               "headers": ["Baseline", "Servers in scope", "Compliant", "Compliant (%)", "Violations",
                           "Critical"],
               "styles": ["text", "num", "num", "pct-bar", "num", "num"],
               "rows": [[b["name"], f"{b['servers']:,}", f"{b['compliant']:,}",
                         _fmt_pct(b["compliant"] / b["servers"] * 100 if b["servers"] else None, 1),
                         f"{b['violations']:,}", f"{b['critical']:,}"] for b in blist]}]
    if detail:
        tables.append({
            "title": _rows_title(f"Open violations{sev_label}", min(top_n, len(detail)), len(detail)),
            "headers": ["Server", "Baseline", "Rule", "Finding", "Severity", "Found", "Expected",
                        "Failing since"],
            "styles": ["text", "text", "text", "status", "severity", "text", "text", "text"],
            "rows": [[r["server"], r["baseline"], _clip(rule(r), 60), r.get("status") or "—",
                      r.get("severity") if r.get("severity") in _SEV_RANK else "info", found(r),
                      _clip(r.get("expected"), 60) or "—", ctx.fmt_date(r.get("first_failed_at"))]
                     for r in detail[:top_n]]})
    notes = ["Results for disabled baselines, disabled servers and servers no longer in a baseline's "
             "scope are excluded, as on the server Compliance tab.",
             "Each server is evaluated against its latest software inventory on every inventory upload."]
    if not violations:
        notes.append("Every server in scope meets its software baselines.")
    elif not detail:
        notes.append(f"No open violations at the selected severity{sev_label}.")
    return _section(
        sid, title,
        description=f"{n_servers:,} server(s) evaluated against {len(baselines):,} software baseline(s), "
                    "as of report generation.",
        kpis=[
            {"label": "Servers compliant", "value": _fmt_pct(pct, 1),
             "accent": "success" if not bad_servers else "warning" if not sev["critical"] else "danger",
             "subtitle": f"{n_ok:,} of {n_servers:,} servers"},
            {"label": "Violations", "value": f"{violations:,}", "accent": "warning" if violations else "success",
             "subtitle": f"on {bad_servers:,} server(s)"},
            {"label": "Critical violations", "value": f"{sev['critical']:,}",
             "accent": "danger" if sev["critical"] else "success"},
            {"label": "Rule checks passing", "value": _fmt_pct((results - violations) / results * 100, 1)
             if results else "—", "accent": "info", "subtitle": f"{results - violations:,} of {results:,}"},
        ],
        charts=[{"title": "Violations by severity",
                 "series": {"kind": "donut", "unit": "violations",
                            "points": [{"label": k.capitalize(), "value": v} for k, v in sev.items() if v]}},
                {"title": "Violations by type",
                 "series": {"kind": "donut", "unit": "violations",
                            "points": [{"label": k.capitalize(), "value": v} for k, v in kinds.items() if v]}}]
        if violations else [],
        tables=tables, notes=notes,
    )


# ─── Software inventory ─────────────────────────────────────────────────────

_SW_FROM = ("FROM server_software_inventory i JOIN servers s ON s.id = i.server_id "
            "WHERE s.status <> 'disabled'")


def _version_sort_key():
    from app.services.baseline_service import compare_versions

    return cmp_to_key(lambda a, b: compare_versions(str(a).lstrip("vV"), str(b).lstrip("vV")))


@register_section(
    "server_software_inventory", title="Server Software Inventory", category="Inventory",
    description=("The most widely installed software, packages running several versions across the "
                 "fleet and software installed in the period, as of each server's last inventory."),
    scope="fleet", requires=("servers.view",), semantics="snapshot",
    options=[top_n_option(20, 500)],
)
async def server_software_inventory(ctx: SectionCtx) -> dict:
    sid, title = "server_software_inventory", "Server Software Inventory"
    p = {"f": ctx.frm, "t": ctx.to}
    agg = await _pg_one(ctx, f"""
        SELECT COUNT(DISTINCT i.package_name) AS packages, COUNT(*) AS installs,
               COUNT(DISTINCT i.server_id) AS servers,
               COUNT(*) FILTER (WHERE i.install_date >= :f AND i.install_date < :t) AS new_installs,
               COUNT(DISTINCT i.server_id) FILTER (WHERE i.install_date >= :f AND i.install_date < :t)
                   AS new_servers,
               (SELECT COUNT(*) FROM servers WHERE status <> 'disabled') AS fleet,
               (SELECT COUNT(*) FROM (SELECT i2.package_name FROM server_software_inventory i2
                                      JOIN servers s2 ON s2.id = i2.server_id
                                      WHERE s2.status <> 'disabled'
                                      GROUP BY i2.package_name
                                      HAVING COUNT(DISTINCT i2.version) > 1) x) AS sprawl
        {_SW_FROM}""", p)
    packages = _int(agg.get("packages"))
    if not packages:
        return _section(sid, title, description="Installed software across the server fleet.",
                        notes=["No software inventory has been received from any enabled server.",
                               "Patches, hotfixes and Windows updates are not collected by the agent."])
    top_n = int(ctx.opt("top_n"))
    top = await _pg(ctx, f"""
        SELECT i.package_name AS package, MAX(i.vendor) AS vendor,
               COUNT(DISTINCT i.server_id) AS servers, COUNT(DISTINCT i.version) AS versions
        {_SW_FROM}
        GROUP BY i.package_name ORDER BY servers DESC, i.package_name LIMIT :lim""", {"lim": top_n})
    sprawl_n = _int(agg.get("sprawl"))
    sprawl = await _pg(ctx, f"""
        SELECT i.package_name AS package, COUNT(DISTINCT i.version) AS versions,
               COUNT(DISTINCT i.server_id) AS servers
        {_SW_FROM}
        GROUP BY i.package_name HAVING COUNT(DISTINCT i.version) > 1
        ORDER BY versions DESC, servers DESC, i.package_name LIMIT :lim""", {"lim": top_n}) if sprawl_n else []
    per_version: dict[str, dict[str, int]] = {}
    if sprawl:
        for r in await _pg(ctx, f"""
                SELECT i.package_name AS package, i.version, COUNT(DISTINCT i.server_id) AS servers
                {_SW_FROM} AND i.package_name = ANY(:names) AND i.version IS NOT NULL
                GROUP BY i.package_name, i.version""", {"names": [r["package"] for r in sprawl]}):
            per_version.setdefault(r["package"], {})[str(r["version"])] = _int(r["servers"])
    new_n = _int(agg.get("new_installs"))
    installed = await _pg(ctx, f"""
        SELECT {SERVER_NAME} AS server, i.package_name AS package, i.version, i.vendor, i.install_date
        {_SW_FROM} AND i.install_date >= :f AND i.install_date < :t
        ORDER BY i.install_date DESC, server, i.package_name LIMIT :lim""", {**p, "lim": top_n}) if new_n else []

    key = _version_sort_key()
    sprawl_rows = []
    for r in sprawl:
        vers = per_version.get(r["package"], {})
        newest = max(vers, key=key) if vers else None
        behind = sum(n for v, n in vers.items() if v != newest) if newest else None
        sprawl_rows.append([r["package"], f"{_int(r['versions']):,}", f"{_int(r['servers']):,}",
                            newest or "—", f"{behind:,}" if behind is not None else "—"])
    servers, fleet = _int(agg.get("servers")), _int(agg.get("fleet"))
    tables = [{"title": _rows_title("Most widely installed software", len(top), packages),
               "headers": ["Package", "Vendor", "Servers", "Versions"],
               "styles": ["text", "text", "num", "num"],
               "rows": [[_clip(r["package"], 80), _clip(r.get("vendor"), 40) or "—", f"{_int(r['servers']):,}",
                         f"{_int(r['versions']):,}"] for r in top]}]
    if sprawl_rows:
        tables.append({"title": _rows_title("Version sprawl", len(sprawl_rows), sprawl_n),
                       "headers": ["Package", "Versions", "Servers", "Newest version", "Servers on older versions"],
                       "styles": ["text", "num", "num", "mono", "num"], "rows": sprawl_rows})
    if installed:
        tables.append({"title": _rows_title("Installed in this period", len(installed), new_n),
                       "headers": ["Server", "Package", "Version", "Vendor", "Installed"],
                       "styles": ["text", "text", "mono", "text", "text"],
                       "rows": [[r["server"], _clip(r["package"], 80), r.get("version") or "—",
                                 _clip(r.get("vendor"), 40) or "—", ctx.fmt_date(r.get("install_date"))]
                                for r in installed]})
    notes = ["Patches, hotfixes and Windows updates are not collected by the agent, so they do not "
             "appear here and patch compliance cannot be reported.",
             "Install dates are the calendar dates the operating system reports; software without an "
             "install date is not counted as installed in the period."]
    if not new_n:
        notes.append("No software with an install date in this period.")
    return _section(
        sid, title,
        description=f"{packages:,} distinct package(s) across {servers:,} server(s), as of each server's "
                    "last inventory upload.",
        kpis=[
            {"label": "Distinct packages", "value": f"{packages:,}", "accent": "primary",
             "subtitle": f"{_int(agg.get('installs')):,} installations"},
            {"label": "Servers inventoried", "value": f"{servers:,}", "accent": "info",
             "subtitle": _share(servers, fleet)},
            {"label": "Packages with several versions", "value": f"{sprawl_n:,}",
             "accent": "warning" if sprawl_n else "success", "subtitle": "Version sprawl"},
            {"label": "Installed in period", "value": f"{new_n:,}", "accent": "neutral",
             "subtitle": f"on {_int(agg.get('new_servers')):,} server(s)"},
        ],
        charts=[{"title": "Most widely installed software",
                 "series": {"kind": "rank", "unit": "servers",
                            "points": [{"label": _clip(r["package"], 48), "value": _int(r["servers"])}
                                       for r in top[:10]]}}] if top else [],
        tables=tables, notes=notes,
    )


# ─── Hardware inventory ─────────────────────────────────────────────────────

_DISKS_LATERAL = """
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS n,
           SUM(CASE WHEN (d->>'size_bytes') ~ '^[0-9]+$' THEN (d->>'size_bytes')::numeric END) AS size,
           COUNT(*) FILTER (WHERE lower(COALESCE(d->>'status', '')) NOT IN ('', 'ok', 'healthy', 'unknown'))
               AS faulty
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(s.physical_disks) = 'array'
                                   THEN s.physical_disks ELSE '[]'::jsonb END) d) dk ON TRUE
"""
_HW_SORT = {"name": "server", "memory": "s.memory_total_bytes DESC NULLS LAST, server",
            "cores": "s.cpu_cores DESC NULLS LAST, server"}
_GIB = 1024 ** 3


@register_section(
    "server_hardware_inventory", title="Server Hardware Inventory", category="Inventory",
    description=("Operating system, CPU model and cores, memory and physical disks of each server, as "
                 "reported by its agent, as of report generation."),
    scope="fleet", requires=("servers.view",), semantics="snapshot",
    options=[
        top_n_option(50, 1000),
        {"key": "sort", "label": "Sort by", "type": "select", "default": "name",
         "choices": [["name", "Server name"], ["memory", "Memory (largest first)"],
                     ["cores", "CPU cores (most first)"]]},
    ],
)
async def server_hardware_inventory(ctx: SectionCtx) -> dict:
    sid, title = "server_hardware_inventory", "Server Hardware Inventory"
    gib = "ROUND(s.memory_total_bytes / 1073741824.0)"
    agg = await _pg_one(ctx, f"""
        SELECT COUNT(*) AS servers,
               COUNT(*) FILTER (WHERE s.cpu_model IS NOT NULL OR s.cpu_cores IS NOT NULL
                                OR s.memory_total_bytes IS NOT NULL OR COALESCE(dk.n, 0) > 0) AS with_hw,
               SUM(s.cpu_cores) AS logical, SUM(s.cpu_physical_cores) AS physical,
               SUM(s.memory_total_bytes) AS memory, SUM(dk.n) AS disks, SUM(dk.size) AS disk_bytes,
               SUM(dk.faulty) AS faulty,
               COUNT(*) FILTER (WHERE {gib} <= 8) AS m8,
               COUNT(*) FILTER (WHERE {gib} > 8 AND {gib} <= 16) AS m16,
               COUNT(*) FILTER (WHERE {gib} > 16 AND {gib} <= 32) AS m32,
               COUNT(*) FILTER (WHERE {gib} > 32 AND {gib} <= 64) AS m64,
               COUNT(*) FILTER (WHERE {gib} > 64 AND {gib} <= 128) AS m128,
               COUNT(*) FILTER (WHERE {gib} > 128) AS mmore,
               COUNT(*) FILTER (WHERE s.memory_total_bytes IS NULL) AS mnone
        FROM servers s {_DISKS_LATERAL}
        WHERE s.status <> 'disabled'""")
    servers, with_hw = _int(agg.get("servers")), _int(agg.get("with_hw"))
    if not servers:
        return _section(sid, title, description="Server hardware as reported by agents.",
                        notes=["No servers are registered."])
    top_n = int(ctx.opt("top_n"))
    order = _HW_SORT.get(ctx.opt("sort"), "server")
    rows = await _pg(ctx, f"""
        SELECT {SERVER_NAME} AS server, s.os_type, s.os_name, s.os_version, s.architecture, s.cpu_model,
               s.cpu_physical_cores, s.cpu_cores, s.memory_total_bytes, dk.n AS disks, dk.size AS disk_bytes,
               dk.faulty
        FROM servers s {_DISKS_LATERAL}
        WHERE s.status <> 'disabled'
        ORDER BY {order} LIMIT :lim""", {"lim": top_n})
    models = await _pg(ctx, """
        SELECT COALESCE(NULLIF(s.cpu_model, ''), 'Not reported') AS model, COUNT(*) AS n
        FROM servers s WHERE s.status <> 'disabled'
        GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 8""")

    def cores(r: dict) -> str:
        phys, logi = r.get("cpu_physical_cores"), r.get("cpu_cores")
        if phys is None and logi is None:
            return "—"
        return f"{phys if phys is not None else '—'} / {logi if logi is not None else '—'}"

    def disks(r: dict) -> str:
        n = _int(r.get("disks"))
        if not n:
            return "—"
        size = _num(r.get("disk_bytes"))
        return f"{n} · {_fmt_bytes(size)}" if size else str(n)

    def disk_health(r: dict) -> str:
        if not _int(r.get("disks")):
            return "—"
        return "degraded" if _int(r.get("faulty")) else "healthy"

    def os_text(r: dict) -> str:
        base = _os_label(r.get("os_name"), r.get("os_type"))
        return f"{base} ({r['architecture']})" if r.get("architecture") else base

    faulty = _int(agg.get("faulty"))
    mem_total = _num(agg.get("memory"))
    disk_total = _num(agg.get("disk_bytes"))
    bands = [("Up to 8 GB", "m8"), ("9–16 GB", "m16"), ("17–32 GB", "m32"), ("33–64 GB", "m64"),
             ("65–128 GB", "m128"), ("Over 128 GB", "mmore"), ("Not reported", "mnone")]
    sort_label = {"name": "", "memory": " by memory", "cores": " by CPU cores"}.get(ctx.opt("sort"), "")
    notes = ["Hardware is reported by the agent's inventory; servers without an agent or with older "
             "agents show — where a value is not reported."]
    if faulty:
        notes.append(f"{faulty:,} physical disk(s) report a status other than OK.")
    return _section(
        sid, title,
        description=f"Hardware of {servers:,} server(s) ({with_hw:,} with hardware inventory), as of report "
                    "generation.",
        kpis=[
            {"label": "Servers with hardware data", "value": f"{with_hw:,}", "accent": "primary",
             "subtitle": _share(with_hw, servers)},
            {"label": "Logical CPU cores", "value": _count(agg.get("logical")) if agg.get("logical") is not None
             else "—", "accent": "info",
             "subtitle": f"{_int(agg.get('physical')):,} physical" if agg.get("physical") is not None
             else "Physical cores not reported"},
            {"label": "Installed memory", "value": _fmt_bytes(mem_total) if mem_total else "—", "accent": "info"},
            {"label": "Physical disks", "value": _count(agg.get("disks")) if agg.get("disks") is not None
             else "—", "accent": "neutral",
             "subtitle": _fmt_bytes(disk_total) + " raw capacity" if disk_total else "Capacity not reported"},
            {"label": "Disks reporting a fault", "value": f"{faulty:,}" if agg.get("disks") is not None else "—",
             "accent": "danger" if faulty else "success"},
        ],
        charts=[c for c in (
            {"title": "Servers by installed memory",
             "series": {"kind": "donut", "unit": "servers",
                        "points": [{"label": label, "value": _int(agg.get(k))} for label, k in bands
                                   if _int(agg.get(k))]}},
            {"title": "Most common CPU models",
             "series": {"kind": "rank", "unit": "servers",
                        "points": [{"label": _clip(r["model"], 48), "value": _int(r["n"])} for r in models]}}
            if models else None) if c],
        tables=[{"title": _rows_title(f"Server hardware{sort_label}", len(rows), servers),
                 "headers": ["Server", "Operating system", "CPU model", "Cores (physical / logical)", "Memory",
                             "Disks", "Disk health"],
                 "styles": ["text", "text", "text", "num", "num", "num", "status"],
                 "rows": [[r["server"], _clip(os_text(r), 60), _clip(r.get("cpu_model"), 60) or "—", cores(r),
                           _fmt_bytes(float(r["memory_total_bytes"])) if r.get("memory_total_bytes") else "—",
                           disks(r), disk_health(r)] for r in rows]}] if rows else [],
        notes=notes,
    )


# ─── Agent fleet health ─────────────────────────────────────────────────────

_AGENT_ISSUE_SQL = (
    "(a.status IN ('offline', 'stale', 'error') OR a.authorized_at IS NULL"
    " OR a.certificate_expires_at < NOW() + make_interval(days => :cert_days)"
    " OR NULLIF(a.config_apply_error, '') IS NOT NULL OR a.update_status->>'state' = 'failed'"
    " OR a.pending_conflict_secret_hash IS NOT NULL)")
AGENT_STATUS_ORDER = ("online", "updating", "enrolling", "stale", "offline", "error", "disabled")


@register_section(
    "agent_fleet_health", title="Agent Fleet Health", category="Agents",
    description=("Agents by status and version against the newest published package, pending "
                 "authorizations, expiring certificates, configuration errors and upgrades, as of "
                 "report generation."),
    scope="fleet", requires=("servers.view",), semantics="snapshot",
    options=[
        top_n_option(25, 500),
        {"key": "cert_days", "label": "Certificate warning (days)", "type": "int", "default": 30,
         "min": 1, "max": 365, "help": "Certificates expiring within this many days are flagged."},
    ],
)
async def agent_fleet_health(ctx: SectionCtx) -> dict:
    sid, title = "agent_fleet_health", "Agent Fleet Health"
    cert_days = int(ctx.opt("cert_days"))
    p = {"cert_days": cert_days}
    agg = await _pg_one(ctx, f"""
        SELECT COUNT(*) FILTER (WHERE a.revoked_at IS NULL) AS agents,
               COUNT(*) FILTER (WHERE a.revoked_at IS NOT NULL) AS revoked,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND a.authorized_at IS NULL) AS pending_auth,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND a.certificate_expires_at IS NOT NULL)
                   AS cert_reported,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL
                                AND a.certificate_expires_at < NOW() + make_interval(days => :cert_days))
                   AS cert_expiring,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND a.certificate_expires_at < NOW()) AS cert_expired,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND NULLIF(a.config_apply_error, '') IS NOT NULL)
                   AS config_errors,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND a.update_status->>'state' = 'failed')
                   AS update_failed,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND (
                       a.update_status->>'state' IN ('requested', 'downloading', 'installing')
                       OR NULLIF(a.desired_version, '') IS NOT NULL)) AS updating,
               COUNT(*) FILTER (WHERE a.revoked_at IS NULL AND {_AGENT_ISSUE_SQL}) AS attention
        FROM agents a""", p)
    agents = _int(agg.get("agents"))
    if not agents:
        return _section(sid, title, description="Server monitoring agents as of report generation.",
                        notes=["No agents are enrolled."
                               + (f" {_int(agg.get('revoked')):,} revoked agent(s) are not counted."
                                  if _int(agg.get("revoked")) else "")])
    status = {str(r["status"]): _int(r["n"]) for r in await _pg(
        ctx, "SELECT a.status, COUNT(*) AS n FROM agents a WHERE a.revoked_at IS NULL GROUP BY 1")}
    versions = await _pg(ctx, """
        SELECT a.platform, COALESCE(NULLIF(a.version, ''), '') AS version, COUNT(*) AS n
        FROM agents a WHERE a.revoked_at IS NULL GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2""")
    packages = await _pg(ctx, "SELECT p.platform, p.version FROM agent_packages p WHERE p.channel = 'stable'")
    key = _version_sort_key()
    latest: dict[str, str] = {}
    for pk in packages:
        cur = latest.get(pk["platform"])
        if pk.get("version") and (cur is None or key(pk["version"]) > key(cur)):
            latest[pk["platform"]] = pk["version"]

    def version_state(platform: str, version: str) -> str:
        newest = latest.get(platform)
        if not version:
            return "Not reported"
        if not newest:
            return "No package published"
        return "Latest" if key(version) >= key(newest) else f"Outdated (latest {newest})"

    outdated = sum(_int(v["n"]) for v in versions
                   if v["version"] and latest.get(v["platform"])
                   and key(v["version"]) < key(latest[v["platform"]]))
    top_n = int(ctx.opt("top_n"))
    attention_n = _int(agg.get("attention"))
    rows = await _pg(ctx, f"""
        SELECT COALESCE(NULLIF(a.hostname, ''), a.agent_uid) AS agent,
               COALESCE(NULLIF(s.display_name, ''), NULLIF(s.hostname, '')) AS server,
               a.platform, a.status, a.version, a.last_heartbeat_at, a.authorized_at IS NULL AS pending,
               a.certificate_expires_at, a.config_apply_error,
               a.update_status->>'state' AS upd_state, a.update_status->>'error' AS upd_error,
               a.update_status->>'target_version' AS upd_target,
               a.pending_conflict_secret_hash IS NOT NULL AS conflict
        FROM agents a LEFT JOIN servers s ON s.id = a.server_id
        WHERE a.revoked_at IS NULL AND {_AGENT_ISSUE_SQL}
        ORDER BY CASE a.status WHEN 'error' THEN 0 WHEN 'offline' THEN 1 WHEN 'stale' THEN 2 ELSE 3 END,
                 a.last_heartbeat_at ASC NULLS FIRST, agent
        LIMIT :lim""", {**p, "lim": top_n}) if attention_n else []
    now = _utcnow()

    def issues(r: dict) -> str:
        out = []
        if r.get("status") in ("offline", "stale"):
            out.append(f"No heartbeat since {ctx.fmt_short(r.get('last_heartbeat_at'))}"
                       if r.get("last_heartbeat_at") else "Never sent a heartbeat")
        if r.get("status") == "error":
            out.append("Agent reports an error")
        if r.get("pending"):
            out.append("Pending authorization")
        exp = _aware(r.get("certificate_expires_at"))
        if exp is not None and exp < now + timedelta(days=cert_days):
            out.append(("Certificate expired " if exp < now else "Certificate expires ") + ctx.fmt_date(exp))
        if r.get("config_apply_error"):
            out.append("Config error: " + _clip(r["config_apply_error"], 80))
        if r.get("upd_state") == "failed":
            target = f" to {r['upd_target']}" if r.get("upd_target") else ""
            out.append(f"Upgrade{target} failed" + (": " + _clip(r["upd_error"], 60) if r.get("upd_error") else ""))
        if r.get("conflict"):
            out.append("Registration conflict")
        return "; ".join(out) or "—"

    online = status.get("online", 0)
    offline, stale = status.get("offline", 0), status.get("stale", 0)
    pending_auth = _int(agg.get("pending_auth"))
    cert_reported = _int(agg.get("cert_reported"))
    cert_expiring, cert_expired = _int(agg.get("cert_expiring")), _int(agg.get("cert_expired"))
    updating, failed = _int(agg.get("updating")), _int(agg.get("update_failed"))
    revoked = _int(agg.get("revoked"))
    notes = ["Outdated compares each agent with the newest stable package published on this appliance for "
             "its platform; agents on platforms without a published package are not rated."]
    if revoked:
        notes.append(f"{revoked:,} revoked agent(s) are not counted.")
    if _int(agg.get("config_errors")):
        notes.append(f"{_int(agg.get('config_errors')):,} agent(s) report a configuration apply error.")
    if not cert_reported:
        notes.append("No agent reports a certificate expiry date: certificate status is not measured.")
    if not attention_n:
        notes.append("No agent needs attention.")
    return _section(
        sid, title,
        description=f"State of {agents:,} enrolled agent(s) as of report generation.",
        kpis=[
            {"label": "Agents", "value": f"{agents:,}", "accent": "primary",
             "subtitle": f"{revoked:,} revoked not counted" if revoked else "All enrolled agents"},
            {"label": "Online", "value": f"{online:,}", "accent": "success", "subtitle": _share(online, agents)},
            {"label": "Offline or stale", "value": f"{offline + stale:,}",
             "accent": "danger" if offline else "warning" if stale else "success",
             "subtitle": f"{offline:,} offline · {stale:,} stale"},
            {"label": "Pending authorization", "value": f"{pending_auth:,}",
             "accent": "warning" if pending_auth else "success"},
            {"label": "Outdated", "value": f"{outdated:,}" if latest else "—",
             "accent": "warning" if outdated else "neutral" if not latest else "success",
             "subtitle": f"{updating:,} upgrade(s) in progress · {failed:,} failed"},
            {"label": f"Certificates expiring ≤ {cert_days} d", "value": f"{cert_expiring:,}" if cert_reported
             else "—", "accent": "danger" if cert_expired else "warning" if cert_expiring else
             "neutral" if not cert_reported else "success",
             "subtitle": f"{cert_expired:,} already expired" if cert_reported else "Not reported by agents"},
        ],
        charts=[
            {"title": "Agents by status",
             "series": {"kind": "donut", "unit": "agents",
                        "points": [{"label": k.capitalize(), "value": status[k]} for k in
                                   AGENT_STATUS_ORDER + tuple(sorted(set(status) - set(AGENT_STATUS_ORDER)))
                                   if status.get(k)]}},
            {"title": "Agents by version",
             "series": {"kind": "rank", "unit": "agents",
                        "points": [{"label": f"{OS_FAMILY.get(v['platform'], str(v['platform']).title())} "
                                             f"{v['version'] or '(version not reported)'}", "value": _int(v["n"])}
                                   for v in versions[:10]]}},
        ],
        tables=[t for t in (
            {"title": "Agent versions",
             "headers": ["Platform", "Version", "Agents", "Status"],
             "styles": ["text", "mono", "num", "text"],
             "rows": [[OS_FAMILY.get(v["platform"], str(v["platform"]).title()), v["version"] or "—",
                       f"{_int(v['n']):,}", version_state(v["platform"], v["version"])] for v in versions]},
            {"title": _rows_title("Agents needing attention", len(rows), attention_n),
             "headers": ["Agent", "Server", "Status", "Version", "Issues", "Last heartbeat"],
             "styles": ["text", "text", "status", "mono", "text", "text"],
             "rows": [[r["agent"], r.get("server") or "—", r.get("status") or "unknown", r.get("version") or "—",
                       issues(r), ctx.fmt_short(r.get("last_heartbeat_at"))] for r in rows]}
            if rows else None) if t],
        notes=notes,
    )


# ─── Agent resource overhead ────────────────────────────────────────────────

_OVERHEAD_SORT = {"cpu": ("cpu_avg", "CPU"), "memory": ("mem_avg", "memory"),
                  "lag": ("lag_p95", "upload lag"), "backlog": ("spool_max", "spool backlog")}


@register_section(
    "agent_overhead", title="Agent Resource Overhead", category="Agents",
    description=("CPU and memory used by the monitoring agents themselves, their queue and spool backlog "
                 "and upload lag over the period."),
    scope="fleet", requires=("servers.view",), semantics="window",
    options=[
        top_n_option(15, 200),
        {"key": "sort", "label": "Rank agents by", "type": "select", "default": "cpu",
         "choices": [["cpu", "Agent CPU"], ["memory", "Agent memory"], ["lag", "Upload lag (p95)"],
                     ["backlog", "Spool backlog"]]},
    ],
)
async def agent_overhead(ctx: SectionCtx) -> dict:
    sid, title = "agent_overhead", "Agent Resource Overhead"
    p = {"f": ctx.frm, "t": ctx.to}
    tot = await ctx.ch_rows("""
        SELECT uniqExact(agent_id) AS agents, avg(cpu_pct) AS cpu_avg, quantile(0.95)(cpu_pct) AS cpu_p95,
               max(cpu_pct) AS cpu_max, avg(memory_bytes) AS mem_avg, max(memory_bytes) AS mem_max,
               quantile(0.95)(upload_lag_ms) AS lag_p95, max(spool_bytes) AS spool_max,
               max(queue_depth) AS queue_max, countIf(config_apply_ok = 0) AS cfg_fail, count() AS samples
        FROM zenplus.agent_health_metrics
        WHERE timestamp >= %(f)s AND timestamp < %(t)s""", p)
    t = tot[0] if tot else None
    retention = ctx.window_note(AGENT_HEALTH_DAYS, "Agent health telemetry")
    if not t or not _int(t[10]):
        return _section(sid, title, description="Resource use of the monitoring agents over the period.",
                        notes=["No agent health telemetry was received in this period."]
                        + ([retention] if retention else []))
    sort_key = ctx.opt("sort") if ctx.opt("sort") in _OVERHEAD_SORT else "cpu"
    order_col, sort_label = _OVERHEAD_SORT[sort_key]
    top_n = int(ctx.opt("top_n"))
    top = await ctx.ch_rows(f"""
        SELECT toString(agent_id) AS aid, toString(argMax(server_id, timestamp)) AS sid,
               avg(cpu_pct) AS cpu_avg, max(cpu_pct) AS cpu_max, avg(memory_bytes) AS mem_avg,
               max(memory_bytes) AS mem_max, max(queue_depth) AS queue_max, max(spool_bytes) AS spool_max,
               quantile(0.95)(upload_lag_ms) AS lag_p95, countIf(config_apply_ok = 0) AS cfg_fail
        FROM zenplus.agent_health_metrics
        WHERE timestamp >= %(f)s AND timestamp < %(t)s
        GROUP BY agent_id
        ORDER BY {order_col} DESC, aid
        LIMIT %(lim)s""", {**p, "lim": top_n})
    bucket, bparams = ctx.ch_bucket("timestamp")
    trend = await ctx.ch_rows(f"""
        SELECT toUnixTimestamp({bucket}) AS b, avg(cpu_pct) AS cpu_avg, max(cpu_pct) AS cpu_max,
               quantile(0.95)(upload_lag_ms) AS lag_p95
        FROM zenplus.agent_health_metrics
        WHERE timestamp >= %(f)s AND timestamp < %(t)s
        GROUP BY b ORDER BY b""", {**p, **bparams})
    ids = [str(r[0]) for r in top]
    labels: dict[str, tuple[str, str]] = {}
    if ids:
        for r in await _pg(ctx, """
                SELECT a.id::text AS id, COALESCE(NULLIF(a.hostname, ''), a.agent_uid) AS agent,
                       COALESCE(NULLIF(s.display_name, ''), NULLIF(s.hostname, '')) AS server
                FROM agents a LEFT JOIN servers s ON s.id = a.server_id
                WHERE a.id::text = ANY(:ids)""", {"ids": ids}):
            labels[str(r["id"])] = (r["agent"], r.get("server") or "—")

    def pct(v: Any) -> str:
        return _fmt_pct(_num(v), 1)

    def size(v: Any) -> str:
        n = _num(v)
        return "—" if n is None else _fmt_bytes(n)

    agents = _int(t[0])
    cfg_fail = _int(t[9])
    notes = ["Agent CPU and memory are the agent process's own use, not the host's."]
    if cfg_fail:
        notes.append(f"{cfg_fail:,} health sample(s) reported a failed configuration apply.")
    if retention:
        notes.append(retention)
    return _section(
        sid, title,
        description=f"Resource use of {agents:,} agent(s) sending health telemetry over the period.",
        kpis=[
            {"label": "Agents reporting", "value": f"{agents:,}", "accent": "primary"},
            {"label": "Agent CPU (average)", "value": pct(t[1]), "accent": "info",
             "subtitle": f"p95 {pct(t[2])} · peak {pct(t[3])}"},
            {"label": "Agent memory (average)", "value": size(t[4]), "accent": "info",
             "subtitle": f"peak {size(t[5])}"},
            {"label": "Upload lag (p95)", "value": _fmt_ms(_num(t[6])), "accent": "neutral"},
            {"label": "Largest spool backlog", "value": size(t[7]),
             "accent": "warning" if _int(t[7]) else "success",
             "subtitle": f"max queue {_int(t[8]):,} item(s)"},
        ],
        charts=[c for c in (
            {"title": "Agent CPU",
             "series": {"kind": "line", "unit": "%",
                        "series": [{"name": "Average", "color": "primary",
                                    "points": [{"t": _iso_epoch(ctx, _int(r[0])), "v": _num(r[1])} for r in trend]},
                                   {"name": "Peak", "color": "warning",
                                    "points": [{"t": _iso_epoch(ctx, _int(r[0])), "v": _num(r[2])} for r in trend]}],
                        "points": []}} if trend else None,
            {"title": "Upload lag (p95)",
             "series": {"kind": "line", "unit": "ms", "color": "info",
                        "points": [{"t": _iso_epoch(ctx, _int(r[0])), "v": _num(r[3])} for r in trend]}}
            if trend else None) if c],
        tables=[{"title": _rows_title(f"Agents by {sort_label}", len(top), agents),
                 "headers": ["Agent", "Server", "Avg CPU", "Peak CPU", "Avg memory", "Max spool",
                             "Upload lag p95"],
                 "styles": ["text", "text", "num", "num", "num", "num", "num"],
                 "rows": [[labels.get(str(r[0]), (str(r[0])[:8], "—"))[0],
                           labels.get(str(r[0]), (str(r[0])[:8], "—"))[1],
                           pct(r[2]), pct(r[3]), size(r[4]), size(r[7]), _fmt_ms(_num(r[8]))]
                          for r in top]}] if top else [],
        notes=notes,
    )


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "servers_fleet_health", "Server Fleet Health",
    "Operational review of the server estate: health and the servers that need attention, telemetry "
    "coverage, reboots, Windows services, event log errors, server alerts and agent health.",
    "servers",
    ["server_fleet_status", "server_telemetry_coverage", "server_uptime_reboots", "server_service_health",
     "server_event_log_errors", "server_alerts", "agent_fleet_health"],
    audience=("IT manager", "Systems engineer"), tags=("servers", "health", "weekly review"),
    default_period="last_7d", frequency="weekly", icon="Server", featured=True,
)
template(
    "servers_telemetry_coverage", "Server Telemetry Coverage",
    "Whether every agent-monitored server delivered the metrics its policy promises: samples received "
    "against expected, silent servers and the agents behind the gaps.",
    "servers",
    ["server_telemetry_coverage", S("agent_fleet_health", "Agents Needing Attention", top_n=25)],
    audience=("Systems engineer", "NOC operator"), tags=("coverage", "data quality", "agents"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)
template(
    "servers_reboots_uptime", "Server Reboots and Uptime",
    "Servers that restarted in the period, the longest-running servers and those overdue for a "
    "reboot, with current server health.",
    "servers",
    ["server_uptime_reboots", S("server_fleet_status", "Current Server Health", top_n=10)],
    audience=("Systems engineer", "IT manager"), tags=("reboots", "uptime", "patching"),
    default_period="last_30d", frequency="monthly", icon="Gauge",
)
template(
    "servers_windows_services", "Windows Service Health",
    "Automatic-start and watched Windows services that are not running, the services that stop or "
    "flap most often, and the related event log errors.",
    "servers",
    ["server_service_health", S("server_event_log_errors", "Related Event Log Errors", top_n=10)],
    audience=("Systems engineer", "NOC operator"), tags=("windows", "services"),
    default_period="last_7d", frequency="weekly", icon="Settings2",
)
template(
    "servers_event_log_errors", "Windows Event Log Errors",
    "Critical and error events per day across the fleet and the servers and logs that raised the "
    "most, with a comparison to the previous period.",
    "servers",
    ["server_event_log_errors", S("server_event_log_errors", "Event Log Errors and Warnings", level="warning",
                                  top_n=10)],
    audience=("Systems engineer", "NOC operator"), tags=("windows", "event log", "errors"),
    default_period="last_7d", frequency="weekly", icon="FileText",
)
template(
    "servers_alerts", "Server Alerts",
    "Alerts raised on servers by severity and source, the servers with the most alerts, time to "
    "resolve and the current health of the fleet.",
    "servers",
    ["server_alerts", S("server_fleet_status", "Current Server Health", top_n=10)],
    audience=("NOC operator", "IT manager"), tags=("alerts", "mttr", "servers"),
    default_period="last_7d", frequency="weekly", icon="Bell",
)
template(
    "servers_software_compliance", "Software Baseline Compliance",
    "Servers compliant with their software baselines, violations by severity and type, open "
    "violations and the installed software behind them.",
    "security",
    ["server_software_compliance",
     S("server_software_inventory", "Software Inventory and Version Sprawl", top_n=15)],
    audience=("Security & audit", "Systems engineer", "IT manager"), tags=("compliance", "software", "audit"),
    default_period="last_30d", frequency="monthly", icon="ShieldCheck",
)
template(
    "servers_software_inventory", "Server Software Inventory",
    "The most widely installed software, packages running several versions across the fleet and "
    "software installed in the period.",
    "inventory",
    ["server_software_inventory"],
    audience=("IT manager", "Systems engineer", "Security & audit"), tags=("software", "assets"),
    default_period="last_30d", frequency="monthly", icon="Layers",
)
template(
    "servers_hardware_inventory", "Server Hardware Inventory",
    "Operating system, CPU model and cores, memory and physical disks of every server, with disks "
    "that report a fault.",
    "inventory",
    ["server_hardware_inventory"],
    audience=("IT manager", "Capacity planner", "Systems engineer"), tags=("hardware", "assets"),
    default_period="last_30d", frequency="monthly", icon="Cpu",
)
template(
    "agents_fleet_health", "Agent Fleet Health",
    "Monitoring agents by status and version against the newest published package, pending "
    "authorizations, expiring certificates, failed upgrades, telemetry coverage and agent overhead.",
    "servers",
    ["agent_fleet_health", "server_telemetry_coverage", S("agent_overhead", None, top_n=10)],
    audience=("Systems engineer", "IT manager"), tags=("agents", "versions", "certificates"),
    default_period="last_7d", frequency="weekly", icon="Boxes",
)
template(
    "agents_resource_overhead", "Agent Resource Overhead",
    "CPU and memory used by the monitoring agents themselves, spool and queue backlog and upload lag, "
    "ranked by CPU and by upload lag.",
    "servers",
    ["agent_overhead", S("agent_overhead", "Agents with the Highest Upload Lag", sort="lag", top_n=10)],
    audience=("Systems engineer",), tags=("agents", "overhead", "cpu"),
    default_period="last_7d", frequency="weekly", icon="Gauge",
)
