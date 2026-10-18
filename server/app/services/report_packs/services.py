"""Service-check sections (HTTP/TCP/ICMP/DNS/TLS): SLA compliance, confirmed
incidents, response times, daily uptime, TLS certificate expiry, availability
by group and owner, maintenance windows and per-site results.

Availability is always the *confirmed* basis shared with the Services list and
detail pages (service_availability.read_fleet_segments -> summarize /
incident_intervals / daily_summaries, the same computation as
service_reports.fleet_report): only confirmed incidents are downtime, isolated
failed probes are diagnostic evidence, planned maintenance leaves the
denominator and monitoring gaps stay unknown (coverage). Targets come from each
service's own objective, otherwise the availability policy
(availability_policy.resolve_target). Availability is shown with two decimals,
truncated so a non-perfect value never reads 100.00%.

Every section reads fleet-wide service checks (scope "fleet"; they are
withheld from visibility-scoped accounts) and needs service_checks.view.

Cost: the confirmed evidence is read once per render (ctx._cache) and shared by
every section here; contiguous identical pieces are merged as they stream in so
the cache stays small. Response-time percentiles and per-site statistics are
aggregated in ClickHouse, never by pulling raw probes into Python.
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
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

REQUIRES = ("service_checks.view",)
RAW_RETENTION_DAYS = 30      # zenplus.service_metrics TTL
ROLLUP_RETENTION_DAYS = 90   # zenplus.service_metrics_5m TTL
MAX_MATRIX_COLUMNS = 16      # day/week columns per uptime table (PDF width)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

BASIS_NOTE = ("Confirmed availability: only confirmed incidents count as downtime; isolated failed "
              "probes are diagnostic evidence. Planned maintenance is excluded and monitoring gaps "
              "are unknown (shown as coverage), never counted as up or down.")

CHECK_TYPE_CHOICES = [["all", "All check types"], ["http", "HTTP/HTTPS"], ["tcp", "TCP"],
                      ["dns", "DNS"], ["icmp", "ICMP"], ["tls", "TLS"]]
TYPE_LABEL = {"http": "HTTP", "tcp": "TCP", "dns": "DNS", "icmp": "ICMP", "tls": "TLS"}


# ─── formatting ─────────────────────────────────────────────────────────────

def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _avail_num(v: Optional[float]) -> Optional[float]:
    """Availability truncated to two decimals; a non-perfect value never reads 100
    (the rule of the Services and Availability pages)."""
    if v is None:
        return None
    if v >= 100:
        return 100.0
    return min(math.floor(v * 100 + 1e-7) / 100, 99.99)


def _avail(v: Optional[float]) -> str:
    n = _avail_num(v)
    return "—" if n is None else f"{n:.2f}%"


def _share(v: Optional[float]) -> str:
    """Coverage-style share: one decimal, never rounded up to 100."""
    if v is None:
        return "—"
    if 99.95 <= v < 100:
        return "99.9%"
    return f"{v:.1f}%"


def _target(v: Optional[float]) -> str:
    """Targets as configured (99.9%, 99.95%, 95%)."""
    return "—" if v is None else f"{round(float(v), 3):g}%"


def _ms(v: Optional[float]) -> str:
    """Response time with precision that suits its size (0.42 ms, 12.3 ms, 245 ms, 1.20 s)."""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    if v >= 100:
        return f"{v:.0f} ms"
    if v >= 10:
        return f"{v:.1f} ms"
    return f"{v:.2f} ms"


def _dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(round(max(0.0, float(seconds))))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if (s and m < 10) else f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def _clip(value: Any, limit: int = 60) -> str:
    s = " ".join(str(value or "").split())
    if not s:
        return "—"
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _ch_time(dt: datetime) -> str:
    """ClickHouse DateTime literal (UTC, no microseconds)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _window(ctx: SectionCtx) -> tuple[datetime, datetime, datetime]:
    """(start, end, now) as aware UTC; the end never lies in the future."""
    now = datetime.now(timezone.utc)
    start = _aware(ctx.frm) or now
    end = min(_aware(ctx.to) or now, now)
    return start, max(end, start), now


def _truncated(shown: int, total: int, noun: str) -> Optional[str]:
    return f"Showing the top {shown} of {total} {noun}." if total > shown else None


def _top_title(title: str, shown: int, total: int) -> str:
    return f"{title} (top {shown} of {total})" if total > shown else title


