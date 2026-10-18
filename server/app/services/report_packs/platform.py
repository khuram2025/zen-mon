"""Platform pack: appliance operations, security and audit.

Sections
- Remote sensors (settings.view): sensor_fleet_health (live connection state
  from heartbeat age), sensor_availability (connected share of the period,
  rebuilt from sensor_events, which keeps 30 days) and sensor_results_volume
  (results per poller from the raw ClickHouse metrics).
- Access and audit: access_review and active_sessions (users.view),
  login_activity and audit_activity (audit.view), role_changes (roles.view).
- Appliance: license_usage (settings.view), data_retention_coverage,
  storage_utilization and backup_status (system.admin), report_delivery
  (reports.view).

Shared rules in this pack
- Every section reads appliance-wide data, so every section is fleet-scoped:
  accounts limited to a visibility scope never receive them.
- Secrets never leave the database. No query selects password hashes, MFA
  secrets or recovery codes, sensor API key hashes, license keys or
  configuration text. Audit metadata is reduced to its key names in SQL, and
  e-mail addresses in delivery errors are masked.
- Missing data is "—" (not measured), never zero. Snapshot sections read the
  current time through _now() (tests pin it).
- ClickHouse is read through ctx.ch_rows only. The storage service's own
  inventory helpers open their own client and run subprocesses, so this pack
  re-runs the same system-table queries and reuses only the service's pure
  helpers (classify_table, _parse_ttl_days, CATEGORIES).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _aware_utc,
    _fmt_bytes,
    _fmt_pct,
    _local_bucket_starts,
    _naive_utc,
    _retention_days,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

logger = logging.getLogger("zenplus.reports")

SENSOR_HISTORY_DAYS = 30        # sensor_health_service purges sensor_events after 30 days
SESSION_PURGE_DAYS = 7          # session_service drops sessions 7 days after expiry
OVERDUE_GRACE = timedelta(minutes=15)   # report scheduler ticks every 60 s
CONTROLLER_LABEL = "Controller (central poller)"


# ─── Small shared helpers ──────────────────────────────────────────────────

def _now() -> datetime:
    """Current time (aware UTC); a function so tests can pin it."""
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    """Aware UTC datetime from a datetime or ISO string (naive = UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _kpi(label: str, value: str, accent: str = "neutral", subtitle: Optional[str] = None) -> dict:
    out = {"label": label, "value": value, "accent": accent}
    if subtitle:
        out["subtitle"] = subtitle
    return out


def _n(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _c(v: Any) -> str:
    return f"{_n(v):,}"


def _pct1(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.1f}%"


def _share(part: float, whole: float) -> Optional[float]:
    return part / whole * 100.0 if whole else None


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{s:.0f} s"
    m = int(round(s / 60))
    if m < 60:
        return f"{m} min"
    if m < 1440:
        h, mm = divmod(m, 60)
        return f"{h} h {mm:02d} min"
    d, rem = divmod(m, 1440)
    return f"{d} d {rem // 60} h"


def _ago(ts: Optional[datetime], now: datetime) -> str:
    ts = _aware(ts)
    return "—" if ts is None else f"{_duration((now - ts).total_seconds())} ago"


def _jlist(v: Any) -> list:
    """A JSONB array as a list (drivers return it decoded or as text)."""
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str) and v.strip():
        try:
            out = json.loads(v)
        except ValueError:
            return []
        return out if isinstance(out, list) else []
    return []


def _titled(title: str, shown: int, total: int, noun: str) -> str:
    return f"{title} — top {shown} of {total} {noun}" if total > shown else title


def _delta(curr: Optional[float], prev: Optional[float]) -> str:
    if curr is None:
        return "Not measured"
    if prev is None:
        return "No data for the previous period"
    d = curr - prev
    if d == 0:
        return "No change vs previous period"
    return f"{'▲' if d > 0 else '▼'} {abs(d):,.0f} vs previous period"


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _mask(s: Any) -> str:
    """Delivery errors can name recipients: mask e-mail addresses."""
    return _EMAIL_RE.sub("<address>", str(s or ""))


def _window(ctx: SectionCtx) -> dict:
    return {"f": _aware_utc(ctx.frm), "t": _aware_utc(ctx.to)}


def _prior(ctx: SectionCtx) -> dict:
    pf, pt = ctx.prior_window()
    return {"f": _aware_utc(pf), "t": _aware_utc(pt)}


# Organisation-local time buckets for Postgres (epoch of the local bucket start).

def _unit(ctx: SectionCtx) -> str:
    return "hour" if ctx.hours <= 48 else "day"


def _pg_bucket(column: str) -> str:
    return (f"EXTRACT(EPOCH FROM (date_trunc(:unit, {column} AT TIME ZONE :org_tz)"
            f" AT TIME ZONE :org_tz))::bigint")


def _bucket_params(ctx: SectionCtx) -> dict:
    return {"unit": _unit(ctx), "org_tz": ctx.tz_name}


def _bucket_keys(ctx: SectionCtx, unit: str) -> list[int]:
    return [int(d.timestamp()) for d in _local_bucket_starts(ctx, unit)]


def _stacked(ctx: SectionCtx, unit: str, counts: dict[str, dict[int, float]],
             names: Iterable[tuple], unit_label: str, color: str = "primary") -> dict:
    """A stacked series per (key, label[, color]) plus total points over every
    local bucket of the window. Empty buckets are 0: these are complete event
    logs, so no record means nothing happened. Outcome series pass a colour
    (success/warning/danger) so "Failed" never takes a palette green."""
    names = [(n[0], n[1], n[2] if len(n) > 2 else None) for n in names]
    keys = set(_bucket_keys(ctx, unit))
    lo = min(keys) if keys else None
    hi = int(_aware_utc(ctx.to).timestamp())
    for per in counts.values():
        keys.update(k for k in per if lo is None or lo <= k <= hi)
    ordered = sorted(keys)

    def iso(k: int) -> Optional[str]:
        return ctx.iso(datetime.fromtimestamp(k, timezone.utc))

    series = []
    for key, label, tone in names:
        entry: dict[str, Any] = {"name": label, "points": [{"t": iso(k), "v": counts.get(key, {}).get(k, 0)}
                                                            for k in ordered]}
        if tone:
            entry["color"] = tone
        series.append(entry)
    totals = [{"t": iso(k), "v": sum(counts.get(key, {}).get(k, 0) for key, _l, _c in names)}
              for k in ordered]
    return {"kind": "stacked", "unit": unit_label, "color": color, "points": totals, "series": series}


# ─── Roles and effective permissions ────────────────────────────────────────

async def _roles(ctx: SectionCtx) -> dict[str, dict]:
    """roles.name -> {display, perms, scope} (cached per render)."""
    key = "_platform_roles"
    if key not in ctx._cache:
        rows = (await ctx.db.execute(text(
            "SELECT name, display_name, permissions, scope_tags, is_system FROM roles "
            "ORDER BY name"))).fetchall()
        ctx._cache[key] = {r[0]: {"display": r[1] or r[0], "perms": _jlist(r[2]),
                                  "scope": [str(t) for t in _jlist(r[3])], "system": bool(r[4])}
                           for r in rows}
    return ctx._cache[key]


def _effective(roles: dict[str, dict], role: Optional[str]) -> frozenset[str]:
    """What a role grants, the way users.py's access review computes it
    (database role, else the legacy built-in definition)."""
    from app.core.permissions import LEGACY_ROLE_PERMISSIONS, effective_permissions
    entry = roles.get(role or "")
    granted = entry["perms"] if entry else LEGACY_ROLE_PERMISSIONS.get(role or "", [])
    return effective_permissions(granted)


def _privileged(perms: Iterable[str]) -> list[str]:
    from app.core.permissions import risk_of
    return sorted(p for p in perms if risk_of(p) in ("privileged", "critical"))


def _privileged_label(perms: frozenset[str]) -> str:
    from app.core.permissions import SUPERUSER_PERMISSION
    if SUPERUSER_PERMISSION in perms:
        return "Full administration"
    risky = _privileged(perms)
    return ", ".join(risky) if risky else "—"


def _scope_label(own: Iterable[str], role_scope: Iterable[str], perms: frozenset[str]) -> str:
    """The account's visibility scope: its own tags plus its role's."""
    from app.core.permissions import SUPERUSER_PERMISSION
    if SUPERUSER_PERMISSION in perms:
        return "All resources"
    tags: list[str] = []
    seen: set[str] = set()
    for t in list(own) + list(role_scope):
        t = str(t)
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    return ", ".join(tags) if tags else "All resources"


# ─── Remote sensors ─────────────────────────────────────────────────────────

STATE_ORDER = ("offline", "degraded", "pending", "online", "disabled")


def _version_parts(value: Optional[str]) -> tuple[int, ...]:
    """Same parsing as the sensor heartbeat (sensor_api._version_parts)."""
    return tuple(int(part) for part in re.findall(r"\d+", value or "")[:3])


def _live_state(row: dict, now: datetime) -> str:
    from app.services.sensor_overview import sensor_connection
    state = sensor_connection(row, now)
    return state if state in STATE_ORDER else "unknown"


_SENSOR_TARGETS_SQL = """
    SELECT s.id::text, s.name, COALESCE(si.name, NULLIF(s.location, ''), '') AS site,
           s.status, s.status_reason, s.version, s.min_supported_version, s.last_heartbeat_at,
           (s.api_key_hash IS NOT NULL) AS has_key,
           COALESCE((s.bootstrap_config->>'authorization_pending')::boolean, FALSE) AS auth_pending,
           s.degraded_after_s, s.offline_after_s, s.queue_depth, s.queue_dropped_count,
           s.uptime_seconds,
           (SELECT count(*) FROM devices d
             WHERE EXISTS (SELECT 1 FROM sensor_assignments a WHERE a.sensor_id = s.id
                             AND a.target_type = 'device' AND a.target_id = d.id)
                OR (NOT EXISTS (SELECT 1 FROM monitoring_policies p
                                 WHERE p.target_type = 'device' AND p.target_id = d.id)
                    AND (d.default_sensor_id = s.id
                         OR EXISTS (SELECT 1 FROM sensor_assignments a WHERE a.sensor_id = s.id
                                      AND a.target_type = 'group' AND a.target_id = d.group_id)))
           ) AS devices,
           (SELECT count(*) FROM service_checks sc
             WHERE EXISTS (SELECT 1 FROM sensor_assignments a WHERE a.sensor_id = s.id
                             AND a.target_type = 'service_check' AND a.target_id = sc.id)
                OR (sc.default_sensor_id = s.id
                    AND NOT EXISTS (SELECT 1 FROM monitoring_policies p
                                     WHERE p.target_type = 'service_check' AND p.target_id = sc.id))
           ) AS checks
      FROM sensors s LEFT JOIN sites si ON si.id = s.site_id
     ORDER BY lower(s.name)
"""

_SENSOR_COLS = ("id", "name", "site", "status", "status_reason", "version", "min_version",
                "last_heartbeat_at", "has_key", "auth_pending", "degraded_after_s",
                "offline_after_s", "queue_depth", "dropped", "uptime_s", "devices", "checks")


def _connection_row(r: dict) -> dict:
    """The fields sensor_overview.sensor_connection reads (no key material:
    only whether an API key exists)."""
    return {"status": r["status"], "api_key_hash": bool(r["has_key"]),
            "bootstrap_config": {"authorization_pending": bool(r["auth_pending"])},
            "last_heartbeat_at": _aware(r["last_heartbeat_at"]),
            "degraded_after_s": r["degraded_after_s"], "offline_after_s": r["offline_after_s"]}


@register_section(
    "sensor_fleet_health", title="Sensor Fleet Health", category="Platform",
    description=("Live connection state of every remote sensor from its heartbeat age, its version "
                 "against the required minimum, queue and dropped results and the targets assigned "
                 "to it, as of report generation."),
    scope="fleet", requires=("settings.view",), semantics="snapshot",
    options=[top_n_option(50, 500)],
)
async def sensor_fleet_health(ctx: SectionCtx) -> dict:
    sid, title = "sensor_fleet_health", "Sensor Fleet Health"
    now = _now()
    rows = [dict(zip(_SENSOR_COLS, r)) for r in (await ctx.db.execute(text(_SENSOR_TARGETS_SQL))).fetchall()]
    if not rows:
        return _section(sid, title, description="Remote sensors registered on this appliance.",
                        notes=["No remote sensors are registered. Results from the controller's own "
                               "poller are covered by the sensor results volume section."])

    env_min = os.getenv("ZENPLUS_MIN_SENSOR_VERSION", "").strip() or None
    newest = max((_version_parts(r["version"]) for r in rows if _version_parts(r["version"])), default=())
    newest_label = next((r["version"] for r in rows
                         if r["version"] and _version_parts(r["version"]) == newest), None)
    counts: dict[str, int] = defaultdict(int)
    below_min = behind = 0
    has_minimum = bool(env_min) or any(_version_parts(r["min_version"]) for r in rows)
    for r in rows:
        r["state"] = _live_state(_connection_row(r), now)
        counts[r["state"]] += 1
        minimum = env_min or r["min_version"]
        parts, min_parts = _version_parts(r["version"]), _version_parts(minimum)
        if not parts:
            r["version_label"] = "Not reported"
        elif min_parts and parts < min_parts:
            r["version_label"] = f"{r['version']} (below {minimum})"
            below_min += 1
        elif newest and parts < newest:
            r["version_label"] = f"{r['version']} (newest {newest_label})"
            behind += 1
        else:
            r["version_label"] = str(r["version"])
        if min_parts and not parts:
            below_min += 1

    enabled = len(rows) - counts["disabled"]
    devices = sum(_n(r["devices"]) for r in rows)
    checks = sum(_n(r["checks"]) for r in rows)
    dropped = sum(_n(r["dropped"]) for r in rows if r["dropped"] is not None)
    any_drop_reported = any(r["dropped"] is not None for r in rows)
    order = {s: i for i, s in enumerate(STATE_ORDER)}
    rows.sort(key=lambda r: (order.get(r["state"], len(order)), (r["name"] or "").lower()))
    top_n = int(ctx.opt("top_n"))
    shown = rows[:top_n]
    bad = counts["offline"] + counts["degraded"]

    tables = [{
        "title": _titled("Sensors", len(shown), len(rows), "sensors"),
        "headers": ["Sensor", "Site", "State", "Last heartbeat", "Version", "Queue", "Dropped",
                    "Devices / checks", "Uptime"],
        "styles": ["text", "text", "status", "text", "text", "num", "num", "num", "num"],
        "rows": [[r["name"] or "—", r["site"] or "—", r["state"],
                  _ago(r["last_heartbeat_at"], now) if r["last_heartbeat_at"] else "Never",
                  r["version_label"],
                  "—" if r["queue_depth"] is None else _c(r["queue_depth"]),
                  "—" if r["dropped"] is None else _c(r["dropped"]),
                  f"{_c(r['devices'])} / {_c(r['checks'])}",
                  _duration(r["uptime_s"]) if r["uptime_s"] is not None else "—"]
                 for r in shown],
    }]
    attention = [r for r in rows if r["state"] in ("offline", "degraded", "pending")]
    if attention:
        tables.append({
            "title": "Sensors needing attention",
            "headers": ["Sensor", "State", "Reason", "Last heartbeat"],
            "styles": ["text", "status", "text", "text"],
            "rows": [[r["name"] or "—", r["state"],
                      _clip(r["status_reason"], 120) or
                      ("Awaiting enrollment or authorization" if r["state"] == "pending"
                       else "Heartbeat overdue"),
                      ctx.fmt_short(r["last_heartbeat_at"]) if r["last_heartbeat_at"] else "Never"]
                     for r in attention[:top_n]],
        })

    notes = ["Live state compares each sensor's last heartbeat with its own degraded and offline "
             "thresholds, so it is current even when the stored status has not been swept yet.",
             "Dropped is the sensor's own count of probe results its buffer discarded; any increase "
             "marks the sensor degraded."]
    if behind:
        notes.append(f"{behind} sensor(s) run an older version than the newest one in the fleet "
                     f"({newest_label}).")
    return _section(
        sid, title,
        description=f"State of {len(rows):,} remote sensor(s) when the report was generated.",
        kpis=[
            _kpi("Sensors", _c(len(rows)), "primary",
                 f"{counts['disabled']:,} disabled" if counts["disabled"] else "All enabled"),
            _kpi("Online", _c(counts["online"]),
                 "success" if enabled and counts["online"] == enabled else "warning" if enabled else "neutral",
                 f"of {enabled:,} enabled"),
            _kpi("Offline or degraded", _c(bad),
                 "danger" if counts["offline"] else "warning" if bad else "success",
                 f"{counts['offline']:,} offline · {counts['degraded']:,} degraded"),
            _kpi("Below minimum version", _c(below_min), "danger" if below_min else "success",
                 f"{behind:,} behind the newest" if behind else
                 "Required minimum met" if has_minimum else "No minimum version configured"),
            _kpi("Results dropped", _c(dropped) if any_drop_reported else "—",
                 "warning" if dropped else "neutral", "Reported by the sensors"),
            _kpi("Assigned targets", _c(devices + checks), "info",
                 f"{devices:,} devices · {checks:,} service checks"),
        ],
        charts=[{"title": "Sensors by live state",
                 "series": {"kind": "donut", "unit": "sensors",
                            "points": [{"label": s.capitalize(), "value": counts[s]}
                                       for s in STATE_ORDER + ("unknown",) if counts.get(s)]}}],
        tables=tables, notes=notes,
    )


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def _norm_state(s: Any) -> str:
    s = str(s or "").strip().lower()
    return s if s in STATE_ORDER else "unknown"


@register_section(
    "sensor_availability", title="Sensor Availability", category="Platform",
    description=("Share of the period each remote sensor was connected (online or degraded), "
                 "rebuilt from its recorded status changes, with offline episodes and the longest "
                 "outage."),
    scope="fleet", requires=("settings.view",),
    options=[{"key": "target", "label": "Availability target (%)", "type": "number",
              "default": 99.0, "min": 50, "max": 100},
             top_n_option(25, 500)],
)
async def sensor_availability(ctx: SectionCtx) -> dict:
    sid, title = "sensor_availability", "Sensor Availability"
    now = _now()
    target = float(ctx.opt("target"))
    frm, to = _aware_utc(ctx.frm), min(_aware_utc(ctx.to), now)
    horizon = now - timedelta(days=SENSOR_HISTORY_DAYS)
    cov_from = max(frm, horizon)
    notes: list[str] = []
    note = ctx.window_note(SENSOR_HISTORY_DAYS, "Sensor status history")
    if note:
        notes.append(note)
    if cov_from >= to:
        return _section(sid, title, notes=notes + [
            "This period ended before the oldest retained sensor status change, so sensor "
            "availability cannot be rebuilt for it."])

    sensors = (await ctx.db.execute(text(
        "SELECT s.id::text, s.name, COALESCE(si.name, NULLIF(s.location, ''), '') AS site, s.status, "
        "s.created_at, s.last_heartbeat_at, (s.api_key_hash IS NOT NULL), "
        "COALESCE((s.bootstrap_config->>'authorization_pending')::boolean, FALSE), "
        "s.degraded_after_s, s.offline_after_s "
        "FROM sensors s LEFT JOIN sites si ON si.id = s.site_id ORDER BY lower(s.name)"))).fetchall()
    if not sensors:
        return _section(sid, title, notes=notes + ["No remote sensors are registered."])

    p = {"f": cov_from, "t": to}
    before = {r[0]: r[1] for r in (await ctx.db.execute(text(
        "SELECT DISTINCT ON (sensor_id) sensor_id::text, detail->>'to' FROM sensor_events "
        "WHERE kind = 'status_changed' AND ts < :f ORDER BY sensor_id, ts DESC"), p)).fetchall()}
    inside: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)
    for r in (await ctx.db.execute(text(
            "SELECT sensor_id::text, ts, detail->>'from', detail->>'to' FROM sensor_events "
            "WHERE kind = 'status_changed' AND ts >= :f AND ts < :t ORDER BY sensor_id, ts"), p)).fetchall():
        ts = _aware(r[1])
        if ts is not None:
            inside[r[0]].append((ts, _norm_state(r[2]), _norm_state(r[3])))
    after = {r[0]: r[1] for r in (await ctx.db.execute(text(
        "SELECT DISTINCT ON (sensor_id) sensor_id::text, detail->>'from' FROM sensor_events "
        "WHERE kind = 'status_changed' AND ts >= :t ORDER BY sensor_id, ts ASC"), p)).fetchall()}

    unit = "day"
    day_starts = _local_bucket_starts(ctx, unit) if ctx.hours >= 48 else []
    day_bounds = [(d, day_starts[i + 1] if i + 1 < len(day_starts) else to)
                  for i, d in enumerate(day_starts)]
    day_up = [0.0] * len(day_bounds)
    day_mon = [0.0] * len(day_bounds)

    results: list[dict] = []
    inferred = 0
    late = 0
    for r in sensors:
        sid_, name, site, status, created = r[0], r[1], r[2], r[3], _aware(r[4])
        start = max(cov_from, created) if created else cov_from
        live = _live_state({"status": status, "api_key_hash": bool(r[6]),
                            "bootstrap_config": {"authorization_pending": bool(r[7])},
                            "last_heartbeat_at": _aware(r[5]), "degraded_after_s": r[8],
                            "offline_after_s": r[9]}, now)
        if start >= to:
            late += 1
            continue
        events = [e for e in inside.get(sid_, []) if e[0] >= start]
        if sid_ in before:
            state = _norm_state(before[sid_])
        else:
            inferred += 1
            if events:
                state = events[0][1]
            elif sid_ in after:
                state = _norm_state(after[sid_])
            else:
                state = _norm_state(status)
        intervals: list[list] = []
        cur_t = start
        transitions = 0
        for ts, frm_state, to_state in events:
            if ts > cur_t:
                intervals.append([cur_t, ts, state])
            if to_state != state:
                transitions += 1
            state, cur_t = to_state, max(cur_t, ts)
        if to > cur_t:
            intervals.append([cur_t, to, state])
        merged: list[list] = []
        for iv in intervals:
            if merged and merged[-1][2] == iv[2] and merged[-1][1] == iv[0]:
                merged[-1][1] = iv[1]
            else:
                merged.append(list(iv))
        secs: dict[str, float] = defaultdict(float)
        offline_runs: list[float] = []
        for a, b, st in merged:
            d = (b - a).total_seconds()
            secs[st] += d
            if st == "offline":
                offline_runs.append(d)
            for i, (d0, d1) in enumerate(day_bounds):
                o = _overlap(a, b, d0, d1)
                if o and st in ("online", "degraded", "offline"):
                    day_mon[i] += o
                    if st != "offline":
                        day_up[i] += o
        connected = secs["online"] + secs["degraded"]
        monitored = connected + secs["offline"]
        results.append({
            "name": name or "—", "site": site or "—", "live": live,
            "avail": _share(connected, monitored),
            "connected": connected, "monitored": monitored,
            "degraded_s": secs["degraded"], "offline_s": secs["offline"],
            "episodes": len(offline_runs), "longest": max(offline_runs) if offline_runs else None,
            "transitions": transitions,
            "excluded_s": secs["disabled"] + secs["pending"] + secs["unknown"],
        })

    if not results:
        return _section(sid, title, notes=notes + ["Every registered sensor was added after this period."])

    total_conn = sum(x["connected"] for x in results)
    total_mon = sum(x["monitored"] for x in results)
    fleet = _share(total_conn, total_mon)
    measured = [x for x in results if x["avail"] is not None]
    below = [x for x in measured if x["avail"] < target]
    episodes = sum(x["episodes"] for x in results)
    worst = max((x for x in results if x["longest"]), key=lambda x: x["longest"], default=None)
    transitions = sum(x["transitions"] for x in results)
    results.sort(key=lambda x: (x["avail"] is None, x["avail"] if x["avail"] is not None else 0.0,
                                x["name"].lower()))
    top_n = int(ctx.opt("top_n"))
    shown = results[:top_n]

    charts = []
    lowest = sorted(measured, key=lambda x: (x["avail"], x["name"].lower()))[:15]
    if lowest:
        charts.append({"title": ("Availability by sensor" if len(measured) <= len(lowest) else
                                 f"Lowest availability — {len(lowest)} of {len(measured)} sensors"),
                       "series": {"kind": "rank", "unit": "%", "color": "success",
                                  "points": [{"label": x["name"], "value": round(x["avail"], 2)}
                                             for x in lowest]}})
    daily = [round(day_up[i] / day_mon[i] * 100, 2) if day_mon[i] else None
             for i in range(len(day_bounds))]
    measured_days = [v for v in daily if v is not None]
    if measured_days:
        charts.append({"title": "Fleet sensor availability per day",
                       "series": {"kind": "line", "unit": "%", "color": "success",
                                  "y_domain": [max(0.0, min(measured_days) - 1.0), 100],
                                  "points": [{"t": ctx.iso(d0), "v": daily[i]}
                                             for i, (d0, _d1) in enumerate(day_bounds)]}})
    if inferred:
        notes.append(f"No status change was retained before this period for {inferred} sensor(s): "
                     "their starting state is taken from the previous state of their next recorded "
                     "change, or their current state when nothing changed since.")
    if late:
        notes.append(f"{late} sensor(s) registered after this period are not included.")
    notes.append("Availability counts online and degraded time (the sensor kept reporting) against "
                 "online, degraded and offline time; disabled and not-yet-enrolled time is excluded.")
    return _section(
        sid, title,
        description=(f"Connected share of the period for {len(results):,} remote sensor(s), rebuilt "
                     "from recorded status changes."),
        kpis=[
            _kpi("Fleet sensor availability", _fmt_pct(fleet),
                 "neutral" if fleet is None else "success" if fleet >= target else "warning",
                 f"Target {target:.2f}%"),
            _kpi("Sensors below target", _c(len(below)),
                 "warning" if below else ("success" if measured else "neutral"),
                 f"of {len(measured):,} measured"),
            _kpi("Offline episodes", _c(episodes), "danger" if episodes else "success"),
            _kpi("Longest offline", _duration(worst["longest"]) if worst else "—",
                 "warning" if worst else "neutral", worst["name"] if worst else "No offline time"),
            _kpi("Status changes", _c(transitions), "info"),
        ],
        charts=charts,
        tables=[{
            "title": _titled("Availability by sensor", len(shown), len(results), "sensors"),
            "headers": ["Sensor", "Site", "Availability", "Degraded", "Offline", "Episodes", "Longest",
                        "Now"],
            "styles": ["text", "text", "pct-bar", "num", "num", "num", "num", "status"],
            "rows": [[x["name"], x["site"], _fmt_pct(x["avail"]),
                      _duration(x["degraded_s"]) if x["monitored"] else "—",
                      _duration(x["offline_s"]) if x["monitored"] else "—",
                      _c(x["episodes"]), _duration(x["longest"]) if x["longest"] else "—", x["live"]]
                     for x in shown],
        }],
        notes=notes,
    )


