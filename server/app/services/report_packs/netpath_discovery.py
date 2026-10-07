"""NetPath and Discovery sections.

NetPath (migrate-075): per-probe path health, route changes and stability,
the latency/loss trend of the slowest paths and the hops that drop packets.
Discovery (migrate-017/018/031): scan activity, devices found on the network
that are not managed (shadow inventory) and hosts whose credentials failed.

Data rules:
- netpath_snapshots holds one row per trace run. A run that did not reach its
  target stores rtt_ms/jitter_ms NULL and loss_pct 100, so RTT, jitter and
  end-to-end loss are measured over the runs that reached the target and
  unreached runs count only against reachability. worst_hop_loss_pct (set by
  the poller) is the highest loss at any intermediate hop, destination
  excluded. path_changed is set only when this run and the previous one both
  traced hops and their topology hash differs.
- Probe status follows the NetPath API (_CURRENT_STATUS_SQL in
  api/v1/netpath.py): a probe that has not run for three intervals is stale.
- Discovery stores one result per scanned address per run (including the
  addresses that did not answer, status 'failed'), so results are reduced to
  the latest relevant observation per IP (DISTINCT ON) inside a bounded
  look-back. Credential failures are the poller's own definition
  (credential_status invalid / permission_issue), as counted on each run.

Neither module carries tag scoping, so every section here is fleet-scoped
(omitted for visibility-scoped accounts).

Query cost: NetPath snapshots are never pruned, so every NetPath query walks
the (probe_id, run_at) index through a LATERAL join per probe, aggregates in
Postgres and reads only scalar columns. Only netpath_problem_hops expands the
per-run hop JSON, only for reached runs whose worst intermediate hop crossed
the loss threshold, and at most HOP_RUN_CAP such runs per probe (sampled
evenly across the period and weighted back up when a probe has more).
"""

from __future__ import annotations

import ipaddress
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

NETPATH = ("netpath.view",)
DISCOVERY = ("discovery.view",)

# Same rule as the NetPath API (_CURRENT_STATUS_SQL in api/v1/netpath.py):
# a probe that has not run for three intervals is stale, not "ok".
_PROBE_STATUS_SQL = """CASE WHEN NOT p.enabled THEN 'disabled'
    WHEN p.last_run_at IS NULL THEN 'pending'
    WHEN p.last_run_at <= now() - make_interval(secs => GREATEST(180, p.interval_s * 3)) THEN 'stale'
    ELSE COALESCE(p.last_status, 'pending') END"""
_STATUS_WORD = {"unreached": "unreachable"}
_TROUBLE = ("degraded", "down", "unreached")

_BUCKET_SECONDS = {"5 MINUTE": 300, "30 MINUTE": 1800, "2 HOUR": 7200, "1 DAY": 86400}
_INTERVAL_WORDS = {300: "5 minutes", 1800: "30 minutes", 3600: "hour", 7200: "2 hours", 86400: "day"}

# Problem hops: at most this many lossy runs per probe have their hop JSON
# expanded; probes with more are sampled evenly across the period.
HOP_RUN_CAP = 2500
# Lines per trend chart (the fleet reference line plus five probes keeps every
# line in the categorical palette; more probes become further charts).
PROBES_PER_CHART = 5

_ICMP_CAVEAT = ("Routers often rate-limit or deprioritise the ICMP replies that traceroute relies "
                "on, so loss that shows at one hop but not at the hops after it or at the target "
                "is usually not real forwarding loss. Treat a hop as a problem when the target "
                "also loses packets in the same runs.")


# ─── shared helpers ─────────────────────────────────────────────────────────

def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _i(v: Any) -> int:
    return int(v or 0)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _zone(ctx: SectionCtx):
    try:
        return ZoneInfo(ctx.tz_name)
    except Exception:
        return timezone.utc


def _pg_bucket(col: str, seconds: int) -> str:
    """Postgres bucket start (timestamptz) on a grid anchored at the
    organisation's local midnight (``:org_tz``). ``seconds`` comes from this
    module's constants only (never user input)."""
    local = f"({col} AT TIME ZONE :org_tz)"
    day = f"date_trunc('day', {local})"
    if seconds >= 86400:
        return f"({day} AT TIME ZONE :org_tz)"
    return (f"(({day} + make_interval(secs => (floor(extract(epoch FROM ({local} - {day})) "
            f"/ {int(seconds)}) * {int(seconds)})::float8)) AT TIME ZONE :org_tz)")


def _bucket_starts(ctx: SectionCtx, seconds: int) -> list[datetime]:
    """Every bucket start (aware UTC) overlapping [ctx.frm, ctx.to), on the
    same local-midnight grid as _pg_bucket."""
    tz = _zone(ctx)
    start, end = _utc(ctx.frm), _utc(ctx.to)
    step = timedelta(seconds=seconds)
    out: list[datetime] = []
    day: date = start.astimezone(tz).date() - timedelta(days=1)
    while len(out) < 5000:
        midnight = datetime.combine(day, time(0))
        if midnight.replace(tzinfo=tz).astimezone(timezone.utc) >= end:
            break
        k = 0
        while k < 86400:
            b = (midnight + timedelta(seconds=k)).replace(tzinfo=tz).astimezone(timezone.utc)
            if b >= end:
                break
            if b + step > start:
                out.append(b)
            k += seconds
        day += timedelta(days=1)
    return out


def _ms(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    if v >= 10:
        return f"{v:.0f} ms"
    if v >= 1:
        return f"{v:.1f} ms"
    return f"{v:.2f} ms"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f}%"


def _avail(v: Optional[float]) -> str:
    """Two-decimal availability that never rounds a miss up to 100.00%."""
    if v is None:
        return "—"
    if v < 100 and round(v, 2) >= 100:
        return "99.99%"
    return f"{v:.2f}%"


def _avail_accent(v: Optional[float]) -> str:
    if v is None:
        return "neutral"
    return "success" if v >= 99.9 else "warning" if v >= 99 else "danger"


def _loss_accent(v: Optional[float]) -> str:
    if v is None:
        return "neutral"
    return "success" if v < 1 else "warning" if v < 5 else "danger"


def _delta_pts(cur: Optional[float], prev: Optional[float]) -> str:
    if cur is None:
        return ""
    if prev is None:
        return "No runs in the previous period"
    d = cur - prev
    if abs(d) < 0.005:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):.2f} pts vs previous period"


def _delta_count(cur: int, prev: int) -> str:
    d = cur - prev
    if d == 0:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):,} vs previous period"


def _of(title: str, shown: int, total: int) -> str:
    return f"{title} (top {shown} of {total})" if total > shown else title


def _target(host: Optional[str], protocol: Optional[str], port: Any) -> str:
    """'8.8.8.8' for ICMP probes, 'crm.example.com:443/tcp' otherwise."""
    proto = (protocol or "icmp").lower()
    if proto == "icmp":
        return host or "—"
    return f"{host or '—'}{f':{port}' if port else ''}/{proto}"


def _duration(ms: Optional[float]) -> str:
    if ms is None:
        return "—"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f} s"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m} min {sec:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def _ratio(n: float, d: float) -> Optional[float]:
    return n / d * 100.0 if d else None


def _weighted(pairs: list[tuple[Optional[float], int]]) -> Optional[float]:
    """Sample-weighted mean of (value, weight) pairs; None when nothing measured."""
    pairs = [(v, n) for v, n in pairs if v is not None and n]
    total = sum(n for _v, n in pairs)
    return sum(v * n for v, n in pairs) / total if total else None


# ─── NetPath: per-probe window statistics (shared, cached per render) ──────

