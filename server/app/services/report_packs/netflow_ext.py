"""NetFlow section pack: interface capacity and 95th-percentile billing, QoS
(DSCP), conversations, countries and autonomous systems, IP groups / sites,
interface volume, security findings, exporter health and TCP-flag scan
indicators.

Every section here is ``scope="netflow"`` (category "Traffic", requires
``netflow.view``) and starts with ``netflow_report.resolve_scope(ctx)``: the
requester's flow visibility (exporter ownership + tag scope, a schedule's
owner, orphaned owners fail closed) and the report's own NetFlow scope are
applied before any query, exactly as for the ``traffic_*`` sections. The
queries reuse the NetFlow foundation -- the source planner (hour rollups for
complete hours, raw records for the edges, retention cuts disclosed), the
filter compiler, the breakdown routes, the capacity statistics
(``netflow_capacity``) and the exporter health status -- so a report answers
what the NetFlow pages answer.

Rules carried over from the NetFlow pages: shares are of the whole scope with
an explicit remainder (NF-25); what an exporter did not send (ToS, TCP flags,
AS numbers) is disclosed apart, never counted as zero; unknown speed means
unknown utilisation; a ClickHouse failure becomes a note on the section, not
a failed report. Cost: every ranking is one aggregate query on a rollup
where the filter allows it; the capacity result is shared between the
capacity and billing sections of one report, and runout forecasts (one
history query per interface) are limited to the few highest-risk
interfaces and run one at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable
from zoneinfo import ZoneInfo

from app.services.report_sections import SectionCtx, _section, register_section, top_n_option
from app.services.report_templates import S, template

REQUIRES = ("netflow.view",)
# Capacity state -> status cell (the report renderers colour these words;
# "over" is at least critical and is spelled out in the utilisation cell).
_CAP_STATE_CELL = {"over": "critical", "critical": "critical", "warning": "warning", "healthy": "healthy",
                   "insufficient": "insufficient", "unknown": "unknown"}
# Detection severity -> severity cell vocabulary.
_SEV_CELL = {"critical": "critical", "high": "major", "warning": "warning", "medium": "warning",
             "low": "minor", "info": "info"}
_SEV_LABEL = {"critical": "Critical", "high": "High", "warning": "Warning", "medium": "Medium",
              "low": "Low", "info": "Info"}
# Exporter delivery state -> status cell (delivery, never device reachability).
_EXPORT_STATE_CELL = {"receiving": "online", "late": "stale", "silent": "offline", "never": "unknown"}
_EXPORT_STATE_RANK = {"silent": 0, "late": 1, "never": 2, "receiving": 3}
_ISSUE_LABEL = {
    "rejected_allowlist": "Rejected (allow-list)", "waiting_template": "Waiting for template",
    "template_gaps": "Template gaps", "parse_errors": "Parse errors", "clock_clamped": "Clock clamped",
    "sampling_ambiguous": "Ambiguous sampling", "sequence_loss": "Sequence loss", "clock_skew": "Clock skew",
    "no_tcp_flags": "No TCP flags", "unmapped": "Not mapped to a device",
}


def _nr():
    from app.services import netflow_report
    return netflow_report


# ── Registration ─────────────────────────────────────────────────────────────

Build = Callable[[SectionCtx, Any], Awaitable[dict]]


def netflow_section(sid: str, *, title: str, description: str, options: list[dict] | None = None,
                    semantics: str = "window", half: bool = False) -> Callable[[Build], Callable]:
    """Register a NetFlow pack section. The wrapped builder receives the
    resolved NetFlow scope; a scope the requester may not use (no
    netflow.view, orphaned owner, invalid or foreign scope) returns that
    message instead of numbers, and a flow-database failure returns a note."""
    def deco(build: Build) -> Callable:
        @functools.wraps(build)
        async def run(ctx: SectionCtx) -> dict:
            from app.api.v1.netflow.common import NetflowError

            nr = _nr()
            try:
                nf = await nr.resolve_scope(ctx)
            except nr.ScopeError as exc:
                return _section(sid, title, notes=[str(exc)])
            try:
                return await build(ctx, nf)
            except NetflowError as exc:
                detail = str(exc.detail).rstrip(".")
                return _section(sid, title, description=nr._scope_description(nf, description),
                                notes=list(nf.notes) + [f"No figures: {detail}. The other sections are "
                                                        "unaffected."])

        register_section(sid, title=title, category="Traffic", description=description, scope="netflow",
                         requires=REQUIRES, options=options, semantics=semantics, half=half)(run)
        return run
    return deco


# ── Small helpers ────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _captured_meta():
    """A fresh NetFlow QueryMeta for the calls inside, so the breakdown
    routes' warnings (retention cuts, fields not exported) reach the notes."""
    from app.api.v1.netflow import common as C

    meta = C.QueryMeta()
    token = C._META.set(meta)
    try:
        yield meta
    finally:
        C._META.reset(token)


def _meta_notes(meta, skip: Iterable[str] = ()) -> list[str]:
    skip = set(skip)
    out = []
    for w in meta.warnings:
        if w.get("code") in skip:
            continue
        msg = str(w.get("message") or "").strip()
        if msg:
            out.append(msg if msg.endswith(".") else msg + ".")
    return out


def _dropped_note(meta, nf, what: str) -> list[str]:
    keys = sorted(set(getattr(meta, "dropped", set())) & set(nf.f.active_keys()))
    if not keys:
        return []
    return [f"The scope's {', '.join(keys)} filter does not apply to {what}."]


def _bctx(nf):
    from app.api.v1.netflow import breakdowns as bd
    return bd.Ctx(nf.win, nf.f, nf.vis)


def _period(ctx: SectionCtx, start: datetime, end: datetime) -> str:
    return f"{ctx.fmt_dt(start)} → {ctx.fmt_dt(end)} {ctx.tz_label()}"


def _tz_name(ctx: SectionCtx) -> str:
    try:
        ZoneInfo(ctx.tz_name)
        return ctx.tz_name
    except Exception:
        return "UTC"


def _scale(values: Iterable[float | None], unit: str) -> tuple[float, str]:
    """(divisor, unit label) so chart values read like the tables (decimal SI)."""
    top = max((abs(v) for v in values if v is not None), default=0.0)
    for factor, prefix in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if top >= factor:
            return factor, f"{prefix}{unit}"
    return 1.0, unit


def _rank_chart(title: str, items: list[tuple[str, float]], unit: str, color: str = "primary",
                scale_unit: str | None = None) -> dict:
    """A rank chart; ``scale_unit`` ("B" / "bps") rescales the values."""
    if scale_unit:
        factor, label = _scale([v for _, v in items], scale_unit)
    else:
        factor, label = 1.0, unit
    return {"title": title, "series": {"kind": "rank", "unit": label, "color": color,
                                       "points": [{"label": lbl, "value": round(v / factor, 2)}
                                                  for lbl, v in items]}}


def _cov_text(v: float | None) -> str:
    """Coverage with two decimals rounded down, so it never reads as a
    threshold it does not meet (98.999% is "98.99%")."""
    if v is None:
        return "—"
    return f"{math.floor(float(v) * 100) / 100:.2f}%"


def _top_title(noun: str, shown: int, total: int, by: str) -> str:
    if total > shown:
        return f"Top {shown:,} of {total:,} {noun} by {by}"
    return f"{noun[:1].upper()}{noun[1:]} by {by}"


_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else _ORDINAL_SUFFIX.get(n % 10, "th")
    return f"{n}{suffix}"


def _iface_label(row: dict) -> str:
    return _nr()._iface_label(row)


def _speed_text(sp_in: float | None, sp_out: float | None) -> str:
    nr = _nr()
    if not sp_in and not sp_out:
        return "—"
    if sp_in == sp_out:
        return nr.fmt_bps(sp_in)
    return f"{nr.fmt_bps(sp_in) if sp_in else '—'} / {nr.fmt_bps(sp_out) if sp_out else '—'}"


# ── Capacity (shared by netflow_capacity and netflow_billing_95th) ───────────

def _capacity_filter(nf) -> tuple[Any, list[str]]:
    from app.services import netflow_capacity as cap

    unsupported = cap.unsupported_keys(nf.f)
    if not unsupported:
        return nf.f, []
    return nf.f.without(*unsupported), [
        "Capacity measures whole interfaces; the scope's traffic filters (" + ", ".join(sorted(unsupported))
        + ") do not apply to it."]


async def _capacity_result(ctx: SectionCtx, nf, f, settings, business_hours: bool, *, previous: bool):
    """compute_capacity for the report window, once per report for the same
    filter and settings (the billing section reuses the capacity section's)."""
    from app.services import netflow_capacity as cap

    key = ("_nfx_capacity", tuple(sorted(f.canonical().items())), settings, bool(business_hours))
    hit = ctx._cache.get(key)
    if hit is not None and (hit[0] or not previous):
        return hit[1]
    result = await cap.compute_capacity(nf.win, f, nf.vis, ctx.db, settings=settings,
                                        business_hours=business_hours, previous=previous,
                                        record_meta=False, exclude_provisional=True)
    ctx._cache[key] = (previous, result)
    return result


def _capacity_notes(ctx: SectionCtx, result) -> list[str]:
    sw = result.sw
    notes = []
    if sw.empty:
        notes.append("The period holds no complete 5-minute slot, so no rate can be stated.")
    if sw.retention_cut:
        notes.append(f"Interface history does not reach back to the start of the period; it is measured "
                     f"from {ctx.fmt_dt(sw.first)}.")
    if result.raw_from is not None:
        notes.append(f"The interface rollup was behind; samples from {ctx.fmt_dt(result.raw_from)} were "
                     "computed from raw flow records.")
    if result.provisional:
        minutes = max(p["provisional_slots"] for p in result.provisional) * 5
        notes.append(f"The newest {minutes} minutes are still settling (long-lived flows not reported yet) and "
                     "are left out, not counted as idle.")
    return notes


def _dir_rate(d: dict) -> str:
    nr = _nr()
    if d.get("p_bps") is not None:
        return nr.fmt_bps(d["p_bps"])
    if d.get("max_bps") is not None:
        return f"{nr.fmt_bps(d['max_bps'])} peak"
    return "—"


def _days_text(ctx: SectionCtx, days: float | None, fc: dict, horizon: int) -> str:
    if days is None:
        return f"> {horizon // 365} years" if fc.get("status") in ("ok", "beyond_horizon") else "—"
    if days <= 0:
        return "Reached"
    when = datetime.now(timezone.utc) + timedelta(days=float(days))
    return f"{days:,.0f} days ({ctx.fmt_date(when)})"