_RESULT_TABLES = (("ping", "ping_metrics", "Ping"), ("snmp", "snmp_metrics", "SNMP"),
                  ("service", "service_metrics", "Service checks"))


@register_section(
    "sensor_results_volume", title="Sensor Results Volume", category="Platform",
    description=("Monitoring results stored per poller (the controller and each remote sensor) per "
                 "day — ping, SNMP and service checks — and registered sensors that sent nothing."),
    scope="fleet", requires=("settings.view",),
    options=[{"key": "kind", "label": "Results", "type": "select", "default": "all",
              "choices": [["all", "All results"], ["ping", "Ping"], ["snmp", "SNMP"],
                          ["service", "Service checks"]]},
             top_n_option(25, 500)],
)
async def sensor_results_volume(ctx: SectionCtx) -> dict:
    sid = "sensor_results_volume"
    kind = ctx.opt("kind")
    tables = [t for t in _RESULT_TABLES if kind == "all" or t[0] == kind]
    title = "Sensor Results Volume" if kind == "all" else f"Sensor Results Volume — {tables[0][2]}"
    retention = min([await _retention_days(ctx, t[1], 30) for t in tables])
    notes: list[str] = []
    note = ctx.window_note(retention, "Raw monitoring results")
    if note:
        notes.append(note)
    now = _now()
    start = max(_aware_utc(ctx.frm), now - timedelta(days=retention))
    end = min(_aware_utc(ctx.to), now)
    if start >= end:
        return _section(sid, title, notes=notes + [
            "This period ended before the oldest retained monitoring result, so result volumes "
            "cannot be counted for it."])
    # Averages use the part of the period the raw results still cover.
    days = max((end - start).total_seconds() / 86400.0, 1 / 24.0)

    hourly = ctx.hours <= 48
    unit = "hour" if hourly else "day"
    bucket, bparams = ctx.ch_bucket("timestamp", "1 HOUR" if hourly else "1 DAY")
    controller = os.getenv("POLLER_ID", "poller-01")
    per_kind: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_bucket: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    last: dict[str, datetime] = {}
    for key, table, _label in tables:
        rows = await ctx.ch_rows(
            f"SELECT if(poller_id = %(controller)s, 'central', poller_id) AS poller, "
            f"toUnixTimestamp(toDateTime({bucket}, %(org_tz)s)) AS b, count() AS n, "
            f"max(timestamp) AS last_ts FROM zenplus.{table} "
            "WHERE timestamp >= %(f)s AND timestamp < %(t)s GROUP BY poller, b",
            {"controller": controller, "f": _naive_utc(ctx.frm), "t": _naive_utc(ctx.to), **bparams})
        for poller, b, n, last_ts in rows:
            poller = str(poller or "")
            per_kind[poller][key] += _n(n)
            per_bucket[poller][int(b)] += _n(n)
            ts = _aware(last_ts)
            if ts and (poller not in last or ts > last[poller]):
                last[poller] = ts

    sensors = {r[0]: {"name": r[1], "site": r[2], "status": r[3]} for r in (await ctx.db.execute(text(
        "SELECT s.id::text, s.name, COALESCE(si.name, NULLIF(s.location, ''), ''), s.status "
        "FROM sensors s LEFT JOIN sites si ON si.id = s.site_id"))).fetchall()}

    def label(poller: str) -> tuple[str, str]:
        if poller == "central":
            return CONTROLLER_LABEL, "Controller"
        s = sensors.get(poller)
        if s:
            return s["name"] or poller, s["site"] or "—"
        return f"Unregistered poller {poller[:12]}", "—"

    # Enabled, enrolled sensors only: a pending sensor has never reported.
    silent = [s for k, s in sensors.items()
              if s["status"] not in ("disabled", "pending") and k not in per_kind]
    if not per_kind and not silent:
        return _section(sid, title, notes=notes + ["No monitoring results were stored in this period."])

    rows_out = []
    grand = sum(sum(v.values()) for v in per_kind.values())
    for poller, kinds in per_kind.items():
        total = sum(kinds.values())
        name, site = label(poller)
        rows_out.append({"poller": poller, "name": name, "site": site, "kinds": kinds, "total": total,
                         "last": last.get(poller)})
    rows_out.sort(key=lambda x: (-x["total"], x["name"].lower()))
    top_n = int(ctx.opt("top_n"))
    shown = rows_out[:top_n]

    headers = ["Poller", "Site"] + [t[2] for t in tables]
    styles = ["text", "text"] + ["num"] * len(tables)
    if len(tables) > 1:
        headers.append("Total")
        styles.append("num")
    headers += ["Share", "Per day", "Last result"]
    styles += ["pct-bar", "num", "text"]
    table_rows = []
    for x in shown:
        row = [x["name"], x["site"]] + [_c(x["kinds"].get(t[0], 0)) for t in tables]
        if len(tables) > 1:
            row.append(_c(x["total"]))
        row += [_pct1(_share(x["total"], grand)), f"{x['total'] / days:,.0f}", ctx.fmt_short(x["last"])]
        table_rows.append(row)
    for s in silent:
        if len(table_rows) >= top_n:
            break
        row = [s["name"] or "—", s["site"] or "—"] + ["0"] * len(tables)
        if len(tables) > 1:
            row.append("0")
        row += ["0.0%", "0", "None in period"]
        table_rows.append(row)

    top_names = [x["poller"] for x in rows_out[:5]]
    names = [(p, label(p)[0]) for p in top_names]
    counts = {p: dict(per_bucket[p]) for p in top_names}
    others = [p for p in per_bucket if p not in top_names]
    if others:
        merged: dict[int, float] = defaultdict(float)
        for p in others:
            for k, v in per_bucket[p].items():
                merged[k] += v
        counts["__other__"] = dict(merged)
        names.append(("__other__", "Other pollers"))
    charts = [{"title": f"Results stored per {'hour' if hourly else 'day'} by poller",
               "series": _stacked(ctx, unit, counts, names, "results", "primary")}] if grand else []

    busiest = rows_out[0] if rows_out else None
    if silent:
        notes.append(f"{len(silent)} enabled sensor(s) stored no results in this period: "
                     + ", ".join(sorted((s['name'] or '—') for s in silent)[:10])
                     + ("…" if len(silent) > 10 else "") + ".")
    notes.append("Counts are stored result rows: one per ping probe and service check run, and one "
                 "per SNMP value polled. Results from a poller id that is not a registered sensor "
                 "come from a removed or re-enrolled sensor.")
    return _section(
        sid, title,
        description=("Monitoring results stored per poller over the reporting period, with registered "
                     "sensors that sent none."),
        kpis=[
            _kpi("Results stored", _c(grand), "primary", f"{grand / days:,.0f} per day"),
            _kpi("Pollers reporting", _c(len(per_kind)), "info",
                 f"{sum(1 for p in per_kind if p in sensors):,} registered sensor(s)"),
            _kpi("Silent sensors", _c(len(silent)), "danger" if silent else "success",
                 "Enabled, no results in the period"),
            _kpi("Busiest poller",
                 ("Controller" if busiest["poller"] == "central" else _clip(busiest["name"], 24))
                 if busiest else "—", "neutral",
                 f"{_pct1(_share(busiest['total'], grand))} of results" if busiest else None),
        ],
        charts=charts,
        tables=[{"title": _titled("Results by poller", len(shown), len(rows_out), "pollers"),
                 "headers": headers, "styles": styles, "rows": table_rows}],
        notes=notes,
    )