_PROBE_STATS_SQL = f"""
SELECT p.id::text AS id, p.name, p.target_host, p.target_port, p.protocol, p.enabled,
       {_PROBE_STATUS_SQL} AS status,
       a.runs, a.reached, a.traced, a.avg_rtt, a.p95_rtt, a.avg_loss, a.avg_worst_hop,
       a.changes, a.last_change, a.paths,
       a.prior_runs, a.prior_reached, a.prior_changes
FROM netpath_probes p
CROSS JOIN LATERAL (
    SELECT count(*) FILTER (WHERE s.run_at >= :f) AS runs,
           count(*) FILTER (WHERE s.run_at >= :f AND s.reached) AS reached,
           count(*) FILTER (WHERE s.run_at >= :f AND s.hop_count > 0) AS traced,
           avg(s.rtt_ms) FILTER (WHERE s.run_at >= :f AND s.reached) AS avg_rtt,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY s.rtt_ms)
               FILTER (WHERE s.run_at >= :f AND s.reached) AS p95_rtt,
           avg(s.loss_pct) FILTER (WHERE s.run_at >= :f AND s.reached) AS avg_loss,
           avg(s.worst_hop_loss_pct) FILTER (WHERE s.run_at >= :f AND s.reached) AS avg_worst_hop,
           count(*) FILTER (WHERE s.run_at >= :f AND s.path_changed) AS changes,
           max(s.run_at) FILTER (WHERE s.run_at >= :f AND s.path_changed) AS last_change,
           count(DISTINCT s.path_hash) FILTER (WHERE s.run_at >= :f AND s.hop_count > 0) AS paths,
           count(*) FILTER (WHERE s.run_at < :f) AS prior_runs,
           count(*) FILTER (WHERE s.run_at < :f AND s.reached) AS prior_reached,
           count(*) FILTER (WHERE s.run_at < :f AND s.path_changed) AS prior_changes
    FROM netpath_snapshots s
    WHERE s.probe_id = p.id AND s.run_at >= :pf AND s.run_at < :t
) a
ORDER BY p.name
"""


async def _probe_stats(ctx: SectionCtx) -> list[dict]:
    """One row per probe that is enabled or ran in the window (window and
    previous-window aggregates; RTT/loss/worst-hop over runs that reached)."""
    if "netpath:probe_stats" in ctx._cache:
        return ctx._cache["netpath:probe_stats"]
    pf, _ = ctx.prior_window()
    rows = (await ctx.db.execute(text(_PROBE_STATS_SQL),
                                 {"f": ctx.frm, "t": ctx.to, "pf": pf})).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("runs", "reached", "traced", "changes", "paths", "prior_runs", "prior_reached",
                  "prior_changes"):
            d[k] = _i(d.get(k))
        for k in ("avg_rtt", "p95_rtt", "avg_loss", "avg_worst_hop"):
            d[k] = _f(d.get(k))
        d["reach"] = _ratio(d["reached"], d["runs"])
        if not d.get("enabled") and not d["runs"]:
            continue
        out.append(d)
    ctx._cache["netpath:probe_stats"] = out
    return out


def _no_probes_note(stats: list[dict]) -> str:
    return ("No NetPath probes are configured." if not stats
            else "No NetPath runs in this period.")


def _name(r: dict) -> str:
    return r.get("name") or str(r.get("id") or "")[:8] or "—"


# ─── NetPath sections ───────────────────────────────────────────────────────

_HEALTH_SORTS = [["reach", "Lowest reachability"], ["rtt", "Highest p95 latency"],
                 ["loss", "Highest packet loss"], ["name", "Probe name"]]
_HEALTH_SORT_LABEL = {k: v for k, v in _HEALTH_SORTS}


def _sort_probes(rows: list[dict], how: str) -> list[dict]:
    """Rank probes; probes without a measurement (no runs, nothing reached)
    come last, by name."""
    if how == "name":
        return sorted(rows, key=lambda r: _name(r).lower())
    key = {"reach": "reach", "rtt": "p95_rtt", "loss": "avg_loss"}[how]
    measured = [r for r in rows if r[key] is not None]
    rest = [r for r in rows if r[key] is None]
    measured.sort(key=lambda r: (r[key] if how == "reach" else -r[key], _name(r).lower()))
    return measured + sorted(rest, key=lambda r: _name(r).lower())


@register_section(
    "netpath_probe_health", title="Network Path Health", category="Paths",
    description=("Per NetPath probe: reachability, average and 95th-percentile round-trip time, "
                 "end-to-end and worst-hop packet loss over the period, and current status."),
    scope="fleet", requires=NETPATH,
    options=[top_n_option(25, 200),
             {"key": "sort", "label": "Rank probes by", "type": "select", "default": "reach",
              "choices": _HEALTH_SORTS}],
)
async def netpath_probe_health(ctx: SectionCtx) -> dict:
    title = "Network Path Health"
    stats = await _probe_stats(ctx)
    if not any(r["runs"] for r in stats):
        return _section("netpath_probe_health", title, notes=[_no_probes_note(stats)])

    how = ctx.opt("sort")
    top_n = int(ctx.opt("top_n"))
    runs = sum(r["runs"] for r in stats)
    reached = sum(r["reached"] for r in stats)
    prior_runs = sum(r["prior_runs"] for r in stats)
    reach = _ratio(reached, runs)
    prior = _ratio(sum(r["prior_reached"] for r in stats), prior_runs)
    avg_rtt = _weighted([(r["avg_rtt"], r["reached"]) for r in stats])
    avg_loss = _weighted([(r["avg_loss"], r["reached"]) for r in stats])
    worst = max((r for r in stats if r["p95_rtt"] is not None), key=lambda r: r["p95_rtt"], default=None)
    enabled = sum(1 for r in stats if r.get("enabled"))
    trouble = sum(1 for r in stats if r["status"] in _TROUBLE)
    not_measured = sum(1 for r in stats if not r["runs"])

    ranked = _sort_probes(stats, how)
    for r in stats:
        r["unreached_pct"] = None if r["reach"] is None else 100.0 - r["reach"]
    metric, unit, chart_title = {
        "reach": ("unreached_pct", "%", "Runs that did not reach the target"),
        "loss": ("avg_loss", "%", "Highest average packet loss"),
    }.get(how, ("p95_rtt", "ms", "Highest 95th-percentile round-trip time"))
    chart_rows = [r for r in (ranked if how != "name" else _sort_probes(stats, "rtt"))
                  if r[metric] is not None and (round(r[metric], 2) > 0 or metric == "p95_rtt")][:10]
    shown = ranked[:top_n]

    notes = ["Round-trip time and loss are measured on the runs that reached the target; runs that "
             "did not reach it count only against reachability.",
             "Hop loss is the highest loss at any intermediate hop in a run (worst hop), averaged over "
             "the runs that reached the target. Routers often rate-limit the ICMP replies traceroute "
             "relies on, so it can overstate real loss (see Problem Hops).",
             "Status is each probe's state when the report was generated; stale means it has not "
             "run for three of its intervals."]
    if not_measured:
        notes.append(f"{not_measured} enabled probe(s) did not run in this period (not measured).")
    if prior_runs == 0:
        notes.append("No NetPath runs in the previous period to compare against.")

    return _section(
        "netpath_probe_health", title,
        description=(f"Reachability, latency and packet loss for {len(stats)} NetPath probe(s) over "
                     f"the period, ranked by {_HEALTH_SORT_LABEL[how].lower()}."),
        kpis=[
            {"label": "Path probes", "value": f"{len(stats):,}", "accent": "primary",
             "subtitle": f"{enabled:,} enabled · {runs:,} runs"},
            {"label": "Reachability", "value": _avail(reach), "accent": _avail_accent(reach),
             "subtitle": _delta_pts(reach, prior) or f"{reached:,} of {runs:,} runs"},
            {"label": "Average RTT", "value": _ms(avg_rtt), "accent": "info" if avg_rtt is not None
             else "neutral", "subtitle": "Runs that reached the target"},
            {"label": "Highest p95 RTT", "value": _ms(worst["p95_rtt"]) if worst else "—",
             "accent": "warning" if worst else "neutral",
             "subtitle": _name(worst) if worst else "Not measured"},
            {"label": "Average loss", "value": _pct(avg_loss), "accent": _loss_accent(avg_loss),
             "subtitle": "Runs that reached the target"},
            {"label": "Unhealthy now", "value": f"{trouble:,}",
             "accent": "danger" if trouble else "success",
             "subtitle": f"Degraded, down or unreachable · of {enabled:,} enabled"},
        ],
        charts=[{"title": chart_title + " by probe",
                 "series": {"kind": "rank", "unit": unit,
                            "color": "danger" if metric != "p95_rtt" else "warning",
                            "points": [{"label": _name(r), "value": round(r[metric], 2)}
                                       for r in chart_rows]}}] if chart_rows else [],
        tables=[{"title": _of(f"Probes by {_HEALTH_SORT_LABEL[how].lower()}", len(shown), len(ranked)),
                 "headers": ["Probe", "Target", "Reachability", "Avg RTT", "p95 RTT", "Avg loss",
                             "Hop loss", "Status"],
                 "styles": ["text", "mono", "pct-bar", "num", "num", "num", "num", "status"],
                 "rows": [[_name(r), _target(r["target_host"], r["protocol"], r["target_port"]),
                           _avail(r["reach"]), _ms(r["avg_rtt"]), _ms(r["p95_rtt"]), _pct(r["avg_loss"]),
                           _pct(r["avg_worst_hop"]), _STATUS_WORD.get(r["status"], r["status"] or "unknown")]
                          for r in shown]}],
        notes=notes,
    )


