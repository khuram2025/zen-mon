"""NetFlow capacity: per-direction interface utilization, billing, trends and
interface overrides (B4: NF-13..NF-16, CR-08, CR-09, FE-05).

The statistics themselves live in ``app.services.netflow_capacity`` (one
definition shared with the management report); this module is the HTTP layer:

    GET    /capacity                                ranked interfaces + full-set summary (v2 with
                                                    envelope=1 or v=2; legacy list otherwise; CSV)
    GET    /capacity/trend                          daily per-direction series + runout forecast
    GET    /capacity/billing                        percentile billing report for a period (JSON, CSV)
    GET    /capacity/settings                       methodology settings (percentile, direction
    PUT    /capacity/settings                       mode, business hours, thresholds) -- audited
    GET    /capacity/overrides                      NetFlow interface overrides (name, per-direction
    PUT    /capacity/overrides/{exporter}/{ifindex} speed, role, note) -- audited
    DELETE /capacity/overrides/{exporter}/{ifindex}
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.services import netflow_capacity as cap
from app.services.audit_service import write_audit_log

from .access import (
    MANAGE_PERMISSION,
    FlowVisibility,
    flow_visibility,
    require_exporter_visible,
    require_manage,
    require_view,
)
from .common import (
    Window,
    invalid_filter,
    invalid_window,
    now_utc,
    resolve_window,
    validate_tz,
    wants_envelope,
    window_params,
)
from .filters import FlowFilter, flow_filter, require_filter_exporters_visible

router = APIRouter()

SORTS = ("risk", "util", "in", "out", "in_util", "out_util", "combined", "headroom", "change", "name",
         "coverage", "exporter")
ROLES = ("wan", "lan", "uplink", "internal", "ignore")
MAX_TREND_SPAN = timedelta(days=cap.MAX_TREND_DAYS)
MAX_BILLING_SPAN = timedelta(days=31, hours=1)


def _csv_list(raw: str | None, allowed: tuple[str, ...], key: str) -> set[str] | None:
    if raw is None or raw.strip() == "":
        return None
    values = {v.strip() for v in raw.split(",") if v.strip()}
    bad = values - set(allowed)
    if bad:
        raise invalid_filter(f"Unknown {key} value(s): {', '.join(sorted(bad))[:120]}; "
                             f"use {', '.join(allowed)}", {key: "unknown value"})
    return values


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _permissions(user: User, vis: FlowVisibility) -> dict:
    return {
        "manage": vis.can(MANAGE_PERMISSION),
        # PATCH /devices/{id}/interfaces/{if_index} needs devices.manage.
        "device_speed": vis.can("devices.manage"),
    }


def _matches_query(row: dict, q: str) -> bool:
    hay = " ".join(str(x) for x in (
        row["display_name"], row.get("if_name"), row.get("if_descr"), row.get("if_alias"),
        row.get("device_hostname"), row["exporter"], row["ifindex"], (row.get("override") or {}).get("note"),
        row.get("role"),
    ) if x is not None).lower()
    return all(term in hay for term in q.lower().split())


def _sort_value(row: dict, sort: str):
    h = row["headline"]
    return {
        "util": h["utilization_pct"],
        "in": row["in"]["p_bps"] if row["in"]["p_bps"] is not None else row["in"]["max_bps"],
        "out": row["out"]["p_bps"] if row["out"]["p_bps"] is not None else row["out"]["max_bps"],
        "in_util": row["in"]["utilization_p_pct"] if row["in"]["utilization_p_pct"] is not None
        else row["in"]["utilization_max_pct"],
        "out_util": row["out"]["utilization_p_pct"] if row["out"]["utilization_p_pct"] is not None
        else row["out"]["utilization_max_pct"],
        "combined": row["combined"]["avg_bps"],
        "headroom": h["headroom_pct"],
        "change": (row.get("previous") or {}).get("change_pct"),
        "name": row["display_name"].lower(),
        "coverage": row["coverage_pct"],
        "exporter": (row["exporter"], row["ifindex"]),
    }[sort]


def sort_rows(rows: list[dict], sort: str, order: str) -> list[dict]:
    """Sort with unknown values always last; ``risk`` desc = worst first."""
    if sort == "risk":
        ranked = sorted(rows, key=cap.risk_key)
        return ranked if order == "desc" else ranked[::-1]
    known = [r for r in rows if _sort_value(r, sort) is not None]
    unknown = [r for r in rows if _sort_value(r, sort) is None]
    known.sort(key=lambda r: (_sort_value(r, sort), r["exporter"], r["ifindex"]), reverse=order == "desc")
    unknown.sort(key=cap.risk_key)
    return known + unknown


def filter_rows(rows: list[dict], *, q: str | None, states: set[str] | None, resolutions: set[str] | None,
                roles: set[str] | None) -> list[dict]:
    out = []
    for r in rows:
        if q and not _matches_query(r, q):
            continue
        if states and r["headline"]["state"] not in states:
            continue
        if resolutions:
            res = r["resolution"]
            if res not in resolutions and not ("unresolved" in resolutions and res != "resolved"):
                continue
        if roles and (r.get("role") or "none") not in roles:
            continue
        out.append(r)
    return out


def window_block(win: Window, result: cap.CapacityResult, business_hours: bool) -> dict:
    sw = result.sw
    prev = sw.shifted_back() if not sw.empty else None
    return {
        "from": _iso(win.start), "to": _iso(win.end), "tz": win.tz, "mode": win.mode,
        "first_slot": _iso(sw.first), "end_exclusive": _iso(sw.end), "slot_seconds": cap.SLOT_S,
        "complete_slots": sw.n_slots, "expected_slots": result.expected,
        "head_excluded_s": sw.head_excluded_s, "tail_excluded_s": sw.tail_excluded_s,
        "retention_cut": sw.retention_cut, "business_hours": business_hours,
        "raw_from": _iso(result.raw_from),
        "previous": {"from": _iso(prev.first), "to": _iso(prev.end)} if prev else None,
        # Exporters whose newest slots are still settling (left out until
        # long flows still running can no longer be reported into them).
        "provisional": result.provisional,
    }


def legacy_row(r: dict) -> dict:
    """The pre-v2 row shape, with the corrected statistics: the headline
    direction's values (never in + out against one speed)."""
    h = r["headline"]
    d = r[h["direction"]]
    return {
        "exporter_ip": r["exporter"],
        "ifindex": r["ifindex"],
        "if_name": r["if_name"],
        "if_descr": r["if_descr"],
        "if_alias": r["if_alias"],
        "if_speed": d["speed_bps"],
        "device_hostname": r["device_hostname"],
        "display_name": r["display_name"],
        "p95_bps": d["p_bps"],
        "avg_bps": d["avg_bps"],
        "max_bps": d["max_bps"],
        "utilization_p95_pct": d["utilization_p_pct"],
        "utilization_max_pct": d["utilization_max_pct"],
        "total_bytes": int(r["combined"]["bytes"] or 0),
        "total_packets": int((r["in"]["packets"] or 0) + (r["out"]["packets"] or 0)),
        "direction": h["direction"],
        "samples": r["samples"],
        "coverage_pct": r["coverage_pct"],
    }


