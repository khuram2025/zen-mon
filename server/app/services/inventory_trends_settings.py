"""Trends (GET /inventory/trends) and module settings (GET and PUT /inventory/settings).

Trends read ``inventory_daily_snapshots``, which the daily snapshot loop writes for the whole estate. The table has
no tags, so a restricted caller gets ``restricted: true`` and no series (a fleet-wide series would leak the size of
assets the caller may not see; principle 3). A day with no snapshot is a gap in the series, never a zero.

Settings are ``system_settings['inventory']`` (doc 02 section 9). Reading merges the stored blob over the defaults
with ``inventory_service.merge_inventory_settings``; writing validates the request, merges it over the CURRENT
settings (keys left out keep their value), stores the whole merged blob, audits the change and drops the summary cache.

A write is a read-modify-write of one blob, so writers are serialised by a transaction-scoped advisory lock (a row lock
cannot be taken before the row exists): two administrators saving different keys at the same moment both keep their
change (F11 / BN-07). The audit entry is part of the write: when it cannot be stored nothing is saved and the route
answers 503 (F1).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_normalize
from app.services import inventory_query as iq
from app.services import inventory_service as svc
from app.services import inventory_software_query as isq
from app.services.audit_service import AuditUnavailable, write_audit_log

TREND_METRICS = svc.SNAPSHOT_METRICS
MIN_TREND_DAYS, MAX_TREND_DAYS, DEFAULT_TREND_DAYS = 1, 400, 30
MAX_TREND_SERIES = 60

SETTINGS_ACTION = "inventory.settings.update"
SETTINGS_LOCK = "inventory-settings"       # key (hashed by the database) of the advisory lock that serialises writers
SETTINGS_FIELDS = ("fresh_hours", "stale_days", "change_retention_days", "snapshot_hour",
                   "hide_categories_by_default", "trends_enabled")
_INT_FIELDS = ("stale_days", "change_retention_days", "snapshot_hour")
SETTINGS_LIMITS = {
    "fresh_hours": svc._FRESH_HOURS_RANGE,
    "stale_days": svc._STALE_DAYS_RANGE,
    "change_retention_days": svc._RETENTION_DAYS_RANGE,
    "snapshot_hour": svc._SNAPSHOT_HOUR_RANGE,
}


# ── Trends ───────────────────────────────────────────────────────────

async def trends(db: AsyncSession, *, scope: Optional[Sequence[str]], metric: str, dimension: Optional[str] = None,
                 days: int = DEFAULT_TREND_DAYS, settings: Optional[Mapping[str, Any]] = None,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Snapshot series of one metric family over the last ``days`` appliance-local days (today included).

    ``metric`` is one of ``svc.SNAPSHOT_METRICS``; ``dimension`` is exact and optional (without it every dimension
    of the metric is returned, at most ``MAX_TREND_SERIES`` of them, those with the highest latest value first,
    and ``truncated`` says more exist). The series hold only the days that have a snapshot (``days_with_data``
    counts the distinct days over all series: the dashboard draws a sparkline from three).
    """
    if metric not in TREND_METRICS:
        raise iq.InvalidFilter(f"metric must be one of: {', '.join(TREND_METRICS)}")
    if not isinstance(days, int) or isinstance(days, bool) or not MIN_TREND_DAYS <= days <= MAX_TREND_DAYS:
        raise iq.InvalidFilter(f"days must be between {MIN_TREND_DAYS} and {MAX_TREND_DAYS}")
    dimension = iq._clean(dimension, 255)
    now = now or iq.utcnow()
    await iq._tune(db)
    settings = await iq._settings(db, settings)
    zone = await svc.get_appliance_zone(db)
    today = svc.snapshot_local_day(now, zone)
    since = today - timedelta(days=days - 1)
    out: dict[str, Any] = {
        "generated_at": iq.iso(now), "restricted": scope is not None, "enabled": bool(settings["trends_enabled"]),
        "metric": metric, "dimension": dimension, "days": days,
        "window": {"since": since.isoformat(), "until": today.isoformat()},
        "first_day": None, "days_with_data": 0, "dimensions_total": 0, "truncated": False, "series": [],
    }
    if scope is not None or not out["enabled"]:
        return out      # the snapshots are fleet-wide: nothing a restricted caller may see, or the owner switched them off
    p: dict[str, Any] = {"m": metric, "since": since, "until": today}
    extra = ""
    if dimension is not None:
        p["d"] = dimension
        extra = " AND dimension = :d"
    rows = await iq._run(db, f"""
        SELECT day, dimension, value FROM inventory_daily_snapshots
         WHERE metric = :m AND day >= :since AND day <= :until{extra} ORDER BY dimension, day""", p)
    first = await iq._run(db, "SELECT min(day) AS d FROM inventory_daily_snapshots WHERE metric = :m", {"m": metric})
    out["first_day"] = first[0]["d"].isoformat() if first and first[0]["d"] is not None else None
    series: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        series.setdefault(r["dimension"], []).append({"day": r["day"].isoformat(), "value": int(r["value"])})
    if dimension is not None:
        series.setdefault(dimension, [])
    out["dimensions_total"] = len(series)
    names = sorted(series, key=lambda d: (-(series[d][-1]["value"] if series[d] else -1), d))
    if dimension is None and len(names) > MAX_TREND_SERIES:
        names = names[:MAX_TREND_SERIES]
        out["truncated"] = True
    out["series"] = [{"dimension": d, "points": series[d]} for d in names]
    out["days_with_data"] = len({pt["day"] for d in names for pt in series[d]})
    return out