def _forecast_basis(fc: dict) -> str:
    status = fc.get("status")
    if status == "ok":
        return f"{fc.get('confidence') or 'low'} confidence (R² {fc.get('r2') or 0:.2f}, {fc.get('days_used')} days)"
    if status == "beyond_horizon":
        return f"No level within 2 years (R² {fc.get('r2') or 0:.2f})"
    if status == "not_growing":
        return "Traffic flat or falling"
    if status == "speed_unknown":
        return "Speed unknown"
    if status == "insufficient_history":
        return f"Insufficient history: {fc.get('reason') or 'too few days'}"
    return fc.get("reason") or "—"


@netflow_section(
    "netflow_capacity", title="NetFlow Interface Capacity",
    description="Per-interface percentile utilisation in and out, headroom, state, change against the previous "
                "period and runout forecasts for flow-measured interfaces.",
    options=[
        top_n_option(15, 200),
        {"key": "include", "label": "Interfaces", "type": "select", "default": "all",
         "choices": [["all", "All, highest risk first"], ["at_risk", "Only warning, critical or over"]]},
        {"key": "business_hours", "label": "Business hours only", "type": "bool", "default": False,
         "help": "Only samples inside the business hours configured for capacity."},
        {"key": "forecast_top", "label": "Runout forecasts (interfaces)", "type": "int", "default": 5,
         "min": 0, "max": 10, "help": "Forecast the runout of this many highest-risk interfaces (0 = none)."},
        {"key": "forecast_days", "label": "Forecast history (days)", "type": "int", "default": 90,
         "min": 14, "max": 400},
    ],
)
async def netflow_capacity(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow.common import NetflowError, Window
    from app.services import netflow_capacity as cap

    nr = _nr()
    sid = "netflow_capacity"
    bh = bool(ctx.opt("business_hours"))
    title = "NetFlow Interface Capacity" + (" (Business Hours)" if bh else "")
    f, notes = _capacity_filter(nf)
    settings, _info = await cap.load_settings(ctx.db)
    result = await _capacity_result(ctx, nf, f, settings, bh, previous=True)
    stat = f"p{settings.percentile}"
    description = nr._scope_description(
        nf, f"Interface utilisation as the {_ordinal(settings.percentile)} percentile of complete 5-minute "
            f"samples per direction, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + notes + _capacity_notes(ctx, result)
    rows = [r for r in result.rows if r.get("role") != "ignore"]
    ignored = len(result.rows) - len(rows)
    if ignored:
        notes.append(f"{ignored} interface{'s' if ignored != 1 else ''} marked 'ignore' in NetFlow interface "
                     f"settings {'are' if ignored != 1 else 'is'} left out.")
    if not rows:
        return _section(sid, title, description=description,
                        notes=notes + ["No interface carried flow traffic in scope in this period."])

    summary = cap.summarize(rows)
    by_state = summary["by_state"]
    rows.sort(key=cap.risk_key)
    listed = rows if ctx.opt("include") != "at_risk" else [
        r for r in rows if r["headline"]["state"] in ("over", "critical", "warning")]
    top_n = int(ctx.opt("top_n"))
    shown = listed[:top_n]
    assessable = summary["speed_known"]
    risky = by_state.get("over", 0) + by_state.get("critical", 0)

    kpis = [
        {"label": "Interfaces", "value": f"{len(rows):,}", "accent": "primary",
         "subtitle": f"{assessable:,} with a known speed"},
        {"label": "Over or critical", "value": f"{risky:,}" if assessable else "—",
         "accent": ("danger" if risky else "success") if assessable else "neutral",
         "subtitle": (f"{stat} at or above {settings.crit_pct:g}%" if assessable
                      else "no interface has a known speed")},
        {"label": "Warning", "value": f"{by_state.get('warning', 0):,}" if assessable else "—",
         "accent": ("warning" if by_state.get("warning") else "success") if assessable else "neutral",
         "subtitle": f"{stat} {settings.warn_pct:g}–{settings.crit_pct:g}%"},
        {"label": "Healthy", "value": f"{by_state.get('healthy', 0):,}" if assessable else "—",
         "accent": "success" if by_state.get("healthy") else "neutral"},
        {"label": "Not assessable", "value": f"{by_state.get('insufficient', 0) + by_state.get('unknown', 0):,}",
         "accent": "neutral", "subtitle": "speed unknown or too little data"},
    ]

    table_rows = []
    for r in shown:
        h = r["headline"]
        util = h.get("utilization_pct")
        util_text = "—"
        if util is not None:
            util_text = f"{nr.fmt_pct(util)} {h.get('direction') or ''}".strip()
            if h.get("state") == "over":
                util_text += " (over speed)"
        prev = r.get("previous") or {}
        table_rows.append([
            _iface_label(r), _speed_text(r["speed"].get("in_bps"), r["speed"].get("out_bps")),
            _dir_rate(r["in"]), _dir_rate(r["out"]), util_text, nr.fmt_bps(h.get("headroom_bps")),
            nr.fmt_change(prev.get("change_pct")), _CAP_STATE_CELL.get(h.get("state"), "unknown"),
        ])
    tables = [{"title": _top_title("interfaces", len(shown), len(listed), "utilisation risk"),
               "headers": ["Interface", "Speed", f"In {stat}", f"Out {stat}", "Utilisation", "Headroom",
                           "Change", "State"],
               "styles": ["text", "num", "num", "num", "pct-bar", "num", "num", "status"],
               "rows": table_rows}] if table_rows else []
    if not table_rows:
        notes.append("No interface is at warning or above in this period.")

    charts = []
    ranked_util = [(f"{_iface_label(r)} ({r['headline']['direction']})", float(r["headline"]["utilization_pct"]))
                   for r in shown if r["headline"].get("utilization_pct") is not None][:10]
    if ranked_util:
        charts.append({"title": f"Utilisation ({stat}, higher direction)",
                       "series": {"kind": "rank", "unit": "%", "color": "primary",
                                  "points": [{"label": lbl, "value": round(v, 1)} for lbl, v in ranked_util]}})

    # Runout forecasts for the highest-risk interfaces with a known speed.
    n_fc = int(ctx.opt("forecast_top"))
    if n_fc > 0:
        days = int(ctx.opt("forecast_days"))
        fwin = Window(start=nf.win.end - timedelta(days=days), end=nf.win.end, mode="fixed", tz=_tz_name(ctx))
        candidates = [r for r in listed if r[r["headline"]["direction"]].get("speed_bps")][:n_fc]
        fc_rows = []
        for r in candidates:
            try:
                tr = await cap.interface_trend(fwin, r["exporter"], r["ifindex"], nf.vis, ctx.db,
                                               settings=settings, business_hours=bh)
            except NetflowError:
                fc_rows.append([_iface_label(r), "—", "—", "—", "—", "—", "History could not be read"])
                continue
            head = tr["forecast"].get("headline_direction") or r["headline"]["direction"]
            fc = tr["forecast"].get(head) or {}
            slope = fc.get("slope_bps_per_day")
            fc_rows.append([
                _iface_label(r), head, f"{nr.fmt_bps(slope)}/day" if slope is not None else "—",
                _days_text(ctx, fc.get("days_to_warn"), fc, cap.FORECAST_HORIZON_DAYS),
                _days_text(ctx, fc.get("days_to_crit"), fc, cap.FORECAST_HORIZON_DAYS),
                _days_text(ctx, fc.get("days_to_full"), fc, cap.FORECAST_HORIZON_DAYS),
                _forecast_basis(fc),
            ])
        if fc_rows:
            which = (f"the {len(fc_rows)} highest-risk interfaces" if len(fc_rows) != 1
                     else "the highest-risk interface")
            tables.append({"title": f"Runout forecast for {which} with a known speed",
                           "headers": ["Interface", "Direction", "Growth", f"To {settings.warn_pct:g}%",
                                       f"To {settings.crit_pct:g}%", "To 100%", "Forecast basis"],
                           "styles": ["text", "text", "num", "num", "num", "num", "text"],
                           "rows": fc_rows})
            notes.append(f"Runout: a least-squares line through the daily {stat} of complete days over the last "
                         f"{days} days (at least {cap.FORECAST_MIN_DAYS} qualifying days), against the current "
                         "speed; R² is the confidence.")
        else:
            notes.append("No runout forecast: no listed interface has a known speed.")

    if summary["speed_unknown"]:
        n = summary["speed_unknown"]
        notes.append(f"{n} of {len(rows)} interfaces with flow traffic {'lack' if n != 1 else 'lacks'} a known speed "
                     "for at least one direction; utilisation there is unknown, not zero.")
    notes.append(f"{stat} is the nearest-rank percentile of complete 5-minute samples per direction (in = received, "
                 "out = transmitted); collection gaps are excluded, not counted as idle; exporters that export "
                 "both ingress and egress records of the same packets are counted once. State: warning from "
                 f"{settings.warn_pct:g}%, critical from {settings.crit_pct:g}%; 'insufficient' means less than "
                 f"{settings.min_coverage_pct:g}% coverage or too few samples.")
    if by_state.get("over"):
        notes.append(f"{by_state['over']} interface{'s' if by_state['over'] != 1 else ''} measured above the "
                     "interface speed: check the circuit speed or override, duplicate ingress/egress export and "
                     "the sampling rate.")
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=tables,
                    notes=list(dict.fromkeys(notes)))


@netflow_section(
    "netflow_billing_95th", title="95th Percentile Bandwidth Billing",
    description="Per-interface burstable billing rate (nearest-rank percentile of 5-minute samples) with "
                "billing-grade flags for complete periods and coverage.",
    options=[
        top_n_option(50, 500),
        {"key": "percentile", "label": "Percentile", "type": "select", "default": "configured",
         "choices": [["configured", "Configured (capacity settings)"], ["95", "95th"], ["90", "90th"],
                     ["85", "85th"], ["80", "80th"], ["75", "75th"]]},
        {"key": "direction_mode", "label": "Directions", "type": "select", "default": "configured",
         "choices": [["configured", "Configured"], ["separate", "Separate (bill the higher direction)"],
                     ["merged", "Merged (in + out per sample)"]]},
        {"key": "hours", "label": "Samples", "type": "select", "default": "configured",
         "choices": [["configured", "Configured"], ["all", "All hours"], ["business", "Business hours only"]]},
    ],
)
async def netflow_billing_95th(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow.common import now_utc
    from app.services import netflow_capacity as cap

    nr = _nr()
    sid = "netflow_billing_95th"
    f, notes = _capacity_filter(nf)
    settings, _info = await cap.load_settings(ctx.db)
    overrides: dict[str, Any] = {}
    if ctx.opt("percentile") != "configured":
        overrides["percentile"] = int(ctx.opt("percentile"))
    if ctx.opt("direction_mode") != "configured":
        overrides["direction_mode"] = ctx.opt("direction_mode")
    if overrides:
        settings = cap.parse_settings(overrides, base=settings)
    hours = ctx.opt("hours")
    bh = settings.business_hours.enabled if hours == "configured" else hours == "business"
    pct = settings.percentile
    title = f"{_ordinal(pct)} Percentile Bandwidth Billing"
    result = await _capacity_result(ctx, nf, f, settings, bh, previous=False)
    complete = nf.win.end <= now_utc() and nf.win.mode == "fixed"
    how = ("per direction (the higher is billed)" if settings.direction_mode == "separate"
           else "of in + out per sample")
    description = nr._scope_description(
        nf, f"Billing period {_period(ctx, nf.win.start, nf.win.end)}; {_ordinal(pct)} percentile of complete "
            f"5-minute samples, {how}.")
    notes = list(nf.notes) + notes + _capacity_notes(ctx, result)
    rows = sorted((r for r in result.rows if r.get("role") != "ignore"),
                  key=lambda r: (r["billable"]["bps"] is None, -(r["billable"]["bps"] or 0.0),
                                 r["exporter"], r["ifindex"]))
    if not rows:
        return _section(sid, title, description=description,
                        notes=notes + ["No interface carried flow traffic in scope in this period."])

    items = []
    for r in rows:
        reasons = cap.billing_grade(r, period_complete=complete, retention_cut=result.sw.retention_cut,
                                    settings=settings)
        billable = r["billable"]["bps"]
        direction = r["billable"]["direction"]
        util = r[direction]["utilization_p_pct"] if direction in ("in", "out") and billable is not None else None
        items.append((r, billable, direction, util, reasons))
    grade = sum(1 for *_x, reasons in items if not reasons)
    billables = [(i[0], i[1]) for i in items if i[1] is not None]
    top = billables[0] if billables else None
    kpis = [
        {"label": "Interfaces", "value": f"{len(items):,}", "accent": "primary"},
        {"label": "Billing-grade", "value": f"{grade:,}", "accent": "success" if grade == len(items) else "warning",
         "subtitle": f"of {len(items):,} interfaces"},
        {"label": "Indicative only", "value": f"{len(items) - grade:,}",
         "accent": "warning" if grade < len(items) else "success",
         "subtitle": "period not ended, coverage or samples short" if grade < len(items) else None},
        {"label": "Highest billable rate", "value": nr.fmt_bps(top[1]) if top else "—", "accent": "info",
         "subtitle": _iface_label(top[0]) if top else "no interface has enough samples"},
        {"label": "Sum of billable rates", "value": nr.fmt_bps(sum(b for _r, b in billables)) if billables else "—",
         "accent": "primary", "subtitle": "per-interface percentiles added"},
    ]
    top_n = int(ctx.opt("top_n"))
    table_rows = []
    for r, billable, direction, util, reasons in items[:top_n]:
        bill_text = "—"
        if billable is not None:
            bill_text = nr.fmt_bps(billable) + (" (in + out)" if direction == "merged" else f" ({direction})")
        table_rows.append([
            _iface_label(r), _speed_text(r["speed"].get("in_bps"), r["speed"].get("out_bps")),
            nr.fmt_bps(r["in"]["p_bps"]), nr.fmt_bps(r["out"]["p_bps"]), bill_text, nr.fmt_pct(util),
            _cov_text(r.get("coverage_pct")), "Yes" if not reasons else "No: " + "; ".join(reasons),
        ])
    charts = []
    if billables:
        charts.append(_rank_chart(f"Billable rate ({_ordinal(pct)} percentile)",
                                  [(_iface_label(r), float(b)) for r, b in billables[:10]], "bps",
                                  scale_unit="bps"))
    notes.append(f"Billing-grade means a complete past period with at least {settings.billing_min_coverage_pct:g}% of "
                 f"its 5-minute samples collected and at least {cap.min_samples(pct)} samples; other figures are "
                 "indicative.")
    notes.append(str(result.method.get("percentile_definition") or ""))
    notes.append("Separate: the percentile of each direction is computed and the higher one is billed."
                 if settings.direction_mode == "separate" else
                 "Merged: the percentile of in + out per sample is billed (not divided by a speed).")
    if not complete:
        notes.append("The billing period has not ended; every figure is indicative.")
    if bh:
        b = settings.business_hours
        days = ", ".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d - 1] for d in b.days if 1 <= d <= 7)
        notes.append(f"Only business-hours samples count ({b.start_hour:02d}:00–{b.end_hour:02d}:00 {b.tz}, "
                     f"{days}).")
    if overrides:
        notes.append("This report overrides the configured " + " and ".join(
            {"percentile": "percentile", "direction_mode": "direction mode"}[k] for k in sorted(overrides)) + ".")
    return _section(sid, title, description=description, kpis=[{k: v for k, v in x.items() if v is not None}
                                                                for x in kpis],
                    charts=charts,
                    tables=[{"title": _top_title("interfaces", len(table_rows), len(items), "billable rate"),
                             "headers": ["Interface", "Speed", f"In p{pct}", f"Out p{pct}", "Billable",
                                         "Utilisation", "Coverage", "Billing grade"],
                             "styles": ["text", "num", "num", "num", "num", "pct-bar", "num", "text"],
                             "rows": table_rows}],
                    notes=[n for n in dict.fromkeys(notes) if n])


