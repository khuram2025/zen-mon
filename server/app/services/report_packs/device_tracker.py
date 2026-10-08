"""Device Tracker (UDT) sections: switch port capacity, endpoint activity,
rogue and watched endpoints, endpoint inventory, IP address reassignments,
user logins and VLANs.

Shared rules in this pack (AGENTS.md, Device Tracker):
- Device scope means the device that observed the data — the switch for
  ports, sessions, VLANs and activity, the reporting router for IP bindings.
  Every Postgres query of device data carries ctx.pg_device_clause on that
  column, so an account limited to a visibility scope only sees what its own
  devices reported.
- Activity events, user logins and closed port sessions are kept for 90 days
  (udt_sweeper.RETENTION); IP observation periods and the daily port-capacity
  snapshots are never pruned. Windows that reach past retention say so, and
  days that were pruned are gaps, not zeros.
- ARP/ND sightings from separate reporters never establish exclusive IP
  ownership. Interface indexes are local to their reporter, so a port is
  always shown with its switch. Observation periods are summarised per
  endpoint and address for display, never de-duplicated. An address binding
  is "confirmed" only by three sightings within 24 hours from one
  reporter/interface/source (udt_ip_evidence); repeat evidence is never
  inferred from the number of history periods.
- Randomized (locally administered) MACs are counted separately: they inflate
  new-endpoint and rogue counts and carry no OUI vendor.
- User names are personal data: only udt_user_logins (udt.view_users) shows
  them; no other section selects user columns or user_login events.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
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

logger = logging.getLogger("zenplus.reports")

RETENTION_DAYS = 90          # udt_sweeper.RETENTION: events, logins, closed port sessions
ONLINE_MINUTES = 15          # Device Tracker's "online" (api/v1/udt.py list_endpoints)
REPLACED_GAP = "1 hour"      # stale ARP caches and poll offsets are not concurrent use

EVENT_TYPES = ("new_endpoint", "endpoint_moved", "rogue_detected", "watch_seen")
EVENT_LABELS = {
    "new_endpoint": "New endpoints",
    "endpoint_moved": "Moves",
    "rogue_detected": "Rogue detections",
    "watch_seen": "Watch-list sightings",
}
EVENT_SINGULAR = {
    "new_endpoint": "New endpoint",
    "endpoint_moved": "Moved",
    "rogue_detected": "Rogue detected",
    "watch_seen": "Watch-list sighting",
}
TYPE_LABELS = {
    "workstation": "Workstation", "server": "Server", "phone": "Phone", "printer": "Printer",
    "access_point": "Access point", "camera": "Camera", "virtual": "Virtual machine",
    "network": "Network device", "iot": "IoT device", "unknown": "Unclassified",
}

# One reporter/interface/source row (alias {a}) whose three most recent
# retained sightings fall within 24 hours: the replacement-primary rule.
CONFIRMED_SQL = ("(cardinality({a}.recent_sightings) >= 3 AND "
                 "(SELECT max(s) - min(s) FROM unnest({a}.recent_sightings) s) <= INTERVAL '24 hours')")


# ─── Shared helpers ─────────────────────────────────────────────────────────

def _aware(dt: datetime) -> datetime:
    """UTC-aware (ctx.frm/to are naive UTC; asyncpg reads naive as local time)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tz(ctx: SectionCtx):
    try:
        return ZoneInfo(ctx.tz_name)
    except Exception:
        return timezone.utc


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _n(v: Any) -> str:
    return f"{_i(v):,}"


def _count(n: Any, word: str, plural: Optional[str] = None) -> str:
    """'1 endpoint', '12 endpoints'."""
    n = _i(n)
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


def _share(part: Any, whole: Any, digits: int = 1) -> str:
    whole = _i(whole)
    return f"{_i(part) / whole * 100:.{digits}f}%" if whole else "—"


def _kpi(label: str, value: str, accent: str, subtitle: Optional[str] = None) -> dict:
    out = {"label": label, "value": value, "accent": accent}
    if subtitle:
        out["subtitle"] = subtitle
    return out


def _truncated(title: str, shown: int, total: int, noun: str) -> str:
    return f"{title} — top {shown:,} of {total:,} {noun}" if total > shown else title


def _count_change(curr: int, prev: Optional[int]) -> str:
    """'▲ 12 vs previous period' (or why there is no comparison)."""
    if prev is None:
        return "Previous period is past the 90-day retention"
    d = curr - prev
    if d == 0:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):,} vs previous period"


def _type_label(t: Optional[str]) -> str:
    if not t:
        return "Unclassified"
    return TYPE_LABELS.get(t) or t.replace("_", " ").replace("-", " ").capitalize()


def _endpoint(hostname: Optional[str], mac: Optional[str]) -> str:
    return (hostname or "").strip() or mac or "—"


def _identity(hostname: Optional[str], vendor: Optional[str], randomized: Any) -> str:
    """Hostname, else what the MAC says about the device (for tables that also
    show the MAC address)."""
    return ((hostname or "").strip() or ("Randomized MAC" if randomized else
                                         (vendor or "").strip() or "Unknown vendor"))


def _port(if_name: Optional[str], if_index: Any) -> str:
    """Interface name, else the reporter-local index (always shown with its switch)."""
    if if_name:
        return if_name
    return f"ifIndex {if_index}" if if_index is not None else "—"


def _user(name: Optional[str], domain: Optional[str]) -> str:
    name = (name or "").strip() or "—"
    return f"{domain}\\{name}" if domain else name


def _date_noon(d: Any) -> Optional[datetime]:
    """A calendar DATE as a datetime that formats to the same date anywhere."""
    if isinstance(d, datetime):
        return d
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)
    return None


def _local_days(ctx: SectionCtx) -> list[datetime]:
    """Aware local midnights of every organisation-calendar day in the window."""
    tz = _tz(ctx)
    end = _aware(ctx.to)
    cur = _aware(ctx.frm).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    while cur < end and len(out) < 400:
        out.append(cur)
        cur = (cur.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=tz)
    return out


def _day_epoch_sql(column: str) -> str:
    """Epoch of the local day start of ``column`` (param :org_tz)."""
    return (f"EXTRACT(EPOCH FROM (date_trunc('day', {column} AT TIME ZONE :org_tz)"
            f" AT TIME ZONE :org_tz))::bigint")


def _retention_cutoff() -> datetime:
    return _now() - timedelta(days=RETENTION_DAYS)


def _daily_points(ctx: SectionCtx, counts: dict[int, int], retained: bool = True) -> list[dict]:
    """One point per local day; days wholly before retention are gaps (None)."""
    cutoff = _retention_cutoff()
    out = []
    for day in _local_days(ctx):
        gone = retained and day + timedelta(days=1) <= cutoff
        out.append({"t": ctx.iso(day), "v": None if gone else counts.get(int(day.timestamp()), 0)})
    return out


def _fleet_note(ctx: SectionCtx) -> list[str]:
    if ctx.device_ids:
        return ["Covers every tracked endpoint: the report's device filter does not apply to this section."]
    return []


async def _rows(ctx: SectionCtx, sql: str, params: dict) -> list:
    return list((await ctx.db.execute(text(sql), params)).fetchall())


async def _first(ctx: SectionCtx, sql: str, params: dict):
    return (await ctx.db.execute(text(sql), params)).first()


# ─── Switch port capacity ───────────────────────────────────────────────────

CAPACITY_SORTS = {"used_pct": "Highest % used", "free": "Fewest free ports", "name": "Switch name"}