CSV_HEADER = [
    "exporter", "ifindex", "interface", "alias", "device", "role", "resolution",
    "speed_in_bps", "speed_out_bps", "speed_source_in", "speed_source_out",
    "samples", "expected_slots", "coverage_pct", "statistic",
    "in_avg_bps", "in_percentile_bps", "in_max_bps", "in_util_pct",
    "out_avg_bps", "out_percentile_bps", "out_max_bps", "out_util_pct",
    "combined_avg_bps", "combined_percentile_bps",
    "headline_direction", "headline_util_pct", "headroom_bps", "headroom_pct", "state",
    "previous_value_bps", "change_pct",
]


def csv_rows(rows: list[dict]) -> list[list]:
    out = []
    for r in rows:
        h = r["headline"]
        util_key = "utilization_p_pct" if h["statistic"] != "max" else "utilization_max_pct"
        prev = r.get("previous") or {}
        out.append([
            r["exporter"], r["ifindex"], r["display_name"], r.get("if_alias"), r.get("device_hostname"),
            r.get("role"), r["resolution"], r["speed"]["in_bps"], r["speed"]["out_bps"],
            r["speed"]["source_in"], r["speed"]["source_out"], r["samples"], r["expected_slots"],
            r["coverage_pct"], h["statistic"],
            r["in"]["avg_bps"], r["in"]["p_bps"], r["in"]["max_bps"], r["in"][util_key],
            r["out"]["avg_bps"], r["out"]["p_bps"], r["out"]["max_bps"], r["out"][util_key],
            r["combined"]["avg_bps"], r["combined"]["p_bps"],
            h["direction"], h["utilization_pct"], h["headroom_bps"], h["headroom_pct"], h["state"],
            prev.get("value_bps"), prev.get("change_pct"),
        ])
    return out