# ── Settings ─────────────────────────────────────────────────────────

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def validate_update(body: Any) -> dict[str, Any]:
    """The request body checked against the settings vocabulary. Raises ``InvalidFilter`` (a 422) naming every problem:
    unknown keys, values of the wrong type and unknown categories. A number outside its range is NOT an error: the
    validator in ``inventory_service`` clamps it and the response says what was stored."""
    if not isinstance(body, Mapping):
        raise iq.InvalidFilter("the body must be a JSON object")
    problems: list[str] = []
    unknown = sorted(str(k) for k in body if k not in SETTINGS_FIELDS)
    if unknown:
        problems.append("unknown setting" + ("s" if len(unknown) > 1 else "") + ": " + ", ".join(unknown))
    for name in _INT_FIELDS:
        if name in body and not _is_number(body[name]):
            problems.append(f"{name} must be a number")
    if "fresh_hours" in body:
        fh = body["fresh_hours"]
        if not isinstance(fh, Mapping):
            problems.append("fresh_hours must be an object with server and device")
        else:
            bad = sorted(str(k) for k in fh if k not in ("server", "device"))
            if bad:
                problems.append("unknown fresh_hours key" + ("s" if len(bad) > 1 else "") + ": " + ", ".join(bad))
            for kind in ("server", "device"):
                if kind in fh and not _is_number(fh[kind]):
                    problems.append(f"fresh_hours.{kind} must be a number")
    if "hide_categories_by_default" in body:
        cats = body["hide_categories_by_default"]
        if not isinstance(cats, (list, tuple)) or not all(isinstance(c, str) for c in cats):
            problems.append("hide_categories_by_default must be a list of category names")
        else:
            bad = sorted({c for c in cats if c not in inventory_normalize.CATEGORIES})
            if bad:
                problems.append("unknown categories: " + ", ".join(bad))
    if "trends_enabled" in body and not isinstance(body["trends_enabled"], bool):
        problems.append("trends_enabled must be true or false")
    if problems:
        raise iq.InvalidFilter("; ".join(problems))
    return dict(body)


def _overlay(current: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(current))
    for k, v in body.items():
        if k == "fresh_hours":
            out["fresh_hours"].update(v)
        else:
            out[k] = v
    return out