def _zone(ctx: SectionCtx) -> ZoneInfo:
    try:
        return ZoneInfo(ctx.tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _local_days(ctx: SectionCtx, start: datetime, end: datetime) -> tuple[ZoneInfo, list[date]]:
    """Calendar days of [start, end) in the organisation timezone."""
    zone = _zone(ctx)
    if end <= start:
        return zone, []
    d0 = start.astimezone(zone).date()
    d1 = (end - timedelta(microseconds=1)).astimezone(zone).date()
    return zone, [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def _day_label(day: date) -> str:
    return f"{_MONTHS[day.month - 1]} {day.day:02d}"


def _history_notes(ctx: SectionCtx) -> list[str]:
    """Disclose which probe history the confirmed evidence comes from."""
    start, _end, now = _window(ctx)
    notes = []
    if start < now - timedelta(days=RAW_RETENTION_DAYS):
        notes.append(f"Raw probes are kept for {RAW_RETENTION_DAYS} days; older parts of the period "
                     "are measured from 5-minute rollups (5-minute resolution).")
    note = ctx.window_note(ROLLUP_RETENTION_DAYS, "The 5-minute service probe history")
    if note:
        notes.append(note + " Earlier time is unknown (coverage), not downtime.")
    return notes


# ─── shared loaders (cached per render) ─────────────────────────────────────

_CHECK_COLS = ("id", "name", "check_type", "status", "enabled", "created_at", "check_interval",
               "group_id", "group_name", "sla_target_pct", "criticality", "business_owner",
               "owner_team", "tags", "target_url", "target_host", "tls_expiry_date",
               "tls_days_remaining", "tls_issuer", "tls_subject", "tls_warn_days",
               "tls_critical_days", "last_check_at")


async def _checks(ctx: SectionCtx) -> list[dict]:
    """Every service check with its business mapping (fleet-wide)."""
    if "svc_checks" not in ctx._cache:
        rows = (await ctx.db.execute(text(
            "SELECT sc.id::text, sc.name, sc.check_type, COALESCE(sc.status, 'unknown'), sc.enabled, "
            "sc.created_at, sc.check_interval, sc.group_id::text, g.name, sc.sla_target_pct, "
            "sc.criticality, sc.business_owner, sc.owner_team, sc.tags, sc.target_url, "
            "sc.target_host, sc.tls_expiry_date, sc.tls_days_remaining, sc.tls_issuer, "
            "sc.tls_subject, sc.tls_warn_days, sc.tls_critical_days, sc.last_check_at "
            "FROM service_checks sc LEFT JOIN service_check_groups g ON g.id = sc.group_id "
            "ORDER BY lower(sc.name), sc.id"))).fetchall()
        checks = []
        for r in rows:
            c = dict(zip(_CHECK_COLS, r))
            c["id"] = str(c["id"])
            c["name"] = c["name"] or "Unnamed monitor"
            c["check_type"] = (c["check_type"] or "").lower()
            c["tags"] = list(c["tags"] or [])
            c["check_interval"] = int(c["check_interval"] or 60)
            c["criticality"] = (c["criticality"] or "").strip().lower() or None
            checks.append(c)
        ctx._cache["svc_checks"] = checks
    return ctx._cache["svc_checks"]


async def _maintenance_windows(ctx: SectionCtx) -> list[SimpleNamespace]:
    """Service maintenance windows (cancelled ones included) overlapping the period."""
    if "svc_maint" not in ctx._cache:
        start, end, _now = _window(ctx)
        rows = (await ctx.db.execute(text(
            "SELECT m.id::text, m.scope_type, m.scope_check_id::text, m.scope_group_id::text, "
            "m.scope_tag, m.starts_at, m.ends_at, m.ended_at, m.cancelled_at, m.reason, "
            "sc.name, g.name "
            "FROM service_check_maintenance m "
            "LEFT JOIN service_checks sc ON sc.id = m.scope_check_id "
            "LEFT JOIN service_check_groups g ON g.id = m.scope_group_id "
            "WHERE m.starts_at < :t AND LEAST(m.ends_at, COALESCE(m.ended_at, m.ends_at)) > :f "
            "ORDER BY m.starts_at, m.id"), {"f": start, "t": _aware(ctx.to) or end})).fetchall()
        ctx._cache["svc_maint"] = [
            SimpleNamespace(id=r[0], scope_type=r[1], scope_check_id=r[2], scope_group_id=r[3],
                            scope_tag=r[4], starts_at=_aware(r[5]), ends_at=_aware(r[6]),
                            ended_at=_aware(r[7]), cancelled_at=_aware(r[8]), reason=r[9],
                            check_name=r[10], group_name=r[11])
            for r in rows if _aware(r[5]) and _aware(r[6])
        ]
    return ctx._cache["svc_maint"]


def _compact(segments) -> list[tuple]:
    """Merge contiguous pieces with the same up fraction (results unchanged:
    summarize/incident_intervals/daily_summaries treat them as one run)."""
    out: list[tuple] = []
    for lo, hi, fraction in segments:
        if out and out[-1][1] == lo and out[-1][2] == fraction:
            out[-1] = (out[-1][0], hi, fraction)
        else:
            out.append((lo, hi, fraction))
    return out


async def _evidence(ctx: SectionCtx) -> dict:
    """Confirmed-basis evidence for every check that existed in the period.

    {"services": [svc...], "start", "end", "now", "policy": (target, source)}
    where svc carries the check row, summary, incidents, segments and the SLA
    verdict (the same computation as service_reports.fleet_report)."""
    if "svc_evidence" in ctx._cache:
        return ctx._cache["svc_evidence"]
    from app.services import service_maintenance
    from app.services.availability_policy import load_default_target, resolve_target
    from app.services.report_data_service import _compliance
    from app.services.service_availability import (
        incident_intervals,
        read_fleet_segments,
        summarize,
    )

    start, end, now = _window(ctx)
    checks = [c for c in await _checks(ctx)
              if not (_aware(c["created_at"]) and _aware(c["created_at"]) >= end)]
    policy_target, policy_source = await load_default_target(ctx.db)
    windows = [w for w in await _maintenance_windows(ctx) if not w.cancelled_at]
    maintenance: dict[str, list] = {}
    if end > start:
        for c in checks:
            spans = service_maintenance.intervals_from_windows(windows, c, start, end)
            if spans:
                maintenance[c["id"]] = spans
    spec = {c["id"]: (c["created_at"], c["check_interval"]) for c in checks}

    def collect() -> dict:
        out = {}
        for cid, segments, count, observed_start in read_fleet_segments(
                ctx.ch(), spec, start, end, basis="confirmed"):
            segments = _compact(segments)
            excluded = maintenance.get(cid) or []
            out[cid] = {
                "segments": segments, "count": count, "observed_start": observed_start,
                "excluded": excluded,
                "summary": summarize(segments, observed_start, end, excluded),
                "incidents": incident_intervals(segments, observed_start, end, excluded,
                                                merge_gap_sec=2 * spec[cid][1], now=now),
            }
        return out

    data = await asyncio.to_thread(collect) if spec and end > start else {}
    services = []
    for c in checks:
        d = data.get(c["id"]) or {"segments": [], "count": 0, "observed_start": start,
                                  "excluded": maintenance.get(c["id"]) or [],
                                  "summary": None, "incidents": []}
        summary = d["summary"]
        avail = _finite(summary["uptime_pct"]) if summary else None
        covered = float(summary["covered_sec"]) if summary else 0.0
        downtime = float(summary["total_downtime_sec"]) if summary and avail is not None else None
        target, source = resolve_target(c.get("sla_target_pct"), policy_target, policy_source)
        meets, compliance, budget_used = _compliance(avail, target, downtime, covered)
        services.append({
            **d, "check": c, "avail": avail, "covered": covered, "downtime": downtime,
            "unknown": float(summary["unknown_sec"]) if summary else None,
            "coverage": _finite(summary["coverage_pct"]) if summary else None,
            "maintenance_sec": float(summary["maintenance_sec"]) if summary else 0.0,
            "target": target, "target_source": source, "meets": meets,
            "compliance": compliance, "budget_used": _finite(budget_used),
        })
    result = {"services": services, "start": start, "end": end, "now": now,
              "policy": (policy_target, policy_source)}
    ctx._cache["svc_evidence"] = result
    return result


def _weighted_uptime(services: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(confirmed uptime weighted by measured time, coverage) across services."""
    covered = sum(s["covered"] for s in services if s["avail"] is not None)
    down = sum(s["downtime"] or 0.0 for s in services if s["avail"] is not None)
    span = sum(s["covered"] + (s["unknown"] or 0.0) for s in services if s["summary"])
    uptime = (covered - down) / covered * 100 if covered > 0 else None
    return uptime, (sum(s["covered"] for s in services) / span * 100 if span > 0 else None)


def _owner(c: dict) -> str:
    team, biz = (c.get("owner_team") or "").strip(), (c.get("business_owner") or "").strip()
    if team and biz and team != biz:
        return f"{team} ({biz})"
    return team or biz or "—"


# Latest result of each selected monitoring vantage (controller or sensor).
_VANTAGE_COLS = ("check_id", "poller_id", "sensor_name", "site_name", "location", "sensor_status",
                 "state", "last_change_at", "last_result_at", "latency_ms", "error",
                 "tls_expiry", "tls_days", "tls_valid", "tls_issuer", "tls_subject")


def _vantage_label(poller_id: str, sensor: Optional[str], site: Optional[str],
                   location: Optional[str]) -> str:
    if poller_id == "central":
        return "Controller"
    place = site or location or "Unassigned site"
    return f"{place} · {sensor}" if sensor else f"{place} · sensor {str(poller_id)[:8]}"


async def _vantages(ctx: SectionCtx) -> list[dict]:
    if "svc_vantages" not in ctx._cache:
        rows = (await ctx.db.execute(text(
            "SELECT v.service_check_id::text, v.poller_id, s.name, si.name, s.location, s.status, "
            "v.state, v.last_change_at, v.last_result_at, v.last_latency_ms, v.last_error, "
            "v.last_tls_expiry_date, v.last_tls_days_remaining, v.last_tls_valid, "
            "v.last_tls_issuer, v.last_tls_subject "
            "FROM service_check_vantage_status v "
            "JOIN service_monitoring_vantages sel ON sel.service_check_id = v.service_check_id "
            "AND sel.poller_id = v.poller_id "
            "LEFT JOIN sensors s ON s.id::text = v.poller_id "
            "LEFT JOIN sites si ON si.id = s.site_id "
            "ORDER BY v.service_check_id, v.poller_id"))).fetchall()
        out = []
        for r in rows:
            v = dict(zip(_VANTAGE_COLS, r))
            v["check_id"] = str(v["check_id"])
            v["poller_id"] = str(v["poller_id"])
            v["label"] = _vantage_label(v["poller_id"], v["sensor_name"], v["site_name"], v["location"])
            out.append(v)
        ctx._cache["svc_vantages"] = out
    return ctx._cache["svc_vantages"]


# ─── 1. SLA compliance ──────────────────────────────────────────────────────

_COMPLIANCE_ORDER = {"breached": 0, "at_risk": 1, "met": 2, "not_assessed": 3}
# Table verdicts use the report pill vocabulary (compliant = target met).
_COMPLIANCE_PILL = {"met": "compliant", "at_risk": "at risk", "breached": "non-compliant",
                    "not_assessed": "not assessed"}


@register_section(
    "service_sla_compliance", title="Service SLA Compliance", category="Services",
    description=("Confirmed availability of every service check against its target — met, at "
                 "risk, breached or not assessed — with coverage, downtime and error budget used."),
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(50, 500),
             {"key": "show", "label": "Services to list", "type": "select", "default": "all",
              "choices": [["all", "All services"], ["attention", "Breached and at risk only"]]}],
)
async def service_sla_compliance(ctx: SectionCtx) -> dict:
    from app.services.report_data_service import AT_RISK_BUDGET_PCT

    title = "Service SLA Compliance"
    ev = await _evidence(ctx)
    services = ev["services"]
    if not services:
        return _section("service_sla_compliance", title,
                        description="Confirmed availability of service checks against their targets.",
                        notes=["No service checks existed in this period."])
    counts = {k: 0 for k in _COMPLIANCE_ORDER}
    for s in services:
        counts[s["compliance"]] += 1
    assessed = counts["met"] + counts["at_risk"] + counts["breached"]
    uptime, coverage = _weighted_uptime(services)
    policy_target = ev["policy"][0]
    worst_budget = max((s for s in services if s["budget_used"] is not None),
                       key=lambda s: s["budget_used"], default=None)

    def of_assessed(n: int) -> str:
        return f"{n / assessed * 100:.0f}% of {assessed} assessed" if assessed else "None assessed"

    attention = ctx.opt("show") == "attention"
    ordered = sorted(services, key=lambda s: (
        _COMPLIANCE_ORDER[s["compliance"]],
        s["avail"] if s["avail"] is not None else 101.0, s["check"]["name"].lower()))
    if attention:
        ordered = [s for s in ordered if s["compliance"] in ("breached", "at_risk")]
    top_n = int(ctx.opt("top_n"))
    listed = ordered[:top_n]
    rows = [[
        s["check"]["name"],
        s["check"]["criticality"] or "—",
        _owner(s["check"]),
        f"{_target(s['target'])} ({s['target_source']})",
        _avail(s["avail"]),
        _share(s["coverage"]),
        _dur(s["downtime"]),
        "—" if s["budget_used"] is None else f"{s['budget_used']:.1f}%",
        _COMPLIANCE_PILL[s["compliance"]],
    ] for s in listed]
    table_title = ("Services that breached or are at risk of breaching their target"
                   if attention else "Service SLA compliance")
    notes = [BASIS_NOTE,
             f"At risk: the target was met but more than {AT_RISK_BUDGET_PCT:g}% of the error budget "
             "(the downtime the target allows over the measured time) was used. Targets come from each "
             "service's own objective, otherwise the organisation availability policy; the source is "
             "shown next to each target."]
    if counts["not_assessed"]:
        notes.append(f"{counts['not_assessed']} service(s) have no confirmed evidence in this period "
                     "and are not assessed (not counted as up or down).")
    if attention and not ordered:
        notes.insert(0, "No service breached or is at risk of breaching its target in this period.")
    notes += _history_notes(ctx)
    return _section(
        "service_sla_compliance", title,
        description=(f"Confirmed availability of {len(services)} service check(s) against their "
                     "targets over the reporting period."),
        kpis=[
            {"label": "Met", "value": f"{counts['met']:,}",
             "accent": "success" if counts["met"] else "neutral", "subtitle": of_assessed(counts["met"])},
            {"label": "At risk", "value": f"{counts['at_risk']:,}",
             "accent": "warning" if counts["at_risk"] else "neutral",
             "subtitle": f"Over {AT_RISK_BUDGET_PCT:g}% of error budget used"},
            {"label": "Breached", "value": f"{counts['breached']:,}",
             "accent": "danger" if counts["breached"] else ("success" if assessed else "neutral"),
             "subtitle": "Below target"},
            {"label": "Not assessed", "value": f"{counts['not_assessed']:,}", "accent": "neutral",
             "subtitle": "No confirmed evidence"},
            {"label": "Confirmed uptime", "value": _avail(uptime),
             "accent": ("neutral" if uptime is None else
                        "success" if uptime >= policy_target else "warning"),
             "subtitle": f"All services · {_share(coverage)} coverage"},
            {"label": "Most error budget used",
             "value": f"{worst_budget['budget_used']:.1f}%" if worst_budget else "—",
             "accent": ("neutral" if not worst_budget else
                        "danger" if worst_budget["budget_used"] > 100 else
                        "warning" if worst_budget["budget_used"] > AT_RISK_BUDGET_PCT else "success"),
             "subtitle": worst_budget["check"]["name"] if worst_budget else "Not measured"},
        ],
        charts=[{"title": "Services by SLA status",
                 "series": {"kind": "donut", "unit": "services",
                            "points": [{"label": label, "value": counts[key]}
                                       for key, label in (("met", "Met"), ("at_risk", "At risk"),
                                                          ("breached", "Breached"),
                                                          ("not_assessed", "Not assessed"))
                                       if counts[key]]}}],
        tables=[{"title": _top_title(table_title, len(listed), len(ordered)),
                 "headers": ["Service", "Criticality", "Owner", "Target (source)", "Confirmed uptime",
                             "Coverage", "Downtime", "Error budget used", "SLA status"],
                 "styles": ["text", "status", "text", "num", "pct-bar", "pct", "num", "pct", "status"],
                 "rows": rows}] if rows else [],
        notes=notes,
    )


# ─── 2. Confirmed incidents ─────────────────────────────────────────────────

def _incident_span(inc: dict, end: datetime) -> float:
    if inc["duration_sec"] is not None:
        return float(inc["duration_sec"])
    return max(0.0, ((inc["end"] or end) - inc["start"]).total_seconds())


@register_section(
    "service_outages", title="Service Incidents", category="Services",
    description=("Confirmed service incidents in the period — service, group, start, end or "
                 "ongoing and duration — with incidents started per day."),
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(25, 500),
             {"key": "sort", "label": "Sort by", "type": "select", "default": "recent",
              "choices": [["recent", "Most recent first"], ["longest", "Longest first"]]},
             {"key": "min_minutes", "label": "Hide incidents shorter than (minutes)", "type": "int",
              "default": 0, "min": 0, "max": 1440}],
)
async def service_outages(ctx: SectionCtx) -> dict:
    title = "Service Incidents"
    ev = await _evidence(ctx)
    end = ev["end"]
    incidents = [(s, inc) for s in ev["services"] for inc in s["incidents"]]
    zone, days = _local_days(ctx, ev["start"], end)
    if not ev["services"]:
        return _section("service_outages", title,
                        description="Confirmed service incidents in the reporting period.",
                        notes=["No service checks existed in this period."])
    if not incidents:
        return _section("service_outages", title,
                        description="Confirmed service incidents in the reporting period.",
                        kpis=[{"label": "Incidents", "value": "0", "accent": "success",
                               "subtitle": f"{len(ev['services'])} service(s) monitored"}],
                        notes=["No confirmed service incidents in this period.", BASIS_NOTE]
                        + _history_notes(ctx))

    downtime = sum(inc["downtime_sec"] or 0.0 for _s, inc in incidents)
    longest = max(incidents, key=lambda p: _incident_span(p[1], end))
    completed = [inc for _s, inc in incidents
                 if not inc["starts_before_window"] and inc["end"] is not None]
    mttr = (sum(i["duration_sec"] for i in completed) / len(completed)) if completed else None
    ongoing = sum(1 for _s, inc in incidents if inc["ongoing"])
    affected = len({s["check"]["id"] for s, _inc in incidents})

    per_day: dict[date, int] = {d: 0 for d in days}
    for _s, inc in incidents:
        d = inc["start"].astimezone(zone).date()
        if d in per_day:
            per_day[d] += 1

    min_minutes = int(ctx.opt("min_minutes"))
    listed = [p for p in incidents if _incident_span(p[1], end) >= min_minutes * 60]
    longest_first = ctx.opt("sort") == "longest"
    if longest_first:
        listed.sort(key=lambda p: (-_incident_span(p[1], end), p[0]["check"]["name"].lower()))
    else:
        listed.sort(key=lambda p: p[1]["start"], reverse=True)
    top_n = int(ctx.opt("top_n"))
    shown = listed[:top_n]

    def started(inc: dict) -> str:
        return ("≤ " if inc["starts_before_window"] else "") + ctx.fmt_short(inc["start"])

    def ended(inc: dict) -> str:
        if inc["end"] is not None:
            return ctx.fmt_short(inc["end"])
        return "Ongoing" if inc["ongoing"] else "After period end"

    def duration(inc: dict) -> str:
        exact = inc["end"] is not None and not inc["starts_before_window"]
        return ("" if exact else "≥ ") + _dur(_incident_span(inc, end))

    rows = [[s["check"]["name"], s["check"].get("group_name") or "Ungrouped",
             started(inc), ended(inc), duration(inc)] for s, inc in shown]
    notes = [BASIS_NOTE,
             "A run interrupted only by maintenance or by a monitoring gap shorter than two check "
             "intervals is one incident. '≤' marks an incident already in progress when the period "
             f"began; '≥' marks a duration that continues beyond the period. Times are {ctx.tz_label()}."]
    if min_minutes and len(listed) < len(incidents):
        notes.append(f"{len(incidents) - len(listed)} incident(s) shorter than {min_minutes} min are "
                     "counted above but not listed.")
    notes += _history_notes(ctx)
    return _section(
        "service_outages", title,
        description=(f"{len(incidents)} confirmed incident(s) across {affected} service(s) in the "
                     "reporting period."),
        kpis=[
            {"label": "Incidents", "value": f"{len(incidents):,}", "accent": "danger",
             "subtitle": f"{affected} service(s) affected"},
            {"label": "Ongoing", "value": f"{ongoing:,}",
             "accent": "danger" if ongoing else "success"},
            {"label": "Confirmed downtime", "value": _dur(downtime), "accent": "warning",
             "subtitle": "Sum across services, maintenance excluded"},
            {"label": "Longest incident", "value": _dur(_incident_span(longest[1], end)),
             "accent": "warning", "subtitle": longest[0]["check"]["name"]},
            {"label": "Mean time to recover", "value": _dur(mttr), "accent": "info" if completed else "neutral",
             "subtitle": f"{len(completed)} completed incident(s)" if completed
             else "No incident both started and ended in the period"},
        ],
        charts=[{"title": "Incidents started per day",
                 "series": {"kind": "bars", "unit": "incidents", "color": "danger",
                            "points": [{"t": ctx.iso(_midnight(d, zone)), "v": per_day[d]}
                                       for d in days]}}] if days else [],
        tables=[{"title": _top_title("Longest incidents" if longest_first else "Most recent incidents",
                                     len(shown), len(listed)),
                 "headers": ["Service", "Group", "Started", "Ended", "Duration"],
                 "styles": ["text", "text", "text", "text", "num"], "rows": rows}] if rows else [],
        notes=notes,
    )


# ─── 3. Response times ──────────────────────────────────────────────────────

_RT_TITLE = {"p95": "95th-Percentile", "p99": "99th-Percentile", "p50": "Median",
             "avg": "Average", "max": "Maximum"}
_RT_TEXT = {"p95": "95th-percentile", "p99": "99th-percentile", "p50": "median",
            "avg": "average", "max": "maximum"}


@register_section(
    "service_response_times", title="Service Response Times", category="Services",
    description=("Response time percentiles (p50/p95/p99) of successful probes per service, "
                 "computed in the time-series database, with the slowest services ranked."),
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(25, 200),
             {"key": "sort", "label": "Rank by", "type": "select", "default": "p95",
              "choices": [["p95", "95th percentile"], ["p99", "99th percentile"],
                          ["p50", "Median"], ["avg", "Average"], ["max", "Maximum"]]},
             {"key": "check_type", "label": "Check type", "type": "select", "default": "all",
              "choices": CHECK_TYPE_CHOICES},
             {"key": "threshold_ms", "label": "Slow threshold (ms)", "type": "number",
              "default": 1000, "min": 1, "max": 120000}],
)
async def service_response_times(ctx: SectionCtx) -> dict:
    start, end, now = _window(ctx)
    ctype = ctx.opt("check_type")
    top_n = int(ctx.opt("top_n"))
    threshold = float(ctx.opt("threshold_ms"))
    kind = "" if ctype == "all" else f"{TYPE_LABEL.get(ctype, ctype.upper())} "
    checks = {c["id"]: c for c in await _checks(ctx)}
    raw = start >= now - timedelta(days=RAW_RETENTION_DAYS) + timedelta(minutes=5)
    requested = ctx.opt("sort")
    sort = requested if raw or requested in ("p95", "avg", "max") else "p95"
    title = f"Top {top_n} {kind}Services by {_RT_TITLE[sort]} Response Time"

    params: dict[str, Any] = {"f": _ch_time(start), "t": _ch_time(end)}
    id_sql = ""
    if ctype != "all":
        params["ids"] = sorted(cid for cid, c in checks.items() if c["check_type"] == ctype)
        id_sql = " AND service_check_id IN %(ids)s"

    stats: list[dict] = []
    if end > start and (ctype == "all" or params["ids"]):
        if raw:
            # Exact percentiles over every successful probe with a measured time.
            rows = await ctx.ch_rows(
                "SELECT toString(service_check_id) AS sid, count() AS probes, "
                "quantilesExactIf(0.5, 0.95, 0.99)(response_ms, response_ms > 0) AS q, "
                "avgIf(response_ms, response_ms > 0) AS avg_ms, max(response_ms) AS max_ms "
                "FROM zenplus.service_metrics "
                f"WHERE timestamp >= %(f)s AND timestamp < %(t)s AND is_up = 1{id_sql} "
                "GROUP BY sid", params)
            for sid, n, q, avg_ms, max_ms in rows:
                q = list(q or []) + [None, None, None]
                stats.append({"id": str(sid), "n": int(n or 0), "p50": _finite(q[0]),
                              "p95": _finite(q[1]), "p99": _finite(q[2]),
                              "avg": _finite(avg_ms), "max": _finite(max_ms)})
        else:
            # 5-minute buckets in which every probe succeeded: sample-weighted
            # average, the 95th percentile of those 5-minute averages and the max.
            rows = await ctx.ch_rows(
                "SELECT toString(service_check_id) AS sid, sum(sample_count) AS probes, "
                "sum(avg_response_ms * sample_count) / nullIf(sum(sample_count), 0) AS avg_ms, "
                "quantileExactWeighted(0.95)(avg_response_ms, sample_count) AS p95_ms, "
                "max(max_response_ms) AS max_ms "
                "FROM zenplus.service_metrics_5m "
                f"WHERE timestamp >= %(f)s AND timestamp < %(t)s AND uptime_pct >= 1{id_sql} "
                "GROUP BY sid", params)
            for sid, n, avg_ms, p95, max_ms in rows:
                stats.append({"id": str(sid), "n": int(n or 0), "p50": None, "p95": _finite(p95),
                              "p99": None, "avg": _finite(avg_ms), "max": _finite(max_ms)})
    stats = [s for s in stats if s["id"] in checks and s["n"] > 0
             and (ctype == "all" or checks[s["id"]]["check_type"] == ctype)]
    metric_text = _RT_TEXT[sort] if raw or sort != "p95" else "95th percentile of 5-minute averages"
    notes = ["Only successful probes are measured; failed probes are diagnostic evidence and do "
             "not count towards response times. Each service's probes from every monitoring site "
             "are combined."]
    if raw:
        notes.append("Percentiles are exact, computed in the time-series database over every "
                     "successful probe in the period.")
    else:
        notes.append(f"The period reaches past the {RAW_RETENTION_DAYS}-day raw probe retention, so "
                     "figures come from 5-minute rollups of buckets in which every probe succeeded: "
                     "the average is sample-weighted, P95 is the 95th percentile of 5-minute averages "
                     "and P50/P99 are not available.")
        if requested != sort:
            notes.append(f"Ranked by P95 of 5-minute averages instead of {_RT_TEXT[requested]}.")
        note = ctx.window_note(ROLLUP_RETENTION_DAYS, "The 5-minute service rollup")
        if note:
            notes.append(note)
    if not stats:
        return _section(
            "service_response_times", title,
            description=f"Response times of successful {kind}service probes in the reporting period.",
            notes=[f"No successful {kind}service probes were recorded in this period."] + notes[1:],
        )
    stats.sort(key=lambda s: (s[sort] is None, -(s[sort] or 0.0), checks[s["id"]]["name"].lower()))
    shown = stats[:top_n]
    p95s = [s["p95"] for s in stats if s["p95"] is not None]
    median_p95 = statistics.median(p95s) if p95s else None
    slow = [s for s in stats if s["p95"] is not None and s["p95"] > threshold]
    slowest = max((s for s in stats if s["p95"] is not None), key=lambda s: s["p95"], default=None)

    def name(s: dict) -> str:
        return checks[s["id"]]["name"]

    def ctype_of(s: dict) -> str:
        t = checks[s["id"]]["check_type"]
        return TYPE_LABEL.get(t, t.upper() or "—")

    if raw:
        headers = ["Service", "Type", "P50", "P95", "P99", "Average", "Max", "Probes"]
        rows = [[name(s), ctype_of(s), _ms(s["p50"]), _ms(s["p95"]), _ms(s["p99"]),
                 _ms(s["avg"]), _ms(s["max"]), f"{s['n']:,}"] for s in shown]
    else:
        headers = ["Service", "Type", "Average", "P95 of 5-min averages", "Max", "Probes"]
        rows = [[name(s), ctype_of(s), _ms(s["avg"]), _ms(s["p95"]), _ms(s["max"]),
                 f"{s['n']:,}"] for s in shown]
    chart_points = [{"label": name(s), "value": round(s[sort], 2)}
                    for s in shown[:15] if s[sort] is not None]
    return _section(
        "service_response_times", title,
        description=(f"Top {len(shown)} of {len(stats)} {kind}service(s) by {metric_text} response "
                     "time of successful probes over the reporting period."),
        kpis=[
            {"label": "Services measured", "value": f"{len(stats):,}", "accent": "primary",
             "subtitle": f"{sum(s['n'] for s in stats):,} successful probes"},
            {"label": "Slowest P95", "value": _ms(slowest["p95"]) if slowest else "—",
             "accent": ("neutral" if not slowest else
                        "warning" if slowest["p95"] > threshold else "success"),
             "subtitle": name(slowest) if slowest else "Not measured"},
            {"label": "Median service P95", "value": _ms(median_p95),
             "accent": "info" if median_p95 is not None else "neutral",
             "subtitle": f"Across {len(p95s)} service(s)" if p95s else "Not measured"},
            {"label": f"P95 over {threshold:g} ms", "value": f"{len(slow):,}",
             "accent": "danger" if slow else "success",
             "subtitle": f"of {len(stats)} measured service(s)"},
        ],
        charts=[{"title": f"Slowest services by {metric_text} response time",
                 "series": {"kind": "rank", "unit": "ms", "color": "warning",
                            "points": chart_points}}] if chart_points else [],
        tables=[{"title": _top_title("Response times", len(shown), len(stats)),
                 "headers": headers, "styles": ["text", "text"] + ["num"] * (len(headers) - 2),
                 "rows": rows}],
        notes=notes,
    )


# ─── 4. Daily uptime ────────────────────────────────────────────────────────

def _matrix_columns(ctx: SectionCtx, days: list[date]) -> tuple[str, list[tuple[str, list[date]]]]:
    """("day" | "week", [(label, days)]): one column per day, or calendar weeks
    (starting on the organisation's first day of the week)."""
    layout = ctx.opt("columns")
    if layout == "days" or (layout == "auto" and len(days) <= 14):
        return "day", [(_day_label(d), [d]) for d in days]
    first = {"monday": 0, "sunday": 6, "saturday": 5}.get(
        getattr(ctx._brand(), "week_start", "monday"), 0)
    groups: list[list[date]] = []
    for d in days:
        if not groups or d.weekday() == first:
            groups.append([])
        groups[-1].append(d)
    out = []
    for ds in groups:
        a, b = ds[0], ds[-1]
        label = _day_label(a) if a == b else (
            f"{_day_label(a)}–{b.day:02d}" if a.month == b.month else f"{_day_label(a)}–{_day_label(b)}")
        out.append((label, ds))
    return "week", out


@register_section(
    "service_daily_uptime", title="Daily Service Uptime", category="Services",
    description=("Confirmed uptime per calendar day (organisation timezone) for each service, with "
                 "the fleet's daily uptime."),
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(25, 200),
             {"key": "columns", "label": "Table columns", "type": "select", "default": "auto",
              "choices": [["auto", "Days (weeks for periods over 14 days)"], ["days", "Days"],
                          ["weeks", "Weeks"]]},
             {"key": "sort", "label": "Sort services by", "type": "select", "default": "worst",
              "choices": [["worst", "Lowest uptime first"], ["name", "Name"]]}],
)
async def service_daily_uptime(ctx: SectionCtx) -> dict:
    from app.services.service_availability import daily_summaries

    title = "Daily Service Uptime"
    ev = await _evidence(ctx)
    zone, days = _local_days(ctx, ev["start"], ev["end"])
    services = ev["services"]
    if not services or not days:
        return _section("service_daily_uptime", title,
                        description="Confirmed uptime per day for each service.",
                        notes=["No service checks existed in this period."])

    per_service: dict[str, dict[str, dict]] = {}
    fleet: dict[str, list[float]] = {}
    for s in services:
        if not s["segments"] and not s["excluded"]:
            per_service[s["check"]["id"]] = {}
            continue
        daily = daily_summaries(s["segments"], s["observed_start"], ev["end"], zone, s["excluded"])
        per_service[s["check"]["id"]] = {d["date"]: d for d in daily}
        for d in daily:
            f = fleet.setdefault(d["date"], [0.0, 0.0])
            f[0] += float(d["covered_sec"] or 0.0)
            f[1] += float(d["downtime_sec"] or 0.0)

    def uptime_of(covered: float, down: float) -> Optional[float]:
        return (covered - down) / covered * 100 if covered > 0 else None

    fleet_days = []
    for d in days:
        cov, down = fleet.get(d.isoformat(), (0.0, 0.0))
        fleet_days.append((d, uptime_of(cov, down), down))
    measured = [(d, u, down) for d, u, down in fleet_days if u is not None]
    worst = min(measured, key=lambda x: (x[1], x[0]), default=None)
    days_down = sum(1 for _d, _u, down in measured if down > 0)
    uptime, coverage = _weighted_uptime(services)
    with_downtime = sum(1 for s in services if (s["downtime"] or 0) > 0)

    unit, cols = _matrix_columns(ctx, days)

    def cell(entries: dict[str, dict], ds: list[date]) -> str:
        cov = down = maint = 0.0
        for d in ds:
            e = entries.get(d.isoformat())
            if e:
                cov += float(e["covered_sec"] or 0.0)
                down += float(e["downtime_sec"] or 0.0)
                maint += float(e["maintenance_sec"] or 0.0)
        if cov > 0:
            return _avail(uptime_of(cov, down))
        return "maint." if maint > 0 else "—"

    ordered = list(services)
    if ctx.opt("sort") == "name":
        ordered.sort(key=lambda s: s["check"]["name"].lower())
    else:
        ordered.sort(key=lambda s: (s["avail"] is None, s["avail"] if s["avail"] is not None else 0,
                                    s["check"]["name"].lower()))
    top_n = int(ctx.opt("top_n"))
    shown = ordered[:top_n]

    # Wide periods split into several tables so every column stays legible in PDF.
    chunks = [cols[i:i + MAX_MATRIX_COLUMNS] for i in range(0, len(cols), MAX_MATRIX_COLUMNS)]
    tables = []
    for chunk in chunks:
        heading = f"Confirmed uptime by {unit}"
        if len(chunks) > 1:
            heading += f" ({chunk[0][0]} to {chunk[-1][0]})"
        tables.append({
            "title": _top_title(heading, len(shown), len(ordered)),
            "headers": ["Service"] + [label for label, _ds in chunk] + ["Period"],
            "styles": ["text"] + ["pct"] * (len(chunk) + 1),
            "rows": [[s["check"]["name"]] + [cell(per_service[s["check"]["id"]], ds) for _l, ds in chunk]
                     + [_avail(s["avail"])] for s in shown],
        })

    values = [_avail_num(u) for _d, u, _down in fleet_days if u is not None]
    lo = max(0, math.floor(min(values)) - 1) if values else 90
    notes = [f"Calendar days follow the organisation timezone ({ctx.tz_name}). '—' means no confirmed "
             "evidence that day; 'maint.' means the day was entirely planned maintenance.", BASIS_NOTE]
    if unit == "week":
        notes.insert(0, "The period is longer than two weeks, so table columns are calendar weeks; "
                        "the chart shows every day.")
    if not measured:
        notes.insert(0, "No service has confirmed evidence in this period (not measured).")
    notes += _history_notes(ctx)
    return _section(
        "service_daily_uptime", title,
        description=(f"Confirmed uptime per {unit} for {len(shown)} of {len(services)} service(s), and "
                     "the fleet's uptime for each day of the period."),
        kpis=[
            {"label": "Period uptime", "value": _avail(uptime),
             "accent": "neutral" if uptime is None else "success" if uptime >= ev["policy"][0] else "warning",
             "subtitle": f"All services · {_share(coverage)} coverage"},
            {"label": "Worst day", "value": _avail(worst[1]) if worst else "—",
             "accent": "neutral" if not worst else "success" if worst[2] <= 0 else "warning",
             "subtitle": ctx.fmt_date(_midnight(worst[0], zone)) if worst else "Not measured"},
            {"label": "Days with downtime",
             "value": f"{days_down} of {len(measured)}" if measured else "—",
             "accent": "warning" if days_down else ("success" if measured else "neutral"),
             "subtitle": "Measured days" if measured else "No measured day"},
            {"label": "Services with downtime", "value": f"{with_downtime:,}",
             "accent": "warning" if with_downtime else "success",
             "subtitle": f"of {len(services)} service(s)"},
        ],
        charts=[{"title": "Fleet confirmed uptime per day",
                 "series": {"kind": "bars", "unit": "%", "color": "success",
                            "y_domain": [min(lo, 99), 100],
                            "points": [{"t": ctx.iso(_midnight(d, zone)), "v": _avail_num(u)}
                                       for d, u, _down in fleet_days]}}] if values else [],
        tables=tables,
        notes=notes,
    )


# ─── 5. TLS certificate expiry ──────────────────────────────────────────────

def _tls_eligible(c: dict) -> bool:
    """Same rule as report_data_service._tls_eligible."""
    kind = c.get("check_type") or ""
    return kind == "tls" or (kind == "http" and (c.get("target_url") or "").lower().startswith("https://"))


def _days_left(expiry: Optional[datetime], days: Any, now: datetime) -> Optional[int]:
    if expiry is not None:
        return math.floor((expiry - now).total_seconds() / 86400)
    try:
        return int(days) if days is not None else None
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


@register_section(
    "tls_certificate_expiry", title="TLS Certificate Expiry", category="Security",
    description=("Certificates seen by service checks that are expired or expire within the "
                 "horizon, graded by each check's own warning and critical thresholds, as of "
                 "report generation."),
    scope="fleet", requires=REQUIRES, semantics="snapshot",
    options=[top_n_option(50, 500),
             {"key": "days", "label": "Show certificates expiring within (days)", "type": "int",
              "default": 90, "min": 1, "max": 730}],
)
async def tls_certificate_expiry(ctx: SectionCtx) -> dict:
    horizon = int(ctx.opt("days"))
    title = f"TLS Certificates Expiring Within {horizon} Days"
    now = datetime.now(timezone.utc)
    checks = [c for c in await _checks(ctx) if _tls_eligible(c)]
    active = [c for c in checks if c.get("enabled") is not False]
    paused = len(checks) - len(active)
    if not active:
        notes = ["No enabled TLS or HTTPS service checks are configured."]
        if paused:
            notes.append(f"{paused} paused TLS/HTTPS check(s) are not assessed.")
        return _section("tls_certificate_expiry", title,
                        description="Certificate expiry of TLS and HTTPS service checks.", notes=notes)

    by_check: dict[str, list[dict]] = {}
    for v in await _vantages(ctx):
        if v["tls_expiry"] is not None or v["tls_days"] is not None:
            by_check.setdefault(v["check_id"], []).append(v)

    certs: list[dict] = []
    no_evidence: list[str] = []
    invalid: set[str] = set()
    for c in active:
        observed = by_check.get(c["id"]) or []
        if not observed and (c.get("tls_expiry_date") is not None or c.get("tls_days_remaining") is not None):
            observed = [{"tls_expiry": c.get("tls_expiry_date"), "tls_days": c.get("tls_days_remaining"),
                         "tls_subject": c.get("tls_subject"), "tls_issuer": c.get("tls_issuer"),
                         "tls_valid": None, "label": "Latest result"}]
        if not observed:
            no_evidence.append(c["name"])
            continue
        warn = _positive_int(c.get("tls_warn_days"), 30)
        crit = _positive_int(c.get("tls_critical_days"), 7)
        # One row per distinct certificate the vantages saw (load balancers can differ).
        groups: dict[tuple, dict] = {}
        for o in observed:
            expiry = _aware(o["tls_expiry"])
            key = (expiry.replace(microsecond=0) if expiry else o["tls_days"],
                   o.get("tls_subject") or "", o.get("tls_issuer") or "")
            g = groups.setdefault(key, {"check": c, "expiry": expiry, "days": o["tls_days"],
                                        "subject": o.get("tls_subject"), "issuer": o.get("tls_issuer"),
                                        "vantages": []})
            g["vantages"].append(o["label"])
            if o.get("tls_valid") is False:
                invalid.add(c["name"])
        for g in groups.values():
            days = _days_left(g["expiry"], g["days"], now)
            if days is None:
                continue
            expired = days < 0 or (g["expiry"] is not None and g["expiry"] <= now)
            g["days_left"] = days
            g["status"] = ("expired" if expired else "critical" if days <= crit
                           else "warning" if days <= warn else "ok")
            certs.append(g)

    counts = {k: sum(1 for g in certs if g["status"] == k) for k in ("expired", "critical", "warning")}
    listed = [g for g in certs if g["status"] != "ok" or g["days_left"] <= horizon]
    listed.sort(key=lambda g: (g["days_left"], g["check"]["name"].lower()))
    upcoming = sum(1 for g in listed if g["status"] != "expired")
    top_n = int(ctx.opt("top_n"))
    shown = listed[:top_n]
    rows = [[g["check"]["name"], _clip(g["subject"]), _clip(g["issuer"]),
             ctx.fmt_date(g["expiry"]) if g["expiry"] else "—", f"{g['days_left']:,}", g["status"],
             _clip(", ".join(sorted(set(g["vantages"]))), 80)] for g in shown]
    notes = ["Graded with each check's own critical and warning days. Dates come from the latest TLS "
             "handshake seen by each monitoring site; service-check certificates are reported "
             "separately from F5 certificate inventory, which does not prove an active binding."]
    if no_evidence:
        names = ", ".join(no_evidence[:10]) + (" …" if len(no_evidence) > 10 else "")
        notes.append(f"No TLS evidence yet for {len(no_evidence)} check(s) (not measured): {names}.")
    if invalid:
        notes.append(f"Certificate validation failed at a monitoring site for: "
                     f"{', '.join(sorted(invalid)[:10])}.")
    if paused:
        notes.append(f"{paused} paused TLS/HTTPS check(s) are not assessed.")
    if not listed:
        notes.insert(0, f"No certificate is expired or expires within {horizon} days.")
    return _section(
        "tls_certificate_expiry", title,
        description=(f"Certificates of {len(active)} TLS/HTTPS service check(s) that are expired or "
                     f"expire within {horizon} days, as of report generation."),
        kpis=[
            {"label": "Expired", "value": f"{counts['expired']:,}",
             "accent": "danger" if counts["expired"] else "success"},
            {"label": "Critical", "value": f"{counts['critical']:,}",
             "accent": "danger" if counts["critical"] else "success",
             "subtitle": "Within each check's critical days"},
            {"label": "Warning", "value": f"{counts['warning']:,}",
             "accent": "warning" if counts["warning"] else "success",
             "subtitle": "Within each check's warning days"},
            {"label": f"Expiring within {horizon} days", "value": f"{upcoming:,}",
             "accent": "info", "subtitle": f"of {len(certs)} certificate(s) seen"},
            {"label": "No TLS evidence", "value": f"{len(no_evidence):,}",
             "accent": "warning" if no_evidence else "neutral",
             "subtitle": f"of {len(active)} TLS/HTTPS check(s)"},
        ],
        tables=[{"title": _top_title("Certificates needing attention", len(shown), len(listed)),
                 "headers": ["Service", "Subject", "Issuer", "Expires", "Days left", "Status", "Seen by"],
                 "styles": ["text", "text", "text", "text", "num", "status", "text"],
                 "rows": rows}] if rows else [],
        notes=notes,
    )


# ─── 6. By group and owner ──────────────────────────────────────────────────

_GROUPINGS = {
    "group": ("Service group", "Service groups", "Ungrouped",
              lambda c: c.get("group_name") or "Ungrouped"),
    "owner_team": ("Owner team", "Owner teams", "No owner team",
                   lambda c: (c.get("owner_team") or "").strip() or "No owner team"),
    "business_owner": ("Business owner", "Business owners", "No business owner",
                       lambda c: (c.get("business_owner") or "").strip() or "No business owner"),
    "criticality": ("Criticality", "Criticality levels", "Not set",
                    lambda c: c.get("criticality") or "Not set"),
}
_UNMAPPED_LABEL = {"group": "Ungrouped services", "owner_team": "Without an owner team",
                   "business_owner": "Without a business owner", "criticality": "Criticality not set"}


def _down_intervals(s: dict, end: datetime) -> list[tuple[datetime, datetime]]:
    """Confirmed-down time of one service (maintenance removed)."""
    from app.services.service_availability import merge_intervals, subtract_intervals
    pieces = subtract_intervals(s["segments"], merge_intervals(s["excluded"])) if s["excluded"] else s["segments"]
    lo_bound = s["observed_start"]
    return [(max(lo, lo_bound), min(hi, end)) for lo, hi, f in pieces
            if f < 1 and min(hi, end) > max(lo, lo_bound)]


def _group_table(services: list[dict], key: str, end: datetime, top_n: int) -> tuple[dict, list]:
    from app.services.service_availability import merge_intervals
    label, _plural, _fallback, keyfn = _GROUPINGS[key]
    groups: dict[str, list[dict]] = {}
    for s in services:
        groups.setdefault(keyfn(s["check"]), []).append(s)
    stats = []
    for name, members in groups.items():
        assessed = [s for s in members if s["avail"] is not None]
        intervals = [iv for s in assessed for iv in _down_intervals(s, end)]
        merged = sum((hi - lo).total_seconds() for lo, hi in merge_intervals(intervals))
        uptime, _cov = _weighted_uptime(members)
        worst = min(assessed, key=lambda s: (s["avail"], s["check"]["name"].lower()), default=None)
        stats.append({
            "name": name, "services": len(members), "assessed": len(assessed), "uptime": uptime,
            "incidents": sum(len(s["incidents"]) for s in members),
            "merged": merged if assessed else None,
            "impact": sum(s["downtime"] or 0.0 for s in assessed) if assessed else None,
            "worst": worst,
        })
    stats.sort(key=lambda g: (g["merged"] is None, -(g["merged"] or 0.0), g["name"].lower()))
    shown = stats[:top_n]
    rows = [[g["name"], f"{g['services']:,}", f"{g['assessed']:,}", _avail(g["uptime"]),
             f"{g['incidents']:,}", _dur(g["merged"]),
             "—" if g["impact"] is None else f"{g['impact'] / 60:,.1f}",
             f"{g['worst']['check']['name']} ({_avail(g['worst']['avail'])})" if g["worst"] else "—"]
            for g in shown]
    return ({"title": _top_title(f"By {label.lower()}", len(shown), len(stats)),
             "headers": [label, "Services", "Assessed", "Confirmed uptime", "Incidents",
                         "Merged downtime", "Impact (service-min)", "Lowest service"],
             "styles": ["text", "num", "num", "pct-bar", "num", "num", "num", "text"],
             "rows": rows}, stats)


@register_section(
    "service_by_group", title="Availability by Service Group and Owner", category="Services",
    description=("Confirmed uptime, merged downtime and impact minutes per service group and per "
                 "owner team over the period."),
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(20, 200),
             {"key": "group_by", "label": "Group by", "type": "select", "default": "both",
              "choices": [["both", "Service group and owner team"], ["group", "Service group"],
                          ["owner_team", "Owner team"], ["business_owner", "Business owner"],
                          ["criticality", "Criticality"]]}],
)
async def service_by_group(ctx: SectionCtx) -> dict:
    from app.services.service_availability import merge_intervals

    by = ctx.opt("group_by")
    keys = ["group", "owner_team"] if by == "both" else [by]
    title = ("Availability by Service Group and Owner" if by == "both"
             else f"Availability by {_GROUPINGS[by][0]}")
    ev = await _evidence(ctx)
    services, end = ev["services"], ev["end"]
    if not services:
        return _section("service_by_group", title,
                        description="Confirmed availability per service group and owner.",
                        notes=["No service checks existed in this period."])
    top_n = int(ctx.opt("top_n"))
    tables, first_stats = [], None
    for key in keys:
        table, stats = _group_table(services, key, end, top_n)
        tables.append(table)
        first_stats = first_stats if first_stats is not None else stats
    assessed = [s for s in services if s["avail"] is not None]
    all_down = merge_intervals([iv for s in assessed for iv in _down_intervals(s, end)])
    merged_total = sum((hi - lo).total_seconds() for lo, hi in all_down) if assessed else None
    impact_total = sum(s["downtime"] or 0.0 for s in assessed) if assessed else None
    first = keys[0]
    unmapped = sum(1 for s in services if _GROUPINGS[first][3](s["check"]) == _GROUPINGS[first][2])
    chart_points = [{"label": g["name"], "value": round(g["merged"] / 60, 1)}
                    for g in (first_stats or [])[:10] if g["merged"]]
    notes = ["Merged downtime is wall-clock time during which at least one service in the row was "
             "confirmed down (overlaps counted once); impact adds each service's confirmed downtime "
             "(two services down for the same 10 minutes = 20 service-minutes). Uptime is weighted "
             "by each service's measured time.", BASIS_NOTE]
    if not assessed:
        notes.insert(0, "No service has confirmed evidence in this period; downtime is not measured.")
    notes += _history_notes(ctx)
    return _section(
        "service_by_group", title,
        description=(f"Confirmed availability of {len(services)} service(s) rolled up by "
                     + " and by ".join(_GROUPINGS[k][0].lower() for k in keys) + "."),
        kpis=[
            {"label": "Merged downtime", "value": _dur(merged_total),
             "accent": "neutral" if merged_total is None else "warning" if merged_total else "success",
             "subtitle": "Overlapping outages counted once"},
            {"label": "Impact", "value": "—" if impact_total is None else f"{impact_total / 60:,.1f} service-min",
             "accent": "neutral" if impact_total is None else "warning" if impact_total else "success",
             "subtitle": "Each service's downtime added up"},
            {"label": _GROUPINGS[first][1], "value": f"{len(first_stats or []):,}",
             "accent": "primary", "subtitle": f"{len(services)} service(s)"},
            {"label": _UNMAPPED_LABEL[first], "value": f"{unmapped:,}",
             "accent": "warning" if unmapped else "success",
             "subtitle": f"of {len(services)} service(s)"},
        ],
        charts=[{"title": f"Merged downtime by {_GROUPINGS[first][0].lower()} (minutes)",
                 "series": {"kind": "rank", "unit": "min", "color": "warning",
                            "points": chart_points}}] if chart_points else [],
        tables=tables,
        notes=notes,
    )


# ─── 7. Maintenance log ─────────────────────────────────────────────────────

_STATE_LABEL = {"expired": "completed", "ended": "ended early", "cancelled": "cancelled",
                "active": "active", "scheduled": "scheduled"}


def _scope_label(w: SimpleNamespace) -> str:
    if w.scope_type == "check":
        return f"Monitor: {w.check_name}" if w.check_name else "(deleted monitor)"
    if w.scope_type == "group":
        return f"Group: {w.group_name}" if w.group_name else "(deleted group)"
    if w.scope_type == "tag":
        return f"Tag: {w.scope_tag or '—'}"
    return "All monitors"


@register_section(
    "service_maintenance_log", title="Service Maintenance Log", category="Services",
    description=("Service-check maintenance windows that overlapped the period — scope, planned "
                 "and effective times, monitors covered, and whether they ended early or were "
                 "cancelled."),
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(50, 500),
             {"key": "include_cancelled", "label": "Include cancelled windows", "type": "bool",
              "default": True}],
)
async def service_maintenance_log(ctx: SectionCtx) -> dict:
    from app.services import service_maintenance
    from app.services.service_availability import merge_intervals

    title = "Service Maintenance Log"
    start, end, now = _window(ctx)
    windows = await _maintenance_windows(ctx)
    if not ctx.opt("include_cancelled"):
        windows = [w for w in windows if not w.cancelled_at]
    if not windows:
        return _section("service_maintenance_log", title,
                        description="Service maintenance windows in the reporting period.",
                        kpis=[{"label": "Windows", "value": "0", "accent": "neutral"}],
                        notes=["No service maintenance windows in this period."])
    checks = await _checks(ctx)
    period_end = _aware(ctx.to) or end
    applied = [w for w in windows if not w.cancelled_at]
    spans = []
    covered_ids: set[str] = set()
    excluded_sec = 0.0
    for w in applied:
        lo, hi = max(w.starts_at, start), min(service_maintenance.effective_end(w), end)
        if hi > lo:
            spans.append((lo, hi))
    for c in checks:
        iv = service_maintenance.intervals_from_windows(applied, c, start, end)
        if iv:
            covered_ids.add(c["id"])
            excluded_sec += sum((hi - lo).total_seconds() for lo, hi in iv)
    merged = sum((hi - lo).total_seconds() for lo, hi in merge_intervals(spans))

    states = [(w, service_maintenance.window_state(w, now)) for w in windows]
    top_n = int(ctx.opt("top_n"))
    shown = states[:top_n]
    rows = []
    for w, state in shown:
        eff = service_maintenance.effective_end(w)
        in_period = (max(0.0, (min(eff, period_end) - max(w.starts_at, start)).total_seconds())
                     if not w.cancelled_at else None)
        rows.append([
            _scope_label(w), f"{sum(1 for c in checks if service_maintenance.covers(w, c)):,}",
            ctx.fmt_short(w.starts_at), ctx.fmt_short(w.ends_at),
            "—" if w.cancelled_at else ctx.fmt_short(eff),
            _dur(in_period), _STATE_LABEL.get(state, state), _clip(w.reason, 80),
        ])
    ended_early = sum(1 for _w, st in states if st == "ended")
    cancelled = sum(1 for w in windows if w.cancelled_at)
    active = sum(1 for _w, st in states if st == "active")
    notes = ["Maintenance time is excluded from service availability (neither up nor down). Monitors "
             "covered by group and tag windows are counted with today's membership; cancelled windows "
             f"never applied. Times are {ctx.tz_label()}."]
    return _section(
        "service_maintenance_log", title,
        description=f"{len(windows)} service maintenance window(s) overlapped the reporting period.",
        kpis=[
            {"label": "Windows", "value": f"{len(windows):,}", "accent": "info",
             "subtitle": f"{len(applied)} applied" + (f" · {active} active now" if active else "")},
            {"label": "Time in maintenance", "value": _dur(merged), "accent": "info",
             "subtitle": "Wall-clock, overlaps merged"},
            {"label": "Monitors covered", "value": f"{len(covered_ids):,}", "accent": "primary",
             "subtitle": f"{excluded_sec / 3600:,.1f} monitor-hours excluded from SLA"},
            {"label": "Ended early", "value": f"{ended_early:,}", "accent": "neutral"},
            {"label": "Cancelled", "value": f"{cancelled:,}", "accent": "neutral"},
        ],
        tables=[{"title": _top_title("Maintenance windows", len(shown), len(windows)),
                 "headers": ["Scope", "Monitors", "Start", "Planned end", "Effective end",
                             "In period", "State", "Reason"],
                 "styles": ["text", "num", "text", "text", "text", "num", "status", "text"],
                 "rows": rows}],
        notes=notes,
    )


# ─── 8. Results by monitoring site ──────────────────────────────────────────

async def _sensors(ctx: SectionCtx) -> dict[str, tuple]:
    """sensor id -> (name, site, location, status) for every sensor."""
    if "svc_sensors" not in ctx._cache:
        rows = (await ctx.db.execute(text(
            "SELECT s.id::text, s.name, si.name, s.location, s.status FROM sensors s "
            "LEFT JOIN sites si ON si.id = s.site_id"))).fetchall()
        ctx._cache["svc_sensors"] = {str(r[0]): (r[1], r[2], r[3], r[4]) for r in rows}
    return ctx._cache["svc_sensors"]


@register_section(
    "service_vantage_comparison", title="Service Results by Monitoring Site", category="Services",
    description=("Current state and latency of service checks per monitoring site (controller or "
                 "sensor) as of report generation, with probe success and P95 latency per site over "
                 "the period."),
    scope="fleet", requires=REQUIRES, semantics="snapshot",
    options=[top_n_option(25, 200),
             {"key": "detail", "label": "Monitor detail", "type": "select", "default": "differences",
              "choices": [["differences", "Monitors whose sites disagree"],
                          ["all", "Every monitor on two or more sites"], ["none", "Site summary only"]]}],
)
async def service_vantage_comparison(ctx: SectionCtx) -> dict:
    title = "Service Results by Monitoring Site"
    start, end, now = _window(ctx)
    checks = {c["id"]: c for c in await _checks(ctx)}
    # Paused monitors produce no new results; leaving them in would read as stale sites.
    vantages = [v for v in await _vantages(ctx)
                if v["check_id"] in checks and checks[v["check_id"]].get("enabled") is not False]

    # Per-site probe statistics over the period (only raw probes keep the poller).
    period: dict[str, dict] = {}
    raw_from = max(start, now - timedelta(days=RAW_RETENTION_DAYS))
    if end > raw_from:
        rows = await ctx.ch_rows(
            "SELECT if(poller_id = %(controller)s, 'central', poller_id) AS source, count() AS probes, "
            "countIf(is_up = 1) AS ok, "
            "quantileTDigestIf(0.95)(response_ms, is_up = 1 AND response_ms > 0) AS p95_ms "
            "FROM zenplus.service_metrics WHERE timestamp >= %(f)s AND timestamp < %(t)s "
            "GROUP BY source",
            {"controller": os.getenv("POLLER_ID", "poller-01"), "f": _ch_time(raw_from),
             "t": _ch_time(end)})
        for source, n, ok, p95 in rows:
            if int(n or 0) > 0:
                period[str(source)] = {"n": int(n or 0), "ok": int(ok or 0),
                                       "p95": _finite(p95) if ok else None}

    notes = ["Current state is each site's latest result for the enabled monitors assigned to it; a "
             "result older than two check intervals is stale. Probe success per site is diagnostic: "
             "service availability uses confirmed incidents only."]
    note = ctx.window_note(RAW_RETENTION_DAYS, "Per-site probe data")
    if note:
        notes.append(note)
    if not vantages and not period:
        return _section("service_vantage_comparison", title,
                        description="Service-check results per monitoring site.",
                        notes=["No monitoring-site results are recorded for service checks."] + notes[1:])
    sensors = await _sensors(ctx)

    def fresh(v: dict) -> bool:
        ts = _aware(v["last_result_at"])
        interval = checks[v["check_id"]]["check_interval"]
        return bool(ts and (now - ts).total_seconds() <= max(2 * interval, 60))

    def site_entry(pid: str, label: str, status: Optional[str]) -> dict:
        return {"label": label, "status": status, "monitors": 0, "up": 0, "down": 0, "warning": 0,
                "unknown": 0, "stale": 0, "lat": []}

    sites: dict[str, dict] = {}
    per_check: dict[str, list[dict]] = {}
    for v in vantages:
        v["fresh"] = fresh(v)
        s = sites.setdefault(v["poller_id"], site_entry(v["poller_id"], v["label"], v["sensor_status"]))
        s["monitors"] += 1
        if not v["fresh"]:
            s["stale"] += 1
        else:
            state = v["state"] if v["state"] in ("up", "down", "warning") else "unknown"
            s[state] += 1
            if state == "up" and _finite(v["latency_ms"]) is not None:
                s["lat"].append(float(v["latency_ms"]))
        per_check.setdefault(v["check_id"], []).append(v)
    for pid in period:
        if pid in sites:
            continue
        if pid == "central":
            sites[pid] = site_entry(pid, "Controller", None)
        elif pid in sensors:
            name, site, location, status = sensors[pid]
            sites[pid] = site_entry(pid, _vantage_label(pid, name, site, location), status)
        else:
            sites[pid] = site_entry(pid, f"Unknown poller ({pid[:12]})", None)

    ordered_sites = sorted(sites.items(), key=lambda kv: (kv[0] != "central", kv[1]["label"].lower()))
    site_rows = []
    chart = []
    for pid, s in ordered_sites:
        p = period.get(pid)
        site_rows.append([
            s["label"], "online" if pid == "central" else (s["status"] or "unknown"),
            f"{s['monitors']:,}", f"{s['up']:,}", f"{s['down']:,}", f"{s['warning']:,}",
            f"{s['unknown']:,}", f"{s['stale']:,}",
            _ms(statistics.median(s["lat"]) if s["lat"] else None),
            _ms(p["p95"]) if p else "—",
            _share(p["ok"] / p["n"] * 100) if p else "—",
        ])
        if p and p["p95"] is not None:
            chart.append({"label": s["label"], "value": round(p["p95"], 2)})
    multi = {cid: vs for cid, vs in per_check.items() if len(vs) >= 2}
    disagree = {cid: vs for cid, vs in multi.items()
                if len({v["state"] for v in vs if v["fresh"]}) > 1}
    stale = sum(1 for v in vantages if not v["fresh"])

    tables = [{"title": "Monitoring sites",
               "headers": ["Monitoring site", "Site status", "Monitors", "Up", "Down", "Warning",
                           "Unknown", "Stale", "Median latency now", "P95 latency (period)",
                           "Probe success (period)"],
               "styles": ["text", "status", "num", "num", "num", "num", "num", "num", "num", "num", "pct"],
               "rows": site_rows}]
    detail = ctx.opt("detail")
    if detail != "none":
        chosen = disagree if detail == "differences" else multi
        ordered = sorted(chosen.items(), key=lambda kv: (kv[0] not in disagree,
                                                         checks[kv[0]]["name"].lower()))
        top_n = int(ctx.opt("top_n"))
        rows = []
        for cid, vs in ordered[:top_n]:
            for v in sorted(vs, key=lambda v: (v["poller_id"] != "central", v["label"].lower())):
                rows.append([checks[cid]["name"], v["label"],
                             (v["state"] or "unknown") if v["fresh"] else "stale",
                             _ms(_finite(v["latency_ms"])), ctx.fmt_short(v["last_result_at"]),
                             _clip(v["error"], 60)])
        if rows:
            label = ("Monitors whose sites disagree" if detail == "differences"
                     else "Monitors checked from two or more sites")
            if len(ordered) > top_n:
                label += f" (top {top_n} of {len(ordered)} monitors)"
            tables.append({"title": label,
                           "headers": ["Monitor", "Monitoring site", "State", "Latency",
                                       "Last result", "Last error"],
                           "styles": ["text", "text", "status", "num", "text", "text"],
                           "rows": rows})
        elif multi and detail == "differences":
            notes.insert(0, "Every monitor checked from two or more sites currently agrees across sites.")
        else:
            notes.insert(0, "No monitor is checked from two or more sites.")
    return _section(
        "service_vantage_comparison", title,
        description=(f"Latest results of {len(per_check)} monitor(s) from {len(sites)} monitoring "
                     "site(s), with per-site probe statistics over the period."),
        kpis=[
            {"label": "Monitoring sites", "value": f"{len(sites):,}", "accent": "primary"},
            {"label": "Monitors on 2+ sites", "value": f"{len(multi):,}", "accent": "info",
             "subtitle": f"of {len(per_check)} monitor(s) with site results"},
            {"label": "Sites disagree", "value": f"{len(disagree):,}",
             "accent": "warning" if disagree else ("success" if multi else "neutral"),
             "subtitle": "Monitors, right now"},
            {"label": "Stale site results", "value": f"{stale:,}",
             "accent": "warning" if stale else ("success" if vantages else "neutral"),
             "subtitle": "Older than two check intervals"},
        ],
        charts=[{"title": "P95 latency of successful probes by site (period)",
                 "series": {"kind": "rank", "unit": "ms", "color": "info", "points": chart}}] if chart else [],
        tables=tables,
        notes=notes,
    )


# ─── Templates ──────────────────────────────────────────────────────────────

template(
    "service_sla_compliance_monthly", "Service SLA Compliance – Previous Month",
    "Monthly SLA review of business services on the confirmed basis: targets met, at risk and "
    "breached, error budget used, downtime by group and owner, the longest incidents, uptime by "
    "week and certificates about to expire.",
    "executive",
    [S("service_sla_compliance"),
     S("service_by_group", None, group_by="both"),
     S("service_outages", "Longest Service Incidents", sort="longest", top_n=15),
     S("service_daily_uptime", "Weekly Service Uptime", columns="weeks"),
     S("tls_certificate_expiry", "TLS Certificates Expiring Within 30 Days", days=30)],
    audience=("Executive", "IT manager", "Service owner"),
    tags=("sla", "services", "error budget", "monthly review"),
    default_period="previous_month", frequency="monthly", icon="FileCheck2", featured=True,
)

template(
    "service_incidents", "Service Incidents",
    "Every confirmed service incident in the period with start, end and duration, incidents per "
    "day, the services that breached or are at risk, and daily uptime.",
    "services",
    [S("service_outages", None, sort="recent", top_n=50),
     S("service_sla_compliance", "Services Breached or at Risk", show="attention", top_n=25),
     S("service_daily_uptime", None, columns="days")],
    audience=("NOC operator", "Service owner", "IT manager"),
    tags=("incidents", "outages", "mttr", "services"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)

template(
    "service_response_times_web", "Web and API Response Times",
    "Slowest HTTP/HTTPS checks by 95th-percentile response time of successful probes, with "
    "p50/p99, and the slowest checks of every type by 99th percentile.",
    "services",
    [S("service_response_times", "Top 25 Web and API Checks by 95th-Percentile Response Time",
       check_type="http", sort="p95", top_n=25),
     S("service_response_times", "Top 10 Service Checks by 99th-Percentile Response Time",
       check_type="all", sort="p99", top_n=10)],
    audience=("Application owner", "Network engineer", "Service owner"),
    tags=("response time", "latency", "http", "api", "percentiles"),
    default_period="last_7d", frequency="weekly", icon="Gauge", featured=True,
)

template(
    "service_daily_uptime_report", "Daily Service Uptime",
    "Confirmed uptime of each service for every day of the period (organisation timezone), the "
    "fleet's daily uptime and the incidents behind any downtime.",
    "services",
    [S("service_daily_uptime", None, columns="days", sort="worst"),
     S("service_outages", None, sort="recent", top_n=15)],
    audience=("NOC operator", "Service owner"),
    tags=("uptime", "daily", "calendar", "services"),
    default_period="last_7d", frequency="weekly", icon="BarChart3",
)

template(
    "tls_certificate_expiry_report", "TLS Certificate Expiry",
    "Expired and expiring certificates seen by TLS and HTTPS service checks, graded by each "
    "check's own thresholds, with the monitoring site that saw them and the checks lacking TLS "
    "evidence.",
    "security",
    [S("tls_certificate_expiry", None, days=90, top_n=100)],
    audience=("Security & audit", "Service owner", "IT manager"),
    tags=("tls", "ssl", "certificates", "expiry"),
    default_period="last_7d", frequency="weekly", icon="ShieldCheck",
)

template(
    "service_group_owner_availability", "Availability by Service Group and Owner",
    "Confirmed availability rolled up by service group, owner team and criticality: merged "
    "downtime, impact minutes and the weakest service in each, with per-service compliance.",
    "services",
    [S("service_by_group", None, group_by="both"),
     S("service_by_group", "Availability by Criticality", group_by="criticality"),
     S("service_sla_compliance", None, top_n=50)],
    audience=("IT manager", "Service owner", "Executive"),
    tags=("groups", "owners", "accountability", "sla"),
    default_period="previous_month", frequency="monthly", icon="Layers",
)

template(
    "service_maintenance_log_report", "Service Maintenance Log",
    "Planned maintenance on service checks in the period — scope, planned and effective times, "
    "monitors covered, early ends and cancellations — and the time excluded from SLA.",
    "services",
    [S("service_maintenance_log", None, include_cancelled=True, top_n=200)],
    audience=("IT manager", "NOC operator", "Security & audit"),
    tags=("maintenance", "change", "sla exclusions"),
    default_period="previous_month", frequency="monthly", icon="Wrench",
)

template(
    "service_monitoring_sites", "Service Results by Monitoring Site",
    "How each monitoring site (controller and sensors) sees the services right now — states, "
    "stale results and monitors whose sites disagree — with per-site latency over the period.",
    "services",
    [S("service_vantage_comparison", None, detail="differences"),
     S("service_response_times", "Top 10 Services by 95th-Percentile Response Time", top_n=10)],
    audience=("Network engineer", "NOC operator"),
    tags=("sensors", "vantage", "multi-site", "latency"),
    default_period="last_24h", frequency="daily", icon="Globe",
)