_EXTRA_PATH_SQL = """
SELECT p.id::text AS id,
       (SELECT max(z.c) FROM (
            SELECT count(*) AS c FROM netpath_snapshots s
            WHERE s.probe_id = p.id AND s.run_at >= :f AND s.run_at < :t AND s.hop_count > 0
            GROUP BY s.path_hash) z) AS top_path_runs,
       (SELECT count(*) FROM netpath_paths np
         WHERE np.probe_id = p.id AND np.first_seen >= :f AND np.first_seen < :t) AS new_paths,
       (SELECT count(*) FROM netpath_events e
         WHERE e.probe_id = p.id AND e.event_type = 'unreachable'
           AND e.created_at >= :f AND e.created_at < :t) AS outages
FROM netpath_probes p
WHERE p.id::text = ANY(:ids)
"""


_CHANGE_TREND_SQL = """
SELECT b.bucket, sum(b.runs) AS runs, sum(b.changes) AS changes
FROM netpath_probes p
CROSS JOIN LATERAL (
    SELECT {bucket} AS bucket, count(*) AS runs, count(*) FILTER (WHERE s.path_changed) AS changes
    FROM netpath_snapshots s
    WHERE s.probe_id = p.id AND s.run_at >= :f AND s.run_at < :t
    GROUP BY 1
) b
GROUP BY 1 ORDER BY 1
"""


@register_section(
    "netpath_path_changes", title="Path Changes and Route Stability", category="Paths",
    description=("Route changes per NetPath probe: how often the hop-by-hop path changed, how many "
                 "distinct routes were used, the share of runs on the primary route and lost "
                 "reachability."),
    scope="fleet", requires=NETPATH, options=[top_n_option(25, 200)],
)
async def netpath_path_changes(ctx: SectionCtx) -> dict:
    title = "Path Changes and Route Stability"
    all_stats = await _probe_stats(ctx)
    stats = [dict(r) for r in all_stats if r["runs"]]
    if not stats:
        return _section("netpath_path_changes", title, notes=[_no_probes_note(all_stats)])

    ids = [r["id"] for r in stats]
    extra = {r["id"]: dict(r) for r in (await ctx.db.execute(
        text(_EXTRA_PATH_SQL), {"f": ctx.frm, "t": ctx.to, "ids": ids})).mappings().all()}

    seconds = 3600 if ctx.hours <= 48 else 86400
    trend = (await ctx.db.execute(text(_CHANGE_TREND_SQL.format(bucket=_pg_bucket("s.run_at", seconds))),
                                  {"f": ctx.frm, "t": ctx.to, "org_tz": ctx.tz_name})).mappings().all()
    # Intervals without any run are gaps (not measured), not "no changes".
    per_bucket = {ctx.iso(r["bucket"]): _i(r["changes"]) for r in trend if _i(r["runs"])}

    changes = sum(r["changes"] for r in stats)
    prior_changes = sum(r["prior_changes"] for r in stats)
    changed_probes = sum(1 for r in stats if r["changes"])
    for r in stats:
        e = extra.get(r["id"], {})
        r["primary_share"] = _ratio(_i(e.get("top_path_runs")), r["traced"])
        r["new_paths"] = _i(e.get("new_paths"))
        r["outages"] = _i(e.get("outages"))
    new_paths = sum(r["new_paths"] for r in stats)
    outages = sum(r["outages"] for r in stats)
    ranked = sorted(stats, key=lambda r: (-r["changes"], -r["outages"],
                                          100.0 if r["primary_share"] is None else r["primary_share"],
                                          _name(r).lower()))
    top_n = int(ctx.opt("top_n"))
    shown = ranked[:top_n]
    unit_label = _INTERVAL_WORDS[seconds]
    prior_note = [] if sum(r["prior_runs"] for r in stats) else [
        "No NetPath runs in the previous period to compare against."]

    return _section(
        "netpath_path_changes", title,
        description=(f"Route changes and stability for {len(stats)} NetPath probe(s) over the period, "
                     "most changes first."),
        kpis=[
            {"label": "Path changes", "value": f"{changes:,}",
             "accent": "warning" if changes else "success",
             "subtitle": _delta_count(changes, prior_changes)},
            {"label": "Probes with changes", "value": f"{changed_probes:,}",
             "accent": "warning" if changed_probes else "success", "subtitle": f"of {len(stats):,} probes"},
            {"label": "New routes", "value": f"{new_paths:,}", "accent": "info",
             "subtitle": "First seen in this period"},
            {"label": "Reachability lost", "value": f"{outages:,}",
             "accent": "danger" if outages else "success", "subtitle": "Times a target became unreachable"},
        ],
        charts=[{"title": f"Path changes per {unit_label}",
                 "series": {"kind": "bars", "unit": "changes", "color": "warning",
                            "points": [{"t": ctx.iso(b), "v": per_bucket.get(ctx.iso(b))}
                                       for b in _bucket_starts(ctx, seconds)]}}],
        tables=[{"title": _of("Route stability by probe", len(shown), len(ranked)),
                 "headers": ["Probe", "Changes", "Routes", "On primary route", "New routes", "Outages",
                             "Last change"],
                 "styles": ["text", "num", "num", "pct-bar", "num", "num", "text"],
                 "rows": [[_name(r), f"{r['changes']:,}", f"{r['paths']:,}" if r["traced"] else "—",
                           _pct(r["primary_share"]), f"{r['new_paths']:,}", f"{r['outages']:,}",
                           ctx.fmt_short(r.get("last_change"))]
                          for r in shown]}],
        notes=["A path change is a run whose hop-by-hop route differs from the previous run. "
               "Load-balanced (ECMP) networks and hops that answer only some of the time can register "
               "as changes, so read high counts together with the primary route share.",
               "Routes counts the distinct hop-by-hop routes seen; on primary route is the share of "
               "traced runs that used the probe's most common route in the period. A new probe's "
               "first route counts as a new route. Outages count the times the target became "
               "unreachable.",
               "Intervals in which no probe ran are left as gaps.",
               f"Times are in {ctx.tz_label()}."] + prior_note,
    )


_TREND_RANKS = [["rtt", "Highest average RTT"], ["loss", "Highest average loss"],
                ["reach", "Lowest reachability"]]
_TREND_RANK_TITLE = {"rtt": "RTT", "loss": "Packet Loss", "reach": "Lowest Reachability"}
_TREND_RANK_PHRASE = {"rtt": "highest average RTT", "loss": "highest average packet loss",
                      "reach": "lowest reachability"}

_TREND_SQL = """
SELECT p.id::text AS id, p.name, b.bucket, b.runs, b.reached, b.rtt, b.loss, b.jitter
FROM netpath_probes p
CROSS JOIN LATERAL (
    SELECT {bucket} AS bucket, count(*) AS runs,
           count(*) FILTER (WHERE s.reached) AS reached,
           avg(s.rtt_ms) FILTER (WHERE s.reached) AS rtt,
           avg(s.loss_pct) FILTER (WHERE s.reached) AS loss,
           avg(s.jitter_ms) FILTER (WHERE s.reached) AS jitter
    FROM netpath_snapshots s
    WHERE s.probe_id = p.id AND s.run_at >= :f AND s.run_at < :t
    GROUP BY 1
) b
ORDER BY p.name, b.bucket
"""