# ─── Access review ──────────────────────────────────────────────────────────

@register_section(
    "access_review", title="User Access Review", category="Security",
    description=("Accounts by role and sign-in source, MFA coverage, privileged, locked, dormant and "
                 "must-change-password accounts, as of report generation."),
    scope="fleet", requires=("users.view",), semantics="snapshot",
    options=[{"key": "days", "label": "Dormant after (days without sign-in)", "type": "int",
              "default": 90, "min": 1, "max": 730},
             top_n_option(50, 1000)],
)
async def access_review(ctx: SectionCtx) -> dict:
    sid, title = "access_review", "User Access Review"
    from app.core.permissions import SUPERUSER_PERMISSION
    now = _now()
    days = int(ctx.opt("days"))
    cutoff = now - timedelta(days=days)
    # Never read password hashes, MFA secrets or recovery codes.
    rows = (await ctx.db.execute(text(
        "SELECT u.username, COALESCE(u.full_name, ''), u.role, COALESCE(u.is_active, TRUE), "
        "COALESCE(NULLIF(u.auth_source, ''), 'local'), COALESCE(u.mfa_enabled, FALSE), "
        "COALESCE(u.must_change_password, FALSE), u.locked_until, u.last_login, u.created_at, "
        "u.scope_tags FROM users u ORDER BY lower(u.username)"))).fetchall()
    if not rows:
        return _section(sid, title, notes=["No user accounts exist."])
    roles = await _roles(ctx)

    accounts = []
    for r in rows:
        perms = _effective(roles, r[2])
        entry = roles.get(r[2] or "") or {}
        last = _aware(r[8])
        created = _aware(r[9])
        locked = bool(_aware(r[7]) and _aware(r[7]) > now)
        a = {"user": r[0], "name": r[1], "role": r[2] or "—",
             "role_label": entry.get("display") or (r[2] or "—"), "active": bool(r[3]),
             "source": str(r[4]).lower(), "mfa": bool(r[5]), "must_change": bool(r[6]),
             "locked": locked, "last": last, "created": created, "perms": perms,
             "privileged": bool(_privileged(perms)), "admin": SUPERUSER_PERMISSION in perms,
             "scope": _scope_label(_jlist(r[10]), entry.get("scope") or [], perms)}
        a["never"] = last is None
        a["dormant"] = a["active"] and ((last is not None and last < cutoff) or
                                        (last is None and created is not None and created < cutoff))
        findings = []
        if a["active"] and a["privileged"] and a["source"] == "local" and not a["mfa"]:
            findings.append((0, "Privileged without MFA"))
        if a["active"] and a["locked"]:
            findings.append((1, "Locked out"))
        if a["dormant"]:
            findings.append((2 if a["privileged"] else 3,
                             f"No sign-in for {(now - last).days} days" if last else "Never signed in"))
        if a["active"] and a["must_change"]:
            findings.append((4, "Must change password"))
        a["findings"] = findings
        accounts.append(a)

    active = [a for a in accounts if a["active"]]
    disabled = len(accounts) - len(active)
    local = [a for a in active if a["source"] == "local"]
    mfa_local = sum(1 for a in local if a["mfa"])
    coverage = _share(mfa_local, len(local))
    privileged = [a for a in active if a["privileged"]]
    priv_no_mfa = [a for a in privileged if a["source"] == "local" and not a["mfa"]]
    admins = sum(1 for a in privileged if a["admin"])
    dormant = [a for a in active if a["dormant"]]
    locked = [a for a in active if a["locked"]]
    must = [a for a in active if a["must_change"]]
    top_n = int(ctx.opt("top_n"))

    by_role: dict[str, dict] = {}
    for name, entry in roles.items():
        by_role[name] = {"label": entry["display"], "active": 0, "disabled": 0,
                         "sources": defaultdict(int), "local": 0, "mfa": 0,
                         "perms": _effective(roles, name), "scope": entry["scope"]}
    for a in accounts:
        g = by_role.setdefault(a["role"], {"label": a["role_label"], "active": 0, "disabled": 0,
                                           "sources": defaultdict(int), "local": 0, "mfa": 0,
                                           "perms": a["perms"], "scope": []})
        if not a["active"]:
            g["disabled"] += 1
            continue
        g["active"] += 1
        g["sources"][a["source"]] += 1
        if a["source"] == "local":
            g["local"] += 1
            g["mfa"] += int(a["mfa"])
    role_rows = sorted(by_role.values(), key=lambda g: (-g["active"], g["label"].lower()))
    tables = [{
        "title": "Accounts by role",
        "headers": ["Role", "Active", "Disabled", "Sign-in sources", "MFA (local accounts)",
                    "Privileged permissions", "Role visibility scope"],
        "styles": ["text", "num", "num", "text", "text", "text", "text"],
        "rows": [[g["label"], _c(g["active"]), _c(g["disabled"]),
                  ", ".join(f"{s} {n}" for s, n in sorted(g["sources"].items())) or "—",
                  f"{g['mfa']} of {g['local']}" if g["local"] else "—",
                  _privileged_label(g["perms"]),
                  ", ".join(g["scope"]) if g["scope"] else "All resources"]
                 for g in role_rows if g["active"] or g["disabled"]],
    }]
    review = sorted((a for a in accounts if a["findings"]),
                    key=lambda a: (min(f[0] for f in a["findings"]), a["user"].lower()))
    if review:
        tables.append({
            "title": _titled("Accounts needing review", min(top_n, len(review)), len(review), "accounts"),
            "headers": ["Account", "Name", "Role", "Source", "Findings", "Last sign-in", "MFA"],
            "styles": ["mono", "text", "text", "text", "text", "text", "text"],
            "rows": [[a["user"], a["name"] or "—", a["role_label"], a["source"],
                      "; ".join(f[1] for f in sorted(a["findings"])),
                      ctx.fmt_date(a["last"]) if a["last"] else "Never",
                      ("Yes" if a["mfa"] else "No") if a["source"] == "local" else "At provider"]
                     for a in review[:top_n]],
        })
    if privileged:
        priv_sorted = sorted(privileged, key=lambda a: (not a["admin"], a["user"].lower()))
        tables.append({
            "title": _titled("Privileged accounts", min(top_n, len(priv_sorted)), len(priv_sorted),
                             "accounts"),
            "headers": ["Account", "Role", "Privileged permissions", "Source", "MFA", "Last sign-in",
                        "Visibility scope"],
            "styles": ["mono", "text", "text", "text", "text", "text", "text"],
            "rows": [[a["user"], a["role_label"], _privileged_label(a["perms"]), a["source"],
                      ("Yes" if a["mfa"] else "No") if a["source"] == "local" else "At provider",
                      ctx.fmt_date(a["last"]) if a["last"] else "Never", a["scope"]]
                     for a in priv_sorted[:top_n]],
        })

    role_counts: dict[str, int] = defaultdict(int)
    for a in active:
        role_counts[a["role_label"]] += 1
    return _section(
        sid, title,
        description=(f"Access state of {len(accounts):,} account(s) when the report was generated; "
                     f"dormant means no sign-in for {days} days or more."),
        kpis=[
            _kpi("Active accounts", _c(len(active)), "primary",
                 f"{disabled:,} disabled · {len(accounts):,} total"),
            _kpi("MFA coverage", _pct1(coverage),
                 "neutral" if coverage is None else "success" if coverage >= 100 else
                 "warning" if coverage >= 50 else "danger",
                 f"{mfa_local:,} of {len(local):,} active local accounts" if local else "No local accounts"),
            _kpi("Privileged accounts", _c(len(privileged)),
                 "danger" if priv_no_mfa else "info",
                 f"{admins:,} full administrators · {len(priv_no_mfa):,} without MFA"),
            _kpi(f"Dormant ({days}+ days)", _c(len(dormant)), "warning" if dormant else "success",
                 f"{sum(1 for a in dormant if a['never']):,} never signed in"),
            _kpi("Locked now", _c(len(locked)), "warning" if locked else "success"),
            _kpi("Must change password", _c(len(must)), "info" if must else "neutral"),
        ],
        charts=[{"title": "Active accounts by role",
                 "series": {"kind": "donut", "unit": "accounts",
                            "points": [{"label": k, "value": v}
                                       for k, v in sorted(role_counts.items(), key=lambda kv: -kv[1])]}}]
        if role_counts else [],
        tables=tables,
        notes=["MFA applies to local accounts; LDAP, RADIUS and single sign-on accounts authenticate "
               "(and enforce MFA) at their identity provider.",
               "Privileged means the role grants a permission rated privileged or critical: managing "
               "users, roles, settings or credentials, shared NetFlow configuration, or full "
               "administration. Visibility scope is the account's own scope tags plus its role's.",
               "Password hashes, MFA secrets and recovery codes are never read by this report."],
    )


# ─── Sign-in activity ───────────────────────────────────────────────────────

_REASON_PATTERNS = {"p_lock": "%temporarily locked%", "p_lock2": "%account locked%",
                    "p_blocked": "Too many failed sign-in attempts. Try again%",
                    "p_mfa": "Invalid verification code%", "p_disabled": "%disabled%",
                    "p_sso_only": "%signs in with single sign-on%",
                    "p_invalid": "Invalid username or password%"}
_LOCKED_SQL = "(metadata->>'reason' ILIKE :p_lock OR metadata->>'reason' ILIKE :p_lock2)"
_REASON_SQL = f"""CASE
    WHEN {_LOCKED_SQL} THEN 'locked'
    WHEN metadata->>'reason' ILIKE :p_blocked THEN 'blocked'
    WHEN metadata->>'reason' = 'mfa' OR metadata->>'reason' ILIKE :p_mfa THEN 'mfa'
    WHEN metadata->>'reason' ILIKE :p_disabled THEN 'disabled'
    WHEN metadata->>'reason' ILIKE :p_sso_only THEN 'sso_only'
    WHEN metadata->>'method' = 'oidc' THEN 'sso'
    WHEN metadata->>'reason' ILIKE :p_invalid THEN 'invalid'
    ELSE 'other' END"""
_REASON_LABELS = {
    "invalid": "Wrong username or password",
    "locked": "Account locked (failure threshold reached)",
    "blocked": "Attempt while the account was locked",
    "mfa": "Wrong MFA code",
    "disabled": "Account disabled",
    "sso_only": "Single sign-on account used the password form",
    "sso": "Single sign-on error",
    "other": "Other",
}
_LOGIN_WHERE = "action IN ('auth.login_succeeded', 'auth.login_failed') AND created_at >= :f AND created_at < :t"