def _changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: {"from": before.get(k), "to": after.get(k)} for k in SETTINGS_FIELDS if before.get(k) != after.get(k)}


def _adjusted(body: Mapping[str, Any], stored: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None) -> list[str]:
    """Numbers the validator changed: requested ones clamped into range, rounded, or capped by ``stale_days``, and
    (when ``current`` is given) numbers nobody asked to change that the stale limit pulled down (lowering
    ``stale_days`` to 3 caps ``fresh_hours.server`` at 72)."""
    out = []
    for name in _INT_FIELDS:
        if name in body and body[name] != stored[name]:
            out.append(name)
    asked = body.get("fresh_hours") or {}
    for kind in ("server", "device"):
        if kind in asked:
            if asked[kind] != stored["fresh_hours"][kind]:
                out.append(f"fresh_hours.{kind}")
        elif current is not None and current["fresh_hours"][kind] != stored["fresh_hours"][kind]:
            out.append(f"fresh_hours.{kind}")
    return out


async def _updated_at(db: AsyncSession) -> Optional[str]:
    row = (await db.execute(text("SELECT updated_at FROM system_settings WHERE key = :k"), {"k": svc.SETTINGS_KEY})).first()
    return iq.iso(row[0]) if row and row[0] is not None else None


def settings_response(settings: Mapping[str, Any], updated_at: Optional[str], changed: Sequence[str] = (),
                      adjusted: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "settings": dict(settings),
        "defaults": svc.merge_inventory_settings(None),
        "limits": {k: {"min": lo, "max": hi} for k, (lo, hi) in SETTINGS_LIMITS.items()},
        "categories": list(inventory_normalize.CATEGORIES),
        "updated_at": updated_at,
        "changed": list(changed),
        "adjusted": list(adjusted),
    }


async def read_settings(db: AsyncSession) -> dict[str, Any]:
    """The merged settings, the defaults, the limits and the category vocabulary (GET /inventory/settings)."""
    return settings_response(await svc.get_inventory_settings(db), await _updated_at(db))


async def update_settings(db: AsyncSession, actor: Any, body: Any) -> dict[str, Any]:
    """Validate, merge over the current settings, store, audit and return what is now in force.

    Nothing is written (and nothing audited) when the merged result equals the current settings. The audit entry
    names the changed keys only, with their old and new values. Other API workers keep a cached summary for up to 60 s.

    Concurrent writers queue on an advisory lock taken BEFORE the current settings are read, so each merges over the
    other's committed result. The audit entry is required: when it cannot be stored the write is rolled back and
    ``AuditUnavailable`` is raised (the route answers 503), so a change is never saved without its trail.
    """
    requested = validate_update(body)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": SETTINGS_LOCK})
    current = await svc.get_inventory_settings(db)
    merged = svc.merge_inventory_settings(_overlay(current, requested))
    changed = _changed(current, merged)
    adjusted = _adjusted(requested, merged, current)
    if changed:
        user_id = getattr(actor, "id", None)
        await db.execute(text(
            "INSERT INTO system_settings (key, value, updated_at, updated_by) "
            "VALUES (:k, CAST(:v AS jsonb), NOW(), (SELECT id FROM users WHERE id = :u)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by"),
            {"k": svc.SETTINGS_KEY, "v": json.dumps(merged), "u": user_id})
        try:
            await write_audit_log(db, actor=actor, action=SETTINGS_ACTION, resource_type="inventory_settings",
                                  resource_id=svc.SETTINGS_KEY, metadata={"changed": changed}, required=True)
        except AuditUnavailable:
            await db.rollback()
            raise
        await db.commit()
        iq.clear_caches()
        isq.clear_caches()
    else:
        await db.rollback()                 # release the advisory lock; nothing was written
    return settings_response(merged, await _updated_at(db), changed=list(changed), adjusted=adjusted)