@register_section(
    "udt_port_capacity", title="Switch Port Capacity", category="Device Tracker",
    description=("Total, used and free Ethernet ports per switch at the end of the period, ports with an "
                 "endpoint seen in the last 24 hours, uplinks, and the daily port-usage trend."),
    scope="device", requires=("udt.view",), semantics="window",
    options=[
        top_n_option(25, 500),
        {"key": "threshold", "label": "Warning threshold (% used)", "type": "number", "default": 80,
         "min": 1, "max": 100, "help": "Switches at or above this share of used ports are flagged."},
        {"key": "sort", "label": "Sort by", "type": "select", "default": "used_pct",
         "choices": [[k, v] for k, v in CAPACITY_SORTS.items()]},
    ],
)
async def udt_port_capacity(ctx: SectionCtx) -> dict:
    sid, title = "udt_port_capacity", "Switch Port Capacity"
    top_n, threshold, sort = int(ctx.opt("top_n")), float(ctx.opt("threshold")), ctx.opt("sort")
    tz = _tz(ctx)
    d0 = _aware(ctx.frm).astimezone(tz).date()
    d1 = max((_aware(ctx.to) - timedelta(microseconds=1)).astimezone(tz).date(), d0)
    dev, dparams = ctx.pg_device_clause("c.device_id")
    params = {"d0": d0, "d1": d1, **dparams}
    rows = await _rows(ctx, f"""
        /* udt_port_capacity_latest */
        SELECT l.device_id::text, COALESCE(d.hostname, host(d.ip_address), l.device_id::text),
               COALESCE(NULLIF(d.location, ''), '—'), l.day, l.total_ports, l.used_ports,
               l.active_ports, l.uplink_ports
        FROM (SELECT DISTINCT ON (c.device_id) c.device_id, c.day, c.total_ports, c.used_ports,
                     c.active_ports, c.uplink_ports
              FROM udt_port_capacity_daily c
              WHERE c.day BETWEEN :d0 AND :d1{dev}
              ORDER BY c.device_id, c.day DESC) l
        JOIN devices d ON d.id = l.device_id""", params)
    scope_note = (["Limited to the selected switches."] if ctx.device_ids else [])
    if not rows:
        return _section(sid, title, description=(
            "Ethernet port usage per switch from Device Tracker's daily snapshots."),
            notes=["No switch port snapshots in this period. Device Tracker records a daily snapshot for "
                   "every switch it polls; check that Device Tracker collection is enabled for your "
                   "switches."] + scope_note)
    trend = await _rows(ctx, f"""
        /* udt_port_capacity_trend */
        SELECT c.day, sum(c.total_ports), sum(c.used_ports), sum(c.active_ports), count(*)
        FROM udt_port_capacity_daily c
        WHERE c.day BETWEEN :d0 AND :d1{dev}
        GROUP BY c.day ORDER BY c.day""", params)

    switches = []
    for r in rows:
        total, used = _i(r[4]), _i(r[5])
        pct = used / total * 100 if total else None
        switches.append({"name": r[1] or "—", "location": r[2] or "—", "day": r[3], "total": total,
                         "used": used, "free": max(total - used, 0), "pct": pct,
                         "active": _i(r[6]), "uplinks": _i(r[7])})
    if sort == "free":
        switches.sort(key=lambda s: (s["total"] == 0, s["free"], s["name"].lower()))
    elif sort == "name":
        switches.sort(key=lambda s: s["name"].lower())
    else:
        switches.sort(key=lambda s: (s["pct"] is None, -(s["pct"] or 0), s["name"].lower()))

    total = sum(s["total"] for s in switches)
    used = sum(s["used"] for s in switches)
    active = sum(s["active"] for s in switches)
    uplinks = sum(s["uplinks"] for s in switches)
    pct = used / total * 100 if total else None
    over = [s for s in switches if s["pct"] is not None and s["pct"] >= threshold]

    by_day = {}
    for r in trend:
        day = r[0].date() if isinstance(r[0], datetime) else r[0]
        t_total = _i(r[1])
        if t_total:
            by_day[day] = (_i(r[2]) / t_total * 100, _i(r[3]) / t_total * 100)
    used_pts, active_pts = [], []
    d = d0
    while d <= d1 and len(used_pts) < 400:
        t = ctx.iso(datetime(d.year, d.month, d.day, tzinfo=tz))
        v = by_day.get(d)
        used_pts.append({"t": t, "v": round(v[0], 2) if v else None})
        active_pts.append({"t": t, "v": round(v[1], 2) if v else None})
        d += timedelta(days=1)
    measured = sorted(by_day)
    change = None
    if len(measured) >= 2:
        delta = round(by_day[measured[-1]][0] - by_day[measured[0]][0], 2)
        since = ctx.fmt_date(_date_noon(measured[0]))
        change = ("No change since " + since if delta == 0 else
                  f"{'▲' if delta > 0 else '▼'} {abs(delta):.2f} pts since {since}")

    shown = switches[:top_n]
    stale = [s for s in switches if s["day"] is not None and
             (s["day"].date() if isinstance(s["day"], datetime) else s["day"]) < d1]
    notes = ["Ports are Ethernet interfaces (ifType 6/117) on switches Device Tracker polls. Used means "
             "the link is up, free is total minus used, and active means an endpoint was learned on the "
             "port within 24 hours of the snapshot."]
    if stale:
        notes.append(f"{_count(len(stale), 'switch', 'switches')} without a snapshot for "
                     f"{ctx.fmt_date(_date_noon(d1))}: the latest snapshot in the period is shown.")
    if len(measured) < len(used_pts):
        notes.append("Days without a snapshot are left blank (not measured); snapshots start when Device "
                     "Tracker first polled a switch.")
    notes += scope_note

    def status(s: dict) -> str:
        if s["pct"] is None:
            return "unknown"
        return "warning" if s["pct"] >= threshold else "ok"

    return _section(
        sid, title,
        description=(f"Ethernet port usage on {_count(len(switches), 'switch', 'switches')} at the end of "
                     "the period "
                     f"({CAPACITY_SORTS.get(sort, '').lower()} first), with the daily trend."),
        kpis=[
            _kpi("Switches", _n(len(switches)), "primary", _count(uplinks, "uplink port")),
            _kpi("Port usage", f"{pct:.1f}%" if pct is not None else "—",
                 "neutral" if pct is None else ("warning" if pct >= threshold else "success"),
                 change or ("Not measured" if pct is None else None)),
            _kpi("Used ports", _n(used), "info", f"of {_count(total, 'port')}"),
            _kpi("Free ports", _n(max(total - used, 0)), "success" if total - used > 0 else "warning",
                 _share(max(total - used, 0), total) + " of total" if total else None),
            _kpi("Active in last 24 h", _n(active), "info", _share(active, total) + " of total"
                 if total else None),
            _kpi(f"Switches ≥ {threshold:g}% used", _n(len(over)), "warning" if over else "success",
                 f"of {len(switches):,}"),
        ],
        charts=[{"title": "Port usage by day",
                 "series": {"kind": "line", "unit": "%", "color": "primary", "y_domain": [0, 100],
                            "points": used_pts,
                            "series": [{"name": "Used (link up)", "points": used_pts},
                                       {"name": "Endpoint seen in 24 h", "points": active_pts}]}}],
        tables=[{"title": _truncated("Ports per switch", len(shown), len(switches), "switches"),
                 "headers": ["Switch", "Location", "Total ports", "Used", "Free", "% used",
                             "Active (24 h)", "Uplinks", "Status"],
                 "styles": ["text", "text", "num", "num", "num", "pct-bar", "num", "num", "status"],
                 "rows": [[s["name"], s["location"], _n(s["total"]), _n(s["used"]), _n(s["free"]),
                           f"{s['pct']:.1f}%" if s["pct"] is not None else "—", _n(s["active"]),
                           _n(s["uplinks"]), status(s)] for s in shown]}],
        notes=notes,
    )


# ─── Endpoint activity ──────────────────────────────────────────────────────

ACTIVITY_FOCUS: dict[str, tuple[str, tuple[str, ...]]] = {
    "all": ("Endpoint Activity", EVENT_TYPES),
    "new_endpoint": ("New Endpoints", ("new_endpoint",)),
    "endpoint_moved": ("Endpoint Moves", ("endpoint_moved",)),
    "security": ("Rogue and Watch-List Activity", ("rogue_detected", "watch_seen")),
}
EVENT_ACCENT = {"new_endpoint": "primary", "endpoint_moved": "info"}