async def _login_totals(ctx: SectionCtx, window: dict) -> Optional[tuple]:
    return (await ctx.db.execute(text(
        "SELECT count(*) FILTER (WHERE action = 'auth.login_succeeded'), "
        "count(*) FILTER (WHERE action = 'auth.login_failed'), "
        "count(DISTINCT metadata->>'username') FILTER (WHERE action = 'auth.login_succeeded'), "
        "count(DISTINCT metadata->>'source_ip') FILTER (WHERE action = 'auth.login_failed'), "
        "count(*) FILTER (WHERE action = 'auth.login_succeeded' AND metadata->>'mfa' = 'true'), "
        "count(*) FILTER (WHERE action = 'auth.login_succeeded' AND metadata->>'method' = 'oidc'), "
        f"count(*) FILTER (WHERE action = 'auth.login_failed' AND {_LOCKED_SQL}), "
        "count(*) FILTER (WHERE action = 'auth.login_failed' AND metadata->>'reason' ILIKE :p_blocked), "
        "count(*) FILTER (WHERE action = 'auth.login_failed' AND NOT EXISTS "
        "(SELECT 1 FROM users u WHERE u.username = audit_logs.metadata->>'username')) "
        f"FROM audit_logs WHERE {_LOGIN_WHERE}"), {**window, **_REASON_PATTERNS})).first()


@register_section(
    "login_activity", title="Sign-in Activity", category="Security",
    description=("Successful and failed sign-ins per day, failure reasons, the usernames and source "
                 "addresses with the most failures, and account lockouts."),
    scope="fleet", requires=("audit.view",),
    options=[top_n_option(10, 100)],
)
async def login_activity(ctx: SectionCtx) -> dict:
    sid, title = "login_activity", "Sign-in Activity"
    w = _window(ctx)
    cur = await _login_totals(ctx, w)
    ok, failed = (_n(cur[0]), _n(cur[1])) if cur else (0, 0)
    if not cur or ok + failed == 0:
        return _section(sid, title, notes=["No sign-in attempts were recorded in this period."])
    users, fail_ips, mfa, sso, lockouts, blocked, unknown = (_n(v) for v in cur[2:9])
    prev = await _login_totals(ctx, _prior(ctx))
    p_ok, p_failed = (_n(prev[0]), _n(prev[1])) if prev else (None, None)
    if prev is not None and p_ok + p_failed == 0:
        p_ok = p_failed = None
    top_n = int(ctx.opt("top_n"))

    unit = _unit(ctx)
    counts: dict[str, dict[int, float]] = {"ok": {}, "failed": {}}
    for b, n_ok, n_failed in (await ctx.db.execute(text(
            f"SELECT {_pg_bucket('created_at')} AS b, "
            "count(*) FILTER (WHERE action = 'auth.login_succeeded'), "
            "count(*) FILTER (WHERE action = 'auth.login_failed') "
            f"FROM audit_logs WHERE {_LOGIN_WHERE} GROUP BY 1"), {**w, **_bucket_params(ctx)})).fetchall():
        counts["ok"][int(b)] = _n(n_ok)
        counts["failed"][int(b)] = _n(n_failed)

    reasons = (await ctx.db.execute(text(
        f"SELECT {_REASON_SQL} AS reason, count(*) AS n, count(DISTINCT metadata->>'username') "
        "FROM audit_logs WHERE action = 'auth.login_failed' AND created_at >= :f AND created_at < :t "
        "GROUP BY 1 ORDER BY 2 DESC"), {**w, **_REASON_PATTERNS})).fetchall()

    names = (await ctx.db.execute(text(
        "SELECT g.uname, g.failed, g.ok, g.ips, g.lockouts, g.last_fail, "
        "EXISTS (SELECT 1 FROM users u WHERE u.username = g.uname) AS known, "
        "count(*) OVER () AS total FROM ("
        "  SELECT metadata->>'username' AS uname, "
        "         count(*) FILTER (WHERE action = 'auth.login_failed') AS failed, "
        "         count(*) FILTER (WHERE action = 'auth.login_succeeded') AS ok, "
        "         count(DISTINCT metadata->>'source_ip') FILTER (WHERE action = 'auth.login_failed') AS ips, "
        f"        count(*) FILTER (WHERE action = 'auth.login_failed' AND {_LOCKED_SQL}) AS lockouts, "
        "         max(created_at) FILTER (WHERE action = 'auth.login_failed') AS last_fail "
        f"  FROM audit_logs WHERE {_LOGIN_WHERE} GROUP BY 1"
        "  HAVING count(*) FILTER (WHERE action = 'auth.login_failed') > 0) g "
        "ORDER BY g.failed DESC, g.uname NULLS LAST LIMIT :lim"),
        {**w, **_REASON_PATTERNS, "lim": top_n})).fetchall()

    sources = (await ctx.db.execute(text(
        "SELECT g.ip, g.failed, g.ok, g.names, g.last_seen, count(*) OVER () AS total FROM ("
        "  SELECT metadata->>'source_ip' AS ip, "
        "         count(*) FILTER (WHERE action = 'auth.login_failed') AS failed, "
        "         count(*) FILTER (WHERE action = 'auth.login_succeeded') AS ok, "
        "         count(DISTINCT metadata->>'username') FILTER (WHERE action = 'auth.login_failed') AS names, "
        "         max(created_at) AS last_seen "
        f"  FROM audit_logs WHERE {_LOGIN_WHERE} GROUP BY 1"
        "  HAVING count(*) FILTER (WHERE action = 'auth.login_failed') > 0) g "
        "ORDER BY g.failed DESC, g.ip NULLS LAST LIMIT :lim"), {**w, "lim": top_n})).fetchall()

    tables = []
    if reasons:
        tables.append({
            "title": "Failed sign-ins by reason",
            "headers": ["Reason", "Failures", "Share", "Usernames"],
            "styles": ["text", "num", "pct-bar", "num"],
            "rows": [[_REASON_LABELS.get(r[0], "Other"), _c(r[1]), _pct1(_share(_n(r[1]), failed)),
                      _c(r[2])] for r in reasons],
        })
    if names:
        total = _n(names[0][7])
        tables.append({
            "title": _titled("Usernames with the most failed sign-ins", len(names), total, "usernames"),
            "headers": ["Username", "Account", "Failed", "Succeeded", "Source addresses", "Lockouts",
                        "Last failure"],
            "styles": ["mono", "text", "num", "num", "num", "num", "text"],
            "rows": [[_clip(r[0], 64) if r[0] else "(not provided)",
                      "Existing account" if r[6] else "No such account", _c(r[1]), _c(r[2]), _c(r[3]),
                      _c(r[4]), ctx.fmt_short(r[5])] for r in names],
        })
    if sources:
        total = _n(sources[0][5])
        tables.append({
            "title": _titled("Source addresses with the most failed sign-ins", len(sources), total,
                             "addresses"),
            "headers": ["Source address", "Failed", "Succeeded", "Usernames tried", "Last attempt"],
            "styles": ["mono", "num", "num", "num", "text"],
            "rows": [[r[0] or "—", _c(r[1]), _c(r[2]), _c(r[3]), ctx.fmt_short(r[4])] for r in sources],
        })

    attempts = ok + failed
    return _section(
        sid, title,
        description=f"{attempts:,} sign-in attempt(s) over the reporting period.",
        kpis=[
            _kpi("Successful sign-ins", _c(ok), "success", _delta(ok, p_ok)),
            _kpi("Failed sign-ins", _c(failed),
                 "danger" if failed > ok else "warning" if failed else "success",
                 f"{_pct1(_share(failed, attempts))} of attempts · {_delta(failed, p_failed)}"),
            _kpi("Account lockouts", _c(lockouts), "warning" if lockouts else "success",
                 f"{blocked:,} attempt(s) while locked"),
            _kpi("Addresses with failures", _c(fail_ips), "warning" if fail_ips else "neutral",
                 "Distinct source addresses"),
            _kpi("Unknown-username failures", _c(unknown), "warning" if unknown else "neutral",
                 f"{_pct1(_share(unknown, failed))} of failures" if failed else None),
            _kpi("Signed-in accounts", _c(users), "info",
                 f"{_pct1(_share(mfa, ok))} with MFA · {sso:,} via SSO" if ok else None),
        ],
        charts=[{"title": f"Sign-ins per {unit}",
                 "series": _stacked(ctx, unit, counts,
                                    [("ok", "Succeeded", "success"), ("failed", "Failed", "danger")],
                                    "sign-ins", "primary")}],
        tables=tables,
        notes=["Usernames are shown as typed. Many failures for a username that does not exist can be "
               "a typo, a password typed into the username field, or password spraying.",
               "Attempts rejected by sign-in rate limiting (per address and per account) are refused "
               "before they reach the audit log and are not counted."],
    )


# ─── Administrative audit trail ─────────────────────────────────────────────

_SIGN_IN_ACTIONS = ("auth.login_succeeded", "auth.login_failed", "auth.logout")
_FOCUS = {
    "all": ("Administrative Audit Trail", None),
    "access": ("Access-Control Audit Trail",
               {"fx_prefix": ["user", "role", "auth"],
                "fx_actions": ["settings.auth.update", "settings.auth.toggle", "settings.auth.policy"],
                "fx_types": ["user", "role"]}),
    "system": ("System and Settings Audit Trail",
               {"fx_prefix": ["system", "settings", "security", "support_bundle", "ssh_terminal",
                              "snmp_credential", "windows_credential", "sensor"],
                "fx_actions": [], "fx_types": ["storage_backup", "sensor"]}),
}
_FOCUS_SQL = ("(split_part(action, '.', 1) = ANY(:fx_prefix) OR action = ANY(:fx_actions) "
              "OR resource_type = ANY(:fx_types))")
_ACCESS_PARAMS = {"ax_prefix": ["user", "role", "auth"],
                  "ax_actions": ["settings.auth.update", "settings.auth.toggle", "settings.auth.policy"],
                  "ax_types": ["user", "role"], "ax_signin": list(_SIGN_IN_ACTIONS)}
# Access-control activity never counts sign-ins, even when the trail includes them.
_ACCESS_SQL = ("((split_part(action, '.', 1) = ANY(:ax_prefix) OR action = ANY(:ax_actions) "
               "OR resource_type = ANY(:ax_types)) AND action <> ALL(:ax_signin))")


def _audit_where(ctx: SectionCtx) -> tuple[str, dict]:
    focus = ctx.opt("focus")
    where = "created_at >= :f AND created_at < :t"
    params: dict[str, Any] = {}
    if not ctx.opt("include_sign_ins"):
        where += " AND action <> ALL(:signin)"
        params["signin"] = list(_SIGN_IN_ACTIONS)
    fx = _FOCUS.get(focus, _FOCUS["all"])[1]
    if fx:
        where += f" AND {_FOCUS_SQL}"
        params.update(fx)
    return where, params


@register_section(
    "audit_activity", title="Administrative Audit Trail", category="Security",
    description=("Audited administrative activity — changes, exports and privileged access — by action "
                 "and resource type, the most active people and the latest events; metadata is listed "
                 "by field name only, never by value."),
    scope="fleet", requires=("audit.view",),
    options=[{"key": "focus", "label": "Changes", "type": "select", "default": "all",
              "choices": [["all", "All administrative changes"],
                          ["access", "Users, roles and authentication"],
                          ["system", "System, settings, sensors and credentials"]]},
             {"key": "include_sign_ins", "label": "Include sign-ins and sign-outs", "type": "bool",
              "default": False},
             top_n_option(25, 500)],
)
async def audit_activity(ctx: SectionCtx) -> dict:
    sid = "audit_activity"
    title = _FOCUS.get(ctx.opt("focus"), _FOCUS["all"])[0]
    where, params = _audit_where(ctx)
    w = {**_window(ctx), **params, **_ACCESS_PARAMS, "del_pat": "%delete%"}
    totals_sql = text(
        "SELECT count(*), count(DISTINCT COALESCE(actor_username, '')), "
        f"count(*) FILTER (WHERE {_ACCESS_SQL}), count(*) FILTER (WHERE action ILIKE :del_pat), "
        "count(DISTINCT action) "
        f"FROM audit_logs WHERE {where}")
    cur = (await ctx.db.execute(totals_sql, w)).first()
    total = _n(cur[0]) if cur else 0
    if not total:
        return _section(sid, title, notes=["No audited activity was recorded in this period."])
    actors, access, deletes, actions = (_n(v) for v in cur[1:5])
    prev = (await ctx.db.execute(totals_sql, {**w, **_prior(ctx)})).first()
    p_total = _n(prev[0]) if prev else None
    top_n = int(ctx.opt("top_n"))

    by_action = (await ctx.db.execute(text(
        "SELECT action, count(*), count(DISTINCT COALESCE(actor_username, '')), max(created_at) "
        f"FROM audit_logs WHERE {where} GROUP BY action ORDER BY 2 DESC, action LIMIT 15"), w)).fetchall()
    by_type = (await ctx.db.execute(text(
        f"SELECT resource_type, count(*) FROM audit_logs WHERE {where} "
        "GROUP BY resource_type ORDER BY 2 DESC, resource_type"), w)).fetchall()
    by_actor = (await ctx.db.execute(text(
        "SELECT COALESCE(NULLIF(actor_username, ''), ''), max(actor_role), count(*), "
        f"count(DISTINCT action), count(*) FILTER (WHERE {_ACCESS_SQL}), max(created_at) "
        f"FROM audit_logs WHERE {where} GROUP BY 1 ORDER BY 3 DESC, 1 LIMIT 10"), w)).fetchall()
    unit = _unit(ctx)
    daily = {"n": {int(b): _n(n) for b, n in (await ctx.db.execute(text(
        f"SELECT {_pg_bucket('created_at')} AS b, count(*) FROM audit_logs WHERE {where} GROUP BY 1"),
        {**w, **_bucket_params(ctx)})).fetchall()}}
    # Metadata keys only: values never leave the database.
    log = (await ctx.db.execute(text(
        "SELECT created_at, COALESCE(NULLIF(actor_username, ''), ''), COALESCE(actor_role, ''), action, "
        "resource_type, COALESCE(resource_id, ''), "
        "CASE WHEN jsonb_typeof(metadata) = 'object' "
        "THEN ARRAY(SELECT k FROM jsonb_object_keys(metadata) AS k ORDER BY k) "
        "ELSE ARRAY[]::text[] END "
        f"FROM audit_logs WHERE {where} ORDER BY created_at DESC LIMIT :lim"), {**w, "lim": top_n})).fetchall()

    other_types = sum(_n(r[1]) for r in by_type[8:])
    type_points = [{"label": r[0] or "—", "value": _n(r[1])} for r in by_type[:8]]
    if other_types:
        type_points.append({"label": "Other", "value": other_types})
    series = _stacked(ctx, unit, daily, [("n", "Events")], "events", "primary")
    series["kind"] = "bars"
    series.pop("series", None)
    return _section(
        sid, title,
        description=f"{total:,} audited event(s) by {actors:,} actor(s) over the reporting period.",
        kpis=[
            _kpi("Audited events", _c(total), "primary", _delta(total, p_total)),
            _kpi("Actors", _c(actors), "info", f"{actions:,} distinct actions"),
            _kpi("Access-control events", _c(access), "warning" if access else "neutral",
                 "Users, roles and authentication"),
            _kpi("Deletions", _c(deletes), "warning" if deletes else "neutral"),
        ],
        charts=[{"title": f"Audited events per {unit}", "series": series},
                {"title": "Events by resource type",
                 "series": {"kind": "donut", "unit": "events", "points": type_points}}],
        tables=[
            {"title": "Most frequent actions",
             "headers": ["Action", "Count", "Actors", "Last"],
             "styles": ["mono", "num", "num", "text"],
             "rows": [[r[0], _c(r[1]), _c(r[2]), ctx.fmt_short(r[3])] for r in by_action]},
            {"title": "Most active people",
             "headers": ["Actor", "Role", "Events", "Distinct actions", "Access-control events",
                         "Last event"],
             "styles": ["mono", "text", "num", "num", "num", "text"],
             "rows": [[r[0] or "system", r[1] or "—", _c(r[2]), _c(r[3]), _c(r[4]), ctx.fmt_short(r[5])]
                      for r in by_actor]},
            {"title": _titled("Latest events", len(log), total, "events"),
             "headers": ["Time", "Actor", "Action", "Resource", "Resource id", "Fields recorded"],
             "styles": ["text", "mono", "mono", "text", "mono", "text"],
             "rows": [[ctx.fmt_dt(r[0]), r[1] or "system", r[3], r[4] or "—", _clip(r[5], 40) or "—",
                       ", ".join(str(k) for k in (r[6] or [])) or "—"] for r in log]},
        ],
        notes=["The event log lists the names of the fields each event recorded, never their "
               "values, so this report can be shared without exposing settings or credentials."]
        + ([] if ctx.opt("include_sign_ins") else
           ["Sign-ins and sign-outs are reported in the Sign-in Activity section."]),
    )