# ── QoS / DSCP ───────────────────────────────────────────────────────────────

@netflow_section(
    "netflow_qos_dscp", title="Traffic by DSCP Class (QoS)",
    description="Share of flow volume per DSCP code point, with traffic whose exporter did not send the ToS "
                "field disclosed apart.",
    options=[top_n_option(10, 64)],
)
async def netflow_qos_dscp(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow import breakdowns as bd

    nr = _nr()
    sid, title = "netflow_qos_dscp", "Traffic by DSCP Class (QoS)"
    with _captured_meta() as meta:
        body = await bd.netflow_dscp(with_totals=1, ctx=_bctx(nf))
    description = nr._scope_description(nf, f"Flow volume per DSCP class, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + _meta_notes(meta, skip={"dscp_not_exported"}) + _dropped_note(meta, nf, "this breakdown")
    total = int((body.get("total") or {}).get("bytes") or 0)
    if not total:
        return _section(sid, title, description=description, notes=notes + ["No flow traffic in scope in this period."])
    items = list(body.get("items") or [])
    unknown = body.get("not_exported") or {}
    top_n = int(ctx.opt("top_n"))
    shown, rest = items[:top_n], items[top_n:]
    rows = [[it["label"], str(it["dscp"]), nr.fmt_bytes(it["bytes"]), nr.fmt_pct(nr.share(it["bytes"], total)),
             nr.fmt_count(it["packets"]), nr.fmt_count(it["flows"])] for it in shown]
    if rest:
        rb = sum(i["bytes"] for i in rest)
        rows.append([f"Other DSCP classes ({len(rest)})", "—", nr.fmt_bytes(rb), nr.fmt_pct(nr.share(rb, total)),
                     nr.fmt_count(sum(i["packets"] for i in rest)), nr.fmt_count(sum(i["flows"] for i in rest))])
    if unknown.get("bytes") or unknown.get("flows"):
        rows.append(["DSCP not exported", "—", nr.fmt_bytes(unknown.get("bytes")),
                     nr.fmt_pct(nr.share(unknown.get("bytes"), total)), nr.fmt_count(unknown.get("packets")),
                     nr.fmt_count(unknown.get("flows"))])
    known = total - int(unknown.get("bytes") or 0)
    be = next((i["bytes"] for i in items if i["dscp"] == 0), 0)
    ef = next((i["bytes"] for i in items if i["dscp"] == 46), 0)
    tos_cov = body.get("tos_coverage_pct")
    kpis = [
        {"label": "Total volume", "value": nr.fmt_bytes(total), "accent": "primary",
         "subtitle": f"{len(items)} DSCP class{'es' if len(items) != 1 else ''} seen"},
        {"label": "Marked (not best effort)", "value": nr.fmt_pct(nr.share(known - be, total)) if known else "—",
         "accent": "info", "subtitle": "of all traffic in scope"},
        {"label": "Voice (EF)", "value": nr.fmt_pct(nr.share(ef, total)) if known else "—", "accent": "info",
         "subtitle": "DSCP 46"},
        {"label": "DSCP known", "value": nr.fmt_pct(tos_cov),
         "accent": "success" if (tos_cov or 0) >= 99.95 else "warning",
         "subtitle": "share of bytes whose exporter sent the ToS field"},
    ]
    donut = [(it["label"], it["bytes"]) for it in items[:6]]
    if len(items) > 6:
        donut.append(("Other classes", sum(i["bytes"] for i in items[6:])))
    if unknown.get("bytes"):
        donut.append(("DSCP not exported", unknown["bytes"]))
    factor, unit = _scale([b for _l, b in donut], "B")
    notes.append("DSCP is the top six bits of the ToS / traffic-class byte. Records whose exporter did not send "
                 "the ToS field are shown as 'DSCP not exported', not as best effort.")
    if unknown.get("bytes"):
        notes.append(f"{nr.fmt_pct(nr.share(unknown['bytes'], total))} of the traffic carries no exported ToS "
                     "field; its class is unknown.")
    return _section(
        sid, title, description=description, kpis=kpis,
        charts=[{"title": "Share of total volume",
                 "series": {"kind": "donut", "unit": unit,
                            "points": [{"label": lbl, "value": round(b / factor, 2)} for lbl, b in donut]}}],
        tables=[{"title": _top_title("DSCP classes", len(shown), len(items), "volume"),
                 "headers": ["DSCP class", "Code point", "Volume", "Share of total", "Packets", "Flows"],
                 "styles": ["text", "num", "num", "pct-bar", "num", "num"], "rows": rows}],
        notes=list(dict.fromkeys(notes)))


# ── Conversations ────────────────────────────────────────────────────────────

_SORT_LABEL = {"bytes": "volume", "packets": "packets", "flows": "flows"}


def _service_text(proto: int, svc_key: int) -> str:
    from app.api.v1.netflow import breakdowns as bd
    from app.services import netflow_classify as nc

    info = bd._service_info(proto, svc_key)
    pname = nc.protocol_name(proto)
    if info.get("service_port"):
        name, base = info.get("service"), f"{pname}/{info['service_port']}"
        return base + (f" ({name})" if name and name != base else "")
    if info.get("service"):
        return f"{pname} {info['service']}"
    return pname


@netflow_section(
    "netflow_conversations", title="Top Conversations",
    description="Client-to-server conversations with their service and application, ranked by volume, packets "
                "or flows, as shares of all traffic in scope.",
    options=[
        top_n_option(25, 200),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "bytes",
         "choices": [["bytes", "Volume"], ["packets", "Packets"], ["flows", "Flows"]]},
    ],
)
async def netflow_conversations(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow import breakdowns as bd
    from app.api.v1.netflow.common import CONVERSATIONS_1H, run_query
    from app.api.v1.netflow.sql import DST, SRC, plan_source
    from app.services import netflow_classify as nc

    nr = _nr()
    sid = "netflow_conversations"
    top_n = int(ctx.opt("top_n"))
    sort = ctx.opt("sort") if ctx.opt("sort") in _SORT_LABEL else "bytes"
    title = f"Top {top_n} Conversations"
    plan = await plan_source(nf.win, nf.f, rollup=CONVERSATIONS_1H, record=False)
    cols = {"x_s": SRC, "x_d": DST, "x_proto": "protocol", "x_dport": "dst_port", **bd._classifier_columns(),
            "x_b": "bytes", "x_p": "packets", "x_n": bd.FLOWS}
    sub, params = bd._union(plan, cols, _bctx(nf))
    params["nf_limit"] = top_n
    metric = {"bytes": "b", "packets": "p", "flows": "n"}[sort]
    res = await run_query(
        f"""
        SELECT {bd._CLIENT} AS c, {bd._SERVER} AS sv, x_proto AS proto, x_sp AS sp,
               sum(x_b) AS b, sum(x_p) AS p, sum(x_n) AS n,
               argMax(x_app, x_b) AS app, argMax(x_dport, x_b) AS top_dport,
               count() OVER () AS n_items,
               sum(b) OVER () AS all_b, sum(p) OVER () AS all_p, sum(n) OVER () AS all_n
        FROM {sub} AS src
        GROUP BY c, sv, proto, sp
        ORDER BY {metric} DESC, c ASC, sv ASC, proto ASC, sp ASC
        LIMIT %(nf_limit)s
        """, params, budget_s=nr.REPORT_BUDGET_S, label="report-conversations")
    description = nr._scope_description(
        nf, f"Conversations ranked by {_SORT_LABEL[sort]}, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + nr._plan_notes(plan, nf.branding)
    rows = list(res.result_rows)
    if not rows:
        return _section(sid, title, description=description,
                        notes=notes + ["No flow traffic in scope in this period."])
    first = rows[0]
    n_items, all_b, all_p, all_n = int(first[9] or 0), int(first[10] or 0), int(first[11] or 0), int(first[12] or 0)
    total_metric = {"bytes": all_b, "packets": all_p, "flows": all_n}[sort]
    items = []
    for r in rows:
        proto = int(r[2] or 0)
        svc = int(r[8] or 0) if proto in nc.ICMP_PROTOCOLS else int(r[3] or 0)
        items.append({"client": bd._ip(r[0]) or "—", "server": bd._ip(r[1]) or "—",
                      "service": _service_text(proto, svc), "app": str(r[7] or nc.APP_OTHER),
                      "b": int(r[4] or 0), "p": int(r[5] or 0), "n": int(r[6] or 0)})
    table = [[it["client"], it["server"], it["service"], it["app"], nr.fmt_bytes(it["b"]),
              nr.fmt_pct(nr.share(it[metric], total_metric)), nr.fmt_count(it["n"])] for it in items]
    rest = {k: tot - sum(it[k] for it in items) for k, tot in (("b", all_b), ("p", all_p), ("n", all_n))}
    if rest[metric] > 0:
        table.append(["All other conversations", "—", "—", "—", nr.fmt_bytes(max(0, rest["b"])),
                      nr.fmt_pct(nr.share(rest[metric], total_metric)), nr.fmt_count(max(0, rest["n"]))])
    top_share = nr.share(sum(it[metric] for it in items), total_metric)
    lead = items[0]
    kpis = [
        {"label": "Total volume", "value": nr.fmt_bytes(all_b), "accent": "primary",
         "subtitle": f"{nr.fmt_count(all_n)} flow records"},
        {"label": "Conversations", "value": f"{n_items:,}", "accent": "info"},
        {"label": f"Top {len(items)} share", "value": nr.fmt_pct(top_share), "accent": "info",
         "subtitle": f"of all {_SORT_LABEL[sort]} in scope"},
        {"label": "Largest conversation", "value": nr.fmt_bytes(lead["b"]) if sort == "bytes"
         else nr.fmt_count(lead[metric]), "accent": "primary",
         "subtitle": f"{lead['client']} → {lead['server']}"},
    ]
    chart_items = [(f"{it['client']} → {it['server']} {it['service']}", float(it[metric])) for it in items[:10]]
    charts = [_rank_chart(f"Top conversations by {_SORT_LABEL[sort]}", chart_items,
                          "count" if sort != "bytes" else "B", scale_unit="B" if sort == "bytes" else None)]
    notes.append("A conversation is a client, a server and the service between them: the request and response "
                 "halves of one exchange are one row (traffic without a recognisable service is ordered by "
                 "address). Shares are of all traffic in scope, with the remainder shown.")
    share_head = "Share of total" if sort == "bytes" else f"Share of {_SORT_LABEL[sort]}"
    return _section(sid, title, description=description, kpis=kpis, charts=charts,
                    tables=[{"title": _top_title("conversations", len(items), n_items, _SORT_LABEL[sort]),
                             "headers": ["Client", "Server", "Service", "Application", "Volume", share_head,
                                         "Flows"],
                             "styles": ["mono", "mono", "text", "text", "num", "pct-bar", "num"],
                             "rows": table}],
                    notes=list(dict.fromkeys(notes)))


# ── Countries and autonomous systems ─────────────────────────────────────────

def _side_rows(items: list[dict], remainder: dict, total: int, top_n: int, name_of: Callable[[dict], str],
               other_label: str, extra: Callable[[dict], list[str]] | None = None) -> list[list[str]]:
    """Per-side rows with whole-scope shares; everything not listed (the
    long tail of the query included) in one remainder row."""
    nr = _nr()
    shown, rest = items[:top_n], items[top_n:]
    rows = []
    for it in shown:
        rows.append([name_of(it), *(extra(it) if extra else []),
                     nr.fmt_bytes(it["src_bytes"]), nr.fmt_pct(nr.share(it["src_bytes"], total)),
                     nr.fmt_bytes(it["dst_bytes"]), nr.fmt_pct(nr.share(it["dst_bytes"], total))])
    rs_ = sum(i["src_bytes"] for i in rest) + int((remainder or {}).get("src_bytes") or 0)
    rd = sum(i["dst_bytes"] for i in rest) + int((remainder or {}).get("dst_bytes") or 0)
    if rs_ > 0 or rd > 0:
        rows.append([other_label, *(["—"] if extra else []), nr.fmt_bytes(rs_), nr.fmt_pct(nr.share(rs_, total)),
                     nr.fmt_bytes(rd), nr.fmt_pct(nr.share(rd, total))])
    return rows


def _side_note(body: dict) -> list[str]:
    pre = body.get("prefixes") or {}
    notes = ["Each flow counts once as a source and once as a destination, so each side's shares add up to "
             "100% of the traffic in scope, remainder included."]
    if (pre.get("total") or 0) > (pre.get("resolved") or 0):
        notes.append(f"The {int(pre['total']) - int(pre['resolved']):,} least busy networks (beyond the top "
                     f"{int(pre['resolved']):,}) are counted in the remainder.")
    return notes


@netflow_section(
    "netflow_countries", title="Traffic by Country",
    description="Source and destination traffic per country (GeoIP), with private and unplaced address space "
                "reported apart.",
    options=[top_n_option(15, 100)],
)
async def netflow_countries(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow import breakdowns as bd

    nr = _nr()
    sid, title = "netflow_countries", "Traffic by Country"
    with _captured_meta() as meta:
        body = await bd.netflow_countries(limit=2000, with_totals=1, ctx=_bctx(nf))
    description = nr._scope_description(nf, f"Traffic per country, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + _meta_notes(meta)
    if not body.get("available"):
        return _section(sid, title, description=description,
                        notes=notes + [str(body.get("reason") or "No GeoIP country database is installed") + "."])
    total = int((body.get("total") or {}).get("bytes") or 0)
    items = list(body.get("items") or [])
    if not total or not items:
        return _section(sid, title, description=description, notes=notes + ["No flow traffic in scope in this period."])

    def name_of(it: dict) -> str:
        return f"{it['country_name']} ({it['country']})" if it.get("country") else it.get("country_name") or "—"

    top_n = int(ctx.opt("top_n"))
    countries = [i for i in items if i.get("kind") == "country"]
    unplaced = sum(i["src_bytes"] + i["dst_bytes"] for i in items if i.get("kind") != "country")
    top_dst = max(countries, key=lambda i: i["dst_bytes"], default=None)
    top_src = max(countries, key=lambda i: i["src_bytes"], default=None)
    kpis = [
        {"label": "Countries", "value": f"{len(countries):,}", "accent": "primary"},
        {"label": "Top destination", "value": top_dst["country_name"] if top_dst else "—", "accent": "info",
         "subtitle": f"{nr.fmt_pct(nr.share(top_dst['dst_bytes'], total))} of traffic received" if top_dst else None},
        {"label": "Top source", "value": top_src["country_name"] if top_src else "—", "accent": "info",
         "subtitle": f"{nr.fmt_pct(nr.share(top_src['src_bytes'], total))} of traffic sent" if top_src else None},
        {"label": "Private or unplaced", "value": nr.fmt_pct(nr.share(unplaced, 2 * total)), "accent": "neutral",
         "subtitle": "of both sides' traffic"},
    ]
    geo = body.get("geoip") or {}
    notes = notes + _side_note(body) + [
        "Addresses are grouped by /24 (IPv4) or /48 (IPv6) network and placed with the GeoIP country database"
        + (f" ({geo['country_db_version']})" if geo.get("country_db_version") else "") + "."]
    return _section(
        sid, title, description=description,
        kpis=[{k: v for k, v in x.items() if v is not None} for x in kpis],
        charts=[_rank_chart("Traffic by country (sent + received)",
                            [(name_of(i), float(i["src_bytes"] + i["dst_bytes"])) for i in items[:10]], "B",
                            scale_unit="B")],
        tables=[{"title": _top_title("countries and address classes", min(top_n, len(items)), len(items),
                                     "volume"),
                 "headers": ["Country", "Sent from", "Source share", "Received in", "Destination share"],
                 "styles": ["text", "num", "pct-bar", "num", "pct-bar"],
                 "rows": _side_rows(items, body.get("remainder") or {}, total, top_n, name_of, "All other countries")}],
        notes=list(dict.fromkeys(notes)))


_ASN_SOURCE = {"flow": "Exporter (BGP)", "geoip": "GeoIP", "flow+geoip": "Exporter + GeoIP"}


@netflow_section(
    "netflow_asns", title="Traffic by Autonomous System",
    description="Source and destination traffic per autonomous system, from the exporter's BGP AS numbers or "
                "the GeoIP ASN database.",
    options=[top_n_option(15, 100)],
)
async def netflow_asns(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow import breakdowns as bd

    nr = _nr()
    sid, title = "netflow_asns", "Traffic by Autonomous System"
    with _captured_meta() as meta:
        body = await bd.netflow_asns(limit=2000, with_totals=1, ctx=_bctx(nf))
    description = nr._scope_description(
        nf, f"Traffic per autonomous system, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + _meta_notes(meta, skip={"asn_rollup_geoip"}) + _dropped_note(meta, nf, "this breakdown")
    total = int((body.get("total") or {}).get("bytes") or 0)
    items = list(body.get("items") or [])
    if not total or not items:
        return _section(sid, title, description=description, notes=notes + ["No flow traffic in scope in this period."])
    basis = body.get("basis_bytes") or {}
    basis_total = sum(int(v or 0) for v in basis.values())
    systems = [i for i in items if i.get("kind") == "asn"]
    top_dst = max(systems, key=lambda i: i["dst_bytes"], default=None)
    kpis = [
        {"label": "Autonomous systems", "value": f"{len(systems):,}", "accent": "primary"},
        {"label": "Top destination AS", "value": top_dst["name"] if top_dst else "—", "accent": "info",
         "subtitle": f"{nr.fmt_pct(nr.share(top_dst['dst_bytes'], total))} of traffic received" if top_dst else None},
        {"label": "Attributed by exporter", "value": nr.fmt_pct(nr.share(basis.get("flow"), basis_total)),
         "accent": "info", "subtitle": "AS numbers from the exporter's BGP table"},
        {"label": "Unknown or private", "value": nr.fmt_pct(nr.share(basis.get("none"), basis_total)),
         "accent": "neutral", "subtitle": "no AS attributable"},
    ]
    top_n = int(ctx.opt("top_n"))
    rows = _side_rows(items, body.get("remainder") or {}, total, top_n, lambda i: i.get("name") or "—",
                      "All other autonomous systems",
                      extra=lambda i: [_ASN_SOURCE.get(i.get("source") or "", "—")])
    notes.append("The AS number is the one the exporter put in the flow record (from its BGP table) when "
                 "present, else the GeoIP ASN database's answer for the address; names come from the GeoIP ASN "
                 "database.")
    if meta.source in ("rollup", "hybrid") or any(w.get("code") == "asn_rollup_geoip" for w in meta.warnings):
        notes.append("Hourly rollups do not keep the exporter's AS numbers: the part of the period served from "
                     "them is attributed with the GeoIP ASN database.")
    if not (body.get("geoip") or {}).get("asn_db"):
        notes.append("No GeoIP ASN database is installed: only AS numbers exported in the flow records are "
                     "attributed.")
    notes += _side_note(body)
    return _section(
        sid, title, description=description,
        kpis=[{k: v for k, v in x.items() if v is not None} for x in kpis],
        charts=[_rank_chart("Traffic by autonomous system (sent + received)",
                            [(i.get("name") or "—", float(i["src_bytes"] + i["dst_bytes"])) for i in items[:10]],
                            "B", scale_unit="B")],
        tables=[{"title": _top_title("autonomous systems", min(top_n, len(items)), len(items), "volume"),
                 "headers": ["Autonomous system", "Attribution", "Sent from", "Source share", "Received in",
                             "Destination share"],
                 "styles": ["text", "text", "num", "pct-bar", "num", "pct-bar"], "rows": rows}],
        notes=list(dict.fromkeys(notes)))


# ── IP groups / sites ────────────────────────────────────────────────────────

def _group_overlaps(groups: list) -> list[tuple[str, str]]:
    """Pairs of listed groups whose networks overlap, other than a group and
    its own sub-group (which is overlap by design)."""
    by_id = {g.id: g for g in groups if getattr(g, "id", None)}

    def ancestors(g) -> set[str]:
        seen: set[str] = set()
        p = getattr(g, "parent_id", None)
        while p and p not in seen:
            seen.add(p)
            parent = by_id.get(p)
            p = parent.parent_id if parent is not None else None
        return seen

    out = []
    for i, a in enumerate(groups):
        na = tuple(a.effective or a.cidrs)
        for b in groups[i + 1:]:
            if (a.id and a.id in ancestors(b)) or (b.id and b.id in ancestors(a)):
                continue
            nb = tuple(b.effective or b.cidrs)
            if any(x.version == y.version and x.overlaps(y) for x in na for y in nb):
                out.append((a.name, b.name))
    return out


@netflow_section(
    "netflow_ip_groups", title="Traffic by Site and IP Group",
    description="Traffic sent, received and internal to each managed IP group or site, with ungrouped traffic "
                "and overlapping groups disclosed.",
    options=[top_n_option(20, 200)],
)
async def netflow_ip_groups(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow import breakdowns as bd
    from app.services import netflow_groups

    nr = _nr()
    sid, title = "netflow_ip_groups", "Traffic by Site and IP Group"
    with _captured_meta() as meta:
        body = await bd.netflow_ip_groups(with_totals=1, ctx=_bctx(nf))
    description = nr._scope_description(
        nf, f"Traffic per IP group / site, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + _meta_notes(meta) + _dropped_note(meta, nf, "this breakdown")
    if not body.get("configured"):
        return _section(sid, title, description=description,
                        notes=notes + ["No IP groups are configured (NetFlow settings → IP groups)."])
    total = int((body.get("total") or {}).get("bytes") or 0)
    items = list(body.get("items") or [])
    if not total:
        return _section(sid, title, description=description, notes=notes + ["No flow traffic in scope in this period."])
    top_n = int(ctx.opt("top_n"))
    shown = items[:top_n]
    ungrouped = body.get("ungrouped") or {}
    rows = [[it["name"], nr.fmt_bytes(it["src_bytes"]), nr.fmt_bytes(it["dst_bytes"]),
             nr.fmt_bytes(it["internal_bytes"]), nr.fmt_bytes(it["bytes"]), nr.fmt_pct(nr.share(it["bytes"], total)),
             nr.fmt_count(it["flows"])] for it in shown]
    rows.append(["Traffic touching no group", "—", "—", "—", nr.fmt_bytes(ungrouped.get("bytes")),
                 nr.fmt_pct(nr.share(ungrouped.get("bytes"), total)), nr.fmt_count(ungrouped.get("flows"))])
    lead = items[0] if items else None
    kpis = [
        {"label": "Total volume", "value": nr.fmt_bytes(total), "accent": "primary"},
        {"label": "IP groups", "value": f"{len(items):,}", "accent": "info",
         "subtitle": f"{sum(1 for i in items if i['bytes']):,} with traffic"},
        {"label": "Busiest group", "value": lead["name"] if lead and lead["bytes"] else "—", "accent": "info",
         "subtitle": (f"{nr.fmt_pct(nr.share(lead['bytes'], total))} of traffic involved"
                      if lead and lead["bytes"] else None)},
        {"label": "Ungrouped traffic", "value": nr.fmt_pct(nr.share(ungrouped.get("bytes"), total)),
         "accent": "neutral", "subtitle": "touches no group"},
    ]
    notes.append("Sent: the source is in the group; received: the destination is; internal: both; involved: "
                 "either. Groups can overlap and a flow between two groups counts for both, so involvement "
                 "shares do not add up to 100%; sub-groups count toward their parent.")
    by_name = {g.name: g for g in netflow_groups.groups_snapshot()}
    overlaps = _group_overlaps([by_name[i["name"]] for i in shown if i["name"] in by_name])
    if overlaps:
        listed = ", ".join(f"{a} and {b}" for a, b in overlaps[:5])
        more = f" and {len(overlaps) - 5} more" if len(overlaps) > 5 else ""
        notes.append(f"Overlapping groups (their traffic is counted in each): {listed}{more}.")
    configured = len(by_name)
    if configured > len(items):
        notes.append(f"Only the first {len(items)} of {configured} configured groups are measured.")
    charts = [_rank_chart("Traffic involving each group", [(i["name"], float(i["bytes"])) for i in shown[:10]
                                                           if i["bytes"]], "B", scale_unit="B")] \
        if any(i["bytes"] for i in shown) else []
    return _section(sid, title, description=description,
                    kpis=[{k: v for k, v in x.items() if v is not None} for x in kpis], charts=charts,
                    tables=[{"title": _top_title("IP groups", len(shown), len(items), "traffic involved"),
                             "headers": ["IP group / site", "Sent", "Received", "Internal", "Involved",
                                         "Share of total", "Flows"],
                             "styles": ["text", "num", "num", "num", "num", "pct-bar", "num"], "rows": rows}],
                    notes=list(dict.fromkeys(notes)))


# ── Interface volume ─────────────────────────────────────────────────────────

@netflow_section(
    "netflow_interface_traffic", title="Top Interfaces by Flow Volume",
    description="Interfaces ranked by the flow volume they received and transmitted, with duplicate "
                "ingress/egress export counted once.",
    options=[
        top_n_option(15, 200),
        {"key": "sort", "label": "Rank by", "type": "select", "default": "total",
         "choices": [["total", "Received + transmitted"], ["in", "Received (in)"], ["out", "Transmitted (out)"]]},
    ],
)
async def netflow_interface_traffic(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow.common import INTERFACE_5M, run_query, table_ref
    from app.api.v1.netflow.filters import compile_filter
    from app.api.v1.netflow.sql import EXPORTER
    from app.services import netflow_capacity as cap

    nr = _nr()
    sid = "netflow_interface_traffic"
    top_n = int(ctx.opt("top_n"))
    sort = ctx.opt("sort") if ctx.opt("sort") in ("total", "in", "out") else "total"
    title = f"Top {top_n} Interfaces by Flow Volume"
    sort_label = {"total": "volume", "in": "volume received", "out": "volume transmitted"}[sort]
    f, notes = _capacity_filter(nf)
    notes = list(nf.notes) + [n.replace("Capacity measures", "Interface volume measures") for n in notes]
    sw = await cap.slot_window_for(nf.win)
    description = nr._scope_description(
        nf, f"Flow volume per interface, {_period(ctx, nf.win.start, nf.win.end)}.")
    if sw.empty:
        return _section(sid, title, description=description,
                        notes=notes + ["The period holds no complete 5-minute slot."])
    if sw.retention_cut:
        notes.append(f"Interface history does not reach back to the start of the period; it is measured from "
                     f"{ctx.fmt_dt(sw.first)}.")
    params: dict = {"cap_first": sw.first, "cap_end": sw.end, "nf_limit": top_n}
    auto = cap._auto_cte(nf.vis, params)
    where, fparams = compile_filter(f, INTERFACE_5M, nf.vis)
    params.update(fparams)
    policy = await cap.load_direction_policy(ctx.db)
    dedup = cap.dedup_sql(policy, params)
    order = {"total": "(ib + ob)", "in": "ib", "out": "ob"}[sort]
    res = await run_query(
        f"""
        WITH {auto}
        SELECT replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS e, ifindex,
               sumIf(bytes, direction = 0) AS ib, sumIf(bytes, direction = 1) AS ob,
               count() OVER () AS n_items
        FROM {table_ref(INTERFACE_5M)}
        WHERE timestamp >= %(cap_first)s AND timestamp < %(cap_end)s AND {where} AND {dedup}
        GROUP BY e, ifindex
        ORDER BY {order} DESC, e ASC, ifindex ASC
        LIMIT %(nf_limit)s
        """, params, budget_s=nr.REPORT_BUDGET_S, label="report-interface-volume")
    rows = list(res.result_rows)
    lag = await cap.rollup_lag(sw, nf.vis)
    if lag is not None:
        notes.append(f"The interface rollup is behind; traffic from {ctx.fmt_dt(lag)} onwards is not included yet.")
    if not rows:
        return _section(sid, title, description=description,
                        notes=notes + ["No interface carried flow traffic in scope in this period."])
    from app.api.v1.netflow.access import canonical_ip

    keyed = [(canonical_ip(r[0]) or str(r[0]), int(r[1]), float(r[2] or 0.0), float(r[3] or 0.0)) for r in rows]
    metas = await cap.resolve_interfaces(ctx.db, [(e, i) for e, i, _a, _b in keyed])
    seconds = max(1.0, (sw.end - sw.first).total_seconds())
    n_items = int(rows[0][4] or 0)
    items = []
    for e, i, ib, ob in keyed:
        m = metas.get((e, i))
        label = _iface_label({"device_hostname": m.device_hostname if m else None, "exporter": e,
                              "display_name": m.display_name if m else None, "ifindex": i})
        sp_in = m.speed("in")[0] if m else None
        sp_out = m.speed("out")[0] if m else None
        items.append({"label": label, "in": ib, "out": ob, "speed": _speed_text(sp_in, sp_out)})
    table = [[it["label"], nr.fmt_bytes(it["in"]), nr.fmt_bytes(it["out"]), nr.fmt_bytes(it["in"] + it["out"]),
              nr.fmt_bps(it["in"] * 8 / seconds), nr.fmt_bps(it["out"] * 8 / seconds), it["speed"]] for it in items]
    value = {"total": lambda it: it["in"] + it["out"], "in": lambda it: it["in"], "out": lambda it: it["out"]}[sort]
    lead = items[0]
    peak = max(((it["label"], d, it[d] * 8 / seconds) for it in items for d in ("in", "out")), key=lambda x: x[2])
    kpis = [
        {"label": "Interfaces with flow traffic", "value": f"{n_items:,}", "accent": "primary"},
        {"label": "Busiest interface", "value": nr.fmt_bytes(value(lead)), "accent": "info", "subtitle": lead["label"]},
        {"label": "Highest average rate", "value": nr.fmt_bps(peak[2]), "accent": "info",
         "subtitle": f"{peak[0]} ({peak[1]})"},
    ]
    notes.append("Volumes come from the 5-minute interface rollup over complete slots (in = received on the "
                 "interface, out = transmitted); exporters that export both ingress- and egress-monitored records "
                 "of the same packets are counted once. Interface volumes do not add up: a flow crosses an "
                 "ingress and an egress interface. Average rates divide by the whole period (collection gaps read "
                 "as idle); utilisation is measured in the NetFlow Interface Capacity section.")
    return _section(sid, title, description=description, kpis=kpis,
                    charts=[_rank_chart(f"Interfaces by {sort_label}", [(it["label"], value(it)) for it in items[:10]],
                                        "B", scale_unit="B")],
                    tables=[{"title": _top_title("interfaces", len(items), n_items, sort_label),
                             "headers": ["Interface", "In (received)", "Out (transmitted)", "Total", "Average in",
                                         "Average out", "Speed"],
                             "styles": ["text", "num", "num", "num", "num", "num", "num"], "rows": table}],
                    notes=list(dict.fromkeys(notes)))


# ── Security findings ────────────────────────────────────────────────────────

def _kind_label(kind: str | None) -> tuple[str, str]:
    try:
        from app.services import netflow_detection as det

        spec = det.DETECTOR_BY_KIND.get(kind or "")
        if spec is not None:
            return spec.label, str(spec.category or "—").replace("_", " ").capitalize()
    except Exception:  # pragma: no cover - the detector registry is optional here
        pass
    return (kind or "finding").replace("_", " ").capitalize(), "—"


@netflow_section(
    "netflow_security_findings", title="NetFlow Security Findings",
    description="Detection findings active in the period by kind, severity and status, and the most severe "
                "open items.",
    options=[top_n_option(15, 100)],
)
async def netflow_security_findings(ctx: SectionCtx, nf) -> dict:
    nr = _nr()
    sid, title = "netflow_security_findings", "NetFlow Security Findings"
    found = await nr.notable_findings(ctx.db, nf, limit=1_000_000)
    description = nr._scope_description(
        nf, f"Flow detection findings active {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes)
    if not found.get("available"):
        return _section(sid, title, description=description,
                        notes=notes + [str(found.get("reason") or "Detection findings are not available.")])
    groups = list(found.get("items") or [])
    by_sev = found.get("by_severity") or {}
    by_status = found.get("by_status") or {}
    severe = sum(n for s, n in by_sev.items() if nr._SEVERITY_RANK.get(s, 0) >= 3)
    hosts = {h for g in groups for h in g.get("hosts") or []}
    kpis = [
        {"label": "Open or acknowledged", "value": f"{found.get('active', 0):,}",
         "accent": "warning" if found.get("active") else "success",
         "subtitle": f"{len(groups):,} distinct detection{'s' if len(groups) != 1 else ''}"},
        {"label": "Critical or high", "value": f"{severe:,}", "accent": "danger" if severe else "success"},
        {"label": "Hosts involved", "value": f"{len(hosts):,}", "accent": "info"},
        {"label": "Resolved", "value": f"{by_status.get('resolved', 0):,}", "accent": "neutral",
         "subtitle": f"{by_status.get('suppressed', 0):,} suppressed"},
    ]
    if not groups:
        return _section(sid, title, description=description, kpis=kpis,
                        notes=notes + ["No open or acknowledged NetFlow detection finding in this period."])
    kinds: dict[str, dict] = {}
    for g in groups:
        k = kinds.setdefault(g["kind"] or "", {"findings": 0, "hosts": set(), "sev": "info", "last": None})
        k["findings"] += int(g.get("findings") or 1)
        k["hosts"].update(g.get("hosts") or [])
        if nr._SEVERITY_RANK.get(g["severity"], 0) > nr._SEVERITY_RANK.get(k["sev"], 0):
            k["sev"] = g["severity"]
        if g.get("last_seen") and (k["last"] is None or g["last_seen"] > k["last"]):
            k["last"] = g["last_seen"]
    kind_rows = []
    for kind, k in sorted(kinds.items(), key=lambda kv: (-nr._SEVERITY_RANK.get(kv[1]["sev"], 0),
                                                         -kv[1]["findings"], kv[0])):
        label, category = _kind_label(kind)
        kind_rows.append([label, category, f"{k['findings']:,}", f"{len(k['hosts']):,}",
                          _SEV_CELL.get(k["sev"], "info"), ctx.fmt_short(k["last"])])
    top_n = int(ctx.opt("top_n"))
    item_rows = [[_SEV_CELL.get(g["severity"], "info"), g.get("title") or _kind_label(g.get("kind"))[0],
                  g.get("status") or "open", nr._list(g["hosts"]) if g.get("hosts") else "—",
                  f"{int(g.get('findings') or 1):,}", ctx.fmt_short(g.get("last_seen"))] for g in groups[:top_n]]
    order = ("critical", "high", "warning", "medium", "low", "info")
    donut = [{"label": _SEV_LABEL[s], "value": by_sev[s]} for s in order if by_sev.get(s)]
    notes.append("Findings active in the period (last seen on or after its start, first seen before its end), "
                 "restricted to the exporters the report's requester may see; the same detection on several "
                 "hosts is one row.")
    return _section(
        sid, title, description=description, kpis=kpis,
        charts=[{"title": "Open findings by severity", "series": {"kind": "donut", "unit": "findings",
                                                                  "points": donut}}] if donut else [],
        tables=[{"title": "Findings by detection",
                 "headers": ["Detection", "Category", "Findings", "Hosts", "Worst severity", "Last seen"],
                 "styles": ["text", "text", "num", "num", "severity", "text"], "rows": kind_rows},
                {"title": _top_title("open findings", len(item_rows), len(groups), "severity"),
                 "headers": ["Severity", "Finding", "Status", "Hosts", "Findings", "Last seen"],
                 "styles": ["severity", "text", "status", "text", "num", "text"], "rows": item_rows}],
        notes=notes)


# ── Exporter health ──────────────────────────────────────────────────────────

COLLECTOR_STATS_DAYS = 30          # flow_collector_stats TTL (migrate-137)
_COLLECTOR_COUNTERS = (
    ("datagrams", "Datagrams received"), ("records_decoded", "Records decoded"),
    ("records_written", "Records written"), ("records_dropped_implausible", "Dropped: implausible"),
    ("records_dropped_queue", "Dropped: queue full"), ("records_dropped_unknown_exporter", "Dropped: unknown exporter"),
    ("parse_errors", "Datagrams not parsed"), ("data_sets_waiting_template", "Data sets waiting for a template"),
    ("sequence_gaps", "Sequence gaps (lost export packets/records)"),
    ("clock_clamped_records", "Records placed at receipt time (clock)"),
    ("sampling_ambiguous_records", "Records with ambiguous sampling"),
)


def _exporter_name(row: dict) -> str:
    name = row.get("display_name") or row.get("device_hostname")
    return f"{name} ({row['exporter']})" if name else row["exporter"]


def _problem_issues(row: dict) -> list[dict]:
    return [i for i in row.get("issues") or [] if i.get("severity") in ("warning", "danger")]


@netflow_section(
    "netflow_exporter_health", title="Flow Exporter Health",
    description="Per exporter: export state as of report generation, last receipt, collection coverage over "
                "the period, sequence loss, clock offset, sampling and template issues.",
    options=[
        top_n_option(50, 500),
        {"key": "only_issues", "label": "Only exporters with problems", "type": "bool", "default": False},
    ],
)
async def netflow_exporter_health(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow.access import canonical_ip
    from app.services import netflow_health as health

    nr = _nr()
    sid, title = "netflow_exporter_health", "Flow Exporter Health"
    status = await health.status_for(ctx.db, nf.vis)
    pinned = canonical_ip(nf.f.exporter or (nf.f.iface.exporter if nf.f.iface else None))
    rows = [r for r in status.get("exporters") or [] if not pinned or r["exporter"] == pinned]
    description = nr._scope_description(
        nf, f"Export state as of report generation; coverage and collector counters for "
            f"{_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes) + [str(w.get("message")) + "." for w in status.get("warnings") or [] if w.get("message")]
    if not rows:
        return _section(sid, title, description=description,
                        notes=notes + ["No flow exporter in scope has sent flows or is configured."])
    cov = await nr.coverage(nf, nf.win)
    per_exp = {e["exporter"]: e for e in (cov or {}).get("exporters") or []}
    cov_totals = (cov or {}).get("totals") or {}
    exporters = [pinned] if pinned else nf.vis.exporter_list()
    # An empty list would select the collector's own process row: never ask for it.
    counters = (await health.collector_totals(nf.win.start, nf.win.end, exporters)
                if exporters is None or exporters else None)
    cov_pct = cov_totals.get("coverage_pct")

    states = {s: 0 for s in _EXPORT_STATE_RANK}
    for r in rows:
        states[r.get("state") or "never"] = states.get(r.get("state") or "never", 0) + 1
    problems = [r for r in rows if r.get("state") != "receiving" or _problem_issues(r)]
    not_receiving = states["late"] + states["silent"] + states["never"]
    kpis = [
        {"label": "Exporters", "value": f"{len(rows):,}", "accent": "primary"},
        {"label": "Receiving", "value": f"{states['receiving']:,}",
         "accent": "success" if not not_receiving else "warning", "subtitle": "as of report generation"},
        {"label": "Late, silent or never", "value": f"{not_receiving:,}",
         "accent": "danger" if states["silent"] else ("warning" if not_receiving else "success"),
         "subtitle": f"{states['late']} late · {states['silent']} silent · {states['never']} never"},
        {"label": "With problems", "value": f"{len(problems):,}", "accent": "warning" if problems else "success",
         "subtitle": "not receiving, or loss, clock, template or decode issues"},
        {"label": "Collection coverage", "value": nr.fmt_pct(cov_pct),
         "accent": "neutral" if cov_pct is None else ("success" if cov_pct >= 95 else "warning"),
         "subtitle": "of the period's minutes had flow records" if cov_pct is not None else "not measured"},
    ]
    listed = problems if ctx.opt("only_issues") else rows
    listed = sorted(listed, key=lambda r: (_EXPORT_STATE_RANK.get(r.get("state"), 9), -len(_problem_issues(r)),
                                           r["exporter"]))
    top_n = int(ctx.opt("top_n"))
    table = []
    for r in listed[:top_n]:
        c = per_exp.get(r["exporter"]) or {}
        offset = r.get("clock_offset_ms")
        clock = "—" if offset is None else f"{offset / 1000:+.1f} s" + (" (suspect)" if r.get("clock_suspect") else "")
        sampling = r.get("sampling_interval") or c.get("max_sampling")
        samp = f"1:{int(sampling):,}" if sampling and int(sampling) > 1 else (
            "unsampled" if c.get("records") and not c.get("sampled_records") else "—")
        issues = ", ".join(_ISSUE_LABEL.get(i.get("code"), str(i.get("code"))) for i in r.get("issues") or [])
        table.append([_exporter_name(r), _EXPORT_STATE_CELL.get(r.get("state"), "unknown"),
                      ctx.fmt_short(r.get("last_received_at")), nr.fmt_pct(c.get("coverage_pct")),
                      nr.fmt_pct(r.get("loss_pct_1h"), 2), clock, samp, issues or "—"])
    tables = [{"title": _top_title("exporters", len(table), len(listed), "state"),
               "headers": ["Exporter", "Export state", "Last receipt", "Coverage", "Loss (1 h)", "Clock offset",
                           "Sampling", "Issues"],
               "styles": ["text", "status", "text", "num", "num", "num", "text", "text"], "rows": table}] \
        if table else []
    if not table:
        notes.append("Every exporter in scope is receiving with no loss, clock, template or decode problem.")
    if counters is not None:
        tot = counters.get("totals") or {}
        tables.append({"title": "Collector counters for the period (exporters in scope)",
                       "headers": ["Counter", "Period total"], "styles": ["text", "num"],
                       "rows": [[label, nr.fmt_count(tot.get(key))] for key, label in _COLLECTOR_COUNTERS]})
        reach = datetime.now(timezone.utc) - timedelta(days=COLLECTOR_STATS_DAYS)
        if nf.win.end <= reach:
            notes.append(f"Collector counters are kept for {COLLECTOR_STATS_DAYS} days; none remain for this period.")
        elif nf.win.start < reach:
            notes.append(f"Collector counters are kept for {COLLECTOR_STATS_DAYS} days; the counters cover the "
                         f"period from {ctx.fmt_dt(reach)} only.")
    else:
        notes.append("Collector statistics could not be read; loss, decode and drop counters for the period are "
                     "unknown.")
    if cov is None:
        notes.append("Collection coverage could not be measured for this period.")
    notes.append("Export state is flow delivery to the collector, not device reachability: online = receiving, "
                 "stale = late, offline = silent, unknown = never received. Loss is the sequence-number loss of the "
                 "last hour; clock offset is receipt time minus export time.")
    return _section(sid, title, description=description, kpis=kpis, tables=tables, notes=list(dict.fromkeys(notes)))


# ── TCP flags (scan indicators) ──────────────────────────────────────────────

_FLAG_ROWS = (
    ("syn_only", "SYN without ACK", "Connection attempts; a high share suggests scanning or unanswered connections"),
    ("rst", "RST", "Refused or aborted connections"),
    ("fin", "FIN", "Orderly connection close"),
    ("ack_only", "ACK without SYN", "Established traffic (records within a connection)"),
    ("psh", "PSH", "Data pushed to the application"),
    ("urg", "URG", "Urgent pointer (rare; unusual when frequent)"),
    ("no_flags", "No flag set", "The flags field was exported but empty"),
)


@netflow_section(
    "netflow_tcp_flags", title="TCP Flag Anomalies (Scan Indicators)",
    description="SYN-without-ACK and RST shares of the TCP flows whose flags were exported, per exporter and "
                "over time, with flag coverage disclosed.",
    options=[
        top_n_option(15, 200),
        {"key": "threshold", "label": "Indicator threshold (%)", "type": "number", "default": 10, "min": 1,
         "max": 100, "help": "A SYN-without-ACK or RST share at or above this marks an exporter."},
    ],
)
async def netflow_tcp_flags(ctx: SectionCtx, nf) -> dict:
    from app.api.v1.netflow import access as nfa
    from app.api.v1.netflow import breakdowns as bd
    from app.api.v1.netflow.common import QOS_1H, run_query
    from app.api.v1.netflow.overview import FLAG_COVERAGE_OK_PCT, RST_MIN_SAMPLE, rst_payload
    from app.api.v1.netflow.sql import EXPORTER, plan_source

    nr = _nr()
    sid, title = "netflow_tcp_flags", "TCP Flag Anomalies (Scan Indicators)"
    description = nr._scope_description(
        nf, f"TCP control flags of flow records, {_period(ctx, nf.win.start, nf.win.end)}.")
    notes = list(nf.notes)
    if nf.f.protocol is not None and nf.f.protocol != 6:
        return _section(sid, title, description=description,
                        notes=notes + ["The report's NetFlow scope selects a protocol other than TCP; TCP flags "
                                       "do not apply."])
    threshold = float(ctx.opt("threshold"))
    drop = frozenset({"tcpflag"})
    plan = await plan_source(nf.win, nf.f, rollup=QOS_1H, drop=drop, record=False)
    cols = {"x_t": "timestamp", "x_e": EXPORTER, "x_f": "tcp_flags",
            "x_present": ("flags_known = 1", "bitTest(fields_present, 0)"),
            "x_legacy": ("flags_known = 2", "fields_present = 0"), "x_n": bd.FLOWS}
    sub, params = bd._union(plan, cols, _bctx(nf), drop, where="protocol = 6")
    known = "(x_present OR (x_legacy AND x_f != 0))"
    conds = bd._FLAG_CONDS
    counts = ", ".join(f"sumIf(x_n, {known} AND {cond}) AS {name}" for name, cond in conds.items())
    interval = ctx.bucket()
    if interval in ("5 MINUTE", "30 MINUTE"):
        interval = "1 HOUR"
    bucket, bparams = ctx.ch_bucket("x_t", interval)
    per_exp, trend = await asyncio.gather(
        run_query(f"SELECT replaceRegexpOne(toString(x_e), '^::ffff:', '') AS e, sum(x_n) AS total, "
                  f"sumIf(x_n, {known}) AS flagged, {counts}, sumIf(x_n, x_present AND x_f = 0) AS no_flags "
                  f"FROM {sub} AS src GROUP BY e ORDER BY total DESC, e ASC",
                  params, budget_s=nr.REPORT_BUDGET_S, label="report-tcp-flags-exporters"),
        run_query(f"SELECT toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS k, sumIf(x_n, {known}) AS flagged, "
                  f"sumIf(x_n, {known} AND {conds['syn_only']}) AS syn, sumIf(x_n, {known} AND {conds['rst']}) AS rst "
                  f"FROM {sub} AS src GROUP BY k ORDER BY k",
                  {**params, **bparams}, budget_s=nr.REPORT_BUDGET_S, label="report-tcp-flags-trend"),
    )
    names = ["total", "flagged", *conds, "no_flags"]
    exp_rows = [{"exporter": nfa.canonical_ip(r[0]) or str(r[0]),
                 **{n: int(r[1 + i] or 0) for i, n in enumerate(names)}} for r in per_exp.result_rows]
    notes += nr._plan_notes(plan, nf.branding)
    if not exp_rows or not sum(e["total"] for e in exp_rows):
        return _section(sid, title, description=description, notes=notes + ["No TCP flows in scope in this period."])
    tot = {n: sum(e[n] for e in exp_rows) for n in names}
    total, flagged = tot["total"], tot["flagged"]
    rst = rst_payload(total, flagged, tot["rst"], [k for k in drop if k in nf.f.active_keys()])
    coverage = nr.share(flagged, total)

    def syn_share(row: dict) -> float | None:
        return row["syn_only"] / row["flagged"] * 100 if row["flagged"] >= RST_MIN_SAMPLE else None

    def rst_share(row: dict) -> float | None:
        return row["rst"] / row["flagged"] * 100 if row["flagged"] >= RST_MIN_SAMPLE else None

    syn = syn_share(tot)
    kpis = [
        {"label": "TCP flows", "value": nr.fmt_count(total), "accent": "primary",
         "subtitle": f"{nr.fmt_count(flagged)} with exported flags"},
        {"label": "Flags exported", "value": nr.fmt_pct(coverage),
         "accent": "success" if (coverage or 0) >= FLAG_COVERAGE_OK_PCT else "warning",
         "subtitle": "share of TCP flows"},
        {"label": "SYN without ACK", "value": nr.fmt_pct(syn, 2),
         "accent": "warning" if syn is not None and syn >= threshold else "info",
         "subtitle": "of flagged TCP flows (scan indicator)"},
        {"label": "RST share", "value": nr.fmt_pct(rst["pct"], 2),
         "accent": "warning" if rst["pct"] is not None and rst["pct"] >= threshold else "info",
         "subtitle": {"ok": "of flagged TCP flows", "partial": "flags exported for part of TCP",
                      "low_sample": f"fewer than {RST_MIN_SAMPLE} flagged TCP flows",
                      "flags_unavailable": "exporters send no TCP flags", "no_tcp": "no TCP flows"}.get(
                          rst["status"], rst["status"])},
    ]
    flag_rows = [[label, nr.fmt_count(tot[key]), nr.fmt_pct(nr.share(tot[key], flagged)) if flagged else "—", why]
                 for key, label, why in _FLAG_ROWS]
    owners = await nfa.resolve_exporter_owners(ctx.db, [e["exporter"] for e in exp_rows])
    top_n = int(ctx.opt("top_n"))
    ex_table = []
    marked = 0
    for e in exp_rows:
        s, r_ = syn_share(e), rst_share(e)
        if not e["flagged"] or s is None:
            indicator = "unknown"
        elif s >= threshold or (r_ or 0) >= threshold:
            indicator = "warning"
            marked += 1
        else:
            indicator = "healthy"
        if len(ex_table) < top_n:
            o = owners.get(e["exporter"])
            name = f"{o.hostname} ({e['exporter']})" if o and getattr(o, "hostname", None) else e["exporter"]
            ex_table.append([name, nr.fmt_count(e["total"]), nr.fmt_pct(nr.share(e["flagged"], e["total"])),
                             nr.fmt_pct(s, 2), nr.fmt_pct(r_, 2), indicator])
    points_syn, points_rst = [], []
    for r in trend.result_rows:
        t = ctx.iso(datetime.fromtimestamp(int(r[0]), tz=timezone.utc))
        fl = int(r[1] or 0)
        points_syn.append({"t": t, "v": round(int(r[2] or 0) / fl * 100, 2) if fl else None})
        points_rst.append({"t": t, "v": round(int(r[3] or 0) / fl * 100, 2) if fl else None})
    per = {"1 HOUR": "hour", "2 HOUR": "2 hours", "1 DAY": "day"}.get(interval, interval.lower())
    charts = [{"title": f"SYN-without-ACK and RST share per {per}",
               "series": {"kind": "line", "unit": "%", "color": "warning", "points": points_syn,
                          "series": [{"name": "SYN without ACK", "points": points_syn},
                                     {"name": "RST", "points": points_rst}]}}] if points_syn else []
    notes.append("Shares are of TCP flows whose flags were exported (a template without the TCP-flags field "
                 "stores zeros, which are not 'no flags'); flag classes overlap, so they do not add up to 100%. "
                 f"A share needs at least {RST_MIN_SAMPLE} flagged TCP flows.")
    if coverage is not None and coverage < 100:
        notes.append(f"TCP flags were not exported for {nr.fmt_pct(100 - coverage)} of TCP flows; their flags are "
                     "unknown, not zero.")
    if marked:
        notes.append(f"{marked} exporter{'s' if marked != 1 else ''} at or above the {threshold:g}% indicator "
                     "threshold; see NetFlow Security Findings for the hosts involved.")
    return _section(sid, title, description=description, kpis=kpis, charts=charts,
                    tables=[{"title": "TCP flag classes",
                             "headers": ["Flag class", "Flows", "Share of flagged TCP", "What it indicates"],
                             "styles": ["text", "num", "pct-bar", "text"], "rows": flag_rows},
                            {"title": _top_title("exporters", len(ex_table), len(exp_rows), "TCP flows"),
                             "headers": ["Exporter", "TCP flows", "Flags exported", "SYN without ACK", "RST share",
                                         "Indicator"],
                             "styles": ["text", "num", "num", "num", "num", "status"], "rows": ex_table}],
                    notes=list(dict.fromkeys(notes)))


# ── Built-in templates ───────────────────────────────────────────────────────

template(
    "netflow_conversations_report", "Top Conversations",
    "The busiest client-to-server conversations with their service and application, with the top sources and "
    "destinations, as shares of all flow traffic in scope.",
    "traffic",
    [S("traffic_kpis"), S("netflow_conversations", "Top 25 Conversations", top_n=25), S("traffic_talkers")],
    audience=("Network engineer", "NOC operator"), tags=("netflow", "conversations", "top-n"),
    default_period="last_24h", frequency="daily", icon="Waypoints",
)
template(
    "netflow_apps_protocols_report", "Top Applications and Protocols",
    "Traffic volume and trend with the top applications, IP protocols and conversations, each as a share of "
    "all flow traffic in scope.",
    "traffic",
    [S("traffic_kpis"), S("traffic_trend"), S("traffic_ports"), S("traffic_protocols"),
     S("netflow_conversations", "Top 10 Conversations", top_n=10)],
    audience=("Network engineer", "IT manager"), tags=("netflow", "applications", "protocols"),
    default_period="last_7d", frequency="weekly", icon="Layers", featured=True,
)
template(
    "netflow_qos_dscp_report", "QoS / DSCP Traffic Report",
    "How traffic is marked: volume per DSCP class (voice, video, best effort), traffic without an exported ToS "
    "field, and the protocol mix.",
    "traffic",
    [S("netflow_qos_dscp", None, top_n=16), S("traffic_protocols"), S("traffic_trend")],
    audience=("Network engineer",), tags=("netflow", "qos", "dscp"),
    default_period="last_7d", frequency="weekly", icon="BarChart3",
)
template(
    "netflow_capacity_forecast_report", "NetFlow Interface Capacity and Forecast",
    "Flow-measured interfaces ranked by 95th-percentile utilisation risk, with headroom, change against the "
    "previous period, runout forecasts and the busiest interfaces by volume.",
    "capacity",
    [S("netflow_capacity", "Top 25 Interfaces by Utilisation Risk", top_n=25, forecast_top=5),
     S("netflow_interface_traffic", "Top 15 Interfaces by Flow Volume", top_n=15)],
    audience=("Capacity planner", "Network engineer", "IT manager"), tags=("netflow", "capacity", "forecast", "p95"),
    default_period="last_30d", frequency="monthly", icon="TrendingUp", featured=True,
)
template(
    "netflow_billing_95th_report", "95th Percentile Bandwidth Billing",
    "Burstable billing per interface for the period: in and out 95th percentiles, the billable rate and "
    "whether each figure is billing-grade (complete period, sample coverage).",
    "capacity",
    [S("netflow_billing_95th", None, percentile="95", top_n=100)],
    audience=("IT manager", "Capacity planner", "Executive"), tags=("netflow", "billing", "95th percentile"),
    default_period="previous_month", frequency="monthly", icon="FileText", featured=True,
)
template(
    "netflow_sites_groups_report", "Traffic by Site and IP Group",
    "Traffic sent, received and kept inside each site or IP group, the traffic that touches no group, and the "
    "top conversations.",
    "traffic",
    [S("traffic_kpis"), S("netflow_ip_groups", None, top_n=30),
     S("netflow_conversations", "Top 10 Conversations", top_n=10)],
    audience=("Network engineer", "IT manager"), tags=("netflow", "sites", "ip groups"),
    default_period="last_7d", frequency="weekly", icon="Network",
)
template(
    "netflow_country_asn_report", "Traffic by Country and ASN",
    "Where traffic comes from and goes to: countries and autonomous systems per side, with private and "
    "unplaced address space reported apart.",
    "traffic",
    [S("netflow_countries", None, top_n=20), S("netflow_asns", None, top_n=20)],
    audience=("Network engineer", "Security & audit"), tags=("netflow", "geoip", "asn"),
    default_period="last_7d", frequency="weekly", icon="Globe",
)
template(
    "netflow_security_findings_report", "NetFlow Security Findings",
    "Flow-based detections active in the period by kind and severity, the most severe open findings and the "
    "TCP flag indicators behind scan detections.",
    "security",
    [S("netflow_security_findings", None, top_n=25), S("netflow_tcp_flags")],
    audience=("Security & audit", "Network engineer"), tags=("netflow", "security", "detections"),
    default_period="last_7d", frequency="weekly", icon="ShieldCheck", featured=True,
)
template(
    "netflow_exporter_health_report", "Flow Exporter Health",
    "Whether every flow exporter is delivering: export state, last receipt, coverage, sequence loss, clock "
    "offset, sampling and template issues, with the traffic trend to spot collection gaps.",
    "traffic",
    [S("netflow_exporter_health"), S("traffic_trend")],
    audience=("NOC operator", "Network engineer"), tags=("netflow", "exporters", "data quality"),
    default_period="last_24h", frequency="daily", icon="Activity",
)
template(
    "netflow_tcp_flags_report", "TCP Flag Anomalies (Scan Indicators)",
    "SYN-without-ACK and RST shares of TCP flows per exporter and over time, with flag coverage, and the "
    "detection findings they relate to.",
    "security",
    [S("netflow_tcp_flags", None, top_n=25), S("netflow_security_findings", "Related Security Findings", top_n=10)],
    audience=("Security & audit", "NOC operator"), tags=("netflow", "tcp flags", "scans"),
    default_period="last_24h", frequency="daily", icon="ShieldCheck",
)