@register_section(
    "udt_endpoint_activity", title="Endpoint Activity", category="Device Tracker",
    description=("New endpoints, port moves, rogue detections and watch-list sightings per day and per "
                 "switch, with the endpoints behind them (90-day history)."),
    scope="device", requires=("udt.view",), semantics="window",
    options=[
        top_n_option(15, 200),
        {"key": "events", "label": "Activity", "type": "select", "default": "all",
         "choices": [["all", "All activity"], ["new_endpoint", "New endpoints"],
                     ["endpoint_moved", "Moves"], ["security", "Rogue and watch-list"]]},
    ],
)
async def udt_endpoint_activity(ctx: SectionCtx) -> dict:
    sid = "udt_endpoint_activity"
    focus = ctx.opt("events")
    title, types = ACTIVITY_FOCUS.get(focus, ACTIVITY_FOCUS["all"])
    top_n = int(ctx.opt("top_n"))
    dev, dparams = ctx.pg_device_clause("ev.device_id")
    pf, _pt = ctx.prior_window()
    params: dict[str, Any] = {"f": _aware(ctx.frm), "t": _aware(ctx.to), "pf": _aware(pf),
                              "types": list(types), "org_tz": ctx.tz_name, "lim": top_n, **dparams}
    where = f"ev.created_at >= :f AND ev.created_at < :t AND ev.event_type = ANY(:types){dev}"
    noun = " and ".join(EVENT_LABELS[t].lower() for t in types) if len(types) < 4 else "endpoint activity"
    description = (f"{noun[0].upper() + noun[1:]} recorded by Device Tracker over the period, per day and "
                   "per switch.")

    totals = await _rows(ctx, f"""
        /* udt_activity_totals */
        SELECT ev.event_type, GROUPING(ev.event_type) AS is_total,
               count(*) FILTER (WHERE ev.created_at >= :f) AS cur,
               count(*) FILTER (WHERE ev.created_at < :f) AS prev,
               count(DISTINCT ev.endpoint_id) FILTER (WHERE ev.created_at >= :f) AS endpoints,
               count(DISTINCT ev.endpoint_id) FILTER (WHERE ev.created_at >= :f AND e.is_randomized)
        FROM udt_events ev LEFT JOIN udt_endpoints e ON e.id = ev.endpoint_id
        WHERE ev.created_at >= :pf AND ev.created_at < :t AND ev.event_type = ANY(:types){dev}
        GROUP BY GROUPING SETS ((ev.event_type), ())""", params)
    cur: dict[str, int] = {}
    prev: dict[str, int] = {}
    rand: dict[str, int] = {}
    involved = 0
    for r in totals:
        if _i(r[1]):
            involved = _i(r[4])
        elif r[0]:
            cur[r[0]], prev[r[0]], rand[r[0]] = _i(r[2]), _i(r[3]), _i(r[5])
    prior_ok = _aware(pf) >= _retention_cutoff()
    pruned = _aware(ctx.to) <= _retention_cutoff()

    notes: list[str] = []
    retention = ctx.window_note(RETENTION_DAYS, "Device Tracker activity")
    if retention:
        notes.append(retention)
    if ctx.device_ids:
        notes.append("Limited to activity reported by the selected switches; rogue detections for "
                     "endpoints without a known switch port are not included.")

    kpis = []
    for t in types:
        n = cur.get(t, 0)
        if pruned:
            kpis.append(_kpi(EVENT_LABELS[t], "—", "neutral", "Not measured: past the 90-day retention"))
            continue
        if t == "rogue_detected":
            accent = "danger" if n else "success"
        elif t == "watch_seen":
            accent = "warning" if n else "neutral"
        else:
            accent = EVENT_ACCENT[t]
        sub = _count_change(n, prev.get(t, 0) if prior_ok else None)
        if t in ("new_endpoint", "rogue_detected") and rand.get(t):
            sub = f"{sub} · {_count(rand[t], 'randomized MAC')}"
        kpis.append(_kpi(EVENT_LABELS[t], _n(n), accent, sub))
    if len(types) > 1 and not pruned:
        kpis.append(_kpi("Endpoints involved", _n(involved), "neutral", "Distinct endpoints"))

    if pruned:
        return _section(sid, title, description=description, kpis=kpis, notes=[
            "This period ended more than 90 days ago: Device Tracker keeps activity for 90 days, so it "
            "cannot be reported."] + [n for n in notes if n != retention])
    if not sum(cur.values()):
        empty = f"No {noun} in this period."
        return _section(sid, title, description=description, kpis=kpis, notes=[empty] + notes)

    daily = await _rows(ctx, f"""
        /* udt_activity_daily */
        SELECT ev.event_type, {_day_epoch_sql('ev.created_at')} AS day, count(*)
        FROM udt_events ev
        WHERE {where}
        GROUP BY 1, 2""", params)
    per_type: dict[str, dict[int, int]] = {t: {} for t in types}
    for r in daily:
        if r[0] in per_type and r[1] is not None:
            per_type[r[0]][int(r[1])] = per_type[r[0]].get(int(r[1]), 0) + _i(r[2])
    if len(types) > 1:
        totals_by_day: dict[int, int] = {}
        for per in per_type.values():
            for k, v in per.items():
                totals_by_day[k] = totals_by_day.get(k, 0) + v
        chart = {"kind": "stacked", "unit": "events", "color": "primary",
                 "points": _daily_points(ctx, totals_by_day),
                 "series": [{"name": EVENT_LABELS[t], "points": _daily_points(ctx, per_type[t])}
                            for t in types]}
    else:
        chart = {"kind": "bars", "unit": "events",
                 "color": "danger" if types[0] == "rogue_detected" else "primary",
                 "points": _daily_points(ctx, per_type[types[0]])}

    by_switch = await _rows(ctx, f"""
        /* udt_activity_by_switch */
        SELECT ev.device_id::text, COALESCE(d.hostname, host(d.ip_address)),
               count(*) FILTER (WHERE ev.event_type = 'new_endpoint'),
               count(*) FILTER (WHERE ev.event_type = 'endpoint_moved'),
               count(*) FILTER (WHERE ev.event_type = 'rogue_detected'),
               count(*) FILTER (WHERE ev.event_type = 'watch_seen'),
               count(*), max(ev.created_at), count(*) OVER ()
        FROM udt_events ev LEFT JOIN devices d ON d.id = ev.device_id
        WHERE {where}
        GROUP BY ev.device_id, d.hostname, d.ip_address
        ORDER BY count(*) DESC, max(ev.created_at) DESC
        LIMIT :lim""", params)
    col = {"new_endpoint": 2, "endpoint_moved": 3, "rogue_detected": 4, "watch_seen": 5}
    sw_total = _i(by_switch[0][8]) if by_switch else 0
    sw_headers = ["Switch"] + [EVENT_LABELS[t] for t in types] + (["Total"] if len(types) > 1 else []) + \
        ["Last event"]
    sw_rows = []
    for r in by_switch:
        name = r[1] or ("No switch (location unknown)" if r[0] is None else r[0][:8])
        sw_rows.append([name] + [_n(r[col[t]]) for t in types] + ([_n(r[6])] if len(types) > 1 else []) +
                       [ctx.fmt_short(r[7])])
    tables = [{"title": _truncated("Activity by switch", len(sw_rows), sw_total, "switches"),
               "headers": sw_headers,
               "styles": ["text"] + ["num"] * (len(sw_headers) - 2) + ["text"],
               "rows": sw_rows}]

    if focus == "new_endpoint":
        tables.append(await _activity_new(ctx, where, params))
    elif focus == "security":
        tables.append(await _activity_security(ctx, where, params))
    elif cur.get("endpoint_moved"):
        tables.append(await _activity_moves(ctx, where, params))
    notes.append("Each port is shown with its switch: interface indexes are local to the switch that "
                 "reported them.")
    return _section(sid, title, description=description, kpis=kpis,
                    charts=[{"title": f"{noun[0].upper() + noun[1:]} per day", "series": chart}],
                    tables=tables, notes=notes)


async def _activity_moves(ctx: SectionCtx, where: str, params: dict) -> dict:
    rows = await _rows(ctx, f"""
        /* udt_activity_moves */
        SELECT e.hostname, e.mac::text, e.vendor, e.is_randomized, count(*) AS moves,
               count(DISTINCT ev.device_id), max(ev.created_at),
               (array_agg(COALESCE(d.hostname, host(d.ip_address)) ORDER BY ev.created_at DESC))[1],
               (array_agg(di.if_name ORDER BY ev.created_at DESC))[1],
               (array_agg(ev.if_index ORDER BY ev.created_at DESC))[1],
               count(*) OVER ()
        FROM udt_events ev
        JOIN udt_endpoints e ON e.id = ev.endpoint_id
        LEFT JOIN devices d ON d.id = ev.device_id
        LEFT JOIN device_interfaces di ON di.device_id = ev.device_id AND di.if_index = ev.if_index
        WHERE {where} AND ev.event_type = 'endpoint_moved'
        GROUP BY ev.endpoint_id, e.hostname, e.mac, e.vendor, e.is_randomized
        ORDER BY moves DESC, max(ev.created_at) DESC
        LIMIT :lim""", params)
    total = _i(rows[0][10]) if rows else 0
    return {"title": _truncated("Endpoints that moved most often", len(rows), total, "endpoints"),
            "headers": ["Endpoint", "MAC address", "Vendor", "Moves", "Switches", "Last port", "Last move"],
            "styles": ["text", "mono", "text", "num", "num", "text", "text"],
            "rows": [[_endpoint(r[0], r[1]), r[1] or "—",
                      "Randomized MAC" if r[3] else (r[2] or "Unknown"), _n(r[4]), _n(r[5]),
                      f"{r[7] or '—'} · {_port(r[8], r[9])}", ctx.fmt_short(r[6])] for r in rows]}


async def _activity_new(ctx: SectionCtx, where: str, params: dict) -> dict:
    rows = await _rows(ctx, f"""
        /* udt_activity_new */
        SELECT e.hostname, e.mac::text, e.vendor, e.endpoint_type, e.is_randomized, host(e.ip_address),
               COALESCE(d.hostname, host(d.ip_address)), di.if_name, ev.if_index, ev.created_at,
               count(*) OVER ()
        FROM udt_events ev
        JOIN udt_endpoints e ON e.id = ev.endpoint_id
        LEFT JOIN devices d ON d.id = ev.device_id
        LEFT JOIN device_interfaces di ON di.device_id = ev.device_id AND di.if_index = ev.if_index
        WHERE {where} AND ev.event_type = 'new_endpoint'
        ORDER BY ev.created_at DESC, e.mac
        LIMIT :lim""", params)
    total = _i(rows[0][10]) if rows else 0
    title = f"Newest {len(rows):,} of {total:,} new endpoints" if total > len(rows) else "New endpoints"
    return {"title": title,
            "headers": ["Endpoint", "MAC address", "Vendor", "Type", "IP address", "First port",
                        "First seen"],
            "styles": ["text", "mono", "text", "text", "mono", "text", "text"],
            "rows": [[_endpoint(r[0], r[1]), r[1] or "—",
                      "Randomized MAC" if r[4] else (r[2] or "Unknown"), _type_label(r[3]), r[5] or "—",
                      f"{r[6]} · {_port(r[7], r[8])}" if r[6] else "—", ctx.fmt_short(r[9])]
                     for r in rows]}


async def _activity_security(ctx: SectionCtx, where: str, params: dict) -> dict:
    """Rogue and watch-list events summarised per endpoint (watch-list
    sightings repeat hourly while the endpoint stays connected)."""
    rows = await _rows(ctx, f"""
        /* udt_activity_security */
        SELECT e.hostname, e.mac::text, e.vendor, e.is_randomized, ev.event_type, count(*),
               min(ev.created_at), max(ev.created_at),
               (array_agg(COALESCE(d.hostname, host(d.ip_address)) ORDER BY ev.created_at DESC))[1],
               (array_agg(di.if_name ORDER BY ev.created_at DESC))[1],
               (array_agg(ev.if_index ORDER BY ev.created_at DESC))[1],
               count(*) OVER ()
        FROM udt_events ev
        JOIN udt_endpoints e ON e.id = ev.endpoint_id
        LEFT JOIN devices d ON d.id = ev.device_id
        LEFT JOIN device_interfaces di ON di.device_id = ev.device_id AND di.if_index = ev.if_index
        WHERE {where} AND ev.event_type IN ('rogue_detected', 'watch_seen')
        GROUP BY ev.endpoint_id, e.hostname, e.mac, e.vendor, e.is_randomized, ev.event_type
        ORDER BY max(ev.created_at) DESC
        LIMIT :lim""", params)
    total = _i(rows[0][11]) if rows else 0
    return {"title": _truncated("Rogue and watch-list events by endpoint", len(rows), total, "endpoints"),
            "headers": ["Endpoint", "MAC address", "Event", "Events", "First", "Last", "Last location"],
            "styles": ["text", "mono", "text", "num", "text", "text", "text"],
            "rows": [[_identity(r[0], r[2], r[3]), r[1] or "—",
                      EVENT_SINGULAR.get(r[4], r[4] or "—"), _n(r[5]), ctx.fmt_short(r[6]),
                      ctx.fmt_short(r[7]), f"{r[8]} · {_port(r[9], r[10])}" if r[8] else "Not located"]
                     for r in rows]}