# ─── Role and permission changes ────────────────────────────────────────────

_ROLE_ACTIONS = {"create": "Created", "update": "Updated", "rename": "Renamed", "delete": "Deleted",
                 "builtin_reconcile": "Built-in definition updated", "builtin_create": "Built-in role added"}


def _perm_list(perms: list[str], limit: int = 6) -> str:
    if not perms:
        return "—"
    head = ", ".join(perms[:limit])
    return head + (f" +{len(perms) - limit} more" if len(perms) > limit else "")


@register_section(
    "role_changes", title="Role and Permission Changes", category="Security",
    description=("Every role created, changed, renamed or deleted in the period with the permissions "
                 "added and removed, privileged grants and the members affected, plus the roles in "
                 "force when the report was generated."),
    scope="fleet", requires=("roles.view",),
    options=[top_n_option(25, 500)],
)
async def role_changes(ctx: SectionCtx) -> dict:
    sid, title = "role_changes", "Role and Permission Changes"
    w = _window(ctx)
    rows = (await ctx.db.execute(text(
        "SELECT occurred_at, role_name, action, COALESCE(NULLIF(actor_username, ''), 'system'), added, "
        "removed, affected_users, COALESCE(reason, ''), changes->'name'->>'from', "
        "CASE WHEN jsonb_typeof(changes) = 'object' "
        "THEN ARRAY(SELECT k FROM jsonb_object_keys(changes) AS k ORDER BY k) ELSE ARRAY[]::text[] END, "
        "role_id::text "
        "FROM role_history WHERE occurred_at >= :f AND occurred_at < :t "
        "ORDER BY occurred_at DESC LIMIT 5000"), w)).fetchall()
    roles = await _roles(ctx)
    members = {r[0]: (_n(r[1]), _n(r[2])) for r in (await ctx.db.execute(text(
        "SELECT role, count(*) FILTER (WHERE is_active), count(*) FROM users GROUP BY role"))).fetchall()}
    top_n = int(ctx.opt("top_n"))

    current = []
    for name, entry in roles.items():
        perms = _effective(roles, name)
        act, _all = members.get(name, (0, 0))
        current.append([entry["display"], "Built-in" if entry["system"] else "Custom", _c(act),
                        _c(len(perms)), _privileged_label(perms),
                        ", ".join(entry["scope"]) if entry["scope"] else "All resources"])
    current.sort(key=lambda r: r[0].lower())
    current_table = {"title": "Roles in force at report generation",
                     "headers": ["Role", "Type", "Active members", "Permissions",
                                 "Privileged permissions", "Visibility scope"],
                     "styles": ["text", "text", "num", "num", "text", "text"], "rows": current}

    if not rows:
        return _section(sid, title,
                        description="Role definitions over the reporting period.",
                        kpis=[_kpi("Role changes", "0", "success"), _kpi("Roles", _c(len(roles)), "info")],
                        tables=[current_table] if current else [],
                        notes=["No role was created, changed or deleted in this period."])

    events = []
    granted = revoked = priv_grants = affected = 0
    changed_roles: set[str] = set()
    for r in rows:
        added = sorted(str(p) for p in _jlist(r[4]))
        removed = sorted(str(p) for p in _jlist(r[5]))
        priv = _privileged(added)
        granted += len(added)
        revoked += len(removed)
        priv_grants += len(priv)
        affected += _n(r[6])
        changed_roles.add(r[10] or r[1])
        fields = [k for k in (r[9] or []) if k not in ("directory_references_migrated", "definition_version")]
        change = _ROLE_ACTIONS.get(r[2], str(r[2]).replace("_", " ").capitalize())
        if r[2] == "rename" and r[8]:
            change = f"Renamed from {r[8]}"
        elif fields and r[2] == "update":
            change = f"Updated ({', '.join(fields)})"
        events.append([ctx.fmt_dt(r[0]), r[1], change, r[3], _perm_list(added), _perm_list(removed),
                       _perm_list(priv) if priv else "—", _c(r[6]), _clip(r[7], 80) or "—"])
    shown = events[:top_n]
    return _section(
        sid, title,
        description=f"{len(rows):,} role change(s) to {len(changed_roles):,} role(s) over the reporting period.",
        kpis=[
            _kpi("Role changes", _c(len(rows)), "primary", f"{len(changed_roles):,} role(s) changed"),
            _kpi("Permissions granted", _c(granted), "info"),
            _kpi("Permissions revoked", _c(revoked), "info"),
            _kpi("Privileged grants", _c(priv_grants), "danger" if priv_grants else "success",
                 "Privileged or critical permissions added"),
            _kpi("Member assignments affected", _c(affected), "neutral",
                 "Members of the role at each change"),
        ],
        tables=[{"title": _titled("Role changes", len(shown), len(events), "changes"),
                 "headers": ["Time", "Role", "Change", "By", "Permissions added", "Permissions removed",
                             "Privileged added", "Members", "Reason"],
                 "styles": ["text", "text", "text", "mono", "text", "text", "text", "num", "text"],
                 "rows": shown}] + ([current_table] if current else []),
        notes=["Role assignments to individual accounts are recorded in the Administrative Audit "
               "Trail (requires audit.view)."],
    )


# ─── Active sessions ────────────────────────────────────────────────────────

_METHOD_LABELS = {"password": "Password", "oidc": "Single sign-on (OIDC)", "ldap": "LDAP",
                  "radius": "RADIUS"}


@register_section(
    "active_sessions", title="Active Sessions", category="Security",
    description=("Signed-in sessions by authentication method, the share verified with MFA and the "
                 "accounts holding them, as of report generation."),
    scope="fleet", requires=("users.view",), semantics="snapshot",
    options=[top_n_option(25, 500)],
)
async def active_sessions(ctx: SectionCtx) -> dict:
    sid, title = "active_sessions", "Active Sessions"
    now = _now()
    p = {"now": now, "recent": now - timedelta(minutes=15)}
    per_user = (await ctx.db.execute(text(
        "SELECT u.username, u.role, COALESCE(NULLIF(u.auth_source, ''), 'local'), count(*), "
        "count(*) FILTER (WHERE s.mfa_verified), "
        "string_agg(DISTINCT s.auth_method, ', ' ORDER BY s.auth_method), "
        "count(DISTINCT s.ip_address), max(s.last_seen_at), min(s.created_at), max(s.expires_at), "
        "count(*) FILTER (WHERE s.last_seen_at >= :recent) "
        "FROM auth_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.revoked_at IS NULL AND s.expires_at > :now "
        "GROUP BY u.username, u.role, u.auth_source ORDER BY 4 DESC, lower(u.username)"), p)).fetchall()
    if not per_user:
        return _section(sid, title, notes=["No account is signed in right now."])
    methods = (await ctx.db.execute(text(
        "SELECT auth_method, count(*), count(*) FILTER (WHERE mfa_verified), count(DISTINCT user_id) "
        "FROM auth_sessions WHERE revoked_at IS NULL AND expires_at > :now GROUP BY 1 ORDER BY 2 DESC"),
        p)).fetchall()
    roles = await _roles(ctx)

    sessions = sum(_n(r[3]) for r in per_user)
    recent = sum(_n(r[10]) for r in per_user)
    # MFA is verified by ZenPlus only for local password sign-ins; LDAP, RADIUS
    # and single sign-on sessions verify (or not) at the identity provider.
    pw = next((r for r in methods if r[0] == "password"), None)
    pw_sessions, pw_verified = (_n(pw[1]), _n(pw[2])) if pw else (0, 0)
    pw_share = _share(pw_verified, pw_sessions)
    priv_no_mfa = 0
    priv_accounts = 0
    for r in per_user:
        if _privileged(_effective(roles, r[1])):
            priv_accounts += 1
            if r[2] == "local":
                priv_no_mfa += _n(r[3]) - _n(r[4])
    top_n = int(ctx.opt("top_n"))
    shown = per_user[:top_n]
    return _section(
        sid, title,
        description=(f"{sessions:,} active session(s) held by {len(per_user):,} account(s) when the "
                     "report was generated."),
        kpis=[
            _kpi("Active sessions", _c(sessions), "primary", f"{recent:,} active in the last 15 minutes"),
            _kpi("Signed-in accounts", _c(len(per_user)), "info",
                 f"{priv_accounts:,} with privileged roles"),
            _kpi("MFA-verified", _pct1(pw_share),
                 "neutral" if pw_share is None else "success" if pw_share >= 100 else "warning",
                 f"{pw_verified:,} of {pw_sessions:,} password sessions" if pw_sessions
                 else "No password sessions"),
            _kpi("Privileged sessions without MFA", _c(priv_no_mfa),
                 "danger" if priv_no_mfa else "success", "Local accounts with privileged roles"),
        ],
        charts=[{"title": "Sessions by sign-in method",
                 "series": {"kind": "donut", "unit": "sessions",
                            "points": [{"label": _METHOD_LABELS.get(r[0], str(r[0] or "Other")),
                                        "value": _n(r[1])} for r in methods]}}] if methods else [],
        tables=[
            {"title": "Sessions by sign-in method",
             "headers": ["Method", "Sessions", "MFA-verified", "Accounts"],
             "styles": ["text", "num", "pct", "num"],
             "rows": [[_METHOD_LABELS.get(r[0], str(r[0] or "Other")), _c(r[1]),
                       _pct1(_share(_n(r[2]), _n(r[1]))), _c(r[3])] for r in methods]},
            {"title": _titled("Accounts with active sessions", len(shown), len(per_user), "accounts"),
             "headers": ["Account", "Role", "Sessions", "Methods", "MFA-verified", "Source addresses",
                         "Last activity", "Oldest session", "Latest expiry"],
             "styles": ["mono", "text", "num", "text", "num", "num", "text", "text", "text"],
             "rows": [[r[0], (roles.get(r[1] or "") or {}).get("display") or r[1] or "—", _c(r[3]),
                       ", ".join(_METHOD_LABELS.get(m.strip(), m.strip()) for m in (r[5] or "").split(",")
                                 if m.strip()) or "—",
                       f"{_n(r[4]):,} of {_n(r[3]):,}", _c(r[6]), ctx.fmt_short(r[7]), ctx.fmt_short(r[8]),
                       ctx.fmt_short(r[9])] for r in shown]},
        ],
        notes=[f"Sessions are kept until {SESSION_PURGE_DAYS} days after they expire, so this section "
               "reports the current state only.",
               "MFA-verified covers password sign-ins to local accounts; LDAP, RADIUS and single "
               "sign-on sessions are verified by their identity provider.",
               "Source addresses are counted, not listed; an administrator with users.manage can see "
               "them per account."],
    )


# ─── License usage ──────────────────────────────────────────────────────────

def _remote_subscription() -> Optional[dict]:
    """The OTA subscription the updater caches on disk (as the Subscription
    page reads it); None when absent or unreadable."""
    try:
        from app.api.v1.subscription import _load_remote_subscription
        remote = _load_remote_subscription()
    except Exception:
        return None
    return remote if isinstance(remote, dict) else None