@register_section(
    "netpath_latency_trend", title="Path Latency Trend", category="Paths",
    description=("Round-trip time and packet loss over the period for the NetPath probes with the "
                 "highest latency (or loss), one line per probe, with the all-probe average."),
    scope="fleet", requires=NETPATH,
    options=[{"key": "top_n", "label": "Probes to chart", "type": "int", "default": 5, "min": 1,
              "max": 10, "help": "How many probes to draw (five per chart)."},
             {"key": "rank", "label": "Choose probes by", "type": "select", "default": "rtt",
              "choices": _TREND_RANKS}],
)
async def netpath_latency_trend(ctx: SectionCtx) -> dict:
    top_n = int(ctx.opt("top_n"))
    rank = ctx.opt("rank")
    title = f"Path Latency Trend — Top {top_n} Probes by {_TREND_RANK_TITLE[rank]}"
    seconds = _BUCKET_SECONDS.get(ctx.bucket(), 7200)
    rows = (await ctx.db.execute(text(_TREND_SQL.format(bucket=_pg_bucket("s.run_at", seconds))),
                                 {"f": ctx.frm, "t": ctx.to, "org_tz": ctx.tz_name})).mappings().all()
    if not rows:
        return _section("netpath_latency_trend", title, notes=["No NetPath runs in this period."])

    probes: dict[str, dict] = {}
    for r in rows:
        p = probes.setdefault(r["id"], {"id": r["id"], "name": r["name"] or r["id"][:8], "runs": 0,
                                        "reached": 0, "rtt": [], "loss": [], "jitter": [], "buckets": {}})
        runs, reached = _i(r["runs"]), _i(r["reached"])
        rtt, loss, jitter = _f(r["rtt"]), _f(r["loss"]), _f(r["jitter"])
        p["runs"] += runs
        p["reached"] += reached
        p["rtt"].append((rtt, reached))
        p["loss"].append((loss, reached))
        p["jitter"].append((jitter, reached))
        p["buckets"][ctx.iso(r["bucket"])] = (rtt, loss, r["bucket"], reached)
    for p in probes.values():
        p["avg_rtt"] = _weighted(p["rtt"])
        p["avg_loss"] = _weighted(p["loss"])
        p["avg_jitter"] = _weighted(p["jitter"])
        p["reach"] = _ratio(p["reached"], p["runs"])

    key = {"rtt": "avg_rtt", "loss": "avg_loss", "reach": "reach"}[rank]
    measured = [p for p in probes.values() if p[key] is not None]
    measured.sort(key=lambda p: (p[key] if rank == "reach" else -p[key], p["name"].lower()))
    chosen = measured[:top_n]
    if not chosen:
        return _section("netpath_latency_trend", title,
                        notes=["No NetPath run reached its target in this period."])

    grid = [ctx.iso(b) for b in _bucket_starts(ctx, seconds)]
    # All-probe reference line: average over every reached run in the interval.
    fleet: dict[str, list[float]] = {}
    for p in probes.values():
        for t, (rtt, loss, _b, n) in p["buckets"].items():
            acc = fleet.setdefault(t, [0.0, 0.0, 0.0, 0.0])
            if rtt is not None and n:
                acc[0] += rtt * n
                acc[1] += n
            if loss is not None and n:
                acc[2] += loss * n
                acc[3] += n

    def fleet_pts(i: int) -> list[dict]:
        return [{"t": t, "v": round(fleet[t][i] / fleet[t][i + 1], 2)
                 if t in fleet and fleet[t][i + 1] else None} for t in grid]

    def probe_pts(p: dict, i: int) -> list[dict]:
        out = []
        for t in grid:
            v = (p["buckets"].get(t) or (None, None, None, 0))[i]
            out.append({"t": t, "v": None if v is None else round(v, 2)})
        return out

    peak = None
    for p in chosen:
        for _t, (rtt, _loss, b, _n) in p["buckets"].items():
            if rtt is not None and (peak is None or rtt > peak[0]):
                peak = (rtt, p["name"], b)
    avg_rtt = _weighted([(p["avg_rtt"], p["reached"]) for p in probes.values()])
    avg_loss = _weighted([(p["avg_loss"], p["reached"]) for p in probes.values()])

    groups = [chosen[i:i + PROBES_PER_CHART] for i in range(0, len(chosen), PROBES_PER_CHART)]

    def part(i: int, g: list) -> str:
        if len(groups) == 1:
            return ""
        first = i * PROBES_PER_CHART + 1
        return f" ({first}–{first + len(g) - 1} of {len(chosen)})"

    charts = []
    for label, unit, color, idx in (("Round-trip time by probe", "ms", "primary", 0),
                                    ("Packet loss by probe", "%", "danger", 1)):
        fleet_line = fleet_pts(0 if idx == 0 else 2)
        for i, g in enumerate(groups):
            charts.append({"title": label + part(i, g),
                           "series": {"kind": "line", "unit": unit, "color": color, "points": fleet_line,
                                      "series": [{"name": "All probes (average)", "points": fleet_line}]
                                      + [{"name": p["name"], "points": probe_pts(p, idx)} for p in g]}})

    def row(p: dict) -> list[str]:
        vals = [v[0] for v in p["buckets"].values() if v[0] is not None]
        return [p["name"], _avail(p["reach"]), _ms(p["avg_rtt"]), _ms(min(vals) if vals else None),
                _ms(max(vals) if vals else None), _ms(p["avg_jitter"]), _pct(p["avg_loss"])]

    interval = _INTERVAL_WORDS[seconds]
    return _section(
        "netpath_latency_trend", title,
        description=(f"Round-trip time and packet loss per {interval} for the {len(chosen)} NetPath "
                     f"probe(s) with the {_TREND_RANK_PHRASE[rank]}, with the all-probe "
                     "average for reference."),
        kpis=[
            {"label": "Average RTT", "value": _ms(avg_rtt), "accent": "info" if avg_rtt is not None
             else "neutral", "subtitle": f"All {len(probes):,} probe(s)"},
            {"label": "Peak interval RTT", "value": _ms(peak[0]) if peak else "—",
             "accent": "warning" if peak else "neutral",
             "subtitle": f"{peak[1]} · {ctx.fmt_short(peak[2])}" if peak else "Not measured"},
            {"label": "Average loss", "value": _pct(avg_loss), "accent": _loss_accent(avg_loss),
             "subtitle": "Runs that reached the target"},
            {"label": "Probes charted", "value": f"{len(chosen):,}", "accent": "primary",
             "subtitle": f"of {len(probes):,} with runs"},
        ],
        charts=charts,
        tables=[{"title": _of("Charted probes", len(chosen), len(measured)),
                 "headers": ["Probe", "Reachability", "Avg RTT", "Best interval", "Worst interval",
                             "Avg jitter", "Avg loss"],
                 "styles": ["text", "pct-bar", "num", "num", "num", "num", "num"],
                 "rows": [row(p) for p in chosen]}],
        notes=[f"Each point is the average of the runs in that {interval} that reached the target; "
               "gaps mean no run reached it.",
               "Best and worst interval are the lowest and highest per-interval average RTT.",
               f"Times are in {ctx.tz_label()}."],
    )


_HOP_PROBES_SQL = """
SELECT p.id::text AS id, p.name, x.reached, x.lossy, x.carried
FROM netpath_probes p
CROSS JOIN LATERAL (
    SELECT count(*) AS reached,
           count(*) FILTER (WHERE s.worst_hop_loss_pct >= :thr) AS lossy,
           count(*) FILTER (WHERE s.worst_hop_loss_pct >= :thr AND s.loss_pct > 0) AS carried
    FROM netpath_snapshots s
    WHERE s.probe_id = p.id AND s.run_at >= :f AND s.run_at < :t AND s.reached
) x
WHERE x.reached > 0
"""

# Lossy runs per probe, thinned to at most :cap evenly spaced runs; w weights
# a sampled run back up to the runs it stands for. Only the picked runs have
# their hop JSON read. One row per (run, hop address).
_HOPS_SQL = """
WITH picked AS (
    SELECT z.id AS sid, z.probe_id, z.e2e_loss,
           CASE WHEN z.n > :cap
                THEN z.n::float8 / floor(z.n::float8 / ceil(z.n::float8 / :cap))
                ELSE 1.0 END AS w
    FROM netpath_probes p
    CROSS JOIN LATERAL (
        SELECT s.id, p.id::text AS probe_id, s.loss_pct AS e2e_loss,
               row_number() OVER (ORDER BY s.run_at, s.id) AS rn, count(*) OVER () AS n
        FROM netpath_snapshots s
        WHERE s.probe_id = p.id AND s.run_at >= :f AND s.run_at < :t
          AND s.reached AND s.worst_hop_loss_pct >= :thr
    ) z
    WHERE z.n <= :cap OR z.rn % ceil(z.n::float8 / :cap)::bigint = 0
), nodes AS (
    SELECT k.sid, k.probe_id, k.w, k.e2e_loss, n->>'ip' AS ip,
           max((n->>'loss_pct')::float8) AS loss
    FROM picked k
    JOIN netpath_snapshots s ON s.id = k.sid
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(s.hops) = 'array' THEN s.hops ELSE '[]'::jsonb END) AS h
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(h->'nodes') = 'array' THEN h->'nodes' ELSE '[]'::jsonb END) AS n
    WHERE COALESCE(n->>'is_dest', 'false') <> 'true'
      AND COALESCE(n->>'ip', '') <> ''
      AND jsonb_typeof(n->'loss_pct') = 'number'
      AND (n->>'loss_pct')::float8 >= :thr
    GROUP BY k.sid, k.probe_id, k.w, k.e2e_loss, n->>'ip'
)
SELECT ip, sum(w) AS lossy_runs, sum(w * loss) / sum(w) AS avg_loss, max(loss) AS peak_loss,
       sum(CASE WHEN e2e_loss > 0 THEN w ELSE 0 END) / sum(w) AS carried,
       array_agg(DISTINCT probe_id) AS probe_ids,
       count(*) OVER () AS total_hops
FROM nodes
GROUP BY ip
ORDER BY sum(w) DESC, sum(w * loss) / sum(w) DESC, ip
LIMIT :lim
"""