# ─── Rogue and watched endpoints ────────────────────────────────────────────

ROGUE_SHOW = {
    "all": ("Rogue and Watched Endpoints", "(e.authorized = FALSE OR e.is_watched)"),
    "rogue": ("Rogue Endpoints", "e.authorized = FALSE"),
    "watched": ("Watched Endpoints", "e.is_watched"),
}


@register_section(
    "udt_rogue_watch", title="Rogue and Watched Endpoints", category="Device Tracker",
    description=("Endpoints flagged rogue (outside the allow list) or on the watch list, with their last "
                 "known switch port, as of report generation; randomized MACs are counted separately."),
    scope="device", requires=("udt.view",), semantics="snapshot",
    options=[
        top_n_option(50, 500),
        {"key": "show", "label": "Show", "type": "select", "default": "all",
         "choices": [["all", "Rogue and watched"], ["rogue", "Rogue only"], ["watched", "Watched only"]]},
        {"key": "include_randomized", "label": "List randomized MACs", "type": "bool", "default": False,
         "help": "Randomized (private) MACs are always counted; list them individually too."},
    ],
)
async def udt_rogue_watch(ctx: SectionCtx) -> dict:
    sid = "udt_rogue_watch"
    show = ctx.opt("show")
    title, flag_sql = ROGUE_SHOW.get(show, ROGUE_SHOW["all"])
    top_n, with_rand = int(ctx.opt("top_n")), bool(ctx.opt("include_randomized"))
    rand_sql = "" if with_rand else " AND NOT e.is_randomized"
    dev_l, dparams = ctx.pg_device_clause("l.device_id")
    in_scope = (f" AND EXISTS (SELECT 1 FROM udt_endpoint_locations l WHERE l.endpoint_id = e.id{dev_l})"
                if ctx.device_ids else "")
    online = f"e.last_seen > NOW() - INTERVAL '{ONLINE_MINUTES} minutes'"
    params: dict[str, Any] = {"lim": top_n, **dparams}

    rules = {r[0]: _i(r[1]) for r in await _rows(ctx, """
        /* udt_rules_enabled */
        SELECT list_type, count(*) FROM udt_rules WHERE enabled GROUP BY list_type""", {})}
    c = await _first(ctx, f"""
        /* udt_rogue_watch_counts */
        SELECT count(*) FILTER (WHERE e.authorized = FALSE AND NOT e.is_randomized),
               count(*) FILTER (WHERE e.authorized = FALSE AND e.is_randomized),
               count(*) FILTER (WHERE e.is_watched AND NOT e.is_randomized),
               count(*) FILTER (WHERE e.is_watched AND e.is_randomized),
               count(*) FILTER (WHERE e.authorized = FALSE AND NOT e.is_randomized AND {online}),
               count(*) FILTER (WHERE e.authorized = FALSE AND e.is_randomized AND {online}),
               count(*) FILTER (WHERE e.is_watched AND NOT e.is_randomized AND {online}),
               count(*) FILTER (WHERE e.is_watched AND e.is_randomized AND {online}),
               count(*) FILTER (WHERE {flag_sql}{rand_sql}),
               count(*) FILTER (WHERE {flag_sql} AND e.is_randomized)
        FROM udt_endpoints e
        WHERE NOT e.ignored AND (e.authorized = FALSE OR e.is_watched){in_scope}""", params)
    rogue, rogue_rand, watched, watched_rand, rogue_on, rogue_rand_on, watched_on, watched_rand_on, \
        listable, randomized = (_i(v) for v in (c or (0,) * 10))
    allow_rules, watch_rules = rules.get("allow", 0), rules.get("watch", 0)

    kpis = []
    if show in ("all", "rogue"):
        kpis.append(_kpi("Rogue endpoints", _n(rogue),
                         "danger" if rogue else ("success" if allow_rules else "neutral"),
                         f"{rogue_on:,} online now" if allow_rules else "Rogue detection is off"))
        kpis.append(_kpi("Randomized rogue", _n(rogue_rand), "warning" if rogue_rand else "neutral",
                         f"{rogue_rand_on:,} online now"))
    if show in ("all", "watched"):
        kpis.append(_kpi("Watched endpoints", _n(watched), "warning" if watched else "neutral",
                         f"{watched_on:,} online now"))
        kpis.append(_kpi("Randomized watched", _n(watched_rand), "neutral",
                         f"{watched_rand_on:,} online now"))
    kpis.append(_kpi("Allow-list rules", _n(allow_rules), "info" if allow_rules else "neutral",
                     _count(watch_rules, "watch-list rule")))

    notes: list[str] = []
    if not allow_rules and show != "watched":
        notes.append("No allow-list rule is enabled, so Device Tracker classifies no endpoint as rogue. Add "
                     "allow rules under Device Tracker → Rules to turn on rogue detection.")
    if not with_rand and randomized:
        notes.append(f"{_count(randomized, 'randomized (private) MAC address', 'randomized (private) MAC addresses')}"
                     " counted above but not listed; enable “List randomized MACs” to include them.")
    notes.append(f"Online means seen within the last {ONLINE_MINUTES} minutes. The location is the current "
                 "port session, or the last recorded one (port history is kept for 90 days); ports are "
                 "shown with their switch because interface indexes are local to each switch.")
    if ctx.device_ids:
        notes.append("Limited to endpoints located on the selected switches.")

    if not listable:
        what = {"rogue": "rogue", "watched": "watched"}.get(show, "rogue or watched")
        return _section(sid, title, description=_rogue_description(show), kpis=kpis,
                        notes=[f"No {what} endpoints right now."] + notes)

    loc_join = "JOIN" if ctx.device_ids else "LEFT JOIN"
    rows = await _rows(ctx, f"""
        /* udt_rogue_watch_list */
        SELECT e.hostname, e.mac::text, e.vendor, host(e.ip_address), (e.authorized = FALSE), e.is_watched,
               e.is_randomized, e.first_seen, e.last_seen, ({online}),
               COALESCE(sw.hostname, host(sw.ip_address)), loc.if_index, di.if_name, loc.vlan_id, loc.active
        FROM udt_endpoints e
        {loc_join} LATERAL (
            SELECT l.device_id, l.if_index, l.vlan_id, l.active FROM udt_endpoint_locations l
            WHERE l.endpoint_id = e.id{dev_l}
            ORDER BY l.active DESC, l.is_direct DESC, l.last_seen DESC LIMIT 1
        ) loc ON TRUE
        LEFT JOIN devices sw ON sw.id = loc.device_id
        LEFT JOIN device_interfaces di ON di.device_id = loc.device_id AND di.if_index = loc.if_index
        WHERE NOT e.ignored AND {flag_sql}{rand_sql}
        ORDER BY ({online}) DESC, e.last_seen DESC, e.mac
        LIMIT :lim""", params)

    def flag(r) -> str:
        parts = [p for p, on in (("Rogue", r[4]), ("Watched", r[5])) if on]
        out = ", ".join(parts).capitalize() if parts else "—"
        return out + (" (randomized)" if r[6] else "")

    def location(r) -> str:
        if not r[10]:
            return "Not located"
        out = f"{r[10]} · {_port(r[12], r[11])}"
        if r[13] is not None:
            out += f" · VLAN {r[13]}"
        return out if r[14] else out + " (last known)"

    return _section(
        sid, title, description=_rogue_description(show), kpis=kpis,
        tables=[{"title": _truncated("Endpoints", len(rows), listable, "endpoints"),
                 "headers": ["Endpoint", "MAC address", "IP address", "Flag", "Location", "First seen",
                             "Last seen", "Presence"],
                 "styles": ["text", "mono", "mono", "text", "text", "text", "text", "status"],
                 "rows": [[_identity(r[0], r[2], r[6]), r[1] or "—", r[3] or "—", flag(r), location(r),
                           ctx.fmt_short(r[7]), ctx.fmt_short(r[8]), "active" if r[9] else "inactive"]
                          for r in rows]}],
        notes=notes,
    )


def _rogue_description(show: str) -> str:
    what = {"rogue": "flagged rogue (matching no allow-list rule)", "watched": "on the watch list"}.get(
        show, "flagged rogue (matching no allow-list rule) or on the watch list")
    return f"Endpoints {what}, with their last known switch port, as of report generation."


# ─── Endpoint inventory ─────────────────────────────────────────────────────

SEEN_WITHIN = {"1": "seen in the last 24 hours", "7": "seen in the last 7 days",
               "30": "seen in the last 30 days", "90": "seen in the last 90 days",
               "all": "on record"}