@register_section(
    "license_usage", title="License Usage", category="Platform",
    description=("Subscription plan, days remaining and devices, service checks and users in use "
                 "against the licensed limits, as of report generation."),
    scope="fleet", requires=("settings.view",), semantics="snapshot",
    options=[{"key": "threshold", "label": "Warn above utilization (%)", "type": "number",
              "default": 80, "min": 1, "max": 100}],
)
async def license_usage(ctx: SectionCtx) -> dict:
    sid, title = "license_usage", "License Usage"
    from app.api.v1 import subscription as sub_api
    now = _now()
    warn = float(ctx.opt("threshold"))
    # No license key: the plan, status, expiry and limits are all a report needs.
    row = (await ctx.db.execute(text(
        "SELECT plan, status, started_at, expires_at, max_devices, max_service_checks, max_users "
        "FROM subscriptions ORDER BY created_at DESC LIMIT 1"))).first()
    remote = _remote_subscription()
    sub = None
    if row is not None or (remote and remote.get("plan")):
        sub = SimpleNamespace(plan=row[0] if row else "trial", status=row[1] if row else "active",
                              expires_at=_aware(row[3]) if row else None,
                              max_devices=row[4] if row else None,
                              max_service_checks=row[5] if row else None,
                              max_users=row[6] if row else None, activated_by=None)
        if remote and remote.get("plan"):
            try:
                sub_api._sync_from_remote(sub, remote)
            except Exception:
                logger.debug("remote subscription could not be applied", exc_info=True)
            sub.expires_at = _aware(sub.expires_at)
        if sub.expires_at is not None and sub.expires_at < now and sub.status == "active":
            sub.status = "expired"
    # Same counting rules as the Subscription page (api/v1/subscription.py):
    # controller-reported children do not consume device licenses.
    usage = (await ctx.db.execute(text(
        "SELECT (SELECT count(*) FROM devices WHERE poll_mode != 'via_controller'), "
        "(SELECT count(*) FROM devices WHERE poll_mode = 'via_controller'), "
        "(SELECT count(*) FROM service_checks), "
        "(SELECT count(*) FROM users WHERE is_active = TRUE)"))).first()
    if sub is None and usage is None:
        return _section(sid, title, notes=["No subscription is recorded on this appliance yet."])

    used = {"devices": usage[0], "checks": usage[2], "users": usage[3]} if usage else {}
    limits = {"devices": getattr(sub, "max_devices", None), "checks": getattr(sub, "max_service_checks", None),
              "users": getattr(sub, "max_users", None)} if sub else {}
    days_left: Optional[int] = None
    if sub is not None:
        if remote and "days_remaining" in remote and remote["days_remaining"] is not None:
            days_left = _n(remote["days_remaining"])
        elif sub.expires_at is not None:
            days_left = max(0, (sub.expires_at - now).days)

    rows_out, points, kpis = [], [], []
    if sub is not None:
        status = str(sub.status or "unknown").lower()
        kpis.append(_kpi("Subscription", status.capitalize(),
                         "success" if status == "active" else "danger" if status == "expired" else "warning",
                         f"{str(sub.plan or '—').replace('_', ' ').title()} plan"))
        if sub.expires_at is None and days_left is None:
            kpis.append(_kpi("Days remaining", "No expiry", "success"))
        else:
            kpis.append(_kpi("Days remaining", _c(days_left) if days_left is not None else "—",
                             "danger" if status == "expired" or (days_left is not None and days_left <= 7)
                             else "warning" if days_left is not None and days_left <= 30 else "success",
                             f"Expires {ctx.fmt_date(sub.expires_at)}" if sub.expires_at else None))
    for key, label in (("devices", "Devices"), ("checks", "Service checks"), ("users", "Users")):
        u = used.get(key)
        lim = limits.get(key)
        pct = _share(_n(u), _n(lim)) if (u is not None and lim) else None
        state = ("unknown" if pct is None else "critical" if pct > 100 else
                 "warning" if pct >= warn else "ok")
        value = f"{_c(u)} / {_c(lim)}" if (u is not None and lim) else (_c(u) if u is not None else "—")
        kpis.append(_kpi(label, value,
                         {"critical": "danger", "warning": "warning", "ok": "success"}.get(state, "neutral"),
                         f"{_pct1(pct)} of license" if pct is not None else "No licensed limit recorded"))
        rows_out.append([label, _c(u) if u is not None else "—", _c(lim) if lim else "—", _pct1(pct),
                         _c(_n(lim) - _n(u)) if (u is not None and lim) else "—", state])
        if pct is not None:
            points.append({"label": label, "value": round(pct, 1)})

    notes = ["Usage is counted the way the Subscription page counts it: devices reported through a "
             "wireless or SD-WAN controller do not consume device licenses, every service check "
             "counts, and only active user accounts count."]
    if usage and _n(usage[1]):
        notes.append(f"{_n(usage[1]):,} controller-reported device(s) are monitored without using a license.")
    if sub is None:
        notes.append("No subscription is recorded on this appliance yet; limits are not known.")
    notes.append("License usage has no history: this section shows the state when the report was generated.")
    return _section(
        sid, title,
        description="Subscription and licensed capacity when the report was generated.",
        kpis=kpis[:6],
        charts=[{"title": "License utilization",
                 "series": {"kind": "rank", "unit": "%", "color": "primary", "points": points}}]
        if points else [],
        tables=[{"title": "Licensed capacity",
                 "headers": ["Resource", "In use", "Licensed", "Utilization", "Headroom", "Status"],
                 "styles": ["text", "num", "num", "pct-bar", "num", "status"], "rows": rows_out}],
        notes=notes,
    )


# ─── ClickHouse storage inventory (same queries as storage_service) ─────────

_WINDOWS = ((1, "24 h"), (7, "7 d"), (30, "30 d"), (90, "90 d"), (365, "1 y"))
_TABLE_LABELS = {
    "ping_metrics": "Ping results", "ping_metrics_5m": "Ping 5-minute rollups",
    "ping_metrics_1h": "Ping hourly rollups", "snmp_metrics": "SNMP values",
    "snmp_metrics_5m": "SNMP 5-minute rollups", "snmp_metrics_1h": "SNMP hourly rollups",
    "snmp_if_metrics": "Interface counters", "snmp_if_metrics_5m": "Interface 5-minute rollups",
    "snmp_if_metrics_1h": "Interface hourly rollups", "service_metrics": "Service check results",
    "service_metrics_5m": "Service check 5-minute rollups", "device_status_log": "Device status changes",
    "service_status_log": "Service status changes", "snmp_traps": "SNMP traps",
    "flow_records": "NetFlow records", "flow_traffic_5m": "NetFlow traffic rollups",
    "flow_interface_5m": "NetFlow interface capacity", "apm_spans": "APM spans",
    "apm_span_metrics_5m": "APM 5-minute metrics", "apm_span_metrics_1h": "APM hourly metrics",
    "apm_rum_events": "Real-user monitoring events", "host_cpu_metrics": "Server CPU samples",
    "host_memory_metrics": "Server memory samples", "host_filesystem_metrics": "Server filesystem samples",
    "host_event_log_summary": "Server event log summaries",
}


def _valid_time(v: Any) -> Optional[datetime]:
    """system.parts reports 1970-01-01 when a table's partition key has no time."""
    dt = _aware(v) if isinstance(v, datetime) else None
    if dt is None and v is not None and hasattr(v, "year") and not isinstance(v, datetime):
        dt = datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    return dt if dt is not None and dt.year >= 2000 else None


async def _ch_tables(ctx: SectionCtx) -> list[dict]:
    """Managed ClickHouse tables: category, size, rows, oldest/newest and TTL
    (storage_service._ch_inventory_sync, through the report's client)."""
    key = "_platform_ch_tables"
    if key in ctx._cache:
        return ctx._cache[key]
    from app.services.storage_service import CH_DATABASE, _parse_ttl_days, classify_table
    p = {"db": CH_DATABASE}
    tables = await ctx.ch_rows(
        "SELECT name, engine, engine_full FROM system.tables WHERE database = %(db)s", p)
    parts = {r[0]: r for r in await ctx.ch_rows(
        "SELECT table, sum(bytes_on_disk), sum(rows), min(min_time), max(max_time), "
        "min(min_date), max(max_date) FROM system.parts WHERE active AND database = %(db)s "
        "GROUP BY table", p)}
    out = []
    for name, engine, engine_full in tables:
        category = classify_table(name)
        if category is None or "MergeTree" not in str(engine):
            continue
        st = parts.get(name)
        oldest = newest = None
        dated = False           # bounds known to the day only (Date partition key)
        if st:
            oldest = _valid_time(st[3]) or _valid_time(st[5])
            newest = _valid_time(st[4])
            if newest is None:
                newest, dated = _valid_time(st[6]), True
        out.append({"table": name, "category": category, "size": _n(st[1]) if st else 0,
                    "rows": _n(st[2]) if st else 0, "oldest": oldest, "newest": newest,
                    "dated": dated, "ttl": _parse_ttl_days(str(engine_full or ""))})
    ctx._cache[key] = out
    return out


def _covered_days(t: dict, now: datetime) -> Optional[float]:
    """Days of history a report can read from this table right now (capped at
    its TTL: older rows go at the next merge)."""
    if not t["rows"] or t["oldest"] is None:
        return None
    span = max(0.0, (now - t["oldest"]).total_seconds() / 86400.0)
    return min(span, float(t["ttl"])) if t["ttl"] else span


def _window_cell(covered: Optional[float], days: int) -> str:
    if covered is None:
        return "No data"
    if covered >= days - 0.01:
        return "Full"
    return f"{covered:.0f} d" if covered >= 1 else f"{covered * 24:.0f} h"


def _ttl_label(ttls: Iterable[Optional[int]]) -> str:
    vals = sorted({t for t in ttls if t})
    none = any(not t for t in ttls)
    if not vals:
        return "No TTL"
    label = f"{vals[0]} days" if len(vals) == 1 else f"{vals[0]}–{vals[-1]} days"
    return label + (" (some without TTL)" if none else "")


@register_section(
    "data_retention_coverage", title="Data Retention Coverage", category="Platform",
    description=("Per dataset: configured retention, the oldest data held and which report periods it "
                 "fully covers, as of report generation."),
    scope="fleet", requires=("system.admin",), semantics="snapshot",
    options=[top_n_option(60, 500)],
)
async def data_retention_coverage(ctx: SectionCtx) -> dict:
    sid, title = "data_retention_coverage", "Data Retention Coverage"
    from app.services.storage_service import CATEGORIES
    now = _now()
    notes: list[str] = []
    try:
        tables = await _ch_tables(ctx)
    except Exception:
        logger.warning("report: ClickHouse inventory unavailable", exc_info=True)
        tables = []
        notes.append("The time-series database did not respond; its datasets are not listed.")

    pg = (await ctx.db.execute(text(
        "SELECT (SELECT min(created_at) FROM audit_logs), (SELECT min(triggered_at) FROM alerts), "
        "(SELECT min(ts) FROM sensor_events), (SELECT min(occurred_at) FROM role_history), "
        "(SELECT min(generated_at) FROM report_runs), (SELECT min(created_at) FROM storage_events)"))).first()
    pg_rows = []
    if pg is not None:
        for (label, keep), oldest in zip((
                ("Audit log", None), ("Alerts", None), ("Sensor status changes", SENSOR_HISTORY_DAYS),
                ("Role change history", None), ("Generated reports", None), ("Storage events", None)), pg):
            oldest = _aware(oldest)
            span = (now - oldest).total_seconds() / 86400.0 if oldest else None
            covered = min(span, keep) if (span is not None and keep) else span
            pg_rows.append([label, f"{keep} days" if keep else "Kept until deleted",
                            ctx.fmt_date(oldest) if oldest else "No data"]
                           + [_window_cell(covered, d) for d, _l in _WINDOWS])

    if not tables and not pg_rows:
        return _section(sid, title, notes=notes + ["No retained datasets were found."])

    order = list(CATEGORIES)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for t in tables:
        by_cat[t["category"]].append(t)
    cat_rows, kpis = [], []
    cat_cover: dict[str, Optional[float]] = {}
    for cat in sorted(by_cat, key=lambda c: order.index(c) if c in order else len(order)):
        ts = by_cat[cat]
        with_data = [t for t in ts if t["rows"] and t["oldest"]]
        # The category's longest-held dataset (a feature enabled recently must
        # not make the whole category look short); each dataset is listed below.
        covered = max((_covered_days(t, now) for t in with_data), default=None)
        cat_cover[cat] = covered
        oldest = min((t["oldest"] for t in with_data), default=None)
        cat_rows.append([CATEGORIES.get(cat, {}).get("label", cat),
                         _ttl_label([t["ttl"] for t in ts]),
                         ctx.fmt_date(oldest) if oldest else "No data",
                         f"{covered:,.0f} days" if covered is not None else "—"]
                        + [_window_cell(covered, d) for d, _l in _WINDOWS])
    for cat, label in (("raw_metrics", "Raw metrics cover"), ("rollups_5m", "5-minute rollups cover"),
                       ("rollups_1h", "Hourly rollups cover"), ("events", "Event history covers")):
        if cat in cat_cover:
            cov = cat_cover[cat]
            kpis.append(_kpi(label, f"{cov:,.0f} days" if cov is not None else "—",
                             "neutral" if cov is None else "success" if cov >= 30 else "warning",
                             f"Retention {_ttl_label([t['ttl'] for t in by_cat[cat]])}"))
    if tables:
        kpis.append(_kpi("Time-series data", _fmt_bytes(sum(t["size"] for t in tables)), "info",
                         f"{sum(1 for t in tables if t['rows']):,} of {len(tables):,} tables hold data"))

    top_n = int(ctx.opt("top_n"))
    detail = sorted(tables, key=lambda t: (order.index(t["category"]) if t["category"] in order else 99,
                                           t["table"]))
    shown = detail[:top_n]
    window_heads = [label for _d, label in _WINDOWS]
    out_tables = []
    if cat_rows:
        out_tables.append({"title": "Coverage by data category",
                           "headers": ["Category", "Retention", "Oldest data", "History held"]
                           + window_heads,
                           "styles": ["text", "text", "text", "num"] + ["text"] * len(_WINDOWS),
                           "rows": cat_rows})
    if shown:
        out_tables.append({
            "title": _titled("Coverage by dataset", len(shown), len(detail), "datasets"),
            "headers": ["Dataset", "Table", "Category", "Retention", "Oldest data", "Newest data", "Rows",
                        "Size", "History held"],
            "styles": ["text", "mono", "text", "text", "text", "text", "num", "num", "num"],
            "rows": [[_TABLE_LABELS.get(t["table"], t["table"].replace("_", " ").capitalize()), t["table"],
                      CATEGORIES.get(t["category"], {}).get("label", t["category"]),
                      f"{t['ttl']} days" if t["ttl"] else "No TTL",
                      ctx.fmt_date(t["oldest"]) if t["oldest"] else "No data",
                      (ctx.fmt_date(t["newest"]) if t["dated"] else ctx.fmt_short(t["newest"]))
                      if t["newest"] else "—", _c(t["rows"]), _fmt_bytes(t["size"]),
                      (f"{_covered_days(t, now):,.0f} days" if _covered_days(t, now) is not None else "—")]
                     for t in shown]})
    if pg_rows:
        out_tables.append({"title": "Application history",
                           "headers": ["Dataset", "Retention", "Oldest record"] + window_heads,
                           "styles": ["text", "text", "text"] + ["text"] * len(_WINDOWS),
                           "rows": pg_rows})
    notes += ["“Full” means a report period of that length ending now reads complete data; a number "
              "is the history actually held. A category row reflects its longest-held dataset; the "
              "dataset table shows each one, since features enabled later hold less history.",
              "History is counted from the oldest retained row and capped at the table's retention "
              "(older rows are removed at the next merge). Retention is changed in Settings → Storage."]
    return _section(sid, title,
                    description="Retention and history held per dataset when the report was generated.",
                    kpis=kpis[:6], tables=out_tables, notes=notes)


# ─── Storage utilization ────────────────────────────────────────────────────

def _data_volume() -> Optional[dict]:
    """Usage of the appliance data volume (statvfs; no subprocess)."""
    try:
        from app.services.storage_service import DATA_MOUNT, _mount_usage
        return _mount_usage(DATA_MOUNT)
    except Exception:
        return None


_STORAGE_EVENTS = {"auto_purge": "Automatic purge", "manual_purge": "Manual purge",
                   "retention_applied": "Retention changed", "os_cleanup": "OS cleanup"}