_HOP_META_SQL = """
SELECT host(m.ip) AS ip, m.hostname, m.asn, m.as_name, m.country, m.is_internal, m.enriched_at,
       d.hostname AS device_name
FROM netpath_hop_meta m
LEFT JOIN devices d ON d.id = m.device_id
WHERE m.ip = ANY(CAST(:ips AS inet[]))
"""


def _is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def _hop_owner(ip: str, m: Optional[dict]) -> tuple[str, bool]:
    """(owner · reverse-DNS name, internal?) for a hop address. The owner
    (managed device, ASN or internal) comes first so a narrow column keeps it."""
    m = m or {}
    internal = bool(m.get("is_internal")) or _is_private(ip)
    parts = []
    if m.get("device_name"):
        parts.append(f"Managed device {m['device_name']}")
    elif m.get("asn"):
        parts.append(f"AS{m['asn']}" + (f" {m['as_name']}" if m.get("as_name") else ""))
    elif internal:
        parts.append("Internal network")
    elif not m.get("enriched_at"):
        parts.append("Lookup pending")
    if m.get("hostname"):
        parts.append(m["hostname"])
    return (" · ".join(parts) or "—"), internal


def _assessment(carried: Optional[float]) -> str:
    """Likely cause of a hop's loss from how often the target lost packets in
    the same runs (a heuristic, explained in the section notes)."""
    if carried is None:
        return "—"
    if carried < 0.2:
        return "Rate limiting"
    if carried >= 0.5:
        return "Loss to target"
    return "Mixed"


@register_section(
    "netpath_problem_hops", title="Problem Hops", category="Paths",
    description=("Intermediate hops that dropped traceroute probes most often across NetPath runs, "
                 "with their network owner (ASN) and whether the target also lost packets."),
    scope="fleet", requires=NETPATH,
    options=[top_n_option(15, 100),
             {"key": "min_loss", "label": "Count a hop as lossy at (%)", "type": "number",
              "default": 10, "min": 1, "max": 100,
              "help": "A run counts against a hop when that hop dropped at least this share of probes."}],
)
async def netpath_problem_hops(ctx: SectionCtx) -> dict:
    top_n = int(ctx.opt("top_n"))
    thr = float(ctx.opt("min_loss"))
    title = f"Top {top_n} Problem Hops by Packet Loss"
    params = {"f": ctx.frm, "t": ctx.to, "thr": thr}
    probes = {r["id"]: dict(r) for r in (await ctx.db.execute(text(_HOP_PROBES_SQL), params))
              .mappings().all()}
    reached = sum(_i(p.get("reached")) for p in probes.values())
    if not reached:
        return _section("netpath_problem_hops", title,
                        notes=["No NetPath run reached its target in this period."])
    lossy_runs = sum(_i(p.get("lossy")) for p in probes.values())
    carried_runs = sum(_i(p.get("carried")) for p in probes.values())
    sampled = {pid for pid, p in probes.items() if _i(p.get("lossy")) > HOP_RUN_CAP}
    thr_txt = f"{thr:g}%"
    kpis = [
        {"label": "Runs with hop loss", "value": _pct(_ratio(lossy_runs, reached)),
         "accent": "success" if not lossy_runs else "warning",
         "subtitle": f"{lossy_runs:,} of {reached:,} runs had a hop at ≥ {thr_txt}"},
        {"label": "Target also lost packets",
         "value": _pct(_ratio(carried_runs, lossy_runs)) if lossy_runs else "—",
         "accent": "neutral" if not lossy_runs else
         "danger" if carried_runs / lossy_runs >= 0.5 else "success",
         "subtitle": "Of the runs with hop loss" if lossy_runs else "No runs with hop loss"},
    ]
    hops = [dict(r) for r in (await ctx.db.execute(
        text(_HOPS_SQL), {**params, "cap": HOP_RUN_CAP, "lim": top_n})).mappings().all()] \
        if lossy_runs else []
    if not hops:
        kpis.insert(0, {"label": "Hops with loss", "value": "0", "accent": "success",
                        "subtitle": f"At ≥ {thr_txt} in this period"})
        return _section(
            "netpath_problem_hops", title,
            description=(f"Intermediate hops that dropped at least {thr_txt} of their traceroute "
                         "probes in the period."),
            kpis=kpis,
            notes=[f"No intermediate hop dropped {thr_txt} or more of its probes in this period.",
                   _ICMP_CAVEAT])

    meta = {r["ip"]: dict(r) for r in (await ctx.db.execute(
        text(_HOP_META_SQL), {"ips": [h["ip"] for h in hops]})).mappings().all()}
    total_hops = _i(hops[0].get("total_hops")) or len(hops)
    rows, chart, internal_n = [], [], 0
    for h in hops:
        owner, internal = _hop_owner(h["ip"], meta.get(h["ip"]))
        internal_n += internal
        pids = [p for p in (h.get("probe_ids") or []) if p]
        denom = sum(_i(probes[p].get("reached")) for p in pids if p in probes)
        carried = _f(h.get("carried"))
        runs = _f(h.get("lossy_runs")) or 0.0
        approx = "≈ " if sampled & set(pids) else ""
        share = _pct(min(100.0, _ratio(runs, denom))) if denom else "—"
        rows.append([h["ip"], owner, f"{len(pids):,}", approx + share if denom else share,
                     _pct(_f(h.get("avg_loss"))), _pct(None if carried is None else carried * 100),
                     _assessment(carried)])
        chart.append({"label": (meta.get(h["ip"]) or {}).get("hostname") or h["ip"], "value": round(runs)})

    kpis.insert(0, {"label": "Hops with loss", "value": f"{total_hops:,}", "accent": "warning",
                    "subtitle": f"At ≥ {thr_txt} in at least one run"})
    kpis.append({"label": "Internal / external", "value": f"{internal_n} / {len(hops) - internal_n}",
                 "accent": "info", "subtitle": "Of the hops listed"})
    notes = [_ICMP_CAVEAT,
             "Probes counts the NetPath probes whose path crossed the hop; % of runs divides the runs in "
             "which the hop dropped at least the threshold (charted above) by every run of those probes. "
             "Target lost is the share of those runs "
             "in which the target lost packets too: under 20% suggests ICMP rate limiting at the hop, "
             "50% or more suggests loss that continues to the target.",
             "Only runs that reached the target are analysed and the destination itself is excluded "
             "(see Network Path Health for end-to-end loss and reachability).",
             "Network owners come from the reverse-DNS/ASN lookup cache; new hops show "
             "\"Lookup pending\" until it has run."]
    if sampled:
        notes.append(f"{len(sampled)} probe(s) had more than {HOP_RUN_CAP:,} runs with hop loss; an "
                     f"even sample of {HOP_RUN_CAP:,} runs across the period was analysed for each, so "
                     "their run counts and shares (marked ≈) are estimates.")
    return _section(
        "netpath_problem_hops", title,
        description=(f"Intermediate hops that dropped at least {thr_txt} of their traceroute probes, "
                     "ranked by how many runs were affected."),
        kpis=kpis,
        charts=[{"title": "Runs with loss by hop",
                 "series": {"kind": "rank", "unit": "runs", "color": "warning", "points": chart[:10]}}],
        tables=[{"title": _of(f"Hops with loss ≥ {thr_txt}", len(rows), total_hops),
                 "headers": ["Hop", "Network", "Probes", "% of runs", "Avg loss", "Target lost",
                             "Likely cause"],
                 "styles": ["mono", "text", "num", "pct", "num", "pct", "text"],
                 "rows": rows}],
        notes=notes,
    )