@register_section(
    "udt_endpoint_inventory", title="Endpoint Inventory by Type and Vendor", category="Device Tracker",
    description=("Tracked endpoints by type and by OUI vendor, with the share of randomized MACs and "
                 "unidentified endpoints, as of report generation."),
    scope="fleet", requires=("udt.view",), semantics="snapshot",
    options=[
        top_n_option(15, 200),
        {"key": "seen_within", "label": "Endpoints", "type": "select", "default": "30",
         "choices": [["1", "Seen in the last 24 hours"], ["7", "Seen in the last 7 days"],
                     ["30", "Seen in the last 30 days"], ["90", "Seen in the last 90 days"],
                     ["all", "All endpoints on record"]]},
    ],
)
async def udt_endpoint_inventory(ctx: SectionCtx) -> dict:
    sid, title = "udt_endpoint_inventory", "Endpoint Inventory by Type and Vendor"
    top_n, seen = int(ctx.opt("top_n")), ctx.opt("seen_within")
    params: dict[str, Any] = {"lim": top_n}
    seen_sql = ""
    if seen != "all":
        seen_sql = " AND e.last_seen >= NOW() - make_interval(days => :days)"
        params["days"] = int(seen)
    online = f"e.last_seen > NOW() - INTERVAL '{ONLINE_MINUTES} minutes'"
    scope_words = SEEN_WITHIN.get(seen, "on record")
    description = (f"Endpoints {scope_words} (ignored endpoints excluded) by type and vendor, as of "
                   "report generation.")
    types = await _rows(ctx, f"""
        /* udt_inventory_types */
        SELECT e.endpoint_type, count(*), count(*) FILTER (WHERE e.is_randomized),
               count(*) FILTER (WHERE {online}),
               count(*) FILTER (WHERE e.vendor IS NULL AND NOT e.is_randomized)
        FROM udt_endpoints e
        WHERE NOT e.ignored{seen_sql}
        GROUP BY e.endpoint_type
        ORDER BY count(*) DESC, e.endpoint_type""", params)
    total = sum(_i(r[1]) for r in types)
    if not total:
        return _section(sid, title, description=description,
                        notes=[f"No endpoints {scope_words}."] + _fleet_note(ctx))
    rand = sum(_i(r[2]) for r in types)
    on = sum(_i(r[3]) for r in types)
    no_vendor = sum(_i(r[4]) for r in types)
    unclassified = sum(_i(r[1]) for r in types if (r[0] or "unknown") == "unknown")

    vendors = await _rows(ctx, f"""
        /* udt_inventory_vendors */
        SELECT e.vendor, count(*), mode() WITHIN GROUP (ORDER BY e.endpoint_type),
               count(*) FILTER (WHERE {online}), count(*) OVER ()
        FROM udt_endpoints e
        WHERE NOT e.ignored AND NOT e.is_randomized AND e.vendor IS NOT NULL{seen_sql}
        GROUP BY e.vendor
        ORDER BY count(*) DESC, e.vendor
        LIMIT :lim""", params)
    vendor_total = _i(vendors[0][4]) if vendors else 0

    tables = [{"title": "Endpoints by type",
               "headers": ["Type", "Endpoints", "Share", "Online now", "Randomized MACs", "Unknown vendor"],
               "styles": ["text", "num", "pct-bar", "num", "num", "num"],
               "rows": [[_type_label(r[0]), _n(r[1]), _share(r[1], total), _n(r[3]), _n(r[2]), _n(r[4])]
                        for r in types]}]
    if vendors:
        tables.append({"title": _truncated("Endpoints by vendor", len(vendors), vendor_total, "vendors"),
                       "headers": ["Vendor", "Endpoints", "Share", "Most common type", "Online now"],
                       "styles": ["text", "num", "pct-bar", "text", "num"],
                       "rows": [[r[0], _n(r[1]), _share(r[1], total), _type_label(r[2]), _n(r[3])]
                                for r in vendors]})
    charts = [{"title": "Endpoints by type",
               "series": {"kind": "donut", "unit": "endpoints",
                          "points": [{"label": _type_label(r[0]), "value": _i(r[1])} for r in types
                                     if _i(r[1])]}}]
    if vendors:
        charts.append({"title": "Top vendors",
                       "series": {"kind": "rank", "unit": "endpoints", "color": "primary",
                                  "points": [{"label": r[0], "value": _i(r[1])} for r in vendors[:10]]}})
    notes = ["Vendor comes from the MAC address's IEEE OUI prefix. Randomized (private) MACs, used by "
             "phones and laptops for privacy, have no vendor and are counted separately; unknown vendor "
             "means a globally unique MAC whose prefix is not in the OUI table."]
    return _section(
        sid, title, description=description,
        kpis=[
            _kpi("Endpoints", _n(total), "primary", f"{on:,} online now"),
            _kpi("Randomized MACs", _share(rand, total), "info" if rand else "neutral",
                 _count(rand, "private address", "private addresses")),
            _kpi("Unknown vendor", _share(no_vendor, total), "warning" if no_vendor else "success",
                 _count(no_vendor, "endpoint")),
            _kpi("Unclassified type", _share(unclassified, total), "warning" if unclassified else "success",
                 _count(unclassified, "endpoint")),
            _kpi("Vendors", _n(vendor_total), "neutral"),
        ],
        charts=charts, tables=tables, notes=notes + _fleet_note(ctx),
    )


# ─── IP address reassignments ───────────────────────────────────────────────

FAMILY_SQL = {"ipv4": " AND family({a}.ip) = 4", "ipv6": " AND family({a}.ip) = 6", "all": ""}
FAMILY_WORDS = {"ipv4": "IPv4", "ipv6": "IPv6", "all": "IPv4 and IPv6"}