def _csv_response(body: str, name: str) -> Response:
    return Response(
        content=body, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")


# ─────────────────────────────────────────────────────────────────
# Capacity: ranked interfaces with the full-set summary
# ─────────────────────────────────────────────────────────────────

@router.get("/capacity")
async def netflow_capacity(
    request: Request,
    win: Window = Depends(window_params(168)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=200, description="Search interface, device, exporter"),
    state: str | None = Query(default=None, description="Comma list of over,critical,warning,healthy,insufficient,unknown"),
    resolution: str | None = Query(default=None, description="Comma list of resolution states, or 'unresolved'"),
    role: str | None = Query(default=None, description="Comma list of wan,lan,uplink,internal,ignore,none"),
    include_ignored: bool = Query(default=False),
    sort: str = Query(default="risk"),
    order: str = Query(default="desc"),
    compare: bool = Query(default=False, description="Add the previous period's value and change"),
    business_hours: bool = Query(default=False, description="Only the configured business hours"),
    crosscheck: bool = Query(default=True, description="SNMP and sFlow-counter cross-checks for the page"),
    format: str | None = Query(default=None, description="csv: every matching interface"),
    v: int | None = Query(default=None, description="2: the v2 object without the envelope"),
):
    """Per-interface, per-direction utilization over complete 5-minute slots
    (see ``app.services.netflow_capacity`` for the statistical contract)."""
    require_filter_exporters_visible(f, vis)
    states = _csv_list(state, cap.STATES, "state")
    resolutions = _csv_list(resolution, cap.RESOLUTIONS + ("unresolved",), "resolution")
    roles = _csv_list(role, ROLES + ("none",), "role")
    if sort not in SORTS:
        raise invalid_filter(f"Unknown sort '{sort[:40]}'; use {', '.join(SORTS)}", {"sort": "unknown value"})
    if order not in ("asc", "desc"):
        raise invalid_filter("order must be asc or desc", {"order": "unknown value"})
    if format not in (None, "", "json", "csv"):
        raise invalid_filter("format must be json or csv", {"format": "unknown value"})
    settings, info = await cap.load_settings(db)
    result = await cap.compute_capacity(win, f, vis, db, settings=settings, business_hours=business_hours,
                                        previous=compare and format != "csv", exclude_provisional=True)
    rows = [r for r in result.rows if include_ignored or r.get("role") != "ignore"]
    if roles and "ignore" in roles:
        rows = list(result.rows)
    summary = cap.summarize(rows)
    matched = sort_rows(filter_rows(rows, q=q, states=states, resolutions=resolutions, roles=roles), sort, order)

    if format == "csv":
        return _csv_response(cap.to_csv(CSV_HEADER, csv_rows(matched)),
                             f"netflow-capacity-{_stamp(win.start)}-{_stamp(win.end)}.csv")

    page = matched[offset:offset + limit]
    if crosscheck and page:
        await cap.snmp_crosscheck(page, result.sw, result.selector, settings.percentile)
        await cap.counter_crosscheck(page, result.sw, vis, settings.percentile)

    if not (wants_envelope(request) or v == 2):
        return [legacy_row(r) for r in page]
    return {
        "window": window_block(win, result, business_hours),
        "method": result.method,
        "settings": {**settings.to_dict(), **info},
        "permissions": _permissions(user, vis),
        "summary": summary,
        "matched": len(matched),
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "order": order,
        "items": page,
        "exporters": result.exporters,
    }


# ─────────────────────────────────────────────────────────────────
# Trend and runout forecast for one interface
# ─────────────────────────────────────────────────────────────────

@router.get("/capacity/trend")
async def netflow_capacity_trend(
    exporter: str = Query(..., description="Exporter address"),
    ifindex: int = Query(..., ge=0, le=4294967295),
    days: int | None = Query(default=None, ge=1, le=cap.MAX_TREND_DAYS),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    tz: str | None = Query(default=None),
    business_hours: bool = Query(default=False),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Daily in/out percentile, peak and average (up to 400 days of interface
    history) and a least-squares runout forecast of the daily percentile."""
    ip = require_exporter_visible(vis, exporter)
    minutes = None if (from_ts or to_ts) else (days or 90) * 1440
    win = resolve_window(None, minutes, from_ts, to_ts, tz, max_span=MAX_TREND_SPAN)
    settings, _ = await cap.load_settings(db)
    return await cap.interface_trend(win, ip, ifindex, vis, db, settings=settings, business_hours=business_hours)


# ─────────────────────────────────────────────────────────────────
# Billing report (CR-08)
# ─────────────────────────────────────────────────────────────────

BILLING_CSV_HEADER = [
    "exporter", "ifindex", "interface", "alias", "device", "role", "period_from", "period_to", "method",
    "percentile", "direction_mode", "samples", "expected_slots", "coverage_pct",
    "in_percentile_bps", "out_percentile_bps", "combined_percentile_bps", "billable_bps", "billable_direction",
    "speed_in_bps", "speed_out_bps", "billable_util_pct", "billing_grade", "notes",
]


def _last_month(tz: str) -> str:
    local = now_utc().astimezone(ZoneInfo(tz))
    y, m = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
    return f"{y:04d}-{m:02d}"


@router.get("/capacity/billing")
async def netflow_capacity_billing(
    request: Request,
    month: str | None = Query(default=None, description="Calendar month YYYY-MM in tz (default: last month)"),
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
    tz: str | None = Query(default=None),
    percentile: int | None = Query(default=None, description="Override the configured percentile"),
    direction_mode: str | None = Query(default=None, description="Override: separate or merged"),
    business_hours: bool | None = Query(default=None, description="Default: the configured business hours"),
    q: str | None = Query(default=None, max_length=200),
    format: str | None = Query(default=None),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Per-interface billing statistic for a period, with the methodology and
    whether each figure is billing-grade (complete period, coverage)."""
    require_filter_exporters_visible(f, vis)
    tz_name = validate_tz(tz)
    if format not in (None, "", "json", "csv"):
        raise invalid_filter("format must be json or csv", {"format": "unknown value"})
    label = None
    if from_ts or to_ts:
        if month:
            raise invalid_window("Give either month or from/to, not both", "month")
        win = resolve_window(None, None, from_ts, to_ts, tz_name, max_span=MAX_BILLING_SPAN)
    else:
        month = month or _last_month(tz_name)
        try:
            start, end = cap.month_bounds(month, tz_name)
        except ValueError as exc:
            raise invalid_window(str(exc), "month") from None
        if start >= now_utc():
            raise invalid_window("The month has not started yet", "month")
        win = resolve_window(None, None, _iso(start), _iso(end), tz_name, max_span=MAX_BILLING_SPAN)
        label = month
    settings, info = await cap.load_settings(db)
    overrides = {}
    if percentile is not None:
        overrides["percentile"] = percentile
    if direction_mode is not None:
        overrides["direction_mode"] = direction_mode
    if overrides:
        try:
            settings = cap.parse_settings(overrides, base=settings)
        except cap.SettingsError as exc:
            raise invalid_filter(str(exc), exc.fields) from None
    bh = settings.business_hours.enabled if business_hours is None else business_hours
    result = await cap.compute_capacity(win, f, vis, db, settings=settings, business_hours=bh,
                                        exclude_provisional=True)
    now = now_utc()
    complete = win.end <= now and win.mode == "fixed"
    items = []
    # Largest billable figure first; interfaces without one (and why) last.
    ordered = sorted(result.rows, key=lambda r: (r["billable"]["bps"] is None, -(r["billable"]["bps"] or 0.0),
                                                 r["exporter"], r["ifindex"]))
    for r in ordered:
        if r.get("role") == "ignore" or (q and not _matches_query(r, q)):
            continue
        notes = cap.billing_grade(r, period_complete=complete, retention_cut=result.sw.retention_cut,
                                  settings=settings)
        billable = r["billable"]["bps"]
        util = None
        if settings.direction_mode == "separate" and billable is not None:
            d = r["billable"]["direction"]
            util = r[d]["utilization_p_pct"] if d in ("in", "out") else None
        items.append({
            "exporter": r["exporter"], "ifindex": r["ifindex"], "display_name": r["display_name"],
            "if_alias": r.get("if_alias"), "device_hostname": r.get("device_hostname"), "role": r.get("role"),
            "samples": r["samples"], "expected_slots": r["expected_slots"], "coverage_pct": r["coverage_pct"],
            "in_p_bps": r["in"]["p_bps"], "out_p_bps": r["out"]["p_bps"], "combined_p_bps": r["combined"]["p_bps"],
            "in_max_bps": r["in"]["max_bps"], "out_max_bps": r["out"]["max_bps"],
            "billable_bps": billable, "billable_direction": r["billable"]["direction"],
            "speed_in_bps": r["speed"]["in_bps"], "speed_out_bps": r["speed"]["out_bps"],
            "billable_util_pct": util,
            "billing_grade": not notes, "notes": notes, "p_reason": r["headline"]["reason"],
        })
    period = {"from": _iso(win.start), "to": _iso(win.end), "tz": tz_name, "month": label,
              "complete": complete, "first_slot": _iso(result.sw.first), "end_exclusive": _iso(result.sw.end),
              "expected_slots": result.expected, "retention_cut": result.sw.retention_cut}
    method = {**result.method, "billing_min_coverage_pct": settings.billing_min_coverage_pct,
              "billing_grade_rule": (f"a complete past period with at least {settings.billing_min_coverage_pct:g}% "
                                     f"of its 5-minute samples collected and at least "
                                     f"{cap.min_samples(settings.percentile)} samples"),
              "overridden": sorted(overrides)}
    if format == "csv":
        stat = f"p{settings.percentile} nearest-rank of 5-minute samples ({settings.direction_mode})"
        body = cap.to_csv(BILLING_CSV_HEADER, [[
            i["exporter"], i["ifindex"], i["display_name"], i["if_alias"], i["device_hostname"], i["role"],
            period["from"], period["to"], stat, settings.percentile, settings.direction_mode, i["samples"],
            i["expected_slots"], i["coverage_pct"], i["in_p_bps"], i["out_p_bps"], i["combined_p_bps"],
            i["billable_bps"], i["billable_direction"], i["speed_in_bps"], i["speed_out_bps"],
            i["billable_util_pct"], "yes" if i["billing_grade"] else "no (indicative)", "; ".join(i["notes"]),
        ] for i in items])
        return _csv_response(body, f"netflow-billing-{label or _stamp(win.start)}.csv")
    return {
        "period": period,
        "method": method,
        "settings": {**settings.to_dict(), **info},
        "items": items,
        "totals": {"interfaces": len(items), "billing_grade": sum(1 for i in items if i["billing_grade"]),
                   "indicative": sum(1 for i in items if not i["billing_grade"])},
    }


# ─────────────────────────────────────────────────────────────────
# Settings (audited)
# ─────────────────────────────────────────────────────────────────

@router.get("/capacity/settings")
async def netflow_capacity_settings(
    vis: FlowVisibility = Depends(flow_visibility),
    user: User = Depends(require_view),
    db: AsyncSession = Depends(get_db),
):
    settings, info = await cap.load_settings(db)
    return {
        "settings": settings.to_dict(),
        "defaults": cap.DEFAULT_SETTINGS.to_dict(),
        "info": info,
        "choices": {"percentile": list(cap.PERCENTILES), "direction_mode": list(cap.DIRECTION_MODES),
                    "roles": list(ROLES)},
        "permissions": _permissions(user, vis),
    }


@router.put("/capacity/settings")
async def netflow_capacity_settings_update(
    payload: dict = Body(...),
    user: User = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Change the capacity methodology (netflow.manage); partial updates merge
    over the stored settings; every change is audited with old and new values."""
    current, _ = await cap.load_settings(db)
    try:
        new = cap.parse_settings(payload, base=current)
    except cap.SettingsError as exc:
        raise invalid_filter(str(exc), exc.fields) from None
    await cap.save_settings(db, new, user)
    settings, info = await cap.load_settings(db)
    return {"settings": settings.to_dict(), "info": info}


# ─────────────────────────────────────────────────────────────────
# NetFlow interface overrides (audited)
# ─────────────────────────────────────────────────────────────────

class InterfaceOverride(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    speed_in_bps: int | None = Field(default=None, gt=0, le=10**15)
    speed_out_bps: int | None = Field(default=None, gt=0, le=10**15)
    role: Literal["wan", "lan", "uplink", "internal", "ignore"] | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.get("/capacity/overrides")
async def netflow_capacity_overrides(
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    rows = await cap.load_overrides(db)
    items = [o for o in rows.values() if vis.allows(o["exporter"])]
    items.sort(key=lambda o: (o["exporter"], o["ifindex"]))
    return {"items": items}


def _override_rid(ip: str, ifindex: int) -> str:
    return f"{ip}|{ifindex}"


@router.put("/capacity/overrides/{exporter}/{ifindex}")
async def netflow_capacity_override_put(
    exporter: str,
    ifindex: int,
    body: InterfaceOverride,
    user: User = Depends(require_manage),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """Create or replace an interface override: display name, per-direction
    circuit speed (used when the device has no configured speed), role and note."""
    ip = require_exporter_visible(vis, exporter)
    if not 0 <= ifindex <= 4294967295:
        raise invalid_filter("ifindex must be between 0 and 4294967295", {"ifindex": "out of range"})
    old = (await cap.load_overrides(db, [ip])).get((ip, ifindex))
    new = body.model_dump()
    if new["display_name"] is not None:
        new["display_name"] = new["display_name"].strip() or None
    await db.execute(
        text("""
            INSERT INTO netflow_interface_overrides
                (exporter_ip, ifindex, display_name, speed_in_bps, speed_out_bps, role, note,
                 created_by, updated_by, created_at, updated_at)
            VALUES (CAST(:ip AS inet), :ifindex, :display_name, :speed_in_bps, :speed_out_bps, :role, :note,
                    :uid, :uid, NOW(), NOW())
            ON CONFLICT (exporter_ip, ifindex) DO UPDATE SET
                display_name = EXCLUDED.display_name, speed_in_bps = EXCLUDED.speed_in_bps,
                speed_out_bps = EXCLUDED.speed_out_bps, role = EXCLUDED.role, note = EXCLUDED.note,
                updated_by = EXCLUDED.updated_by, updated_at = NOW()
        """),
        {"ip": ip, "ifindex": ifindex, **new, "uid": getattr(user, "id", None)},
    )
    changes = {k: {"old": (old or {}).get(k), "new": v} for k, v in new.items() if (old or {}).get(k) != v}
    await write_audit_log(
        db, actor=user, action="netflow.interface_override." + ("update" if old else "create"),
        resource_type="netflow_interface_override", resource_id=_override_rid(ip, ifindex),
        metadata={"exporter": ip, "ifindex": ifindex, "changes": changes},
    )
    await db.commit()
    return (await cap.load_overrides(db, [ip])).get((ip, ifindex))


@router.delete("/capacity/overrides/{exporter}/{ifindex}")
async def netflow_capacity_override_delete(
    exporter: str,
    ifindex: int,
    user: User = Depends(require_manage),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    ip = require_exporter_visible(vis, exporter)
    old = (await cap.load_overrides(db, [ip])).get((ip, ifindex))
    if old is None:
        raise HTTPException(status_code=404, detail="No override for this interface")
    await db.execute(
        text("DELETE FROM netflow_interface_overrides WHERE exporter_ip = CAST(:ip AS inet) AND ifindex = :ifindex"),
        {"ip": ip, "ifindex": ifindex},
    )
    await write_audit_log(
        db, actor=user, action="netflow.interface_override.delete", resource_type="netflow_interface_override",
        resource_id=_override_rid(ip, ifindex),
        metadata={"exporter": ip, "ifindex": ifindex,
                  "changes": {k: {"old": v, "new": None} for k, v in old.items()
                              if k in ("display_name", "speed_in_bps", "speed_out_bps", "role", "note")}},
    )
    await db.commit()
    return {"deleted": True}