# ─── Discovery sections ─────────────────────────────────────────────────────

_RUN_OUTCOMES = (("completed", "Completed"), ("partial", "Partial"), ("failed", "Failed"),
                 ("cancelled", "Cancelled"), ("active", "Running"))
_FINISHED = ("completed", "partial", "failed", "cancelled")


@register_section(
    "discovery_activity", title="Discovery Activity", category="Discovery",
    description=("Network discovery runs in the period: outcome, duration, addresses scanned, "
                 "responding hosts, new and changed devices and credential failures."),
    scope="fleet", requires=DISCOVERY, options=[top_n_option(20, 200)],
)
async def discovery_activity(ctx: SectionCtx) -> dict:
    title = "Discovery Activity"
    params = {"f": ctx.frm, "t": ctx.to}
    s = (await ctx.db.execute(text("""
        SELECT count(*) AS runs,
               count(*) FILTER (WHERE status = 'completed') AS completed,
               count(*) FILTER (WHERE status = 'partial') AS partial,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
               count(*) FILTER (WHERE status IN ('queued', 'running')) AS active,
               COALESCE(sum(total_targets), 0) AS targets,
               COALESCE(sum(responding_targets), 0) AS responding,
               COALESCE(sum(new_devices), 0) AS new_devices,
               COALESCE(sum(changed_devices), 0) AS changed,
               COALESCE(sum(credential_failures), 0) AS cred_failures,
               COALESCE(sum(ready_to_import), 0) AS ready,
               avg(duration_ms) FILTER (WHERE status IN ('completed', 'partial')) AS avg_ms,
               count(DISTINCT profile_id) AS profiles
        FROM discovery_runs WHERE created_at >= :f AND created_at < :t"""), params)).mappings().first()
    s = dict(s or {})
    runs = _i(s.get("runs"))
    if not runs:
        return _section("discovery_activity", title, notes=["No discovery runs in this period."])

    top_n = int(ctx.opt("top_n"))
    latest = (await ctx.db.execute(text("""
        SELECT r.created_at, r.started_at, COALESCE(p.name, '—') AS profile,
               r.status, r.duration_ms, r.total_targets, r.responding_targets, r.new_devices,
               r.changed_devices, r.credential_failures
        FROM discovery_runs r LEFT JOIN discovery_profiles p ON p.id = r.profile_id
        WHERE r.created_at >= :f AND r.created_at < :t
        ORDER BY r.created_at DESC LIMIT :lim"""), {**params, "lim": top_n})).mappings().all()
    tables = [{
        "title": (f"Latest {len(latest)} of {runs:,} discovery runs" if runs > len(latest)
                  else "Discovery runs, newest first"),
        "headers": ["Started", "Profile", "Outcome", "Duration", "Responding", "New / changed",
                    "Credential failures"],
        "styles": ["text", "text", "status", "num", "num", "num", "num"],
        "rows": [[ctx.fmt_short(r["started_at"] or r["created_at"]), r["profile"],
                  r["status"] or "unknown",
                  _duration(_f(r["duration_ms"])) if r["status"] in _FINISHED else "—",
                  f"{_i(r['responding_targets']):,} of {_i(r['total_targets']):,}",
                  f"{_i(r['new_devices']):,} / {_i(r['changed_devices']):,}",
                  f"{_i(r['credential_failures']):,}"] for r in latest]}]
    if _i(s.get("profiles")) > 1:
        by_profile = (await ctx.db.execute(text("""
            SELECT COALESCE(p.name, '—') AS profile, count(*) AS runs, max(r.created_at) AS last_run,
                   (array_agg(r.status ORDER BY r.created_at DESC))[1] AS last_status,
                   avg(r.duration_ms) FILTER (WHERE r.status IN ('completed', 'partial')) AS avg_ms,
                   COALESCE(sum(r.new_devices), 0) AS new_devices,
                   COALESCE(sum(r.credential_failures), 0) AS cred_failures
            FROM discovery_runs r LEFT JOIN discovery_profiles p ON p.id = r.profile_id
            WHERE r.created_at >= :f AND r.created_at < :t
            GROUP BY COALESCE(p.name, '—') ORDER BY count(*) DESC, 1"""), params)).mappings().all()
        tables.append({"title": "By discovery profile",
                       "headers": ["Profile", "Runs", "Last run", "Last outcome", "Avg duration",
                                   "New devices", "Credential failures"],
                       "styles": ["text", "num", "text", "status", "num", "num", "num"],
                       "rows": [[r["profile"], f"{_i(r['runs']):,}", ctx.fmt_short(r["last_run"]),
                                 r["last_status"] or "unknown", _duration(_f(r["avg_ms"])),
                                 f"{_i(r['new_devices']):,}", f"{_i(r['cred_failures']):,}"]
                                for r in by_profile]})

    failed = _i(s.get("failed"))
    cred = _i(s.get("cred_failures"))
    new = _i(s.get("new_devices"))
    changed = _i(s.get("changed"))
    targets, responding = _i(s.get("targets")), _i(s.get("responding"))
    return _section(
        "discovery_activity", title,
        description=(f"{runs:,} discovery run(s) across {_i(s.get('profiles')):,} profile(s) in the period."),
        kpis=[
            {"label": "Discovery runs", "value": f"{runs:,}", "accent": "primary",
             "subtitle": f"{_i(s.get('completed')):,} completed · {failed:,} failed"},
            {"label": "Responding hosts", "value": f"{responding:,}", "accent": "info",
             "subtitle": f"of {targets:,} addresses scanned"},
            {"label": "New devices found", "value": f"{new:,}",
             "accent": "success" if new else "neutral",
             "subtitle": f"{_i(s.get('ready')):,} ready to import"},
            {"label": "Changed devices", "value": f"{changed:,}",
             "accent": "warning" if changed else "neutral",
             "subtitle": "Matched inventory with differences"},
            {"label": "Credential failures", "value": f"{cred:,}",
             "accent": "danger" if cred else "success", "subtitle": "Hosts that rejected the credentials"},
            {"label": "Average duration", "value": _duration(_f(s.get("avg_ms"))), "accent": "neutral",
             "subtitle": "Completed and partial runs"},
        ],
        charts=[{"title": "Runs by outcome",
                 "series": {"kind": "donut", "unit": "runs",
                            "points": [{"label": label, "value": _i(s.get(key))}
                                       for key, label in _RUN_OUTCOMES if _i(s.get(key))]}}],
        tables=tables,
        notes=["Counts are per run: a host found by several runs is counted in each.",
               "Responding counts the hosts that answered at least one probe, of the addresses each run "
               "scanned.",
               "Partial means every responding host rejected the profile's credentials."],
    )


def _latest_results_cte(where: str) -> str:
    """Latest observation per IP within the look-back (:since) that matches
    ``where`` (a constant SQL fragment from this module)."""
    return f"""
WITH latest AS (
    SELECT DISTINCT ON (r.ip_address) r.id, r.ip_address, r.hostname, r.sys_name, r.vendor,
           r.device_type, r.model, r.os, r.status, r.imported, r.ignored, r.import_ready,
           r.credential_status, r.scanned_at, r.profile_id
    FROM discovery_results_v2 r
    WHERE r.scanned_at >= :since AND {where}
    ORDER BY r.ip_address, r.scanned_at DESC, r.id DESC
)"""


# Every result status except 'failed' (the address did not answer).
_RESPONDING = "r.status IN ('new', 'existing', 'changed', 'unknown', 'ignored', 'imported')"
_NOT_IGNORED = ("NOT l.ignored AND NOT EXISTS (SELECT 1 FROM discovery_ignored_devices i "
                "WHERE host(i.ip_address) = host(l.ip_address))")
_UNMANAGED_WHERE = f"""
WHERE l.status IN ('new', 'unknown') AND NOT l.imported AND {_NOT_IGNORED}
  AND NOT EXISTS (SELECT 1 FROM devices d WHERE host(d.ip_address) = host(l.ip_address))
  AND NOT EXISTS (SELECT 1 FROM servers sv WHERE host(sv.primary_ip) = host(l.ip_address))"""