@register_section(
    "storage_utilization", title="Storage Utilization", category="Platform",
    description=("Data volume usage and the size of each data category as of report generation, with "
                 "the purges, retention changes and space freed during the period."),
    scope="fleet", requires=("system.admin",),
    options=[top_n_option(15, 200)],
)
async def storage_utilization(ctx: SectionCtx) -> dict:
    sid, title = "storage_utilization", "Storage Utilization"
    from app.services.storage_service import CATEGORIES, get_retention_settings
    notes: list[str] = []
    vol = _data_volume()
    try:
        tables = await _ch_tables(ctx)
    except Exception:
        logger.warning("report: ClickHouse inventory unavailable", exc_info=True)
        tables = []
        notes.append("The time-series database did not respond; sizes by category are not shown.")
    pg_size = (await ctx.db.execute(text("SELECT pg_database_size(current_database())"))).scalar()
    auto = (await get_retention_settings(ctx.db)).get("auto_purge") or {}
    w = _window(ctx)
    totals = {r[0]: (_n(r[1]), _n(r[2])) for r in (await ctx.db.execute(text(
        "SELECT event_type, count(*), COALESCE(sum(freed_bytes), 0) FROM storage_events "
        "WHERE created_at >= :f AND created_at < :t AND event_type = ANY(:types) GROUP BY 1"),
        {**w, "types": list(_STORAGE_EVENTS)})).fetchall()}
    top_n = int(ctx.opt("top_n"))
    # Selected detail fields only: event details can carry error text.
    events = (await ctx.db.execute(text(
        "SELECT created_at, event_type, COALESCE(actor, 'system'), freed_bytes, "
        "details->>'older_than_days', details->>'usage_before_pct', details->>'usage_after_pct', "
        "details->>'usage_percent', "
        "CASE WHEN jsonb_typeof(details->'dropped_partitions') = 'array' "
        "THEN jsonb_array_length(details->'dropped_partitions') END, "
        "CASE WHEN jsonb_typeof(details->'errors') = 'array' THEN jsonb_array_length(details->'errors') END, "
        "CASE WHEN jsonb_typeof(details->'changes') = 'array' THEN jsonb_array_length(details->'changes') END "
        "FROM storage_events WHERE created_at >= :f AND created_at < :t AND event_type = ANY(:types) "
        "ORDER BY created_at DESC LIMIT :lim"), {**w, "types": list(_STORAGE_EVENTS), "lim": top_n})).fetchall()

    if vol is None and not tables and pg_size is None and not totals:
        return _section(sid, title, notes=notes + ["Storage usage could not be read on this appliance."])

    by_cat: dict[str, dict] = defaultdict(lambda: {"tables": 0, "rows": 0, "size": 0, "ttl": []})
    for t in tables:
        c = by_cat[t["category"]]
        c["tables"] += 1
        c["rows"] += t["rows"]
        c["size"] += t["size"]
        c["ttl"].append(t["ttl"])
    ch_total = sum(c["size"] for c in by_cat.values())
    purges = sum(totals.get(k, (0, 0))[0] for k in ("auto_purge", "manual_purge"))
    freed = sum(v[1] for v in totals.values())
    threshold = _num(auto.get("threshold_pct"))
    used_pct = _num(vol.get("usage_percent")) if vol else None
    kpis = [
        _kpi("Data volume used", _pct1(used_pct),
             "neutral" if used_pct is None else "danger" if threshold and used_pct >= threshold
             else "warning" if threshold and used_pct >= threshold - 10 else "success",
             (f"{_fmt_bytes(_n(vol.get('used_bytes')))} of {_fmt_bytes(_n(vol.get('total_bytes')))} · "
              f"{_fmt_bytes(_n(vol.get('free_bytes')))} free") if vol else "Not measured"),
        _kpi("Time-series data", _fmt_bytes(ch_total) if tables else "—", "info",
             f"{sum(c['rows'] for c in by_cat.values()):,} rows" if tables else None),
        # Customer-facing storage categories; engine names stay in support diagnostics.
        _kpi("Configuration and event database", _fmt_bytes(_n(pg_size)) if pg_size is not None else "—",
             "info", "Inventory, alerts, audit and settings"),
        _kpi("Purges in period", _c(purges), "warning" if totals.get("auto_purge") else "neutral",
             f"{totals.get('auto_purge', (0, 0))[0]:,} automatic"),
        _kpi("Space freed", _fmt_bytes(freed), "info" if freed else "neutral", "In the period"),
        _kpi("Automatic purge", f"At {threshold:.0f}%" if auto.get("enabled") and threshold else "Off",
             "success" if auto.get("enabled") else "warning",
             (f"Purges to {_num(auto.get('target_pct')) or 0:.0f}%, keeps {auto.get('min_keep_days')} days"
              if auto.get("enabled") else "The data volume can fill up")),
    ]
    cats = sorted(by_cat.items(), key=lambda kv: -kv[1]["size"])
    out_tables = []
    if cats:
        out_tables.append({
            "title": "Time-series storage by category",
            "headers": ["Category", "Tables", "Rows", "Size", "Share", "Retention"],
            "styles": ["text", "num", "num", "num", "pct-bar", "text"],
            "rows": [[CATEGORIES.get(k, {}).get("label", k), _c(c["tables"]), _c(c["rows"]),
                      _fmt_bytes(c["size"]), _pct1(_share(c["size"], ch_total)), _ttl_label(c["ttl"])]
                     for k, c in cats]})
        largest = sorted(tables, key=lambda t: -t["size"])[:top_n]
        out_tables.append({
            "title": _titled("Largest tables", len(largest), len(tables), "tables"),
            "headers": ["Table", "Category", "Rows", "Size", "Share"],
            "styles": ["mono", "text", "num", "num", "pct-bar"],
            "rows": [[t["table"], CATEGORIES.get(t["category"], {}).get("label", t["category"]),
                      _c(t["rows"]), _fmt_bytes(t["size"]), _pct1(_share(t["size"], ch_total))]
                     for t in largest]})

    def detail(r) -> str:
        bits = []
        older, before, after, at = _num(r[4]), _num(r[5]), _num(r[6]), _num(r[7])
        if older is not None:
            bits.append(f"older than {older:.0f} days")
        if before is not None and after is not None:
            bits.append(f"volume {before:.1f}% → {after:.1f}%")
        elif at is not None:
            bits.append(f"volume at {at:.1f}%")
        if r[8]:
            bits.append(f"{_n(r[8]):,} partition(s) dropped")
        if r[10]:
            bits.append(f"{_n(r[10]):,} table(s) changed")
        if r[9]:
            bits.append(f"{_n(r[9]):,} error(s)")
        return "; ".join(bits) or "—"

    if events:
        total_events = sum(v[0] for v in totals.values())
        out_tables.append({
            "title": _titled("Storage events in the period", len(events), total_events, "events"),
            "headers": ["Time", "Event", "By", "Space freed", "Detail"],
            "styles": ["text", "text", "mono", "num", "text"],
            "rows": [[ctx.fmt_dt(r[0]), _STORAGE_EVENTS.get(r[1], r[1]), r[2],
                      _fmt_bytes(_n(r[3])) if _n(r[3]) else "—", detail(r)] for r in events]})
    else:
        notes.append("No purge, retention change or OS cleanup was recorded in this period.")
    notes.append("Volume usage and sizes are measured when the report is generated; purges and space "
                 "freed cover the reporting period.")
    findings = []
    if used_pct is not None and threshold and used_pct >= threshold - 10:
        findings.append({"severity": "danger" if used_pct >= threshold else "warning", "weight": used_pct,
                         "text": f"The data volume is {_pct1(used_pct)} full (automatic purge at {threshold:.0f}%)",
                         "action": "Review retention in Settings → Storage", "path": "/settings/general?tab=storage"})
    if not auto.get("enabled"):
        findings.append({"severity": "warning",
                         "text": "Automatic purge is off: the data volume can fill up",
                         "action": "Turn it on in Settings → Storage", "path": "/settings/general?tab=storage"})
    return _section(
        sid, title,
        description="Data volume usage and storage by category, with storage maintenance in the period.",
        email_scope="This appliance",
        findings=findings,
        kpis=kpis,
        charts=[{"title": "Time-series storage by category",
                 "series": {"kind": "donut", "unit": "B",
                            "points": [{"label": CATEGORIES.get(k, {}).get("label", k), "value": c["size"]}
                                       for k, c in cats if c["size"]]}}] if ch_total else [],
        tables=out_tables, notes=notes,
    )


# ─── Backups ────────────────────────────────────────────────────────────────

_BACKUP_STATUS = {"completed": "success", "failed": "failed", "running": "pending"}


def _next_backup(schedule: dict, now: datetime) -> Optional[datetime]:
    """Next run of the backup schedule (hour_utc, and weekday for weekly)."""
    if not schedule.get("enabled"):
        return None
    try:
        hour = int(schedule.get("hour_utc", 2))
        weekday = int(schedule.get("weekday", 6))
    except (TypeError, ValueError):
        return None
    cand = now.replace(minute=0, second=0, microsecond=0, hour=hour % 24)
    for _ in range(9):
        if cand > now and (schedule.get("frequency") != "weekly" or cand.weekday() == weekday):
            return cand
        cand += timedelta(days=1)
    return None


@register_section(
    "backup_status", title="Backup Status", category="Platform",
    description=("Age of the last successful appliance backup, backups and restores in the period, "
                 "failures with their reason and the backup schedule."),
    scope="fleet", requires=("system.admin",),
    options=[{"key": "max_age_days", "label": "Backup is overdue after (days)", "type": "int",
              "default": 7, "min": 1, "max": 90},
             top_n_option(20, 200)],
)
async def backup_status(ctx: SectionCtx) -> dict:
    sid, title = "backup_status", "Backup Status"
    from app.services.storage_service import get_backup_schedule
    now = _now()
    max_age = int(ctx.opt("max_age_days"))
    w = _window(ctx)
    agg = (await ctx.db.execute(text(
        "SELECT max(COALESCE(finished_at, created_at)) FILTER (WHERE status = 'completed'), "
        "count(*) FILTER (WHERE status = 'completed'), "
        "COALESCE(sum(size_bytes) FILTER (WHERE status = 'completed'), 0), "
        "count(*) FILTER (WHERE status = 'running'), count(*) "
        "FROM storage_backups"))).first()
    last_ok = (await ctx.db.execute(text(
        "SELECT COALESCE(finished_at, created_at), kind, include_clickhouse, size_bytes, created_by "
        "FROM storage_backups WHERE status = 'completed' "
        "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1"))).first()
    events = {r[0]: _n(r[1]) for r in (await ctx.db.execute(text(
        "SELECT event_type, count(*) FROM storage_events WHERE created_at >= :f AND created_at < :t "
        "AND event_type IN ('backup_created', 'backup_failed', 'backup_deleted', 'restore_started', "
        "'restore_completed', 'restore_failed') GROUP BY 1"), w)).fetchall()}
    top_n = int(ctx.opt("top_n"))
    recent = (await ctx.db.execute(text(
        "SELECT created_at, finished_at, COALESCE(created_by, ''), kind, status, include_clickhouse, "
        "size_bytes, left(COALESCE(error, ''), 160), last_restore_at, last_restore_status, "
        "(created_at >= :f AND created_at < :t) AS in_window "
        "FROM storage_backups ORDER BY created_at DESC LIMIT :lim"), {**w, "lim": top_n})).fetchall()
    schedule = await get_backup_schedule(ctx.db)

    stored_total = _n(agg[4]) if agg else 0
    catalogue_note = ("Pre-upgrade snapshots and external backups are not listed in the catalogue.")
    if not stored_total and not events:
        # Only the in-app catalogue is read: the updater's pre-upgrade
        # snapshots and backups taken outside ZenPlus may exist regardless.
        notes = [f"No backup is recorded in the ZenPlus backup catalogue (Settings → Storage → Backups) "
                 f"as of {ctx.fmt_dt(now)} {ctx.tz_label()}. {catalogue_note}"]
        findings = [{"severity": "danger",
                     "text": "No backup is recorded in the ZenPlus backup catalogue",
                     "action": "Take a backup or turn on scheduled backups in Settings → Storage",
                     "path": "/settings/general?tab=storage"}]
        if not schedule.get("enabled"):
            notes.append("Scheduled backups are off: turn them on in Settings → Storage → Backups.")
        return _section(sid, title, email_scope="ZenPlus backup catalogue", findings=findings, kpis=[
            _kpi("Since last successful backup", "None catalogued", "danger",
                 "No backup in the ZenPlus catalogue"),
            _kpi("Schedule", "Off" if not schedule.get("enabled") else str(schedule.get("frequency")).title(),
                 "warning" if not schedule.get("enabled") else "info")], notes=notes)

    last_at = _aware(last_ok[0]) if last_ok else None
    age = (now - last_at).total_seconds() if last_at else None
    overdue = age is None or age > max_age * 86400
    created, failed = events.get("backup_created", 0), events.get("backup_failed", 0)
    nxt = _next_backup(schedule, now)
    freq = str(schedule.get("frequency") or "weekly")
    # The next run is shown in the organisation timezone (the setting itself is UTC).
    sched_label = ("Daily" if freq == "daily" else "Weekly") if schedule.get("enabled") else "Off"
    kpis = [
        _kpi("Since last successful backup", _duration(age) if age is not None else "None catalogued",
             "danger" if overdue else "success",
             (f"{ctx.fmt_dt(last_at)} · overdue after {max_age} days" if overdue and last_at else
              ctx.fmt_dt(last_at) if last_at else f"Overdue after {max_age} days")),
        _kpi("Backups in period", _c(created), "success" if created else "neutral",
             f"{failed:,} failed" if failed else "No failures"),
        _kpi("Failed backups", _c(failed), "danger" if failed else "success", "In the period"),
        _kpi("Time-series data included",
             ("Yes" if last_ok[2] else "No") if last_ok else "—",
             "neutral" if not last_ok else "success" if last_ok[2] else "warning",
             "In the last successful backup" if last_ok else None),
        _kpi("Schedule", sched_label, "success" if schedule.get("enabled") else "warning",
             f"Next {ctx.fmt_short(nxt)} · keeps {schedule.get('keep_last')}" if nxt else
             "Turn on in Settings → Storage"),
        _kpi("Stored backups", _c(agg[1]) if agg else "—", "info",
             _fmt_bytes(_n(agg[2])) if agg else None),
    ]

    def kind_label(r) -> str:
        return "Full (with time-series data)" if r[5] else "Configuration and database"

    rows_out = []
    for r in recent:
        start, end = _aware(r[0]), _aware(r[1])
        restore = "—"
        if r[8]:
            restore = f"{str(r[9] or 'unknown').lower()} {ctx.fmt_short(r[8])}"
        rows_out.append([ctx.fmt_dt(start), kind_label(r),
                         "Schedule" if r[2] == "schedule" else (r[2] or "—"),
                         _BACKUP_STATUS.get(str(r[4]), str(r[4] or "unknown")),
                         _fmt_bytes(_n(r[6])) if _n(r[6]) else "—",
                         _duration((end - start).total_seconds()) if (start and end) else "—",
                         restore, _clip(_mask(r[7]), 120) or "—"])
    in_window = sum(1 for r in recent if r[10])
    notes = []
    if not in_window and recent:
        notes.append("No backup was started in this period; the most recent backups are listed.")
    if events.get("restore_completed") or events.get("restore_failed"):
        notes.append(f"Restores in the period: {events.get('restore_completed', 0):,} completed, "
                     f"{events.get('restore_failed', 0):,} failed.")
    if agg and _n(agg[3]):
        notes.append(f"{_n(agg[3]):,} backup(s) are running now.")
    notes.append("Counts in the period come from the storage event log, which keeps the outcome of "
                 "backups pruned by the schedule's retention; the table lists the backup catalogue. "
                 + catalogue_note)
    if not schedule.get("enabled"):
        notes.append("Scheduled backups are off: backups are only taken when someone starts one.")
    findings = []
    if overdue:
        findings.append({"severity": "danger", "weight": (age or 0) / 86400,
                         "text": (f"The last catalogued backup is {_duration(age)} old (overdue after "
                                  f"{max_age} days)") if age is not None else
                                 "No successful backup is recorded in the ZenPlus backup catalogue",
                         "action": "Take a backup in Settings → Storage", "path": "/settings/general?tab=storage"})
    if failed:
        findings.append({"severity": "danger", "weight": failed,
                         "text": f"{failed:,} backup(s) failed in the period",
                         "action": "Review the failure reason in Settings → Storage",
                         "path": "/settings/general?tab=storage"})
    if not schedule.get("enabled"):
        findings.append({"severity": "warning", "text": "Scheduled backups are off",
                         "action": "Turn them on in Settings → Storage", "path": "/settings/general?tab=storage"})
    return _section(
        sid, title,
        description=(f"Appliance backups: the last success, outcomes in the period and the schedule; "
                     f"a backup older than {max_age} days is overdue."),
        email_scope="ZenPlus backup catalogue",
        findings=findings,
        kpis=kpis,
        tables=[{"title": _titled("Backups", len(rows_out), stored_total, "backups"),
                 "headers": ["Started", "Type", "Started by", "Status", "Size", "Duration", "Last restore",
                             "Error"],
                 "styles": ["text", "text", "text", "status", "num", "num", "text", "text"],
                 "rows": rows_out}] if rows_out else [],
        notes=notes,
    )