@register_section(
    "udt_ip_reassignments", title="IP Address Reassignments", category="Device Tracker",
    description=("Endpoints that moved to a new IP address and addresses taken over by another endpoint "
                 "in the period, with the strength of the evidence behind each binding."),
    scope="device", requires=("udt.view",), semantics="window",
    options=[
        top_n_option(50, 500),
        {"key": "family", "label": "Addresses", "type": "select", "default": "ipv4",
         "choices": [["ipv4", "IPv4"], ["ipv6", "IPv6"], ["all", "IPv4 and IPv6"]],
         "help": "IPv6 temporary (privacy) addresses rotate by design."},
        {"key": "include_concurrent", "label": "Include additional (concurrent) addresses", "type": "bool",
         "default": False},
    ],
)
async def udt_ip_reassignments(ctx: SectionCtx) -> dict:
    sid, title = "udt_ip_reassignments", "IP Address Reassignments"
    top_n, family = int(ctx.opt("top_n")), ctx.opt("family")
    with_concurrent = bool(ctx.opt("include_concurrent"))
    fam = FAMILY_SQL.get(family, FAMILY_SQL["ipv4"])
    dev_h, dparams = ctx.pg_device_clause("h.reporting_device_id")
    dev_p, _ = ctx.pg_device_clause("p.reporting_device_id")
    dev_o, _ = ctx.pg_device_clause("o.reporting_device_id")
    dev_x, _ = ctx.pg_device_clause("x.reporting_device_id")
    params: dict[str, Any] = {"f": _aware(ctx.frm), "t": _aware(ctx.to), "lim": top_n,
                              "with_concurrent": with_concurrent, **dparams}
    fam_words = FAMILY_WORDS.get(family, "IPv4")
    description = (f"{fam_words} address changes and reuse reported by ARP/ND over the period, with the "
                   "evidence behind each binding.")
    # Observation periods that began in the window, summarised per endpoint and
    # address (repeated periods are counted, never deleted or de-duplicated).
    cte = f"""
        WITH grouped AS (
            SELECT h.endpoint_id, h.ip, min(h.first_seen) AS started, max(h.last_seen) AS last_seen,
                   count(*) AS periods,
                   (array_agg(h.reporting_device_id ORDER BY h.first_seen, h.id))[1] AS reporter_id
            FROM udt_ip_history h
            WHERE h.first_seen >= :f AND h.first_seen < :t{fam.format(a='h')}{dev_h}
            GROUP BY h.endpoint_id, h.ip
        ), classified AS (
            SELECT g.*, prev.ip AS prev_ip, holder.endpoint_id AS prev_endpoint_id,
                   CASE WHEN prev.ip IS NULL THEN NULL
                        WHEN prev.last_seen < g.last_seen - INTERVAL '{REPLACED_GAP}' THEN 'replaced'
                        ELSE 'concurrent' END AS kind
            FROM grouped g
            LEFT JOIN LATERAL (
                SELECT p.ip, p.last_seen FROM udt_ip_history p
                WHERE p.endpoint_id = g.endpoint_id AND p.ip <> g.ip AND family(p.ip) = family(g.ip)
                  AND p.first_seen < g.started{dev_p}
                ORDER BY p.last_seen DESC, p.id DESC LIMIT 1
            ) prev ON TRUE
            LEFT JOIN LATERAL (
                SELECT o.endpoint_id FROM udt_ip_history o
                WHERE o.ip = g.ip AND o.endpoint_id <> g.endpoint_id AND o.first_seen < g.started{dev_o}
                ORDER BY o.last_seen DESC, o.id DESC LIMIT 1
            ) holder ON TRUE
        ), changes AS (
            SELECT c.*, ev.confirmed, COALESCE(ev.bindings, 0) AS bindings,
                   (c.kind = 'replaced' OR c.prev_endpoint_id IS NOT NULL
                    OR (CAST(:with_concurrent AS boolean) AND c.kind = 'concurrent')) AS included
            FROM classified c
            LEFT JOIN LATERAL (
                SELECT bool_or({CONFIRMED_SQL.format(a='x')}) AS confirmed, count(*) AS bindings
                FROM udt_ip_evidence x
                WHERE x.endpoint_id = c.endpoint_id AND x.ip = c.ip{dev_x}
            ) ev ON TRUE
            WHERE c.kind IS NOT NULL OR c.prev_endpoint_id IS NOT NULL
        )"""
    s = await _first(ctx, cte + """
        /* udt_ip_changes_summary */
        SELECT count(*) FILTER (WHERE included),
               count(*) FILTER (WHERE included AND kind = 'replaced'),
               count(*) FILTER (WHERE included AND prev_endpoint_id IS NOT NULL),
               count(DISTINCT endpoint_id) FILTER (WHERE included),
               count(*) FILTER (WHERE included AND confirmed),
               count(*) FILTER (WHERE included AND NOT COALESCE(confirmed, FALSE) AND bindings > 0),
               count(*) FILTER (WHERE included AND bindings = 0),
               count(*) FILTER (WHERE NOT included AND kind = 'concurrent'),
               count(*) FILTER (WHERE included AND kind = 'concurrent')
        FROM changes""", params)
    included, replaced, reused, endpoints, confirmed, unconfirmed, history_only, concurrent_hidden, \
        concurrent = (_i(v) for v in (s or (0,) * 9))

    shared = await _rows(ctx, f"""
        /* udt_ip_shared_addresses */
        WITH shared AS (
            SELECT o.ip, count(DISTINCT o.endpoint_id) AS endpoints,
                   count(DISTINCT o.reporting_device_id) AS reporters,
                   (array_agg(DISTINCT e.mac::text ORDER BY e.mac::text))[1:4] AS macs,
                   max(o.last_seen) AS last_seen, count(*) AS periods
            FROM udt_ip_history o JOIN udt_endpoints e ON e.id = o.endpoint_id
            WHERE o.first_seen < :t AND o.last_seen >= :f{fam.format(a='o')}{dev_o}
            GROUP BY o.ip
            HAVING count(DISTINCT o.endpoint_id) > 1
        )
        SELECT host(sh.ip), sh.endpoints, sh.reporters, sh.macs, sh.last_seen, sh.periods, cf.n,
               count(*) OVER ()
        FROM shared sh
        LEFT JOIN LATERAL (
            SELECT count(DISTINCT x.endpoint_id) AS n FROM udt_ip_evidence x
            WHERE x.ip = sh.ip AND {CONFIRMED_SQL.format(a='x')}{dev_x}
        ) cf ON TRUE
        ORDER BY sh.endpoints DESC, sh.last_seen DESC
        LIMIT :lim""", params)
    shared_total = _i(shared[0][7]) if shared else 0

    notes = ["ARP/ND sightings do not establish exclusive ownership of an address: proxy ARP, separate "
             "VRFs, DHCP reuse and stale caches all report one address for several endpoints. Treat "
             "shared addresses as leads, not proven conflicts.",
             "Evidence: Confirmed means three sightings within 24 hours from one reporter, interface and "
             "source (each keeps its three most recent sightings); Unconfirmed means fewer; History only "
             "means the observation periods were recorded without per-reporter evidence. Repeat evidence "
             "is never inferred from the number of observation periods.",
             f"Changed means the previous address stopped being reported more than {REPLACED_GAP} before "
             "the new one was last reported; addresses reported together are additional (concurrent)."]
    if concurrent_hidden:
        notes.append(f"{_count(concurrent_hidden, 'additional address', 'additional addresses')} reported "
                     "alongside an endpoint's existing address not listed; enable “Include additional "
                     "(concurrent) addresses” to list them.")
    if family == "ipv4":
        notes.append("IPv6 is excluded: temporary (privacy) IPv6 addresses rotate by design.")
    if ctx.device_ids:
        notes.append("Limited to addresses reported by the selected devices, and to their evidence.")

    kpis = [
        _kpi("Address changes", _n(replaced), "primary", "Endpoint moved to a new address"),
        _kpi("Addresses reused", _n(reused), "info" if reused else "neutral",
             "Previously reported for another endpoint"),
        _kpi("Endpoints affected", _n(endpoints), "neutral",
             _count(concurrent, "additional address", "additional addresses") + " listed"
             if concurrent else None),
        _kpi("Confirmed", _n(confirmed), "success" if confirmed else "neutral",
             f"of {included:,} · 3 sightings in 24 h" if included else "3 sightings in 24 h"),
        _kpi("Unconfirmed / history only", f"{unconfirmed:,} / {history_only:,}",
             "warning" if unconfirmed or history_only else "neutral", "Weaker evidence"),
        _kpi("Shared addresses", _n(shared_total), "warning" if shared_total else "success",
             "Reported for 2+ endpoints in the period"),
    ]
    tables: list[dict] = []
    if included:
        rows = await _rows(ctx, cte + """
            /* udt_ip_changes_detail */
            SELECT e.hostname, e.mac::text, host(c.prev_ip), host(c.ip), c.kind, pe.mac::text,
                   c.started, COALESCE(rd.hostname, host(rd.ip_address)), c.confirmed, c.bindings,
                   c.periods, e.vendor, e.is_randomized
            FROM changes c
            JOIN udt_endpoints e ON e.id = c.endpoint_id
            LEFT JOIN udt_endpoints pe ON pe.id = c.prev_endpoint_id
            LEFT JOIN devices rd ON rd.id = c.reporter_id
            WHERE c.included
            ORDER BY c.started DESC, e.mac
            LIMIT :lim""", params)

        def change(r) -> str:
            parts = []
            if r[4] == "replaced":
                parts.append("Changed")
            elif r[4] == "concurrent":
                parts.append("Additional")
            if r[5]:
                parts.append("Reused")
            return ", ".join(parts).capitalize() or "—"

        def evidence(r) -> str:
            if r[8]:
                return "Confirmed"
            return "Unconfirmed" if _i(r[9]) else "History only"

        def address(r) -> str:
            new = r[3] or "—"
            return f"{r[2]} → {new}" if r[2] else new

        tables.append({
            "title": _truncated("Address changes, newest first", len(rows), included, "changes"),
            "headers": ["Endpoint", "MAC address", "Address", "Change", "Previous endpoint", "First seen",
                        "Reported by", "Evidence"],
            "styles": ["text", "mono", "mono", "text", "mono", "text", "text", "text"],
            "rows": [[_identity(r[0], r[11], r[12]), r[1] or "—", address(r), change(r), r[5] or "—",
                      ctx.fmt_short(r[6]) + (f" ({_i(r[10])} periods)" if _i(r[10]) > 1 else ""),
                      r[7] or "—", evidence(r)] for r in rows]})
    if shared:
        def macs(r) -> str:
            listed = [m for m in (r[3] or []) if m][:2]
            more = _i(r[1]) - len(listed)
            return ", ".join(listed) + (f" +{more} more" if more > 0 else "")

        tables.append({
            "title": _truncated("Addresses reported for more than one endpoint", len(shared), shared_total,
                                "addresses"),
            "headers": ["Address", "Endpoints", "MAC addresses", "Reporters", "Confirmed bindings",
                        "Observation periods", "Last seen"],
            "styles": ["mono", "num", "mono", "num", "num", "num", "text"],
            "rows": [[r[0] or "—", _n(r[1]), macs(r), _n(r[2]), _n(r[6]), _n(r[5]), ctx.fmt_short(r[4])]
                     for r in shared]})
    if not tables:
        notes.insert(0, f"No {fam_words.replace(' and ', ' or ')} address changes or shared addresses in "
                        "this period.")
    return _section(sid, title, description=description, kpis=kpis, tables=tables, notes=notes)


# ─── User logins ────────────────────────────────────────────────────────────

LOGIN_GROUPS = {"user_endpoint": "User Logins by Endpoint", "user": "Logins by User",
                "endpoint": "Logins by Endpoint"}