# What a discovery run counts as a credential failure (discovery_executor).
_CRED_GAP = "('invalid', 'permission_issue')"
_CRED_LABEL = {"invalid": "Invalid credentials", "permission_issue": "Permission denied"}

_DAYS_OPTION = {"key": "days", "label": "Seen within the last (days)", "type": "int", "default": 30,
                "min": 1, "max": 365, "help": "Ignore hosts that discovery has not seen responding since."}


def _since(ctx: SectionCtx) -> tuple[int, datetime]:
    days = int(ctx.opt("days"))
    return days, datetime.now(timezone.utc) - timedelta(days=days)


_TYPE_WORDS = {"ups": "UPS", "pdu": "PDU", "nas": "NAS", "san": "SAN", "voip": "VoIP",
               "ip_phone": "IP phone", "wlc": "Wireless controller"}


def _type_label(v: Optional[str]) -> str:
    v = (v or "unknown").strip().lower() or "unknown"
    return _TYPE_WORDS.get(v) or v.replace("_", " ").capitalize()


@register_section(
    "discovery_unmanaged", title="Unmanaged Devices Found by Discovery", category="Discovery",
    description=("Shadow inventory as of report generation: hosts discovery found responding that are "
                 "not monitored, imported or ignored, by vendor and device type."),
    scope="fleet", requires=DISCOVERY, semantics="snapshot",
    options=[top_n_option(25, 500), _DAYS_OPTION],
)
async def discovery_unmanaged(ctx: SectionCtx) -> dict:
    title = "Unmanaged Devices Found by Discovery"
    days, since = _since(ctx)
    cte = _latest_results_cte(_RESPONDING)
    groups = (await ctx.db.execute(text(f"""{cte}
        SELECT COALESCE(NULLIF(l.vendor, ''), 'Unknown') AS vendor,
               COALESCE(NULLIF(l.device_type, ''), 'unknown') AS device_type,
               count(*) AS n,
               count(*) FILTER (WHERE l.import_ready) AS ready,
               count(*) FILTER (WHERE l.credential_status IN {_CRED_GAP}) AS cred_gaps,
               max(l.scanned_at) AS last_seen
        FROM latest l {_UNMANAGED_WHERE}
        GROUP BY 1, 2 ORDER BY count(*) DESC, 1, 2"""), {"since": since})).mappings().all()
    total = sum(_i(g["n"]) for g in groups)
    if not total:
        return _section("discovery_unmanaged", title,
                        notes=[f"Discovery found no unmanaged hosts in the last {days} day(s)."])

    top_n = int(ctx.opt("top_n"))
    hosts = (await ctx.db.execute(text(f"""{cte}
        SELECT host(l.ip_address) AS ip,
               COALESCE(NULLIF(l.hostname, ''), NULLIF(l.sys_name, '')) AS name,
               l.vendor, l.device_type, l.model, l.os, l.credential_status, l.import_ready,
               l.scanned_at, p.name AS profile
        FROM latest l LEFT JOIN discovery_profiles p ON p.id = l.profile_id
        {_UNMANAGED_WHERE}
        ORDER BY l.scanned_at DESC, l.ip_address LIMIT :lim"""),
        {"since": since, "lim": top_n})).mappings().all()

    vendors: dict[str, int] = {}
    types: dict[str, int] = {}
    for g in groups:
        vendors[g["vendor"]] = vendors.get(g["vendor"], 0) + _i(g["n"])
        t = _type_label(g["device_type"])
        types[t] = types.get(t, 0) + _i(g["n"])
    identified = total - vendors.get("Unknown", 0)
    ready = sum(_i(g["ready"]) for g in groups)
    cred = sum(_i(g["cred_gaps"]) for g in groups)
    type_pts = sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(type_pts) > 6:
        type_pts = type_pts[:5] + [("Other", sum(n for _t, n in type_pts[5:]))]

    def state(h) -> str:
        if h["credential_status"] in _CRED_LABEL:
            return "credentials failed"
        return "ready to import" if h["import_ready"] else "needs review"

    def kind(h) -> str:
        parts = [_type_label(h["device_type"])] + ([h["model"]] if h["model"] else [])
        return " · ".join(parts)

    shown_groups = groups[:top_n]
    return _section(
        "discovery_unmanaged", title,
        description=(f"{total:,} host(s) seen responding by discovery in the last {days} day(s) that are "
                     "not monitored, imported or ignored, as of report generation."),
        kpis=[
            {"label": "Unmanaged devices", "value": f"{total:,}", "accent": "warning",
             "subtitle": f"Seen in the last {days} day(s)"},
            {"label": "Vendor identified", "value": _pct(_ratio(identified, total)), "accent": "info",
             "subtitle": f"{identified:,} of {total:,}"},
            {"label": "Ready to import", "value": f"{ready:,}", "accent": "success" if ready else "neutral",
             "subtitle": "New, responding, credentials valid or not needed"},
            {"label": "Credentials failed", "value": f"{cred:,}", "accent": "danger" if cred else "success",
             "subtitle": "Cannot be identified until fixed"},
        ],
        charts=[
            {"title": "Unmanaged devices by vendor",
             "series": {"kind": "rank", "unit": "devices", "color": "warning",
                        "points": [{"label": v, "value": n}
                                   for v, n in sorted(vendors.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]}},
            {"title": "Unmanaged devices by type",
             "series": {"kind": "donut", "unit": "devices",
                        "points": [{"label": t, "value": n} for t, n in type_pts]}},
        ],
        tables=[
            {"title": _of("By vendor and type", len(shown_groups), len(groups)),
             "headers": ["Vendor", "Type", "Devices", "Ready to import", "Credentials failed", "Last seen"],
             "styles": ["text", "text", "num", "num", "num", "text"],
             "rows": [[g["vendor"], _type_label(g["device_type"]), f"{_i(g['n']):,}",
                       f"{_i(g['ready']):,}", f"{_i(g['cred_gaps']):,}", ctx.fmt_short(g["last_seen"])]
                      for g in shown_groups]},
            {"title": (f"Most recently seen {len(hosts)} of {total:,} unmanaged hosts" if total > len(hosts)
                       else "Unmanaged hosts, most recently seen first"),
             "headers": ["Address", "Name", "Vendor", "Type / model", "OS", "Profile", "Last seen", "State"],
             "styles": ["mono", "text", "text", "text", "text", "text", "text", "text"],
             "rows": [[h["ip"], h["name"] or "—", h["vendor"] or "—", kind(h), h["os"] or "—",
                       h["profile"] or "—", ctx.fmt_short(h["scanned_at"]), state(h)] for h in hosts]},
        ],
        notes=["Each address is counted once, from the most recent discovery result in which it responded.",
               "Hosts already monitored as devices or servers, imported, or on the discovery ignore list "
               "are excluded."],
    )


_PROTOCOLS_SQL = """ARRAY(
    SELECT upper(e.key) FROM jsonb_each(
        CASE WHEN jsonb_typeof(rr.raw_data->'protocol_status') = 'object'
             THEN rr.raw_data->'protocol_status' ELSE '{}'::jsonb END) AS e
    WHERE e.key IN ('snmp', 'winrm', 'wmi')
      AND COALESCE(e.value->>'responsive', 'false') <> 'true'
    ORDER BY e.key)"""
_MANAGED_SQL = ("(l.imported OR l.status IN ('existing', 'changed', 'imported') "
                "OR EXISTS (SELECT 1 FROM devices d WHERE host(d.ip_address) = host(l.ip_address)) "
                "OR EXISTS (SELECT 1 FROM servers sv WHERE host(sv.primary_ip) = host(l.ip_address)))")
# Latest observation per IP in which credentials were actually tested.
_TESTED = f"{_RESPONDING} AND r.credential_status <> 'not_tested'"
_PROTOCOL_LABEL = {"SNMP": "SNMP", "WINRM": "WinRM", "WMI": "WMI"}


@register_section(
    "discovery_credential_gaps", title="Discovery Credential Gaps", category="Discovery",
    description=("Hosts that answered discovery but rejected the configured SNMP or Windows "
                 "credentials at their latest test, as of report generation."),
    scope="fleet", requires=DISCOVERY, semantics="snapshot",
    options=[top_n_option(25, 500), _DAYS_OPTION],
)
async def discovery_credential_gaps(ctx: SectionCtx) -> dict:
    title = "Discovery Credential Gaps"
    days, since = _since(ctx)
    cte = _latest_results_cte(_TESTED)
    agg = (await ctx.db.execute(text(f"""{cte}
        SELECT l.credential_status, {_MANAGED_SQL} AS managed,
               COALESCE(p.name, '—') AS profile, count(*) AS n
        FROM latest l LEFT JOIN discovery_profiles p ON p.id = l.profile_id
        WHERE l.credential_status IN {_CRED_GAP} AND {_NOT_IGNORED}
        GROUP BY 1, 2, 3"""), {"since": since})).mappings().all()
    total = sum(_i(a["n"]) for a in agg)
    if not total:
        return _section("discovery_credential_gaps", title,
                        notes=[f"No credential failures among hosts discovery tested in the last "
                               f"{days} day(s)."])

    top_n = int(ctx.opt("top_n"))
    hosts = (await ctx.db.execute(text(f"""{cte}
        SELECT host(l.ip_address) AS ip,
               COALESCE(NULLIF(l.hostname, ''), NULLIF(l.sys_name, '')) AS name,
               l.vendor, l.device_type, l.credential_status, l.scanned_at, p.name AS profile,
               {_MANAGED_SQL} AS managed, {_PROTOCOLS_SQL} AS protocols
        FROM latest l
        JOIN discovery_results_v2 rr ON rr.id = l.id
        LEFT JOIN discovery_profiles p ON p.id = l.profile_id
        WHERE l.credential_status IN {_CRED_GAP} AND {_NOT_IGNORED}
        ORDER BY 8 DESC, l.scanned_at DESC, l.ip_address LIMIT :lim"""),
        {"since": since, "lim": top_n})).mappings().all()

    by_status: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    managed = 0
    for a in agg:
        n = _i(a["n"])
        by_status[a["credential_status"]] = by_status.get(a["credential_status"], 0) + n
        by_profile[a["profile"]] = by_profile.get(a["profile"], 0) + n
        if a["managed"]:
            managed += n

    def vendor_type(h) -> str:
        v = h["vendor"] or ""
        t = _type_label(h["device_type"]) if h["device_type"] else ""
        return " · ".join(x for x in (v, t) if x) or "—"

    return _section(
        "discovery_credential_gaps", title,
        description=(f"{total:,} host(s) tested by discovery in the last {days} day(s) that responded but "
                     "could not be logged in to with the profile's credentials, as of report generation."),
        kpis=[
            {"label": "Hosts with credential failures", "value": f"{total:,}", "accent": "danger",
             "subtitle": f"Tested in the last {days} day(s)"},
            {"label": "Invalid credentials", "value": f"{by_status.get('invalid', 0):,}",
             "accent": "warning" if by_status.get("invalid") else "neutral"},
            {"label": "Permission denied", "value": f"{by_status.get('permission_issue', 0):,}",
             "accent": "warning" if by_status.get("permission_issue") else "neutral"},
            {"label": "Already in inventory", "value": f"{managed:,}", "accent": "info",
             "subtitle": "Discovery cannot refresh their details"},
        ],
        charts=[{"title": "Credential failures by discovery profile",
                 "series": {"kind": "rank", "unit": "hosts", "color": "danger",
                            "points": [{"label": k, "value": v} for k, v in
                                       sorted(by_profile.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]}}],
        tables=[{"title": _of("Hosts with credential failures, inventory first", len(hosts), total),
                 "headers": ["Address", "Name", "Vendor / type", "Problem", "Protocols", "Managed",
                             "Profile", "Last tested"],
                 "styles": ["mono", "text", "text", "text", "text", "text", "text", "text"],
                 "rows": [[h["ip"], h["name"] or "—", vendor_type(h),
                           _CRED_LABEL.get(h["credential_status"], h["credential_status"] or "—"),
                           ", ".join(_PROTOCOL_LABEL.get(p, p) for p in (h["protocols"] or [])) or "—",
                           "yes" if h["managed"] else "no", h["profile"] or "—",
                           ctx.fmt_short(h["scanned_at"])] for h in hosts]}],
        notes=["Each address is counted once, from the most recent discovery result that tested its "
               "credentials; a later successful test clears it. Ignored hosts are excluded.",
               "Protocols lists the credential protocols (SNMP, WinRM, WMI) that did not answer; "
               "managed hosts are already monitored as devices or servers.",
               "Fix the credential list on the discovery profile (or the device) and re-run discovery; "
               "credential names and secrets are never included in reports."],
    )


# ─── Templates ──────────────────────────────────────────────────────────────

template(
    "netpath_path_health", "Network Path Health",
    "Hop-by-hop path monitoring at a glance: reachability, latency and loss per NetPath probe, the "
    "latency trend of the slowest paths, route changes and the hops that drop packets.",
    "paths",
    [S("netpath_probe_health"), S("netpath_latency_trend", None, top_n=5, rank="rtt"),
     S("netpath_path_changes", None, top_n=10), S("netpath_problem_hops", None, top_n=10)],
    audience=("Network engineer", "NOC operator", "IT manager"),
    tags=("netpath", "latency", "packet loss", "wan"), default_period="last_7d", frequency="weekly",
    icon="Route", featured=True,
)
template(
    "netpath_route_stability", "Path Changes and Route Stability",
    "How often each monitored path changed route, how many distinct routes it used, the share of runs "
    "on its primary route and when targets became unreachable.",
    "paths",
    [S("netpath_path_changes", None, top_n=50),
     S("netpath_probe_health", "Reachability by Probe", sort="reach", top_n=25)],
    audience=("Network engineer", "NOC operator"), tags=("netpath", "routing", "route changes", "stability"),
    default_period="last_30d", frequency="monthly", icon="Waypoints",
)
template(
    "netpath_latency_report", "Path Latency Trend",
    "Round-trip time and packet loss over time for the slowest and lossiest monitored paths, with "
    "per-probe 95th-percentile latency.",
    "paths",
    [S("netpath_latency_trend", None, top_n=10, rank="rtt"),
     S("netpath_latency_trend", None, top_n=5, rank="loss"),
     S("netpath_probe_health", "Latency by Probe (95th Percentile)", sort="rtt", top_n=50)],
    audience=("Network engineer", "Capacity planner"), tags=("netpath", "latency", "trend"),
    default_period="last_7d", frequency="weekly", icon="TrendingUp",
)
template(
    "netpath_problem_hops_report", "Problem Hops",
    "The intermediate routers that drop traceroute probes most often, who owns them (ASN) and whether "
    "the loss reaches the target, with the probes most affected.",
    "paths",
    [S("netpath_problem_hops", None, top_n=25),
     S("netpath_probe_health", "Packet Loss by Probe", sort="loss", top_n=25)],
    audience=("Network engineer",), tags=("netpath", "packet loss", "isp", "hops"),
    default_period="last_7d", frequency="weekly", icon="Network",
)
template(
    "discovery_activity_report", "Discovery Activity",
    "Network discovery runs in the period with their outcome, duration and findings, and the hosts "
    "whose credentials failed.",
    "inventory",
    [S("discovery_activity"), S("discovery_credential_gaps", None, top_n=25)],
    audience=("Network engineer", "IT manager"), tags=("discovery", "scans", "operations"),
    default_period="last_30d", frequency="monthly", icon="Activity",
)
template(
    "discovery_unmanaged_devices", "Unmanaged Devices Found by Discovery",
    "Shadow inventory: devices discovery found on the network that are not monitored, imported or "
    "ignored, by vendor and type, with the hosts discovery could not log in to.",
    "inventory",
    [S("discovery_unmanaged", None, top_n=100), S("discovery_credential_gaps", None, top_n=25)],
    audience=("IT manager", "Network engineer", "Security & audit"),
    tags=("discovery", "shadow it", "inventory", "coverage"), default_period="last_30d",
    frequency="monthly", icon="Boxes", featured=True,
)
template(
    "discovery_credential_gaps_report", "Discovery Credential Gaps",
    "Hosts that answered discovery but rejected the configured SNMP or Windows credentials, whether "
    "they are already in inventory, and the profiles to fix.",
    "inventory",
    [S("discovery_credential_gaps", None, top_n=200)],
    audience=("Network engineer", "Security & audit"), tags=("discovery", "credentials", "snmp", "winrm"),
    default_period="last_30d", frequency="weekly", icon="ShieldCheck",
)