# ─── Report delivery ────────────────────────────────────────────────────────

# First match wins: most specific first ("2 direct recipient(s): timed out" is
# a connection problem, not a rejected recipient).
_FAILURE_RULES = (
    ("No SMTP gateway configured", re.compile(r"no smtp gateway", re.I)),
    ("Channel missing or disabled", re.compile(r"channel missing|disabled", re.I)),
    ("Channel has no recipients", re.compile(r"no recipients|no phone numbers", re.I)),
    ("Attachment not generated", re.compile(r"attachment", re.I)),
    ("Sections withheld or skipped", re.compile(r"section|netflow|withheld|owner", re.I)),
    ("Mail server unreachable or timed out",
     re.compile(r"timed out|timeout|connection refused|unreachable|could not connect|connect", re.I)),
    ("Mail server rejected the sign-in", re.compile(r"authenticat|\b53[045]\b", re.I)),
    ("Recipient rejected", re.compile(r"\b55[0-4]\b|refused|rejected|mailbox", re.I)),
)


def _failure_reason(fragment: str) -> str:
    for label, rx in _FAILURE_RULES:
        if rx.search(fragment):
            return label
    return "Other"


_FREQ = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}


@register_section(
    "report_delivery", title="Report Delivery Health", category="Platform",
    description=("Scheduled and on-demand report runs by schedule and outcome, the reasons deliveries "
                 "failed, and schedules that are overdue now."),
    scope="fleet", requires=("reports.view",),
    options=[top_n_option(25, 500)],
)
async def report_delivery(ctx: SectionCtx) -> dict:
    sid, title = "report_delivery", "Report Delivery Health"
    from app.services import report_templates as rt
    now = _now()
    w = _window(ctx)
    totals_sql = text(
        "SELECT count(*), count(*) FILTER (WHERE status = 'success'), "
        "count(*) FILTER (WHERE status = 'partial'), count(*) FILTER (WHERE status = 'failed') "
        "FROM report_runs WHERE generated_at >= :f AND generated_at < :t")
    cur = (await ctx.db.execute(totals_sql, w)).first()
    runs, ok, partial, failed = (_n(v) for v in cur) if cur else (0, 0, 0, 0)
    prev = (await ctx.db.execute(totals_sql, _prior(ctx))).first()
    p_runs = _n(prev[0]) if prev else None
    schedules = (await ctx.db.execute(text(
        "SELECT id::text, name, enabled, frequency, report_type, last_run_at, last_status, next_run_at, "
        "custom_report_id IS NOT NULL FROM report_schedules ORDER BY lower(name)"))).fetchall()
    if not runs and not schedules:
        return _section(sid, title, notes=["No report was generated or scheduled in this period."])

    per = {r[0]: r for r in (await ctx.db.execute(text(
        "SELECT schedule_id::text, count(*), count(*) FILTER (WHERE status = 'success'), "
        "count(*) FILTER (WHERE status = 'partial'), count(*) FILTER (WHERE status = 'failed'), "
        "max(generated_at) FROM report_runs WHERE generated_at >= :f AND generated_at < :t "
        "GROUP BY 1"), w)).fetchall()}
    errors = (await ctx.db.execute(text(
        "SELECT error FROM report_runs WHERE generated_at >= :f AND generated_at < :t "
        "AND status IN ('failed', 'partial') AND error IS NOT NULL "
        "ORDER BY generated_at DESC LIMIT 2000"), w)).fetchall()
    unit = _unit(ctx)
    counts: dict[str, dict[int, float]] = {"success": {}, "partial": {}, "failed": {}}
    for b, st, n in (await ctx.db.execute(text(
            f"SELECT {_pg_bucket('generated_at')} AS b, status, count(*) FROM report_runs "
            "WHERE generated_at >= :f AND generated_at < :t GROUP BY 1, 2"),
            {**w, **_bucket_params(ctx)})).fetchall():
        if st in counts:
            counts[st][int(b)] = _n(n)

    reasons: dict[str, list] = {}
    for (err,) in errors:
        seen: set[str] = set()
        for frag in str(err or "").split("; "):
            frag = frag.strip()
            if not frag:
                continue
            label = _failure_reason(frag)
            entry = reasons.setdefault(label, [0, _clip(_mask(frag), 140)])
            if label not in seen:
                entry[0] += 1
                seen.add(label)
    enabled = [s for s in schedules if s[2]]
    overdue = [s for s in enabled if s[7] is not None and _aware(s[7]) < now - OVERDUE_GRACE]
    unscheduled = [s for s in enabled if s[7] is None]

    def report_name(key: str, organisation: bool) -> str:
        # An organisation template's name may be private to its owner.
        if organisation:
            return "Organisation template"
        t = rt.TEMPLATES.get(key)
        return t.title if t else str(key or "—").replace("_", " ").capitalize()

    def outcome(stats) -> list[str]:
        n = _n(stats[1]) if stats else 0
        return [_c(n), _pct1(_share(_n(stats[2]), n)) if n else "—",
                f"{_n(stats[3]) if stats else 0:,} / {_n(stats[4]) if stats else 0:,}"]

    sched_rows = []
    for s in schedules:
        stats = per.get(s[0])
        sched_rows.append({"sort": (not s[2], -(_n(stats[4]) if stats else 0), (s[1] or "").lower()),
                           "row": [s[1] or "—", report_name(s[4], bool(s[8])),
                                   _FREQ.get(s[3], str(s[3] or "—"))] + outcome(stats)
                           + [str(s[6] or "—").lower(), ctx.fmt_short(s[7]) if s[2] else "Paused"]})
    if None in per:
        stats = per[None]
        sched_rows.append({"sort": (False, -_n(stats[4]), "~"),
                           "row": ["On demand", "—", "—"] + outcome(stats) + ["—", "—"]})
    sched_rows.sort(key=lambda x: x["sort"])
    top_n = int(ctx.opt("top_n"))
    shown = [x["row"] for x in sched_rows[:top_n]]
    tables = [{"title": _titled("Delivery by schedule", len(shown), len(sched_rows), "schedules"),
               "headers": ["Schedule", "Report", "Frequency", "Runs", "Delivered", "Partial / failed",
                           "Last status", "Next run"],
               "styles": ["text", "text", "text", "num", "pct", "num", "status", "text"],
               "rows": shown}]
    if reasons:
        tables.append({"title": "Why deliveries failed",
                       "headers": ["Reason", "Runs affected", "Example"],
                       "styles": ["text", "num", "text"],
                       "rows": [[k, _c(v[0]), v[1]] for k, v in
                                sorted(reasons.items(), key=lambda kv: -kv[1][0])]})
    if overdue:
        tables.append({"title": "Overdue schedules",
                       "headers": ["Schedule", "Frequency", "Due since", "Last run", "Last status"],
                       "styles": ["text", "text", "text", "text", "status"],
                       "rows": [[s[1] or "—", _FREQ.get(s[3], str(s[3] or "—")), ctx.fmt_dt(s[7]),
                                 ctx.fmt_short(s[5]), str(s[6] or "—").lower()]
                                for s in sorted(overdue, key=lambda s: _aware(s[7]))[:top_n]]})
    notes = ["Delivered means every channel and recipient accepted the report; partial means some "
             "did, or a section or attachment could not be produced. E-mail addresses in error "
             "messages are masked."]
    if unscheduled:
        notes.append(f"{len(unscheduled):,} enabled schedule(s) have no next run time and will not fire.")
    return _section(
        sid, title,
        description=(f"{runs:,} report run(s) over the reporting period across {len(enabled):,} "
                     "enabled schedule(s)."),
        kpis=[
            _kpi("Report runs", _c(runs), "primary", _delta(runs, p_runs)),
            _kpi("Delivered", _pct1(_share(ok, runs)),
                 "neutral" if not runs else "success" if ok == runs else "warning",
                 f"{ok:,} of {runs:,} runs" if runs else "No runs in the period"),
            _kpi("Partially delivered", _c(partial), "warning" if partial else "success"),
            _kpi("Failed", _c(failed), "danger" if failed else "success"),
            _kpi("Overdue schedules", _c(len(overdue)), "danger" if overdue else "success",
                 f"of {len(enabled):,} enabled"),
        ],
        charts=[{"title": f"Report runs per {unit} by outcome",
                 "series": _stacked(ctx, unit, counts, [("success", "Delivered", "success"),
                                                        ("partial", "Partial", "warning"),
                                                        ("failed", "Failed", "danger")], "runs", "primary")}]
        if runs else [],
        tables=tables, notes=notes,
    )


# ─── Built-in templates ─────────────────────────────────────────────────────

template(
    "user_access_review", "User Access Review",
    "Periodic access recertification: every account by role and sign-in source, MFA coverage, "
    "privileged, dormant and locked accounts, role changes in the period and who is signed in now.",
    "security",
    [S("access_review", None, days=90), S("role_changes"), S("active_sessions")],
    audience=("Security & audit", "IT manager"), tags=("access review", "mfa", "privileged", "dormant"),
    default_period="last_90d", frequency="monthly", icon="ShieldCheck", featured=True,
)

template(
    "sign_in_activity", "Sign-in Activity",
    "Successful and failed sign-ins per day, failure reasons, the usernames and source addresses "
    "with the most failures, lockouts and the sessions open now.",
    "security",
    [S("login_activity", None, top_n=15), S("active_sessions")],
    audience=("Security & audit", "NOC operator"), tags=("sign-in", "failed logins", "lockout"),
    default_period="last_7d", frequency="weekly", icon="Activity",
)

template(
    "administrative_audit_trail", "Administrative Audit Trail",
    "Every administrative change in the period by action, resource and person, the access-control "
    "changes on their own and the role edits — with field names, never values.",
    "security",
    [S("audit_activity", None, top_n=50),
     S("audit_activity", "Access-Control Changes", focus="access", top_n=25),
     S("role_changes")],
    audience=("Security & audit", "IT manager"), tags=("audit", "changes", "compliance"),
    default_period="last_7d", frequency="weekly", icon="FileCheck2", featured=True,
)

template(
    "role_permission_changes", "Role and Permission Changes",
    "Roles created, changed or deleted with the permissions added and removed, privileged grants, "
    "the members affected and the user and role administration around them.",
    "security",
    [S("role_changes", None, top_n=50),
     S("audit_activity", "User and Role Administration", focus="access", top_n=50)],
    audience=("Security & audit",), tags=("roles", "permissions", "rbac"),
    default_period="last_30d", frequency="monthly", icon="Layers",
)

template(
    "active_sessions_report", "Active Sessions",
    "Who is signed in right now, by method, with the share of sessions verified by MFA and "
    "privileged sessions without it.",
    "security",
    [S("active_sessions", None, top_n=100)],
    audience=("Security & audit", "IT manager"), tags=("sessions", "mfa"),
    default_period="last_24h", frequency="daily", icon="MonitorSmartphone",
)

template(
    "sensor_fleet_health_report", "Sensor Fleet Health",
    "Remote sensor state from heartbeats, versions against the required minimum, dropped results, "
    "assigned targets, availability over the period and results stored per poller.",
    "platform",
    ["sensor_fleet_health", "sensor_availability", "sensor_results_volume"],
    audience=("NOC operator", "Network engineer"), tags=("sensors", "pollers", "heartbeat"),
    default_period="last_7d", frequency="weekly", icon="Server", featured=True,
)

template(
    "sensor_availability_report", "Sensor Availability",
    "Connected share of the period for every remote sensor, offline episodes and the longest outage, "
    "with the results each poller stored.",
    "platform",
    [S("sensor_availability", None, top_n=100), S("sensor_results_volume", None, top_n=50)],
    audience=("NOC operator", "IT manager"), tags=("sensors", "availability", "sla"),
    default_period="last_30d", frequency="monthly", icon="Activity",
)

template(
    "license_usage_report", "License Usage",
    "Subscription plan and days remaining, with devices, service checks and users in use against "
    "the licensed limits.",
    "platform",
    ["license_usage", "inventory_summary"],
    audience=("IT manager", "Executive"), tags=("license", "subscription", "capacity"),
    default_period="last_30d", frequency="monthly", icon="Briefcase",
)

template(
    "data_retention_coverage_report", "Data Retention Coverage",
    "Configured retention and the history actually held for every dataset, and which report "
    "periods each one fully covers.",
    "platform",
    ["data_retention_coverage", S("storage_utilization", None, top_n=10)],
    audience=("IT manager", "Systems engineer"), tags=("retention", "storage", "data coverage"),
    default_period="last_30d", frequency="monthly", icon="HardDrive",
)

template(
    "storage_backup_status", "Storage and Backup Status",
    "Data volume usage and storage by category, purges and space freed, and appliance backups — "
    "the last success, failures and the schedule.",
    "platform",
    ["storage_utilization", "backup_status"],
    audience=("Systems engineer", "IT manager"), tags=("storage", "backup", "disk"),
    default_period="last_7d", frequency="weekly", icon="HardDrive",
)

template(
    "report_delivery_health", "Report Delivery Health",
    "Report runs by schedule and outcome, why deliveries failed and schedules that are overdue.",
    "platform",
    [S("report_delivery", None, top_n=50)],
    audience=("IT manager", "NOC operator"), tags=("reports", "schedules", "delivery"),
    default_period="last_7d", frequency="weekly", icon="FileText",
)