@register_section(
    "udt_user_logins", title="User Logins by Endpoint", category="Device Tracker",
    description=("Directory logon events per user and endpoint over the period, from the domain "
                 "controllers Device Tracker polls (personal data: needs the view-users permission)."),
    scope="fleet", requires=("udt.view", "udt.view_users"), semantics="window",
    options=[
        top_n_option(25, 500),
        {"key": "group_by", "label": "List", "type": "select", "default": "user_endpoint",
         "choices": [["user_endpoint", "User and endpoint"], ["user", "Users"], ["endpoint", "Endpoints"]]},
        {"key": "show_summary", "label": "Show totals and daily chart", "type": "bool", "default": True},
    ],
)
async def udt_user_logins(ctx: SectionCtx) -> dict:
    sid = "udt_user_logins"
    group_by = ctx.opt("group_by")
    title = LOGIN_GROUPS.get(group_by, LOGIN_GROUPS["user_endpoint"])
    top_n, summary = int(ctx.opt("top_n")), bool(ctx.opt("show_summary"))
    params: dict[str, Any] = {"f": _aware(ctx.frm), "t": _aware(ctx.to), "lim": top_n,
                              "org_tz": ctx.tz_name}
    window = "l.event_time >= :f AND l.event_time < :t"
    description = ("Domain logon events (4624 logon, 4768/4769 Kerberos tickets) correlated to tracked "
                   "endpoints over the period.")
    notes: list[str] = []
    retention = ctx.window_note(RETENTION_DAYS, "User logon history")
    if retention:
        notes.append(retention)

    t = await _first(ctx, f"""
        /* udt_logins_totals */
        SELECT count(*), count(DISTINCT lower(l.user_name)), count(DISTINCT l.endpoint_id),
               count(*) FILTER (WHERE l.endpoint_id IS NULL), count(DISTINCT l.dc_id)
        FROM udt_user_logins l WHERE {window}""", params)
    logins, users, endpoints, unmatched, dcs = (_i(v) for v in (t or (0,) * 5))
    if _aware(ctx.to) <= _retention_cutoff():
        return _section(sid, title, description=description, notes=[
            "This period ended more than 90 days ago: user logon history is kept for 90 days, so it cannot "
            "be reported."] + _fleet_note(ctx))
    if not logins:
        return _section(sid, title, description=description,
                        notes=["No user logons in this period. Logons are collected from the domain "
                               "controllers configured under Device Tracker."] + notes + _fleet_note(ctx))

    kpis: list[dict] = []
    charts: list[dict] = []
    if summary:
        kpis = [
            _kpi("Logon events", _n(logins), "primary",
                 f"From {_count(dcs, 'domain controller')}" if dcs else None),
            _kpi("Users", _n(users), "info"),
            _kpi("Endpoints", _n(endpoints), "info", "Matched to a tracked endpoint"),
            _kpi("Not matched", _share(unmatched, logins), "warning" if unmatched else "success",
                 f"{_count(unmatched, 'logon')} without a tracked endpoint"),
        ]
        daily = await _rows(ctx, f"""
            /* udt_logins_daily */
            SELECT {_day_epoch_sql('l.event_time')} AS day, count(*)
            FROM udt_user_logins l WHERE {window}
            GROUP BY 1""", params)
        counts = {int(r[0]): _i(r[1]) for r in daily if r[0] is not None}
        charts = [{"title": "Logon events per day",
                   "series": {"kind": "bars", "unit": "logons", "color": "primary",
                              "points": _daily_points(ctx, counts)}}]

    if group_by == "user":
        rows = await _rows(ctx, f"""
            /* udt_logins_by_user */
            SELECT max(l.user_name), max(l.user_domain), count(*), count(DISTINCT l.endpoint_id),
                   count(DISTINCT l.ip), max(l.event_time),
                   (array_agg(COALESCE(e.hostname, l.hostname, host(l.ip), e.mac::text)
                              ORDER BY l.event_time DESC))[1],
                   count(*) OVER ()
            FROM udt_user_logins l LEFT JOIN udt_endpoints e ON e.id = l.endpoint_id
            WHERE {window}
            GROUP BY lower(l.user_name)
            ORDER BY count(*) DESC, max(l.event_time) DESC
            LIMIT :lim""", params)
        total = _i(rows[0][7]) if rows else 0
        table = {"title": _truncated("Logons by user", len(rows), total, "users"),
                 "headers": ["User", "Logons", "Endpoints", "IP addresses", "Last logon", "Last endpoint"],
                 "styles": ["text", "num", "num", "num", "text", "text"],
                 "rows": [[_user(r[0], r[1]), _n(r[2]), _n(r[3]), _n(r[4]), ctx.fmt_short(r[5]), r[6] or "—"]
                          for r in rows]}
    elif group_by == "endpoint":
        rows = await _rows(ctx, f"""
            /* udt_logins_by_endpoint */
            SELECT e.hostname, e.mac::text, host(e.ip_address), count(*),
                   count(DISTINCT lower(l.user_name)), max(l.event_time),
                   (array_agg(l.user_name ORDER BY l.event_time DESC))[1],
                   (array_agg(l.user_domain ORDER BY l.event_time DESC))[1],
                   count(*) OVER ()
            FROM udt_user_logins l JOIN udt_endpoints e ON e.id = l.endpoint_id
            WHERE {window}
            GROUP BY l.endpoint_id, e.hostname, e.mac, e.ip_address
            ORDER BY count(DISTINCT lower(l.user_name)) DESC, count(*) DESC, max(l.event_time) DESC
            LIMIT :lim""", params)
        total = _i(rows[0][8]) if rows else 0
        table = {"title": _truncated("Logons by endpoint, most users first", len(rows), total, "endpoints"),
                 "headers": ["Endpoint", "MAC address", "IP address", "Users", "Logons", "Last user",
                             "Last logon"],
                 "styles": ["text", "mono", "mono", "num", "num", "text", "text"],
                 "rows": [[_endpoint(r[0], r[1]), r[1] or "—", r[2] or "—", _n(r[4]), _n(r[3]),
                           _user(r[6], r[7]), ctx.fmt_short(r[5])] for r in rows]}
    else:
        rows = await _rows(ctx, f"""
            /* udt_logins_by_pair */
            WITH x AS (
                SELECT lower(l.user_name) AS ukey, l.user_name, l.user_domain, l.endpoint_id, l.ip,
                       l.hostname, l.event_time,
                       COALESCE(l.endpoint_id::text, 'ip:' || host(l.ip), 'host:' || lower(l.hostname),
                                'unknown') AS ekey
                FROM udt_user_logins l WHERE {window}
            )
            SELECT max(x.user_name), max(x.user_domain), max(e.hostname), max(e.mac::text),
                   max(host(COALESCE(e.ip_address, x.ip))), max(x.hostname), count(*),
                   min(x.event_time), max(x.event_time), bool_or(x.endpoint_id IS NOT NULL),
                   count(*) OVER ()
            FROM x LEFT JOIN udt_endpoints e ON e.id = x.endpoint_id
            GROUP BY x.ukey, x.ekey
            ORDER BY count(*) DESC, max(x.event_time) DESC
            LIMIT :lim""", params)
        total = _i(rows[0][10]) if rows else 0

        def where_(r) -> str:
            name = (r[2] or r[5] or "").strip()
            if r[9]:
                return name or r[3] or "—"
            return f"{name or r[4] or 'Unknown'} (not matched)"

        table = {"title": _truncated("Logons by user and endpoint", len(rows), total, "user/endpoint pairs"),
                 "headers": ["User", "Endpoint", "MAC address", "IP address", "Logons", "First logon",
                             "Last logon"],
                 "styles": ["text", "text", "mono", "mono", "num", "text", "text"],
                 "rows": [[_user(r[0], r[1]), where_(r), r[3] or "—", r[4] or "—", _n(r[6]),
                           ctx.fmt_short(r[7]), ctx.fmt_short(r[8])] for r in rows]}
    notes.append("Counts are authentication events recorded by the domain controllers, not interactive "
                 "sessions; machine and service accounts are excluded at collection. Logons are matched to "
                 "an endpoint by its active IP binding, then by workstation name.")
    return _section(sid, title, description=description, kpis=kpis, charts=charts, tables=[table],
                    notes=notes + _fleet_note(ctx))


# ─── VLAN summary ───────────────────────────────────────────────────────────

VLAN_GROUPS = {
    # breakdown: (udt_vlans group, sessions group, device column, join condition)
    "vlan": ("v.vlan_id", "l.vlan_id", "NULL::uuid", "u.vlan_id = d.vlan_id"),
    "switch": ("v.device_id, v.vlan_id", "l.device_id, l.vlan_id", "COALESCE(d.device_id, u.device_id)",
               "u.device_id = d.device_id AND u.vlan_id = d.vlan_id"),
}


@register_section(
    "udt_vlan_summary", title="VLAN Summary", category="Device Tracker",
    description=("VLANs known to the tracked switches with the switch ports and endpoints currently active "
                 "on each, as of report generation."),
    scope="device", requires=("udt.view",), semantics="snapshot",
    options=[
        top_n_option(50, 1000),
        {"key": "breakdown", "label": "Group by", "type": "select", "default": "vlan",
         "choices": [["vlan", "VLAN (all switches)"], ["switch", "Switch and VLAN"]]},
        {"key": "include_empty", "label": "Include VLANs without active endpoints", "type": "bool",
         "default": True},
    ],
)
async def udt_vlan_summary(ctx: SectionCtx) -> dict:
    sid = "udt_vlan_summary"
    breakdown = ctx.opt("breakdown") if ctx.opt("breakdown") in VLAN_GROUPS else "vlan"
    title = "VLAN Summary" if breakdown == "vlan" else "VLANs by Switch"
    top_n, include_empty = int(ctx.opt("top_n")), bool(ctx.opt("include_empty"))
    gv, gl, dev_sel, join = VLAN_GROUPS[breakdown]
    dev_v, dparams = ctx.pg_device_clause("v.device_id")
    dev_l, _ = ctx.pg_device_clause("l.device_id")
    params = dict(dparams)
    description = ("Endpoints and switch ports active on each VLAN"
                   + (" across all tracked switches" if breakdown == "vlan" else ", per switch")
                   + ", as of report generation.")
    sessions = f"""FROM udt_endpoint_locations l JOIN udt_endpoints e ON e.id = l.endpoint_id
            WHERE l.active AND NOT e.ignored{dev_l}"""
    t = await _first(ctx, f"""
        /* udt_vlan_totals */
        SELECT count(DISTINCT l.endpoint_id) FILTER (WHERE l.vlan_id IS NOT NULL),
               count(DISTINCT l.endpoint_id) FILTER (WHERE l.vlan_id IS NULL)
        {sessions}""", params)
    on_vlans, no_vlan = (_i(v) for v in (t or (0, 0)))
    rows = await _rows(ctx, f"""
        /* udt_vlan_summary */
        WITH defs AS (
            SELECT {gv}, count(DISTINCT v.device_id) AS switches,
                   mode() WITHIN GROUP (ORDER BY v.name) AS name, count(DISTINCT v.name) AS names,
                   max(v.last_seen) AS last_seen
            FROM udt_vlans v WHERE TRUE{dev_v}
            GROUP BY {gv}
        ), used AS (
            SELECT {gl}, count(DISTINCT l.endpoint_id) AS endpoints,
                   count(DISTINCT l.endpoint_id) FILTER (WHERE e.is_randomized) AS randomized,
                   count(DISTINCT l.device_id::text || '/' || l.if_index::text) FILTER (WHERE l.is_direct)
                       AS ports,
                   count(DISTINCT l.device_id) AS switches
            {sessions} AND l.vlan_id IS NOT NULL
            GROUP BY {gl}
        ), k AS (
            SELECT {dev_sel} AS dev_id, COALESCE(d.vlan_id, u.vlan_id) AS vlan_id, d.name,
                   COALESCE(d.names, 0) AS names, GREATEST(COALESCE(d.switches, 0), COALESCE(u.switches, 0))
                       AS switches,
                   COALESCE(u.endpoints, 0) AS endpoints, COALESCE(u.randomized, 0) AS randomized,
                   COALESCE(u.ports, 0) AS ports, d.last_seen
            FROM defs d FULL OUTER JOIN used u ON {join}
        )
        SELECT k.dev_id::text, COALESCE(sw.hostname, host(sw.ip_address)), k.vlan_id, k.name, k.names,
               k.switches, k.endpoints, k.randomized, k.ports, k.last_seen
        FROM k LEFT JOIN devices sw ON sw.id = k.dev_id
        ORDER BY k.endpoints DESC, k.vlan_id""", params)
    scope_note = ["Limited to the selected switches."] if ctx.device_ids else []
    if not rows:
        return _section(sid, title, description=description,
                        notes=["No VLANs or VLAN-tagged port sessions recorded by Device Tracker."]
                        + scope_note)

    vlan_ids = {r[2] for r in rows}
    used_ids = {r[2] for r in rows if _i(r[6])}
    if breakdown == "vlan":
        unused = [r for r in rows if not _i(r[6])]
        mismatched = [r for r in rows if _i(r[4]) > 1]
    else:
        unused = [r for r in rows if not _i(r[6])]
        names: dict[Any, set] = {}
        for r in rows:
            if r[3]:
                names.setdefault(r[2], set()).add(r[3])
        mismatched = [k for k, v in names.items() if len(v) > 1]
    listed = [r for r in rows if include_empty or _i(r[6])]
    shown = listed[:top_n]
    biggest = rows[0] if _i(rows[0][6]) else None

    def label(r) -> str:
        return f"VLAN {r[2]}" + (f" · {r[3]}" if r[3] else "")

    def name(r) -> str:
        if not r[3]:
            return "—"
        extra = _i(r[4]) - 1
        return r[3] + (f" (+{extra} other name{'s' if extra > 1 else ''})" if extra > 0 else "")

    if breakdown == "vlan":
        table = {"title": _truncated("Endpoints and ports per VLAN", len(shown), len(listed), "VLANs"),
                 "headers": ["VLAN", "Name", "Switches", "Ports with endpoints", "Endpoints", "Randomized MACs",
                             "Share of endpoints"],
                 "styles": ["num", "text", "num", "num", "num", "num", "pct-bar"],
                 "rows": [[str(r[2]), name(r), _n(r[5]), _n(r[8]), _n(r[6]), _n(r[7]),
                           _share(r[6], on_vlans)] for r in shown]}
    else:
        table = {"title": _truncated("VLANs per switch", len(shown), len(listed), "switch VLANs"),
                 "headers": ["Switch", "VLAN", "Name", "Ports with endpoints", "Endpoints", "Randomized MACs",
                             "Last polled"],
                 "styles": ["text", "num", "text", "num", "num", "num", "text"],
                 "rows": [[r[1] or "—", str(r[2]), r[3] or "—", _n(r[8]), _n(r[6]), _n(r[7]),
                           ctx.fmt_short(r[9])] for r in shown]}
    notes = ["Endpoints are counted once per VLAN from their active port sessions (an endpoint learned on "
             "several switches counts once); ports are access ports where the endpoint is directly "
             "attached."]
    if breakdown == "vlan":
        notes.append("VLANs are grouped by ID across switches; the same ID on switches that are not "
                     "connected may be separate broadcast domains.")
    if no_vlan:
        notes.append(f"{_count(no_vlan, 'active endpoint')} on ports that report no VLAN, not counted per "
                     "VLAN.")
    if not include_empty and unused:
        notes.append(f"{_count(len(unused), 'VLAN')} without active endpoints not listed.")
    return _section(
        sid, title, description=description,
        kpis=[
            _kpi("VLANs", _n(len(vlan_ids)), "primary",
                 f"On {_count(len({r[0] for r in rows}), 'switch', 'switches')}"
                 if breakdown == "switch" else None),
            _kpi("VLANs in use", _n(len(used_ids)), "success" if used_ids else "neutral",
                 "With active endpoints"),
            _kpi("Endpoints on VLANs", _n(on_vlans), "info",
                 f"{no_vlan:,} without VLAN information" if no_vlan else None),
            _kpi("VLANs without endpoints" if breakdown == "vlan" else "Switch VLANs without endpoints",
                 _n(len(unused)), "warning" if unused else "success", "Clean-up candidates"),
            _kpi("Largest VLAN", label(biggest) if biggest else "—", "neutral",
                 _count(biggest[6], "endpoint") if biggest else "Not measured"),
            _kpi("Name mismatches", _n(len(mismatched)), "warning" if mismatched else "success",
                 "Same VLAN ID, different names"),
        ],
        charts=[{"title": "Top VLANs by endpoints",
                 "series": {"kind": "rank", "unit": "endpoints", "color": "primary",
                            "points": [{"label": label(r) + (f" ({r[1]})" if breakdown == "switch" and r[1]
                                                             else ""),
                                        "value": _i(r[6])} for r in rows[:10] if _i(r[6])]}}]
        if used_ids else [],
        tables=[table], notes=notes + scope_note,
    )


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "udt_switch_port_capacity", "Switch Port Capacity",
    "Used, free and active Ethernet ports on every tracked switch with the daily usage trend, switches "
    "over the warning threshold and ports not up for 30 days — reclaim ports before buying switches.",
    "capacity",
    [S("udt_port_capacity", None, top_n=50),
     S("interface_down_ports", "Ports Not Up for 30+ Days", days=30, top_n=100)],
    audience=("Network engineer", "Capacity planner", "IT manager"),
    tags=("port capacity", "switch ports", "device tracker", "unused ports"),
    default_period="last_30d", frequency="monthly", icon="Network", featured=True,
)

template(
    "udt_endpoint_activity_report", "Endpoint Activity",
    "New endpoints, port moves, rogue detections and watch-list sightings per day and per switch, the "
    "endpoints that moved most and the newest endpoints on the network.",
    "device_tracker",
    [S("udt_endpoint_activity", None, events="all"),
     S("udt_endpoint_activity", "New Endpoints", events="new_endpoint", top_n=50)],
    audience=("NOC operator", "Network engineer", "Security & audit"),
    tags=("endpoints", "moves", "new devices", "device tracker"),
    default_period="last_7d", frequency="weekly", icon="MonitorSmartphone",
)

template(
    "udt_rogue_watched_endpoints", "Rogue and Watched Endpoints",
    "Endpoints outside the allow list and endpoints on the watch list with their last switch port, and "
    "the rogue and watch-list events of the period — randomized MACs counted separately.",
    "security",
    [S("udt_rogue_watch", None, show="all", top_n=100),
     S("udt_endpoint_activity", "Rogue and Watch-List Activity", events="security", top_n=50)],
    audience=("Security & audit", "NOC operator", "Network engineer"),
    tags=("rogue devices", "watch list", "nac", "device tracker"),
    default_period="last_7d", frequency="daily", icon="ShieldCheck", featured=True,
)

template(
    "udt_endpoint_inventory_report", "Endpoint Inventory by Type and Vendor",
    "Endpoints seen in the last 30 days by type and OUI vendor, the share of randomized and unidentified "
    "MACs, and endpoints per VLAN.",
    "device_tracker",
    [S("udt_endpoint_inventory", None, seen_within="30", top_n=25),
     S("udt_vlan_summary", "Endpoints by VLAN", top_n=25, include_empty=False)],
    audience=("IT manager", "Network engineer", "Security & audit"),
    tags=("endpoints", "oui", "vendors", "inventory", "device tracker"),
    default_period="last_30d", frequency="monthly", icon="Boxes",
)

template(
    "udt_ip_address_reassignments", "IP Address Reassignments",
    "Endpoints that changed IPv4 address and addresses taken over by another endpoint, each with its "
    "evidence strength (three sightings in 24 hours from one reporter), and addresses reported for "
    "several endpoints.",
    "device_tracker",
    [S("udt_ip_reassignments", None, family="ipv4", top_n=100)],
    audience=("Network engineer", "Security & audit"),
    tags=("ip address", "arp", "dhcp", "ip conflicts", "device tracker"),
    default_period="last_7d", frequency="weekly", icon="Route",
)

template(
    "udt_user_logins_report", "User Logins by Endpoint",
    "Directory logons per user and endpoint, the users on the most endpoints and the endpoints shared by "
    "the most users — the user history behind every tracked device.",
    "device_tracker",
    [S("udt_user_logins", None, group_by="user_endpoint", top_n=100),
     S("udt_user_logins", "Logins by User", group_by="user", show_summary=False, top_n=50),
     S("udt_user_logins", "Shared Endpoints", group_by="endpoint", show_summary=False, top_n=25)],
    audience=("Security & audit", "IT manager"),
    tags=("user logins", "active directory", "user history", "device tracker"),
    default_period="last_7d", frequency="weekly", icon="FileText",
)

template(
    "udt_vlan_summary_report", "VLAN Summary",
    "Every VLAN with its switch ports and active endpoints, VLANs without endpoints and VLAN IDs named "
    "differently across switches, then the same view per switch.",
    "device_tracker",
    [S("udt_vlan_summary", None, breakdown="vlan", top_n=100),
     S("udt_vlan_summary", "VLANs by Switch", breakdown="switch", top_n=200, include_empty=False)],
    audience=("Network engineer",),
    tags=("vlan", "switch ports", "device tracker"),
    default_period="last_7d", frequency="monthly", icon="Layers",
)

template(
    "udt_device_tracker_overview", "Device Tracker Overview",
    "The state of the access layer: endpoints by type and vendor, activity of the period, rogue and "
    "watched endpoints, switch port capacity and VLAN usage.",
    "device_tracker",
    [S("udt_endpoint_inventory", None, seen_within="7"),
     S("udt_endpoint_activity", None, events="all"),
     S("udt_rogue_watch", None, show="all", top_n=25),
     S("udt_port_capacity", None, top_n=15),
     S("udt_vlan_summary", None, top_n=15, include_empty=False)],
    audience=("IT manager", "Network engineer", "NOC operator"),
    tags=("device tracker", "endpoints", "overview"),
    default_period="last_7d", frequency="weekly", icon="MonitorSmartphone", featured=True,
)
