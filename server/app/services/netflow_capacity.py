"""NetFlow interface capacity: the one statistical contract (NF-13..NF-16, CR-08, CR-09).

Every capacity figure the product shows from flow data -- the Capacity page, the
billing report, trends and runout forecasts, and (through ``compute_capacity``)
the management report -- is produced here, so there is one definition of each:

Samples (NF-13)
    A sample is one complete 5-minute slot of the half-open window
    ``[ceil300(from), floor300(to))``; partial slots at either edge are left out
    (their length is reported, never divided by a full 300 s). A slot is a
    sample for an interface when its exporter was delivering records then
    (``flow_exporter_minutes``, the appliance receipt clock) or when the slot
    carries traffic for the interface. An idle interface on a live exporter is
    a genuine zero; a slot in which the exporter delivered nothing is a
    collection gap and is excluded, not counted as zero. ``coverage_pct`` is
    samples / expected slots. On the Capacity page and in billing an
    exporter's newest slots are provisional while long flows it has not
    reported yet can still be spread back into them; they are excluded
    (and disclosed) until they settle, never counted as idle zeros
    (``settle_plan``).

Volume
    Per-direction bytes come from ``flow_interface_5m``: ``in`` = received on
    the interface (the record's input ifIndex), ``out`` = transmitted (output
    ifIndex). Each record's bytes are spread over the slots its duration
    covered (capped at one hour), so a long-lived flow is not a spike in the
    slot where it ended. When that rollup is more than one slot behind raw
    records (its view was down), the newest records are spread from raw with
    the same arithmetic. Exporters that export both ingress- and
    egress-monitored records of the same packets are de-duplicated per their
    ``count_direction`` (NF-14, FE-12).

Statistics
    Percentile = nearest rank over the samples, ``rank = N - floor(N * (100 - p) / 100)``
    (sort ascending, discard the top (100 - p) %, take the highest remaining:
    the burstable-billing method), deterministic. It is only stated with at
    least ``ceil(100 / (100 - p))`` samples (so at least one sample is
    discarded; 20 for p95) and ``min_coverage_pct`` coverage; otherwise it is
    ``None`` with a reason and the peak is shown instead, labelled as such.

Directions (NF-14)
    In and out have separate statistics against their own speed; the headline
    is the direction with the higher utilization. ``combined`` (in + out) is
    throughput only and is never divided by a speed.

Speed (NF-15, NF-16)
    ``interface_speed.effective_speed``: configured circuit speed, then the
    NetFlow interface override (per direction), then SNMP ifSpeed; sFlow
    counter samples' ifSpeed stands in for SNMP when the interface is not
    polled. Unknown speed means unknown utilization, never a percentage.

Billing (CR-08)
    ``netflow_capacity`` system setting: percentile 95/90/85/80/75, direction
    mode ``separate`` (per-direction percentiles, the higher is billed) or
    ``merged`` (percentile of in + out per sample), optional business hours.
    A figure is only called billing-grade for a complete past period with
    ``billing_min_coverage_pct`` coverage.

Trends and forecast (CR-09, FE-05)
    Daily per-direction percentile/peak/average per interface (up to the
    400-day interface retention), and a least-squares runout forecast of the
    daily headline over at least 14 qualifying days, with R-squared as its
    confidence; otherwise "insufficient history".
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.netflow.access import (
    FlowVisibility,
    canonical_ip,
    resolve_exporter_owners,
    visibility_predicate,
)
from app.api.v1.netflow.common import (
    BUDGET_INTERACTIVE,
    EXPORTER_MINUTES,
    INTERFACE_5M,
    RAW,
    Window,
    current_meta,
    get_retention,
    now_utc,
    run_query,
    table_ref,
    validate_tz,
)
from app.api.v1.netflow.filters import FlowFilter, compile_filter
from app.api.v1.netflow.sql import EXPORTER, VOLUME_GUARD, ceil_to, floor_to, ip_param, table_caps
from app.services.interface_speed import (
    SOURCE_CONFIGURED,
    SOURCE_OVERRIDE,
    SOURCE_SNMP,
    SOURCE_UNKNOWN,
    effective_speed_source,
)
from app.services.netflow_rollup import DURATION_VALID

logger = logging.getLogger(__name__)

SLOT_S = 300
SLOT = timedelta(seconds=SLOT_S)
SOURCE_SFLOW = "sflow"

# ── Settings (CR-08) ──────────────────────────────────────────────────────────

SETTINGS_KEY = "netflow_capacity"
PERCENTILES = (95, 90, 85, 80, 75)
DIRECTION_MODES = ("separate", "merged")
FORECAST_MIN_DAYS = 14
FORECAST_HORIZON_DAYS = 730
MAX_TREND_DAYS = 400


@dataclass(frozen=True)
class BusinessHours:
    """Samples limited to these local hours and weekdays (ISO 1 = Monday)."""

    enabled: bool = False
    start_hour: int = 8
    end_hour: int = 18          # exclusive; end < start wraps past midnight
    days: tuple[int, ...] = (1, 2, 3, 4, 5)
    tz: str = "UTC"

    def hours(self) -> frozenset[int]:
        if self.start_hour < self.end_hour:
            return frozenset(range(self.start_hour, self.end_hour))
        return frozenset(h for h in range(24) if h >= self.start_hour or h < self.end_hour)

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "start_hour": self.start_hour, "end_hour": self.end_hour,
                "days": list(self.days), "tz": self.tz}


@dataclass(frozen=True)
class CapacitySettings:
    percentile: int = 95
    direction_mode: str = "separate"
    business_hours: BusinessHours = field(default_factory=BusinessHours)
    warn_pct: float = 70.0
    crit_pct: float = 90.0
    min_coverage_pct: float = 80.0
    billing_min_coverage_pct: float = 99.0

    def to_dict(self) -> dict:
        return {
            "percentile": self.percentile,
            "direction_mode": self.direction_mode,
            "business_hours": self.business_hours.to_dict(),
            "warn_pct": self.warn_pct,
            "crit_pct": self.crit_pct,
            "min_coverage_pct": self.min_coverage_pct,
            "billing_min_coverage_pct": self.billing_min_coverage_pct,
        }


DEFAULT_SETTINGS = CapacitySettings()


class SettingsError(ValueError):
    """Invalid capacity settings; ``fields`` maps each key to its message."""

    def __init__(self, fields: dict[str, str]):
        self.fields = fields
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))


def _num(value: Any, key: str, lo: float, hi: float, errors: dict[str, str]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        errors[key] = "must be a number"
        return None
    if not lo <= value <= hi:
        errors[key] = f"must be between {lo:g} and {hi:g}"
        return None
    return float(value)


def _int(value: Any, key: str, lo: int, hi: int, errors: dict[str, str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        errors[key] = "must be a whole number"
        return None
    if not lo <= value <= hi:
        errors[key] = f"must be between {lo} and {hi}"
        return None
    return value


def parse_settings(raw: Mapping[str, Any] | None, *, strict: bool = True,
                   base: CapacitySettings = DEFAULT_SETTINGS) -> CapacitySettings:
    """Settings from a JSON object over ``base``; unknown keys are rejected.

    ``strict`` (API writes) raises SettingsError naming every bad key; the
    lenient form (reading what is stored) keeps the default for a bad value.
    """
    raw = dict(raw or {})
    errors: dict[str, str] = {}
    known = {"percentile", "direction_mode", "business_hours", "warn_pct", "crit_pct",
             "min_coverage_pct", "billing_min_coverage_pct"}
    for key in set(raw) - known:
        errors[key] = "unknown setting"
    values: dict[str, Any] = {}
    if "percentile" in raw:
        p = raw["percentile"]
        if isinstance(p, bool) or p not in PERCENTILES:
            errors["percentile"] = f"must be one of {', '.join(map(str, PERCENTILES))}"
        else:
            values["percentile"] = int(p)
    if "direction_mode" in raw:
        if raw["direction_mode"] not in DIRECTION_MODES:
            errors["direction_mode"] = "must be 'separate' or 'merged'"
        else:
            values["direction_mode"] = raw["direction_mode"]
    for key, lo, hi in (("warn_pct", 1, 100), ("crit_pct", 1, 100), ("min_coverage_pct", 50, 100),
                        ("billing_min_coverage_pct", 50, 100)):
        if key in raw:
            v = _num(raw[key], key, lo, hi, errors)
            if v is not None:
                values[key] = v
    if "business_hours" in raw:
        bh_raw = raw["business_hours"]
        if not isinstance(bh_raw, Mapping):
            errors["business_hours"] = "must be an object"
        else:
            bh = base.business_hours
            bh_vals: dict[str, Any] = {}
            for key in set(bh_raw) - {"enabled", "start_hour", "end_hour", "days", "tz"}:
                errors[f"business_hours.{key}"] = "unknown setting"
            if "enabled" in bh_raw:
                if not isinstance(bh_raw["enabled"], bool):
                    errors["business_hours.enabled"] = "must be true or false"
                else:
                    bh_vals["enabled"] = bh_raw["enabled"]
            for key, lo, hi in (("start_hour", 0, 23), ("end_hour", 1, 24)):
                if key in bh_raw:
                    v = _int(bh_raw[key], f"business_hours.{key}", lo, hi, errors)
                    if v is not None:
                        bh_vals[key] = v
            if "days" in bh_raw:
                days = bh_raw["days"]
                if (not isinstance(days, list) or not days
                        or any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7 for d in days)):
                    errors["business_hours.days"] = "must be a non-empty list of weekdays 1 (Monday) to 7 (Sunday)"
                else:
                    bh_vals["days"] = tuple(sorted(set(days)))
            if "tz" in bh_raw:
                try:
                    bh_vals["tz"] = validate_tz(str(bh_raw["tz"]), "business_hours.tz")
                except Exception:
                    errors["business_hours.tz"] = "unknown time zone"
            bh = replace(bh, **bh_vals)
            if bh.start_hour == bh.end_hour % 24:
                errors["business_hours.end_hour"] = "must differ from the start hour"
            else:
                values["business_hours"] = bh
    merged = replace(base, **{k: v for k, v in values.items()})
    if merged.warn_pct >= merged.crit_pct and not ({"warn_pct", "crit_pct"} & set(errors)):
        errors["warn_pct"] = "must be below the critical threshold"
    if errors and strict:
        raise SettingsError(errors)
    if errors:
        logger.warning("netflow capacity settings: ignoring invalid stored values %s", errors)
        # Lenient: drop only what is invalid, keep the valid values.
        good = {k: v for k, v in values.items() if k not in errors}
        merged = replace(base, **good)
        if merged.warn_pct >= merged.crit_pct:
            merged = replace(merged, warn_pct=base.warn_pct, crit_pct=base.crit_pct)
    return merged


async def _pg_rows(db: AsyncSession, sql: str, params: dict | None = None) -> list:
    """Rows of a read that may hit a table an older schema lacks: a failure is
    logged and read as no rows, inside a savepoint so the request's
    transaction stays usable."""
    try:
        if hasattr(db, "begin_nested"):
            async with db.begin_nested():
                return list((await db.execute(text(sql), params or {})).all())
        return list((await db.execute(text(sql), params or {})).all())
    except Exception:
        logger.warning("netflow capacity: PostgreSQL read failed: %s", sql.split("\n")[0][:80], exc_info=True)
        return []


async def load_settings(db: AsyncSession) -> tuple[CapacitySettings, dict]:
    """(settings, info) -- info carries ``updated_at``/``updated_by`` of the stored row."""
    rows = await _pg_rows(
        db,
        "SELECT s.value, s.updated_at, u.username FROM system_settings s "
        "LEFT JOIN users u ON u.id = s.updated_by WHERE s.key = :key",
        {"key": SETTINGS_KEY},
    )
    if not rows:
        return DEFAULT_SETTINGS, {"stored": False, "updated_at": None, "updated_by": None}
    value, updated_at, username = rows[0][0], rows[0][1], rows[0][2]
    settings = parse_settings(value if isinstance(value, Mapping) else {}, strict=False)
    return settings, {
        "stored": True,
        "updated_at": updated_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(updated_at, datetime) else None,
        "updated_by": username,
    }


async def save_settings(db: AsyncSession, settings: CapacitySettings, user) -> None:
    """Store the settings and audit the change (old and new values)."""
    from app.services.audit_service import write_audit_log
    import json

    old, _ = await load_settings(db)
    await db.execute(
        text(
            "INSERT INTO system_settings (key, value, updated_at, updated_by) "
            "VALUES (:key, CAST(:value AS jsonb), NOW(), :uid) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW(), "
            "updated_by = EXCLUDED.updated_by"
        ),
        {"key": SETTINGS_KEY, "value": json.dumps(settings.to_dict()), "uid": getattr(user, "id", None)},
    )
    await write_audit_log(
        db, actor=user, action="netflow.capacity_settings.update", resource_type="system_settings",
        resource_id=SETTINGS_KEY, metadata={"old": old.to_dict(), "new": settings.to_dict()},
    )
    await db.commit()


# ── Slots ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SlotWindow:
    """The complete 5-minute slots of a window: ``[first, end)``."""

    first: datetime
    end: datetime
    head_excluded_s: int = 0     # partial slot at the start, left out
    tail_excluded_s: int = 0     # partial slot at the end, left out
    retention_cut: bool = False  # ``first`` moved forward to the interface retention horizon

    @property
    def n_slots(self) -> int:
        return max(0, int((self.end - self.first).total_seconds()) // SLOT_S)

    @property
    def empty(self) -> bool:
        return self.end <= self.first

    def shifted_back(self) -> "SlotWindow":
        """The previous period of the same length (for period-over-period change)."""
        span = self.end - self.first
        return SlotWindow(self.first - span, self.first)


def complete_slots(start: datetime, end: datetime, horizon: datetime | None = None) -> SlotWindow:
    """Half-open complete slots inside ``[start, end)``: partial edges are
    excluded (NF-13) and reported in seconds. ``horizon`` (the oldest instant
    the interface rollup still holds) moves the first slot forward."""
    first = ceil_to(start, SLOT_S)
    last = floor_to(end, SLOT_S)
    cut = False
    if horizon is not None:
        h = ceil_to(horizon, SLOT_S)
        if h > first:
            first, cut = h, True
    if last <= first:
        return SlotWindow(first, first, int((first - start).total_seconds()) if first > start else 0,
                          int((end - last).total_seconds()) if end > last else 0, cut)
    return SlotWindow(
        first, last,
        head_excluded_s=0 if cut else int((first - start).total_seconds()),
        tail_excluded_s=int((end - last).total_seconds()),
        retention_cut=cut,
    )


@dataclass(frozen=True)
class SlotRule:
    """Slot selection in one time zone: allowed local hours and ISO weekdays."""

    tz: str
    hours: frozenset[int] | None = None
    days: frozenset[int] | None = None


@dataclass(frozen=True)
class SlotSelector:
    """Which slots are samples: every rule must match (business hours, and
    the page's hour/day-of-week filter). An empty selector keeps every slot."""

    rules: tuple[SlotRule, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def matches(self, slot: datetime) -> bool:
        for r in self.rules:
            local = slot.astimezone(ZoneInfo(r.tz))
            if r.hours is not None and local.hour not in r.hours:
                return False
            if r.days is not None and local.isoweekday() not in r.days:
                return False
        return True

    def sql(self, col: str, params: dict, prefix: str = "cap_sel") -> str:
        """Predicate on a DateTime column holding the slot start."""
        parts: list[str] = []
        for i, r in enumerate(self.rules):
            params[f"{prefix}{i}_tz"] = r.tz
            if r.hours is not None:
                params[f"{prefix}{i}_h"] = tuple(sorted(r.hours)) or (-1,)
                parts.append(f"toHour({col}, %({prefix}{i}_tz)s) IN %({prefix}{i}_h)s")
            if r.days is not None:
                params[f"{prefix}{i}_d"] = tuple(sorted(r.days)) or (-1,)
                parts.append(f"toDayOfWeek({col}, 0, %({prefix}{i}_tz)s) IN %({prefix}{i}_d)s")
        return " AND ".join(parts) if parts else "1"

    def describe(self) -> list[dict]:
        return [{"tz": r.tz, "hours": sorted(r.hours) if r.hours is not None else None,
                 "days": sorted(r.days) if r.days is not None else None} for r in self.rules]


def build_selector(f: FlowFilter | None, settings: CapacitySettings, business_hours: bool) -> SlotSelector:
    rules: list[SlotRule] = []
    if business_hours:
        bh = settings.business_hours
        rules.append(SlotRule(tz=bh.tz, hours=bh.hours(), days=frozenset(bh.days)))
    if f is not None and (f.hour is not None or f.dow is not None):
        rules.append(SlotRule(
            tz=f.tz,
            hours=frozenset({f.hour}) if f.hour is not None else None,
            days=frozenset({f.dow}) if f.dow is not None else None,
        ))
    return SlotSelector(tuple(rules))


def iter_slots(sw: SlotWindow) -> Iterable[datetime]:
    t = sw.first
    while t < sw.end:
        yield t
        t += SLOT


def expected_slots(sw: SlotWindow, selector: SlotSelector) -> int:
    """Slots in the window that are samples when the exporter is live."""
    if not selector.active:
        return sw.n_slots
    return sum(1 for s in iter_slots(sw) if selector.matches(s))


# ── Percentiles ───────────────────────────────────────────────────────────────

def nearest_rank(n: int, pct: int) -> int:
    """1-based nearest rank of the ``pct`` percentile among ``n`` sorted samples:
    ``n - floor(n * (100 - pct) / 100)`` -- the highest sample left after the
    top (100 - pct) % are discarded. Integer arithmetic, so exact."""
    if n <= 0:
        return 0
    return n - (n * (100 - pct)) // 100


def min_samples(pct: int) -> int:
    """Fewest samples for which the percentile discards at least one sample
    (below this it is simply the maximum): ceil(100 / (100 - pct))."""
    return -(-100 // (100 - pct))


def percentile_value(sorted_values: list[float], pct: int, zeros: int = 0) -> float | None:
    """Nearest-rank percentile of ``zeros`` zero samples plus ``sorted_values``
    (ascending, all >= 0). The zeros are the smallest samples, so they need not
    be materialised: this is the same arithmetic the SQL uses."""
    n = len(sorted_values) + zeros
    if n == 0:
        return None
    r = nearest_rank(n, pct)
    return 0.0 if r <= zeros else float(sorted_values[r - zeros - 1])


def series_stats(values: list[float], zeros: int, pct: int) -> dict:
    """avg / percentile / max of a zero-padded sample set (Python twin of the SQL)."""
    vals = sorted(values)
    n = len(vals) + zeros
    if n == 0:
        return {"samples": 0, "avg_bps": None, "p_bps": None, "max_bps": None}
    return {
        "samples": n,
        "avg_bps": sum(vals) / n,
        "p_bps": percentile_value(vals, pct, zeros),
        "max_bps": max(vals) if vals else 0.0,
    }


def p_available(samples: int, coverage_pct: float | None, pct: int, min_coverage_pct: float) -> str | None:
    """Why a percentile must not be stated (None = it may be)."""
    if samples <= 0:
        return "no_samples"
    if samples < min_samples(pct):
        return "too_few_samples"
    if coverage_pct is None or coverage_pct + 1e-9 < min_coverage_pct:
        return "low_coverage"
    return None


# ── Utilization, headroom, state ──────────────────────────────────────────────

STATES = ("over", "critical", "warning", "healthy", "insufficient", "unknown")
STATE_RANK = {s: i for i, s in enumerate(STATES)}


def pct_of(value: float | None, speed: int | None) -> float | None:
    if value is None or not speed:
        return None
    return round(value / speed * 100.0, 3)


def classify(util_pct: float | None, settings: CapacitySettings, *, has_samples: bool = True) -> str:
    """Utilization state; unknown speed is 'unknown', never healthy (NF-16)."""
    if util_pct is None:
        return "unknown" if has_samples else "insufficient"
    if util_pct > 100.0:
        return "over"
    if util_pct >= settings.crit_pct:
        return "critical"
    if util_pct >= settings.warn_pct:
        return "warning"
    return "healthy"


def headroom(value_bps: float | None, speed: int | None) -> tuple[float | None, float | None]:
    """(remaining bit/s, remaining %) -- negative when over capacity (NF-15);
    (None, None) when the speed is unknown."""
    if value_bps is None or not speed:
        return None, None
    return round(speed - value_bps, 3), round(100.0 - value_bps / speed * 100.0, 3)


# ── Direction de-duplication (NF-14, FE-12) ───────────────────────────────────

@dataclass(frozen=True)
class DirectionPolicy:
    """Per-exporter counting direction from ``netflow_exporter_settings``;
    exporters without a row are 'auto' (ingress-monitored records only when
    the exporter emits both directions in the window). Values are the IPv6
    bind text of the exporter (``ip_param``)."""

    ingress: tuple[str, ...] = ()
    egress: tuple[str, ...] = ()
    both: tuple[str, ...] = ()
    settings: Mapping[str, str] = field(default_factory=dict)   # canonical ip -> count_direction

    @property
    def explicit(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.ingress) | set(self.egress) | set(self.both)))

    def setting_for(self, exporter: str) -> str:
        return self.settings.get(canonical_ip(exporter) or exporter, "auto")


async def load_direction_policy(db: AsyncSession) -> DirectionPolicy:
    rows = await _pg_rows(db, "SELECT host(exporter_ip), count_direction FROM netflow_exporter_settings")
    ingress, egress, both, settings = [], [], [], {}
    for ip_text, direction in rows:
        ip = canonical_ip(ip_text)
        if ip is None:
            continue
        settings[ip] = direction
        if direction == "ingress":
            ingress.append(ip_param(ip))
        elif direction == "egress":
            egress.append(ip_param(ip))
        elif direction == "both":
            both.append(ip_param(ip))
    return DirectionPolicy(tuple(sorted(ingress)), tuple(sorted(egress)), tuple(sorted(both)), settings)


def dedup_sql(policy: DirectionPolicy, params: dict, auto_cte: str = "nf_cap_auto") -> str:
    """Rows to keep: drop egress-monitored records of ingress-counted
    exporters (explicit 'ingress', or 'auto' exporters that emit both
    directions -- ``auto_cte``) and ingress-monitored records of 'egress'
    exporters. Unknown direction (255) is always kept."""
    ingress_terms = [f"{EXPORTER} IN {auto_cte}"]
    if policy.explicit:
        params["cap_dir_explicit"] = policy.explicit
        ingress_terms = [f"({EXPORTER} NOT IN %(cap_dir_explicit)s AND {EXPORTER} IN {auto_cte})"]
    if policy.ingress:
        params["cap_dir_ingress"] = policy.ingress
        ingress_terms.append(f"{EXPORTER} IN %(cap_dir_ingress)s")
    parts = [f"NOT (flow_direction = 1 AND ({' OR '.join(ingress_terms)}))"]
    if policy.egress:
        params["cap_dir_egress"] = policy.egress
        parts.append(f"NOT (flow_direction = 0 AND {EXPORTER} IN %(cap_dir_egress)s)")
    return " AND ".join(parts)


def accounting_basis(setting: str, directions_seen: Iterable[int]) -> str:
    """Which records an exporter's capacity counts: 'ingress_records',
    'egress_records' or 'all_records'."""
    seen = set(directions_seen) & {0, 1}
    if setting == "ingress" or (setting == "auto" and seen == {0, 1}):
        return "ingress_records"
    if setting == "egress":
        return "egress_records"
    return "all_records"


# ── SQL ───────────────────────────────────────────────────────────────────────

# Filter keys capacity can honour: everything that selects exporters,
# interfaces, record direction or time of day. Traffic-content filters
# (addresses, ports, applications, ...) cannot narrow a link's utilization;
# they are left out and reported as not applied.
SUPPORTED_KEYS = frozenset({"exporter", "iface", "iface_dir", "direction", "hour", "dow", "count"})
# Handled by the slot selector (a sample is a slot), not by the row filter.
SLOT_KEYS = ("hour", "dow")
LAG_LOOKBACK = timedelta(hours=7)   # the healer re-checks rollups every 6 h


def unsupported_keys(f: FlowFilter) -> frozenset[str]:
    return frozenset(f.active_keys() - SUPPORTED_KEYS)


def _bps(expr: str) -> str:
    """Bytes in a 5-minute slot as bit/s, rounded to a thousandth so float
    summation order cannot move a percentile (determinism)."""
    return f"round(({expr}) * 8 / {SLOT_S}, 3)"


def _raw_spread_sql(vis: FlowVisibility, params: dict) -> str:
    """Records ending at or after ``cap_raw_from`` spread over their duration
    into 5-minute slots, exactly as ``flow_interface_5m_mv`` does, shaped like
    ``flow_interface_5m`` rows (used only while that rollup lags)."""
    vis_sql, vis_params = visibility_predicate(vis, param="cap_vis_raw")
    params.update(vis_params)
    return f"""
    SELECT toDateTime(intDiv(slot_ms, 1000), 'UTC') AS timestamp, exporter_ip, exporter_ip6, ifindex,
           direction, flow_direction, raw_bytes * slot_share AS bytes, raw_packets * slot_share AS packets,
           toUInt64(slot_ms = end_slot_ms) AS flows
    FROM (
        SELECT exporter_ip, exporter_ip6, ifindex, direction, flow_direction, raw_bytes, raw_packets, end_slot_ms,
               arrayJoin(range(intDiv(end_ms - span_ms, 300000) * 300000, end_slot_ms + 1, 300000)) AS slot_ms,
               if(span_ms = 0, toFloat64(slot_ms = end_slot_ms),
                  (least(end_ms, slot_ms + 300000) - greatest(end_ms - span_ms, slot_ms)) / span_ms) AS slot_share
        FROM (
            SELECT exporter_ip, exporter_ip6, flow_direction,
                   arrayJoin(arrayFilter(side -> side.2 != 0,
                       [(toUInt8(0), input_if), (toUInt8(1), output_if)])) AS if_side,
                   if_side.1 AS direction, if_side.2 AS ifindex,
                   toFloat64(bytes) AS raw_bytes, toFloat64(packets) AS raw_packets,
                   toUnixTimestamp64Milli(timestamp) AS end_ms,
                   intDiv(end_ms, 300000) * 300000 AS end_slot_ms,
                   if({DURATION_VALID}, toInt64(least(last_switched_ms - first_switched_ms, 3600000)),
                      toInt64(0)) AS span_ms
            FROM {table_ref(RAW)}
            WHERE timestamp >= %(cap_raw_from)s AND timestamp < %(cap_raw_to)s
              AND {VOLUME_GUARD}{f' AND {vis_sql}' if vis_sql else ''}
        )
    )
    WHERE slot_ms >= %(cap_first_ms)s AND slot_ms < %(cap_end_ms)s"""


def _traffic_from(sw: SlotWindow, vis: FlowVisibility, raw_from: datetime | None, params: dict) -> str:
    """FROM clause for per-direction slot volume: the interface rollup, plus
    raw records from ``raw_from`` while the rollup lags."""
    params["cap_first"] = sw.first
    params["cap_end"] = sw.end
    if raw_from is None:
        return f"{table_ref(INTERFACE_5M)} AS flow_interface_5m"
    params["cap_raw_from"] = raw_from
    params["cap_raw_to"] = sw.end + timedelta(hours=1)
    params["cap_first_ms"] = int(sw.first.timestamp() * 1000)
    params["cap_end_ms"] = int(sw.end.timestamp() * 1000)
    rollup = (f"SELECT timestamp, exporter_ip, exporter_ip6, ifindex, direction, flow_direction, bytes, "
              f"packets, flows FROM {table_ref(INTERFACE_5M)} "
              f"WHERE timestamp >= %(cap_first)s AND timestamp < %(cap_end)s")
    return f"({rollup}\n    UNION ALL{_raw_spread_sql(vis, params)}\n) AS flow_interface_5m"


def _row_filter(f: FlowFilter, vis: FlowVisibility, params: dict) -> str:
    """Compiled filter for interface rows: visibility, exporter/iface/direction/
    count keys; unsupported keys are dropped (and disclosed) and time-of-day
    keys become the slot selector."""
    drop = unsupported_keys(f)
    base = f.without(*SLOT_KEYS)
    sql, fparams = compile_filter(base, INTERFACE_5M, vis, drop)
    if drop:
        # compile_filter already recorded them; say why.
        current_meta().warn(
            "capacity_filters_not_applied",
            "Capacity measures whole interfaces; traffic filters ("
            + ", ".join(sorted(drop)) + ") do not apply to it",
        )
    params.update(fparams)
    return sql


def _live_cte(sw: SlotWindow, f: FlowFilter, vis: FlowVisibility, selector: SlotSelector, params: dict,
              settle: "SettlePlan | None" = None) -> str:
    """Per exporter, the 5-minute slots in which it delivered records (only
    settled slots when ``settle`` excludes the provisional ones)."""
    exp = FlowFilter(exporter=f.exporter or (f.iface.exporter if f.iface else None))
    sql, fparams = compile_filter(exp, EXPORTER_MINUTES, vis)
    params.update(fparams)
    settled = f" AND {settle.sql('slot', 'e', params)}" if settle is not None and settle.active else ""
    return f"""nf_cap_live AS (
        SELECT {EXPORTER} AS e, toStartOfInterval(minute, INTERVAL {SLOT_S} SECOND) AS slot
        FROM {table_ref(EXPORTER_MINUTES)}
        WHERE minute >= %(cap_first)s AND minute < %(cap_end)s AND {sql}
        GROUP BY e, slot
        HAVING {selector.sql('slot', params)}{settled}
    )"""


def _auto_cte(vis: FlowVisibility, params: dict) -> str:
    vis_sql, vis_params = visibility_predicate(vis, param="cap_vis_auto")
    params.update(vis_params)
    return f"""nf_cap_auto AS (
        SELECT {EXPORTER} AS e FROM {table_ref(INTERFACE_5M)}
        WHERE timestamp >= %(cap_first)s AND timestamp < %(cap_end)s AND flow_direction IN (0, 1)
          {f'AND {vis_sql}' if vis_sql else ''}
        GROUP BY e HAVING uniqExact(flow_direction) = 2
    )"""


def _stat_cols(prefix: str, arr: str) -> str:
    """avg-sum / percentile / max of the zero-padded samples in ``arr``."""
    return (
        f"arraySort({arr}) AS {prefix}_sorted,\n"
        f"    if(rnk <= zeros, 0., arrayElement({prefix}_sorted, rnk - zeros)) AS {prefix}_p,\n"
        f"    arrayMax({arr}) AS {prefix}_max,\n"
        f"    arraySum({arr}) AS {prefix}_sum"
    )


def stats_sql(sw: SlotWindow, f: FlowFilter, vis: FlowVisibility, selector: SlotSelector,
              policy: DirectionPolicy, pct: int, raw_from: datetime | None,
              *, extra_where: str = "", extra_params: dict | None = None,
              settle: "SettlePlan | None" = None) -> tuple[str, dict]:
    """One row per (exporter, ifindex) with per-direction and combined
    statistics over the window's samples (see the module docstring);
    ``settle`` leaves out each exporter's provisional slots."""
    params: dict = dict(extra_params or {})
    params["cap_pct"] = int(pct)
    source = _traffic_from(sw, vis, raw_from, params)
    live = _live_cte(sw, f, vis, selector, params, settle)
    auto = _auto_cte(vis, params)
    row_filter = _row_filter(f, vis, params)
    dedup = dedup_sql(policy, params)
    time_pred = "timestamp >= %(cap_first)s AND timestamp < %(cap_end)s"
    if settle is not None and settle.active:
        time_pred += f" AND {settle.sql('timestamp', EXPORTER, params)}"
    sel = selector.sql("timestamp", params, prefix="cap_tsel")
    sql = f"""
WITH
    {live},
    {auto}
SELECT
    replaceRegexpOne(toString(s.e), '^::ffff:', '') AS exporter,
    s.ifindex AS ifindex,
    s.n_traffic AS n_traffic,
    lc.n_live AS n_live,
    lc.n_live + s.n_extra AS samples,
    samples - s.n_traffic AS zeros,
    toUInt32(samples - intDiv(samples * (100 - %(cap_pct)s), 100)) AS rnk,
    {_stat_cols('in', 's.in_v')},
    {_stat_cols('out', 's.out_v')},
    {_stat_cols('sum', 's.sum_v')},
    s.in_bytes AS in_bytes, s.out_bytes AS out_bytes,
    s.in_packets AS in_packets, s.out_packets AS out_packets,
    s.flows AS flows, s.last_slot AS last_slot
FROM (
    SELECT t.e AS e, t.ifindex AS ifindex,
           count() AS n_traffic,
           countIf(l.is_live = 0) AS n_extra,
           groupArray(t.in_bps) AS in_v,
           groupArray(t.out_bps) AS out_v,
           groupArray(round(t.in_bps + t.out_bps, 3)) AS sum_v,
           sum(t.in_b) AS in_bytes, sum(t.out_b) AS out_bytes,
           sum(t.in_p) AS in_packets, sum(t.out_p) AS out_packets,
           sum(t.fl) AS flows, max(t.slot) AS last_slot
    FROM (
        SELECT {EXPORTER} AS e, ifindex, timestamp AS slot,
               sumIf(bytes, direction = 0) AS in_b, sumIf(bytes, direction = 1) AS out_b,
               sumIf(packets, direction = 0) AS in_p, sumIf(packets, direction = 1) AS out_p,
               sum(flows) AS fl,
               {_bps('in_b')} AS in_bps, {_bps('out_b')} AS out_bps
        FROM {source}
        WHERE {time_pred} AND {row_filter} AND {dedup} AND {sel}{extra_where}
        GROUP BY e, ifindex, slot
    ) AS t
    LEFT JOIN (SELECT e, slot, toUInt8(1) AS is_live FROM nf_cap_live) AS l ON t.e = l.e AND t.slot = l.slot
    GROUP BY t.e, t.ifindex
) AS s
LEFT JOIN (SELECT e, count() AS n_live FROM nf_cap_live GROUP BY e) AS lc ON s.e = lc.e
"""
    return sql, params


STAT_COLUMNS = (
    "exporter", "ifindex", "n_traffic", "n_live", "samples", "zeros", "rnk",
    "in_sorted", "in_p", "in_max", "in_sum",
    "out_sorted", "out_p", "out_max", "out_sum",
    "sum_sorted", "sum_p", "sum_max", "sum_sum",
    "in_bytes", "out_bytes", "in_packets", "out_packets", "flows", "last_slot",
)


def _select_scalars(sql: str) -> str:
    """Wrap the stats query so the sorted arrays never leave ClickHouse."""
    cols = [c for c in STAT_COLUMNS if not c.endswith("_sorted")]
    return f"SELECT {', '.join(cols)} FROM ({sql})"


SCALAR_COLUMNS = tuple(c for c in STAT_COLUMNS if not c.endswith("_sorted"))


# ── Rollup freshness (raw fallback for the newest minutes) ────────────────────

def _dt(value) -> datetime | None:
    if isinstance(value, datetime) and value.year >= 2000:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


LAG_PROBE_SPAN = timedelta(minutes=15)   # raw slice that confirms a suspected lag
_LAG_PROBE_TTL_S = 60.0
_lag_probe_cache: dict[tuple, tuple[float, dict[str, datetime]]] = {}


def _epoch_dt(value) -> datetime | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _dt(value)
    return datetime.fromtimestamp(n, timezone.utc) if n > 946684800 else None


async def _lag_probe(candidates: dict[str, datetime], probe_to: datetime) -> dict[str, datetime]:
    """{exporter: newest raw record on an interface} for exporters whose
    rollup looks behind, read from a bounded recent raw slice (``candidates``
    maps each exporter to the start of its slice). Cached briefly: a page
    polling every 15 s asks the same question until a slot closes."""
    probe_from = min(candidates.values())
    key = (tuple(sorted(candidates.items())), probe_to)
    now_m = asyncio.get_running_loop().time()
    hit = _lag_probe_cache.get(key)
    if hit is not None and now_m - hit[0] < _LAG_PROBE_TTL_S:
        return hit[1]
    from app.api.v1.netflow.access import exporters_predicate

    exp_sql, params = exporters_predicate(list(candidates), param="cap_lag_e")
    params.update(cap_probe_from=probe_from, cap_probe_to=probe_to)
    res = await run_query(
        f"""SELECT replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS e, toUInt32(max(timestamp)) AS hwm
            FROM {table_ref(RAW)}
            WHERE timestamp >= %(cap_probe_from)s AND timestamp < %(cap_probe_to)s AND {exp_sql}
              AND (input_if != 0 OR output_if != 0) AND {VOLUME_GUARD}
            GROUP BY e""",
        params, label="capacity-lag-probe",
    )
    out: dict[str, datetime] = {}
    for r in res.result_rows:
        ip, hwm = canonical_ip(r[0]), _epoch_dt(r[1]) if len(r) > 1 else None
        if ip and hwm is not None and hwm >= candidates.get(ip, hwm):
            out[ip] = hwm
    if len(_lag_probe_cache) > 256:
        _lag_probe_cache.clear()
    _lag_probe_cache[key] = (now_m, out)
    return out


async def rollup_lag(sw: SlotWindow, vis: FlowVisibility, now: datetime | None = None) -> datetime | None:
    """When the interface rollup has fallen more than one slot behind raw
    records near the end of the window (its materialized view stopped), the
    instant from which raw records must be spread instead; None otherwise.

    Answered from aggregates on the polling path: per exporter, the rollup's
    newest slot against the newest record ``flow_exporter_minutes`` saw it
    deliver. Only an exporter whose delivered records are more than a slot
    ahead of its rollup is confirmed against raw records -- a bounded recent
    slice (``LAG_PROBE_SPAN``, or since its rollup stopped), never a scan of
    the whole lookback (records on no interface, e.g. keepalives, never
    reach the interface rollup and are not a lag)."""
    now = now or now_utc()
    chk_from = max(sw.first, sw.end - LAG_LOOKBACK)
    if sw.empty or chk_from >= sw.end or sw.end < now - LAG_LOOKBACK - SLOT:
        return None
    chk_to = sw.end + SLOT
    params: dict = {"cap_chk_from": chk_from, "cap_chk_to": chk_to,
                    "cap_rcv_from": chk_from - timedelta(hours=1), "cap_rcv_to": chk_to + timedelta(hours=1)}
    vis_sql, vis_params = visibility_predicate(vis, param="cap_vis_lag")
    params.update(vis_params)
    extra = f" AND {vis_sql}" if vis_sql else ""
    res = await run_query(
        f"""SELECT 'r' AS k, replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS e,
                   toUInt32(max(timestamp)) AS hwm
            FROM {table_ref(INTERFACE_5M)}
            WHERE timestamp >= %(cap_chk_from)s AND timestamp < %(cap_chk_to)s AND flows > 0{extra}
            GROUP BY e
            UNION ALL
            SELECT 'f' AS k, replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS e,
                   toUInt32(toDateTime(max(max_flow_ts))) AS hwm
            FROM {table_ref(EXPORTER_MINUTES)}
            WHERE minute >= %(cap_rcv_from)s AND minute < %(cap_rcv_to)s AND records > 0
              AND max_flow_ts >= %(cap_chk_from)s AND max_flow_ts < %(cap_chk_to)s{extra}
            GROUP BY e""",
        params, label="capacity-lag",
    )
    roll: dict[str, datetime] = {}
    flow: dict[str, datetime] = {}
    for r in res.result_rows:
        if len(r) < 3:
            continue
        ip, hwm = canonical_ip(r[1]), _epoch_dt(r[2])
        if ip and hwm is not None:
            (roll if r[0] == "r" else flow)[ip] = hwm
    # Exporters whose delivered records run more than a slot past their
    # rollup: the slice to confirm starts where the rollup stopped (or the
    # lookback start), bounded to the newest LAG_PROBE_SPAN of their records.
    candidates: dict[str, datetime] = {}
    for ip, f_hwm in flow.items():
        base = roll.get(ip)
        if floor_to(f_hwm, SLOT_S) > (base or chk_from) + SLOT:
            start = base + SLOT if base is not None else chk_from
            candidates[ip] = max(start, f_hwm - LAG_PROBE_SPAN)
    if not candidates:
        return None
    raw = await _lag_probe(candidates, chk_to)
    lagging: list[datetime] = []
    for ip, r_hwm in raw.items():
        base = roll.get(ip)
        if floor_to(r_hwm, SLOT_S) > (base or chk_from) + SLOT:
            lagging.append(base + SLOT if base is not None else chk_from)
    return min(lagging) if lagging else None


# ── Provisional (still settling) slots ────────────────────────────────────────
#
# An exporter reports a long-lived flow only when it ends or reaches the
# exporter's active timeout (Cisco IOS: 30 minutes by default), and its bytes
# are then spread back over the slots the flow covered. Until then the newest
# slots of a live exporter look idle: counting them as genuine zeros reported
# a busy link as idle with full coverage. Slots that can still receive spread
# bytes are provisional and are excluded (like a partial edge slot) until they
# settle; the per-exporter horizon is the longest flow span the exporter was
# seen to report recently.

SETTLE_LOOKBACK = timedelta(minutes=65)   # spreading is capped at 1 h; a 60-min active timeout reports within it
SETTLE_DEFAULT_S = 1800                   # no duration evidence: assume the common 30-min active timeout
SETTLE_MAX_S = 3600                       # the spread cap
SETTLE_MARGIN_S = 60                      # export and write delay after a record's end
_SETTLE_TTL_S = 600.0
_settle_cache: dict = {"at": None, "value": None}


@dataclass(frozen=True)
class SettleInfo:
    """How long after its end an exporter's slot can still receive bytes."""

    active_timeout_s: int
    basis: str            # 'observed' (longest valid flow span seen) | 'default' | 'sflow'


def settle_info(n_flow: int, n_dur: int, max_span_ms) -> SettleInfo:
    """The settling horizon of one exporter from its recent records:
    sFlow samples are instants (nothing is spread back); a NetFlow/IPFIX
    exporter reporting valid flow times settles after the longest span it
    reported (its active timeout shows as that span); without any valid
    duration the common 30-minute active timeout is assumed."""
    if n_flow <= 0:
        return SettleInfo(0, "sflow")
    if n_dur > 0:
        try:
            span = int(max_span_ms or 0)
        except (TypeError, ValueError):
            span = 0
        return SettleInfo(max(0, min(SETTLE_MAX_S, math.ceil(span / 1000))), "observed")
    return SettleInfo(SETTLE_DEFAULT_S, "default")


async def exporter_settle_evidence() -> dict[str, SettleInfo]:
    """{exporter: SettleInfo} from the last ``SETTLE_LOOKBACK`` of raw
    records (every exporter; cached ``_SETTLE_TTL_S``). An exporter missing
    from the map sent nothing in the lookback (``SETTLE_DEFAULT_S`` applies)."""
    loop_now = asyncio.get_running_loop().time()
    at = _settle_cache.get("at")
    if at is not None and loop_now - at < _SETTLE_TTL_S and _settle_cache.get("value") is not None:
        return _settle_cache["value"]
    now = now_utc()
    res = await run_query(
        f"""SELECT replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS e,
                   countIf(NOT (startsWith(export_protocol, 'sflow') OR bitAnd(fields_present, 536870912) != 0)) AS n_flow,
                   countIf({DURATION_VALID}) AS n_dur,
                   maxIf(least(last_switched_ms - first_switched_ms, {SETTLE_MAX_S * 1000}), {DURATION_VALID}) AS max_span
            FROM {table_ref(RAW)}
            WHERE timestamp >= %(cap_st_from)s AND timestamp < %(cap_st_to)s
            GROUP BY e""",
        {"cap_st_from": now - SETTLE_LOOKBACK, "cap_st_to": now + SLOT}, label="capacity-settle",
    )
    out: dict[str, SettleInfo] = {}
    for r in res.result_rows:
        ip = canonical_ip(r[0]) if r else None
        if ip and len(r) >= 4:
            out[ip] = settle_info(int(r[1] or 0), int(r[2] or 0), r[3])
    _settle_cache.update(at=loop_now, value=out)
    return out


def settle_at(info: SettleInfo | None, now: datetime) -> datetime:
    """First provisional slot start: slots before it can no longer change."""
    t = info.active_timeout_s if info is not None else SETTLE_DEFAULT_S
    return floor_to(now - timedelta(seconds=t + SETTLE_MARGIN_S), SLOT_S)


@dataclass(frozen=True)
class SettlePlan:
    """Where each exporter's settled slots end. ``ends`` holds every visible
    exporter with recent records (also those already settled for this
    window); any other exporter settles at ``default``. ``affected`` are the
    exporters whose provisional slots fall inside the window."""

    ends: dict[str, datetime]
    default: datetime
    info: dict[str, SettleInfo]
    window_end: datetime

    @property
    def affected(self) -> list[str]:
        return sorted(ip for ip, end in self.ends.items() if end < self.window_end)

    @property
    def active(self) -> bool:
        return bool(self.affected) or self.default < self.window_end

    def end_for(self, exporter: str, sw: SlotWindow) -> datetime:
        end = self.ends.get(exporter, self.default)
        return min(sw.end, max(sw.first, end))

    def sql(self, slot_col: str, e_col: str, params: dict) -> str:
        """Predicate: the slot at ``slot_col`` of exporter ``e_col`` has settled."""
        if not self.active:
            return "1"
        keys = sorted(self.ends)
        params["cap_st_k"] = [ip_param(ip) for ip in keys]
        params["cap_st_v"] = [int(self.ends[ip].timestamp()) for ip in keys]
        params["cap_st_d"] = int(self.default.timestamp())
        return (f"toUInt64(toUnixTimestamp({slot_col})) < transform(toString({e_col}), "
                f"CAST(%(cap_st_k)s AS Array(String)), CAST(%(cap_st_v)s AS Array(UInt64)), "
                f"toUInt64(%(cap_st_d)s))")

    def describe(self) -> list[dict]:
        return [{"exporter": ip, "settled_until": _iso(self.ends[ip]),
                 "active_timeout_s": self.info[ip].active_timeout_s, "basis": self.info[ip].basis}
                for ip in self.affected]


def no_settle(sw: SlotWindow) -> SettlePlan:
    return SettlePlan({}, sw.end, {}, sw.end)


async def settle_plan(sw: SlotWindow, vis: FlowVisibility, now: datetime | None = None) -> SettlePlan:
    """The provisional exclusion for a window: nothing for windows that
    ended before any exporter's slots could still change."""
    now = now or now_utc()
    if sw.empty or sw.end <= floor_to(now - timedelta(seconds=SETTLE_MAX_S + SETTLE_MARGIN_S), SLOT_S):
        return no_settle(sw)
    try:
        evidence = await exporter_settle_evidence()
    except Exception:  # noqa: BLE001 - the side lookup must not fail the page
        logger.warning("netflow capacity: settling evidence unavailable", exc_info=True)
        current_meta().warn("provisional_unknown", "Whether the newest slots are still settling could not be "
                                                   "determined; they are counted as measured")
        return no_settle(sw)
    ends = {ip: settle_at(si, now) for ip, si in evidence.items() if vis.allows(ip)}
    info = {ip: evidence[ip] for ip in ends}
    return SettlePlan(ends, settle_at(None, now), info, sw.end)


# ── Interface metadata and speeds (NF-16) ─────────────────────────────────────

RESOLUTIONS = ("resolved", "exporter_unmapped", "no_interface_inventory", "interface_not_found", "speed_unknown")


@dataclass
class IfaceMeta:
    exporter: str
    ifindex: int
    device_id: str | None = None
    device_hostname: str | None = None
    owner_source: str | None = None          # 'mapped' | 'ip-match'
    device_interfaces: int = 0               # inventory size of the owner device
    found: bool = False                      # ifIndex present in the device inventory
    if_name: str | None = None
    if_descr: str | None = None
    if_alias: str | None = None
    snmp_speed: int | None = None
    configured_speed: int | None = None
    sflow_speed: int | None = None
    override: dict | None = None             # netflow_interface_overrides row

    @property
    def key(self) -> tuple[str, int]:
        return self.exporter, self.ifindex

    @property
    def role(self) -> str | None:
        return (self.override or {}).get("role")

    @property
    def display_name(self) -> str:
        o = self.override or {}
        return (o.get("display_name") or self.if_name or self.if_descr or self.if_alias
                or f"ifIndex {self.ifindex}")

    def speed(self, direction: str) -> tuple[int | None, str]:
        """(bit/s, source) for 'in' or 'out' by the shared rule; sFlow's
        reported ifSpeed stands in for SNMP when the interface is not polled."""
        o = self.override or {}
        override = o.get("speed_in_bps") if direction == "in" else o.get("speed_out_bps")
        snmp = self.snmp_speed if self.snmp_speed else self.sflow_speed
        speed, source = effective_speed_source(self.configured_speed, override, snmp)
        if source == SOURCE_SNMP and not self.snmp_speed:
            source = SOURCE_SFLOW
        return speed, source

    @property
    def resolution(self) -> str:
        """Whether utilization can be computed and, if not, the first thing to fix."""
        if self.speed("in")[0] and self.speed("out")[0]:
            return "resolved"
        if self.device_id is None:
            return "exporter_unmapped"
        if self.device_interfaces == 0:
            return "no_interface_inventory"
        if not self.found:
            return "interface_not_found"
        return "speed_unknown"


def speed_block(meta: IfaceMeta) -> dict:
    """The speeds in use per direction with their sources, every candidate
    speed, and which NetFlow override speeds are not in use because the
    device's configured circuit speed takes precedence (``override_shadowed``,
    directions) -- an override that silently does nothing is a trap."""
    speed_in, src_in = meta.speed("in")
    speed_out, src_out = meta.speed("out")
    o = meta.override or {}
    shadowed = [d for d, src in (("in", src_in), ("out", src_out))
                if o.get(f"speed_{d}_bps") and src == SOURCE_CONFIGURED]
    return {
        "in_bps": speed_in, "out_bps": speed_out, "source_in": src_in, "source_out": src_out,
        "snmp_bps": meta.snmp_speed, "configured_bps": meta.configured_speed,
        "override_in_bps": o.get("speed_in_bps"), "override_out_bps": o.get("speed_out_bps"),
        "sflow_bps": meta.sflow_speed,
        "override_shadowed": shadowed,
    }


async def load_overrides(db: AsyncSession, exporters: list[str] | None = None) -> dict[tuple[str, int], dict]:
    sql = ("SELECT host(o.exporter_ip), o.ifindex, o.display_name, o.speed_in_bps, o.speed_out_bps, o.role, "
           "o.note, o.updated_at, u.username FROM netflow_interface_overrides o "
           "LEFT JOIN users u ON u.id = COALESCE(o.updated_by, o.created_by)")
    params: dict = {}
    if exporters is not None:
        if not exporters:
            return {}
        sql += " WHERE host(o.exporter_ip) = ANY(:ips)"
        params["ips"] = sorted(exporters)
    out: dict[tuple[str, int], dict] = {}
    for r in await _pg_rows(db, sql, params):
        ip = canonical_ip(r[0])
        if ip is None:
            continue
        out[(ip, int(r[1]))] = {
            "exporter": ip, "ifindex": int(r[1]), "display_name": r[2],
            "speed_in_bps": int(r[3]) if r[3] else None, "speed_out_bps": int(r[4]) if r[4] else None,
            "role": r[5], "note": r[6],
            "updated_at": r[7].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(r[7], datetime) else None,
            "updated_by": r[8],
        }
    return out


async def sflow_speeds(sw: SlotWindow, vis: FlowVisibility) -> dict[tuple[str, int], int]:
    """Latest non-zero ifSpeed per interface reported in sFlow counter samples."""
    if sw.empty:
        return {}
    params: dict = {"cap_first": sw.first - timedelta(days=1), "cap_end": sw.end}
    vis_sql, vis_params = visibility_predicate(vis, param="cap_vis_ctr")
    params.update(vis_params)
    res = await run_query(
        f"""SELECT replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS exporter, ifindex,
                   argMax(if_speed, ts) AS speed
            FROM {table_ref('flow_if_counters')}
            WHERE ts >= %(cap_first)s AND ts < %(cap_end)s AND if_speed > 0
              {f'AND {vis_sql}' if vis_sql else ''}
            GROUP BY exporter, ifindex""",
        params, label="capacity-sflow-speed",
    )
    out: dict[tuple[str, int], int] = {}
    for r in res.result_rows:
        ip = canonical_ip(r[0])
        if ip and r[2]:
            out[(ip, int(r[1]))] = int(r[2])
    return out


async def resolve_interfaces(
    db: AsyncSession,
    keys: Iterable[tuple[str, int]],
    *,
    sflow: Mapping[tuple[str, int], int] | None = None,
    overrides: Mapping[tuple[str, int], dict] | None = None,
) -> dict[tuple[str, int], IfaceMeta]:
    """Names, owner device, speeds and resolution state for (exporter, ifindex)
    keys (canonical exporter text). The owner comes from the resolver that
    also decides visibility (explicit mapping beats a management-IP match)."""
    keys = sorted({(canonical_ip(e) or e, int(i)) for e, i in keys})
    if not keys:
        return {}
    exporters = sorted({e for e, _ in keys})
    owners = await resolve_exporter_owners(db, exporters)
    if overrides is None:
        overrides = await load_overrides(db, exporters)
    device_ids = sorted({o.device_id for o in owners.values()})
    inventory: dict[str, dict[int, tuple]] = {d: {} for d in device_ids}
    if device_ids:
        rows = await _pg_rows(
            db,
            "SELECT device_id::text, if_index, if_name, if_descr, if_alias, if_speed, configured_speed_bps "
            "FROM device_interfaces WHERE device_id = ANY(CAST(:ids AS uuid[]))",
            {"ids": device_ids},
        )
        for r in rows:
            inventory.setdefault(r[0], {})[int(r[1])] = r
    out: dict[tuple[str, int], IfaceMeta] = {}
    for exporter, ifindex in keys:
        m = IfaceMeta(exporter=exporter, ifindex=ifindex)
        owner = owners.get(exporter)
        if owner is not None:
            m.device_id, m.device_hostname, m.owner_source = owner.device_id, owner.hostname, owner.source
            dev = inventory.get(owner.device_id, {})
            m.device_interfaces = len(dev)
            row = dev.get(ifindex)
            if row is not None:
                m.found = True
                m.if_name, m.if_descr, m.if_alias = row[2], row[3], row[4]
                m.snmp_speed = int(row[5]) if row[5] and int(row[5]) > 0 else None
                m.configured_speed = int(row[6]) if row[6] and int(row[6]) > 0 else None
        m.sflow_speed = (sflow or {}).get((exporter, ifindex))
        m.override = (overrides or {}).get((exporter, ifindex))
        out[(exporter, ifindex)] = m
    return out


# ── The capacity computation ──────────────────────────────────────────────────

@dataclass
class CapacityResult:
    sw: SlotWindow
    expected: int
    settings: CapacitySettings
    selector: SlotSelector
    rows: list[dict]
    exporters: list[dict]
    raw_from: datetime | None
    method: dict
    # Exporters whose newest slots were still settling and were left out.
    provisional: list[dict] = field(default_factory=list)


def method_block(settings: CapacitySettings, selector: SlotSelector, *, business_hours: bool) -> dict:
    p = settings.percentile
    return {
        "statistic": f"p{p}",
        "percentile": p,
        "percentile_method": "nearest_rank",
        "percentile_definition": (f"Samples sorted ascending; the top {100 - p}% are discarded and the highest "
                                  f"remaining sample is the {p}th percentile (rank = N - floor(N x {100 - p} / 100))."),
        "slot_seconds": SLOT_S,
        "window": "complete 5-minute slots of the half-open window; partial edge slots are excluded",
        "idle": "zero when the exporter delivered records in the slot",
        "gaps": "slots in which the exporter delivered nothing are excluded, not counted as zero",
        "settling": ("an exporter's newest slots are left out while they can still receive the bytes of "
                     "long-lived flows not yet reported (until the longest flow span the exporter reported in "
                     "the last hour has passed, or 30 minutes when it reported no flow durations; sFlow "
                     "samples settle at once)"),
        "flow_attribution": "bytes spread over each record's duration (capped at 1 hour)",
        "min_samples": min_samples(p),
        "min_coverage_pct": settings.min_coverage_pct,
        "headline": "the direction with the higher utilization (in = received, out = transmitted)",
        "combined": "in + out throughput, never divided by a speed",
        "direction_mode": settings.direction_mode,
        "direction_mode_definition": (
            "Merged: the percentile of in + out per sample" if settings.direction_mode == "merged"
            else "Separate: the percentile of each direction; the higher one is billed"),
        "speed_rule": "configured circuit speed, then NetFlow interface override, then SNMP ifSpeed "
                      "(sFlow counter ifSpeed when not SNMP-polled); unknown speed means unknown utilization",
        "thresholds": {"warn_pct": settings.warn_pct, "crit_pct": settings.crit_pct},
        "state_rule": ("state from the percentile; with too few samples (a short range) from the peak; with too "
                       "little coverage the peak can only raise a warning, critical or over state, never call "
                       "the interface healthy (insufficient data); when one direction's speed is unknown the "
                       "other direction can raise a warning, critical or over state but the interface is never "
                       "called healthy (speed unknown)"),
        "business_hours": settings.business_hours.to_dict() if business_hours else None,
        "slot_selection": selector.describe(),
        "dedup": "exporters emitting both ingress- and egress-monitored records count ingress records only "
                 "(per exporter count_direction)",
    }


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _direction(stats: dict, prefix: str, samples: int, expected: int, speed: int | None,
               settings: CapacitySettings, p_reason: str | None) -> dict:
    avg = (_f(stats[f"{prefix}_sum"]) or 0.0) / samples if samples else None
    p = _f(stats[f"{prefix}_p"]) if p_reason is None else None
    mx = _f(stats[f"{prefix}_max"])
    out = {
        "avg_bps": round(avg, 3) if avg is not None else None,
        "p_bps": p,
        "max_bps": mx,
        "p_reason": p_reason,
        "speed_bps": speed,
        "utilization_avg_pct": pct_of(avg, speed),
        "utilization_p_pct": pct_of(p, speed),
        "utilization_max_pct": pct_of(mx, speed),
    }
    return out


def build_row(stats: dict, meta: IfaceMeta, expected: int, settings: CapacitySettings) -> dict:
    """One interface of the capacity answer (the v2 row shape)."""
    samples = int(stats.get("samples") or 0)
    coverage = round(min(100.0, samples / expected * 100.0), 3) if expected else None
    pct = settings.percentile
    reason = p_available(samples, coverage, pct, settings.min_coverage_pct)
    speed_in, src_in = meta.speed("in")
    speed_out, src_out = meta.speed("out")
    d_in = _direction(stats, "in", samples, expected, speed_in, settings, reason)
    d_out = _direction(stats, "out", samples, expected, speed_out, settings, reason)
    d_in["bytes"] = _f(stats.get("in_bytes"))
    d_out["bytes"] = _f(stats.get("out_bytes"))
    d_in["packets"] = _f(stats.get("in_packets"))
    d_out["packets"] = _f(stats.get("out_packets"))
    # Headline: the configured percentile when it may be stated, else the
    # peak (labelled); the direction with the higher utilization, or the
    # higher rate when no speed is known.
    statistic = "p" if reason is None else "max"
    key = "p_bps" if statistic == "p" else "max_bps"
    candidates = [(name, d[key], pct_of(d[key], spd), spd)
                  for name, d, spd in (("in", d_in, speed_in), ("out", d_out, speed_out))]
    known = [c for c in candidates if c[2] is not None]
    if known:
        name, value, util, spd = max(known, key=lambda c: (c[2], c[1] or 0.0))
    else:
        name, value, util, spd = max(candidates, key=lambda c: c[1] or 0.0)
    room_bps, room_pct = headroom(value, spd)
    state = classify(util, settings, has_samples=samples > 0)
    # A speed known for exactly one direction (an override with one side set);
    # a direction without samples is not an unknown speed.
    unknown_directions = [c[0] for c in candidates if not c[3]]
    partial_speed = len(unknown_directions) == 1
    if partial_speed and state == "healthy":
        # One direction has no known speed, so its share of the link is
        # unknown: the interface is not called healthy on the strength of the
        # other direction (NF-16), and it is counted where the coverage card
        # counts it (capacity not known). A measured warning, critical or
        # over state stays -- the unknown direction cannot undo it.
        state = "unknown"
    low_coverage = coverage is None or coverage + 1e-9 < settings.min_coverage_pct
    if low_coverage and state == "healthy":
        # A poorly covered window says nothing about its gaps: its values can
        # show a problem but never vouch for health (whatever else is short).
        state = "insufficient"
    for d, spd in ((d_in, speed_in), (d_out, speed_out)):
        v = d[key]
        d["headroom_bps"], d["headroom_pct"] = headroom(v, spd)
    comb_avg = (_f(stats.get("sum_sum")) or 0.0) / samples if samples else None
    combined = {
        "avg_bps": round(comb_avg, 3) if comb_avg is not None else None,
        "p_bps": _f(stats.get("sum_p")) if reason is None else None,
        "max_bps": _f(stats.get("sum_max")),
        "bytes": (_f(stats.get("in_bytes")) or 0.0) + (_f(stats.get("out_bytes")) or 0.0),
    }
    if settings.direction_mode == "merged":
        billable = combined["p_bps"]
        billable_direction = "merged"
    else:
        pair = [(d_in["p_bps"], "in"), (d_out["p_bps"], "out")]
        pair = [x for x in pair if x[0] is not None]
        billable, billable_direction = max(pair) if pair else (None, None)
    over_note = None
    if state == "over":
        over_note = ("The measured value exceeds the interface speed. Check the circuit speed or override, "
                     "whether the exporter sends both ingress and egress records for the same traffic, "
                     "and the sampling rate.")
    return {
        "key": f"{meta.exporter}|{meta.ifindex}",
        "exporter": meta.exporter,
        "ifindex": meta.ifindex,
        "display_name": meta.display_name,
        "if_name": meta.if_name,
        "if_descr": meta.if_descr,
        "if_alias": meta.if_alias,
        "device_id": meta.device_id,
        "device_hostname": meta.device_hostname,
        "owner_source": meta.owner_source,
        "interface_in_inventory": meta.found,
        "device_interface_count": meta.device_interfaces,
        "role": meta.role,
        "override": meta.override,
        "resolution": meta.resolution,
        "speed": speed_block(meta),
        "samples": samples,
        "expected_slots": expected,
        "gap_slots": max(0, expected - samples),
        "coverage_pct": coverage,
        "in": d_in,
        "out": d_out,
        "combined": combined,
        "billable": {"bps": billable, "direction": billable_direction, "mode": settings.direction_mode},
        "headline": {
            "direction": name,
            "statistic": f"p{pct}" if statistic == "p" else "max",
            "value_bps": value,
            "utilization_pct": util,
            "headroom_bps": room_bps,
            "headroom_pct": room_pct,
            "state": state,
            "reason": reason,
            "note": over_note,
            "partial_speed": partial_speed,
            "unknown_directions": unknown_directions,
        },
        "last_slot": _iso(stats.get("last_slot")),
        "previous": None,
    }


def _iso(v) -> str | None:
    d = _dt(v)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


async def _stats_rows(sw: SlotWindow, f: FlowFilter, vis: FlowVisibility, selector: SlotSelector,
                      policy: DirectionPolicy, pct: int, raw_from: datetime | None, label: str,
                      settle: "SettlePlan | None" = None) -> list[dict]:
    if sw.empty:
        return []
    sql, params = stats_sql(sw, f, vis, selector, policy, pct, raw_from, settle=settle)
    res = await run_query(_select_scalars(sql), params, label=label)
    out = []
    for r in res.result_rows:
        if len(r) < len(SCALAR_COLUMNS):
            continue
        d = dict(zip(SCALAR_COLUMNS, r))
        ip = canonical_ip(d["exporter"])
        if ip is None:
            continue
        d["exporter"] = ip
        d["ifindex"] = int(d["ifindex"])
        out.append(d)
    return out


async def _exporter_directions(sw: SlotWindow, f: FlowFilter, vis: FlowVisibility) -> dict[str, list[int]]:
    if sw.empty:
        return {}
    params: dict = {"cap_first": sw.first, "cap_end": sw.end}
    exp = FlowFilter(exporter=f.exporter or (f.iface.exporter if f.iface else None))
    sql, fparams = compile_filter(exp, INTERFACE_5M, vis)
    params.update(fparams)
    res = await run_query(
        f"""SELECT replaceRegexpOne(toString({EXPORTER}), '^::ffff:', '') AS exporter,
                   groupUniqArray(flow_direction) AS dirs
            FROM {table_ref(INTERFACE_5M)}
            WHERE timestamp >= %(cap_first)s AND timestamp < %(cap_end)s AND {sql}
            GROUP BY exporter""",
        params, label="capacity-directions",
    )
    out: dict[str, list[int]] = {}
    for r in res.result_rows:
        ip = canonical_ip(r[0])
        if ip:
            out[ip] = sorted(int(x) for x in (r[1] or []))
    return out


async def slot_window_for(window: Window) -> SlotWindow:
    retention = await get_retention()
    horizon = retention.horizon(INTERFACE_5M, now_utc()) if retention.known else None
    return complete_slots(window.start, window.end, horizon)


async def compute_capacity(
    window: Window,
    f: FlowFilter,
    vis: FlowVisibility,
    db: AsyncSession,
    *,
    settings: CapacitySettings | None = None,
    business_hours: bool = False,
    previous: bool = False,
    record_meta: bool = True,
    exclude_provisional: bool = False,
) -> CapacityResult:
    """Every interface's capacity statistics for the window (the whole
    authorized set -- callers sort, filter and page). Also the entry point for
    the management report (B8).

    ``exclude_provisional`` (the Capacity page and billing) leaves out each
    exporter's newest slots while long flows can still be reported into them
    (see ``settle_plan``); a row's ``expected_slots`` then ends where its
    exporter's settled slots end. Interface alerts keep their own settle
    delay and do not ask for it."""
    if settings is None:
        settings, _ = await load_settings(db)
    sw = await slot_window_for(window)
    selector = build_selector(f, settings, business_hours)
    expected = expected_slots(sw, selector)
    settle = await settle_plan(sw, vis) if exclude_provisional else no_settle(sw)
    policy = await load_direction_policy(db)
    meta = current_meta()
    if record_meta:
        meta.tables.add(INTERFACE_5M)
        meta.set_source("interface_rollup", sw.first, sw.end if not sw.empty else sw.first, SLOT_S,
                        force=True, retention_checked=True)
        if sw.head_excluded_s or sw.tail_excluded_s:
            meta.warn("partial_slots_excluded",
                      f"Partial 5-minute slots at the edges are excluded ({sw.head_excluded_s} s at the start, "
                      f"{sw.tail_excluded_s} s at the end)")
        if sw.retention_cut:
            meta.retention_limited = True
            retention = await get_retention()
            meta.warn("interface_retention",
                      f"Interface history is kept for {retention.interface_days} days; "
                      "the part of the range before that is not included")
        if sw.empty:
            meta.warn("no_complete_slot", "The range holds no complete 5-minute slot")
    raw_from = await rollup_lag(sw, vis)
    if raw_from is not None and record_meta:
        meta.set_source("hybrid", sw.first, sw.end, SLOT_S, force=True, retention_checked=True)
        meta.tables.add(RAW)
        meta.warn("interface_rollup_lagging",
                  "The interface rollup is behind; the newest records are computed from raw flows "
                  f"(from {_iso(raw_from)})")
    jobs = [
        _stats_rows(sw, f, vis, selector, policy, settings.percentile, raw_from, "capacity-stats", settle),
        _exporter_directions(sw, f, vis),
        sflow_speeds(sw, vis),
    ]
    prev_sw = sw.shifted_back() if previous and not sw.empty else None
    prev_settle = None
    if prev_sw is not None:
        prev_settle = await settle_plan(prev_sw, vis) if exclude_provisional else no_settle(prev_sw)
        jobs.append(_stats_rows(prev_sw, f, vis, selector, policy, settings.percentile, None, "capacity-previous",
                                prev_settle))
    results = await asyncio.gather(*jobs)
    stats, directions, sflow = results[0], results[1], results[2]
    prev_stats = results[3] if prev_sw is not None else []
    metas = await resolve_interfaces(db, [(s["exporter"], s["ifindex"]) for s in stats], sflow=sflow)
    expected_at: dict[datetime, int] = {sw.end: expected}

    def expected_for(plan: SettlePlan, w: SlotWindow, exporter: str, base: int) -> tuple[int, datetime]:
        end = plan.end_for(exporter, w)
        if end >= w.end:
            return base, w.end
        if end not in expected_at:
            expected_at[end] = expected_slots(SlotWindow(w.first, end), selector) if end > w.first else 0
        return expected_at[end], end

    rows = []
    for s in stats:
        exp_n, end = expected_for(settle, sw, s["exporter"], expected)
        row = build_row(s, metas[(s["exporter"], s["ifindex"])], exp_n, settings)
        row["settled_until"] = _iso(end) if end < sw.end else None
        rows.append(row)
    if prev_sw is not None:
        prev_expected = expected_slots(prev_sw, selector)
        prev_by_key = {(s["exporter"], s["ifindex"]): s for s in prev_stats}
        expected_at.clear()
        expected_at[prev_sw.end] = prev_expected
        for row in rows:
            ps = prev_by_key.get((row["exporter"], row["ifindex"]))
            p_exp, _ = expected_for(prev_settle, prev_sw, row["exporter"], prev_expected)
            row["previous"] = previous_block(row, ps, metas[(row["exporter"], row["ifindex"])],
                                             p_exp, settings, prev_sw)
    provisional = []
    if settle.active:
        seen = {r["exporter"] for r in rows} | set(directions)
        for ip in sorted(seen):
            end = settle.end_for(ip, sw)
            if end < sw.end:
                si = settle.info.get(ip)
                provisional.append({"exporter": ip, "settled_until": _iso(end),
                                    "provisional_slots": int((sw.end - end).total_seconds()) // SLOT_S,
                                    "active_timeout_s": si.active_timeout_s if si else SETTLE_DEFAULT_S,
                                    "basis": si.basis if si else "default"})
        if provisional and record_meta:
            longest = max(p["provisional_slots"] for p in provisional) * SLOT_S // 60
            timeouts = sorted({p["active_timeout_s"] for p in provisional})
            meta.warn(
                "provisional_slots_excluded",
                f"The newest {longest} minutes are still settling and are left out: exporters report a "
                "long-lived flow only when it ends or at their active timeout (here up to "
                f"{max(timeouts) // 60} min{', assumed where no flow durations were seen' if any(p['basis'] == 'default' for p in provisional) else ''}"
                "), so those slots can still receive traffic and are not idle",
            )
    exporters = []
    for ip in sorted({r["exporter"] for r in rows} | set(directions)):
        setting = policy.setting_for(ip)
        exporters.append({"exporter": ip, "count_direction": setting,
                          "directions_seen": directions.get(ip, []),
                          "accounting_basis": accounting_basis(setting, directions.get(ip, []))})
    return CapacityResult(sw=sw, expected=expected, settings=settings, selector=selector, rows=rows,
                          exporters=exporters, raw_from=raw_from,
                          method=method_block(settings, selector, business_hours=business_hours),
                          provisional=provisional)


def previous_block(row: dict, ps: dict | None, meta: IfaceMeta, expected: int,
                   settings: CapacitySettings, prev_sw: SlotWindow) -> dict:
    """The same statistic over the previous period, and the change."""
    base = {"from": _iso(prev_sw.first), "to": _iso(prev_sw.end)}
    if ps is None:
        return {**base, "value_bps": None, "utilization_pct": None, "change_pct": None, "samples": 0,
                "reason": "no_traffic"}
    prev = build_row(ps, meta, expected, settings)
    direction = row["headline"]["direction"]
    statistic = row["headline"]["statistic"]
    d = prev[direction]
    # Compare like with like: the same direction and the same statistic.
    value = d["max_bps"] if statistic == "max" else d["p_bps"]
    reason = None if value is not None else (d.get("p_reason") or "no_samples")
    cur = row["headline"]["value_bps"]
    change = None
    if value and cur is not None:
        change = round((cur - value) / value * 100.0, 3)
    return {**base, "direction": direction, "statistic": statistic, "value_bps": value,
            "utilization_pct": pct_of(value, row[direction]["speed_bps"]), "change_pct": change,
            "samples": prev["samples"], "coverage_pct": prev["coverage_pct"], "reason": reason}


def summarize(rows: list[dict]) -> dict:
    """Coverage and state counts over the whole set (NF-16, not a page)."""
    by_resolution = {r: 0 for r in RESOLUTIONS}
    by_state = {s: 0 for s in STATES}
    unmapped: set[str] = set()
    for row in rows:
        by_resolution[row["resolution"]] = by_resolution.get(row["resolution"], 0) + 1
        by_state[row["headline"]["state"]] = by_state.get(row["headline"]["state"], 0) + 1
        if row["resolution"] == "exporter_unmapped":
            unmapped.add(row["exporter"])
    return {
        "interfaces_total": len(rows),
        "speed_known": by_resolution["resolved"],
        "speed_unknown": len(rows) - by_resolution["resolved"],
        "by_resolution": by_resolution,
        "by_state": by_state,
        "exporters_unmapped": sorted(unmapped),
    }


def risk_key(row: dict) -> tuple:
    """Ranking by utilization risk: over > critical > warning > healthy >
    insufficient > unknown, then higher utilization, then higher traffic."""
    h = row["headline"]
    return (STATE_RANK.get(h["state"], 99), -(h["utilization_pct"] or -1.0), -(h["value_bps"] or 0.0),
            row["exporter"], row["ifindex"])


# ── Billing (CR-08) ───────────────────────────────────────────────────────────

def _pct_text(v: float | None) -> str:
    """A coverage percentage with enough decimals never to round onto a threshold."""
    if v is None:
        return "unknown"
    return f"{math.floor(v * 100) / 100:.2f}".rstrip("0").rstrip(".") + "%"


def billing_grade(row: dict, *, period_complete: bool, retention_cut: bool, settings: CapacitySettings) -> list[str]:
    """Reasons a billing figure is only indicative (empty = billing-grade)."""
    notes = []
    if not period_complete:
        notes.append("The period has not ended")
    if retention_cut:
        notes.append("Part of the period is older than the interface history retention")
    cov = row.get("coverage_pct")
    if cov is None or cov + 1e-9 < settings.billing_min_coverage_pct:
        notes.append(f"Coverage {_pct_text(cov)} is below the {settings.billing_min_coverage_pct:g}% billing minimum")
    if row["billable"]["bps"] is None:
        reason = row["headline"].get("reason") or "no_samples"
        notes.append({"too_few_samples": "Too few samples for the percentile",
                      "low_coverage": "Coverage too low for the percentile",
                      "no_samples": "No samples"}.get(reason, reason))
    return notes


def month_bounds(month: str, tz: str) -> tuple[datetime, datetime]:
    """[first instant, first instant of the next month) of ``YYYY-MM`` in ``tz``."""
    try:
        y, m = (int(x) for x in month.split("-", 1))
        if not 2000 <= y <= 2100 or not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        raise ValueError("month must look like 2026-09") from None
    zone = ZoneInfo(tz)
    start = datetime(y, m, 1, tzinfo=zone)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ── Trends and forecast (CR-09, FE-05) ────────────────────────────────────────

def _local_day(slot: datetime, tz: str) -> date:
    return slot.astimezone(ZoneInfo(tz)).date()


def expected_by_day(sw: SlotWindow, selector: SlotSelector, tz: str) -> dict[date, tuple[int, int]]:
    """{local day: (expected sample slots in the window, slots in the full day)}."""
    zone = ZoneInfo(tz)
    counts: dict[date, int] = {}
    for s in iter_slots(sw):
        if selector.active and not selector.matches(s):
            continue
        d = s.astimezone(zone).date()
        counts[d] = counts.get(d, 0) + 1
    out = {}
    for d, n in counts.items():
        day_start = datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(timezone.utc)
        next_day = d + timedelta(days=1)
        day_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone).astimezone(timezone.utc)
        full = SlotWindow(day_start, day_end)
        total = expected_slots(full, selector) if selector.active else full.n_slots
        out[d] = (n, total)
    return out


def trend_sql(sw: SlotWindow, exporter: str, ifindex: int, vis: FlowVisibility, selector: SlotSelector,
              policy: DirectionPolicy, pct: int, tz: str) -> tuple[str, dict]:
    """Per local day: samples and per-direction zero-padded statistics for one interface."""
    from app.api.v1.netflow.filters import IfaceRef

    f = FlowFilter(exporter=exporter, iface=IfaceRef(ifindex, exporter))
    params: dict = {"cap_pct": int(pct), "cap_tz": tz}
    source = _traffic_from(sw, vis, None, params)
    live = _live_cte(sw, f, vis, selector, params)
    auto = _auto_cte(vis, params)
    row_filter = _row_filter(f, vis, params)
    dedup = dedup_sql(policy, params)
    sel = selector.sql("timestamp", params, prefix="cap_tsel")
    sql = f"""
WITH
    {live},
    {auto}
SELECT
    toString(greatest(d.day, s.day)) AS day,
    d.n_live + s.n_extra AS samples,
    s.n_traffic AS n_traffic,
    samples - s.n_traffic AS zeros,
    toUInt32(samples - intDiv(samples * (100 - %(cap_pct)s), 100)) AS rnk,
    arraySort(s.in_v) AS in_sorted,
    if(samples = 0, 0., if(rnk <= zeros, 0., arrayElement(in_sorted, rnk - zeros))) AS in_p,
    if(empty(s.in_v), 0., arrayMax(s.in_v)) AS in_max,
    arraySum(s.in_v) AS in_sum,
    arraySort(s.out_v) AS out_sorted,
    if(samples = 0, 0., if(rnk <= zeros, 0., arrayElement(out_sorted, rnk - zeros))) AS out_p,
    if(empty(s.out_v), 0., arrayMax(s.out_v)) AS out_max,
    arraySum(s.out_v) AS out_sum,
    s.in_bytes AS in_bytes, s.out_bytes AS out_bytes
FROM (
    SELECT toDate(slot, %(cap_tz)s) AS day, count() AS n_live FROM nf_cap_live GROUP BY day
) AS d
FULL OUTER JOIN (
    SELECT toDate(t.slot, %(cap_tz)s) AS day,
           count() AS n_traffic, countIf(l.is_live = 0) AS n_extra,
           groupArray(t.in_bps) AS in_v, groupArray(t.out_bps) AS out_v,
           sum(t.in_b) AS in_bytes, sum(t.out_b) AS out_bytes
    FROM (
        SELECT {EXPORTER} AS e, timestamp AS slot,
               sumIf(bytes, direction = 0) AS in_b, sumIf(bytes, direction = 1) AS out_b,
               {_bps('in_b')} AS in_bps, {_bps('out_b')} AS out_bps
        FROM {source}
        WHERE timestamp >= %(cap_first)s AND timestamp < %(cap_end)s AND {row_filter} AND {dedup} AND {sel}
        GROUP BY e, slot
    ) AS t
    LEFT JOIN (SELECT e, slot, toUInt8(1) AS is_live FROM nf_cap_live) AS l ON t.e = l.e AND t.slot = l.slot
    GROUP BY day
) AS s ON d.day = s.day
"""
    wrapped = ("SELECT day, samples, n_traffic, zeros, in_p, in_max, in_sum, out_p, out_max, out_sum, "
               f"in_bytes, out_bytes FROM ({sql}) ORDER BY day")
    return wrapped, params


TREND_COLUMNS = ("day", "samples", "n_traffic", "zeros", "in_p", "in_max", "in_sum", "out_p", "out_max",
                 "out_sum", "in_bytes", "out_bytes")


def linear_fit(xs: list[float], ys: list[float]) -> dict | None:
    """Least-squares line y = a + b x with R-squared (None for < 2 points)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else (1.0 if ss_res == 0 else 0.0)
    return {"slope": slope, "intercept": intercept, "r2": max(0.0, min(1.0, r2)), "n": n}


def confidence(r2: float, n: int) -> str:
    if r2 >= 0.7 and n >= 28:
        return "high"
    if r2 >= 0.4:
        return "medium"
    return "low"


def forecast(points: list[tuple[date, float]], speed: int | None, settings: CapacitySettings,
             today: date, *, min_days: int = FORECAST_MIN_DAYS) -> dict:
    """Runout forecast from daily values (bit/s): days until the warning,
    critical and full levels at the fitted linear growth, with R-squared as
    confidence. Never forecasts from fewer than ``min_days`` qualifying days."""
    base = {"method": "least_squares_linear", "min_days": min_days, "days_used": len(points),
            "slope_bps_per_day": None, "r2": None, "confidence": None, "fitted_now_bps": None,
            "days_to_warn": None, "days_to_crit": None, "days_to_full": None,
            "status": None, "reason": None}
    if len(points) < min_days:
        return {**base, "status": "insufficient_history",
                "reason": f"{len(points)} qualifying day{'s' if len(points) != 1 else ''}; "
                          f"a forecast needs at least {min_days}"}
    span = (points[-1][0] - points[0][0]).days + 1
    if span < min_days:
        return {**base, "status": "insufficient_history",
                "reason": f"History spans {span} days; a forecast needs at least {min_days}"}
    origin = points[0][0]
    xs = [float((d - origin).days) for d, _ in points]
    ys = [v for _, v in points]
    fit = linear_fit(xs, ys)
    if fit is None:
        return {**base, "status": "insufficient_history", "reason": "History has no spread to fit"}
    x_now = float((today - origin).days)
    fitted_now = fit["intercept"] + fit["slope"] * x_now
    out = {**base, "slope_bps_per_day": round(fit["slope"], 3), "r2": round(fit["r2"], 4),
           "confidence": confidence(fit["r2"], fit["n"]), "fitted_now_bps": round(max(0.0, fitted_now), 3),
           "status": "ok"}
    if not speed:
        return {**out, "status": "speed_unknown",
                "reason": "The interface speed is unknown, so runout cannot be forecast"}
    if fit["slope"] <= 0:
        return {**out, "status": "not_growing", "reason": "Traffic is flat or falling over this history"}
    levels = {"days_to_warn": settings.warn_pct, "days_to_crit": settings.crit_pct, "days_to_full": 100.0}
    for key, level_pct in levels.items():
        level = speed * level_pct / 100.0
        if fitted_now >= level:
            out[key] = 0.0
            continue
        days = (level - fitted_now) / fit["slope"]
        out[key] = round(days, 1) if days <= FORECAST_HORIZON_DAYS else None
    if all(out[k] is None for k in levels):
        out["status"] = "beyond_horizon"
        out["reason"] = f"No level is reached within {FORECAST_HORIZON_DAYS} days at the current growth"
    return out


SPEED_HISTORY_LIMIT = 500
_SPEED_KEYS = {"in": "speed_in_bps", "out": "speed_out_bps"}


def _speed_value(v) -> int | None:
    try:
        n = int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None
    return n if n and n > 0 else None


def parse_speed_change(created_at, action: str, resource_type: str, md: Mapping, actor, ifindex: int) -> dict | None:
    """One audit row as a speed change of interface ``ifindex``: per direction
    ``{old, new}`` (None = no speed), or None when the row changed no speed
    (an override edit of only the name, role or note)."""
    at = created_at.astimezone(timezone.utc) if isinstance(created_at, datetime) else None
    entry: dict = {"at": at.strftime("%Y-%m-%dT%H:%M:%SZ") if at else None, "action": action, "actor": actor,
                   "_at": at}
    if resource_type == "device_interface":
        change = md.get("configured_speed_bps")
        if not isinstance(change, Mapping):
            return None
        old = change.get("old")
        if isinstance(old, Mapping):
            # A batch row written before bulk changes were audited per
            # interface: {"old": {"<ifIndex>": speed}, "new": speed}.
            old = old.get(str(ifindex))
        pair = {"old": _speed_value(old), "new": _speed_value(change.get("new"))}
        if pair["old"] == pair["new"]:
            return None
        return {**entry, "source": "configured", "in": pair, "out": dict(pair), **pair, "changes": None}
    changes = md.get("changes") if isinstance(md.get("changes"), Mapping) else {}
    sides: dict = {}
    for d, key in _SPEED_KEYS.items():
        c = changes.get(key)
        if isinstance(c, Mapping):
            pair = {"old": _speed_value(c.get("old")), "new": _speed_value(c.get("new"))}
            if pair["old"] != pair["new"]:
                sides[d] = pair
    if not sides:
        return None
    same = sides.get("in") == sides.get("out")
    return {**entry, "source": "override", "in": sides.get("in"), "out": sides.get("out"),
            "old": sides["in"]["old"] if same else None, "new": sides["in"]["new"] if same else None,
            "changes": dict(changes)}


async def speed_history(db: AsyncSession, meta: IfaceMeta) -> list[dict]:
    """Audited speed changes of this interface, newest first -- historical
    circuit capacity (FE-05): the device's configured circuit speed (single
    and bulk changes; batch rows of older releases too) and the NetFlow
    override's per-direction speeds, each with its old and new value."""
    ids = [f"{meta.exporter}|{meta.ifindex}"]
    params: dict = {"ids": ids}
    batch = ""
    if meta.device_id:
        ids.append(f"{meta.device_id}:{meta.ifindex}")
        batch = (" OR (resource_type = 'device_interface' AND action = 'device_interface.speed.bulk_update' "
                 "AND metadata->>'device_id' = :dev AND metadata->'if_indexes' @> CAST(:ifx AS jsonb))")
        params.update(dev=str(meta.device_id), ifx=f"[{int(meta.ifindex)}]")
    rows = await _pg_rows(
        db,
        "SELECT created_at, action, resource_type, metadata, actor_username FROM audit_logs "
        "WHERE (resource_type IN ('device_interface', 'netflow_interface_override') AND resource_id = ANY(:ids))"
        f"{batch} ORDER BY created_at DESC LIMIT {SPEED_HISTORY_LIMIT}",
        params,
    )
    out = []
    for r in rows:
        md = r[3] if isinstance(r[3], Mapping) else {}
        e = parse_speed_change(r[0], r[1], r[2], md, r[4], meta.ifindex)
        if e is not None:
            out.append(e)
    return out


@dataclass(frozen=True)
class SpeedState:
    """The speed inputs an audit trail can change (SNMP ifSpeed history is not kept)."""

    configured: int | None
    override_in: int | None
    override_out: int | None


def speed_timeline(meta: IfaceMeta, history: list[dict]) -> list[tuple[datetime | None, SpeedState]]:
    """[(from, state)] oldest first: the state in effect from that instant
    (None = since before the audit trail), reconstructed by undoing the
    audited changes newest first from the current inputs."""
    o = meta.override or {}
    state = SpeedState(meta.configured_speed, _speed_value(o.get("speed_in_bps")),
                       _speed_value(o.get("speed_out_bps")))
    segments: list[tuple[datetime | None, SpeedState]] = []
    for e in history:                       # newest first
        at = e.get("_at")
        if at is None:
            continue
        segments.append((at, state))
        if e["source"] == "configured":
            state = replace(state, configured=e["in"]["old"])
        else:
            if e.get("in"):
                state = replace(state, override_in=e["in"]["old"])
            if e.get("out"):
                state = replace(state, override_out=e["out"]["old"])
    segments.append((None, state))
    return segments[::-1]


def speeds_at(meta: IfaceMeta, timeline: list[tuple[datetime | None, SpeedState]], t: datetime) -> dict:
    """Effective per-direction speeds at instant ``t`` under the shared rule."""
    state = timeline[0][1]
    for start, st in timeline:
        if start is None or start <= t:
            state = st
        else:
            break
    o = dict(meta.override or {})
    o.update(speed_in_bps=state.override_in, speed_out_bps=state.override_out)
    then = replace(meta, configured_speed=state.configured, override=o)
    (sin, src_in), (sout, src_out) = then.speed("in"), then.speed("out")
    return {"in_bps": sin, "out_bps": sout, "source_in": src_in, "source_out": src_out}


def public_history(history: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in history]


async def interface_trend(
    window: Window,
    exporter: str,
    ifindex: int,
    vis: FlowVisibility,
    db: AsyncSession,
    *,
    settings: CapacitySettings | None = None,
    business_hours: bool = False,
) -> dict:
    """Daily per-direction statistics for one interface plus the forecast."""
    if settings is None:
        settings, _ = await load_settings(db)
    tz = window.tz
    sw = await slot_window_for(window)
    selector = build_selector(None, settings, business_hours)
    policy = await load_direction_policy(db)
    meta = current_meta()
    meta.tables.add(INTERFACE_5M)
    meta.set_source("interface_rollup", sw.first, sw.end if not sw.empty else sw.first, 86400,
                    force=True, retention_checked=True)
    if sw.retention_cut:
        meta.retention_limited = True
    rows: list[dict] = []
    if not sw.empty:
        sql, params = trend_sql(sw, exporter, ifindex, vis, selector, policy, settings.percentile, tz)
        res = await run_query(sql, params, label="capacity-trend")
        rows = [dict(zip(TREND_COLUMNS, r)) for r in res.result_rows if len(r) >= len(TREND_COLUMNS)]
    sflow = await sflow_speeds(sw, vis)
    metas = await resolve_interfaces(db, [(exporter, ifindex)], sflow=sflow)
    im = metas[(canonical_ip(exporter) or exporter, ifindex)]
    speed_in, src_in = im.speed("in")
    speed_out, src_out = im.speed("out")
    expected = expected_by_day(sw, selector, tz)
    pct = settings.percentile
    by_day = {str(r["day"]): r for r in rows}
    # Each day is measured against the circuit speed in effect then (FE-05):
    # the audited circuit-speed and override changes are undone back to the
    # day, so an upgrade does not rewrite the utilization of the days before it.
    history = await speed_history(db, im)
    timeline = speed_timeline(im, history)
    zone = ZoneInfo(tz)
    days = []
    for d in sorted(expected):
        exp_n, full_n = expected[d]
        r = by_day.get(d.isoformat())
        samples = int(r["samples"]) if r else 0
        cov = round(min(100.0, samples / exp_n * 100.0), 3) if exp_n else None
        reason = p_available(samples, cov, pct, settings.min_coverage_pct)
        day_start = datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(timezone.utc)
        nd = d + timedelta(days=1)
        # The end of the local day (today: the current speeds -- every audited
        # change is in the past, even one made within the current second).
        day_end = datetime(nd.year, nd.month, nd.day, tzinfo=zone).astimezone(timezone.utc)
        day_speed = speeds_at(im, timeline, day_end - timedelta(microseconds=1))
        day_speed["changed"] = any(e.get("_at") is not None and day_start <= e["_at"] < day_end for e in history)
        entry = {"day": d.isoformat(), "complete": exp_n == full_n, "samples": samples,
                 "expected_slots": exp_n, "coverage_pct": cov, "p_reason": reason, "speed": day_speed}
        for side in ("in", "out"):
            spd = day_speed[f"{side}_bps"]
            if r and samples:
                p = _f(r[f"{side}_p"]) if reason is None else None
                mx = _f(r[f"{side}_max"])
                avg = (_f(r[f"{side}_sum"]) or 0.0) / samples
            else:
                p = mx = avg = None
            entry[side] = {"p_bps": p, "max_bps": mx, "avg_bps": round(avg, 3) if avg is not None else None,
                           "utilization_p_pct": pct_of(p, spd), "utilization_max_pct": pct_of(mx, spd),
                           "bytes": _f(r[f"{side}_bytes"]) if r else None}
        days.append(entry)
    today = now_utc().astimezone(ZoneInfo(tz)).date()
    qualifying = [e for e in days if e["complete"] and e["p_reason"] is None and d_before(e, today)]
    fc = {}
    for side, spd in (("in", speed_in), ("out", speed_out)):
        pts = [(date.fromisoformat(e["day"]), e[side]["p_bps"]) for e in qualifying if e[side]["p_bps"] is not None]
        fc[side] = forecast(pts, spd, settings, today)
    # Headline: the direction that reaches its levels first (or grows faster).
    def _first(fcd: dict) -> float:
        vals = [v for v in (fcd.get("days_to_warn"), fcd.get("days_to_crit"), fcd.get("days_to_full")) if v is not None]
        return min(vals) if vals else math.inf
    head = min(("in", "out"), key=lambda s: (_first(fc[s]), -(fc[s].get("slope_bps_per_day") or 0)))
    return {
        "exporter": im.exporter, "ifindex": ifindex, "display_name": im.display_name,
        "device_id": im.device_id, "device_hostname": im.device_hostname, "resolution": im.resolution,
        "speed": speed_block(im),
        "window": {"first_slot": _iso(sw.first), "end_exclusive": _iso(sw.end), "tz": tz,
                   "days": len(days)},
        "method": {**method_block(settings, selector, business_hours=business_hours),
                   "forecast": f"Least-squares line through the daily p{pct} of complete days with at least "
                               f"{settings.min_coverage_pct:g}% coverage; at least {FORECAST_MIN_DAYS} days; "
                               "R-squared is the confidence; levels are those of the current speed",
                   "daily_speed": "each day's utilization uses the speed in effect at the end of that day, "
                                  "reconstructed from the audited circuit-speed and NetFlow override changes "
                                  "(SNMP ifSpeed history is not kept: its current value stands for the past)"},
        "days": days,
        "forecast": {"in": fc["in"], "out": fc["out"], "headline_direction": head},
        "speed_history": public_history(history),
    }


def d_before(entry: dict, today: date) -> bool:
    return date.fromisoformat(entry["day"]) < today


# ── Cross-checks for a page of rows ───────────────────────────────────────────

async def _table_exists(name: str) -> bool:
    from app.api.v1.netflow import common as C

    try:
        res = await run_query(
            "SELECT count() FROM system.tables WHERE database = %(db)s AND name = %(name)s",
            {"db": C.CH_DB, "name": name}, label="capacity-table-check",
        )
        return bool(res.result_rows and res.result_rows[0][0])
    except Exception:
        return False


async def snmp_crosscheck(rows: list[dict], sw: SlotWindow, selector: SlotSelector, pct: int) -> None:
    """Per row, the same percentile measured by SNMP interface counters
    (``snmp_if_metrics_5m``) when the interface is SNMP-polled; in place."""
    pairs = sorted({(r["device_id"], r["ifindex"]) for r in rows if r.get("device_id") and r.get("interface_in_inventory")})
    for r in rows:
        r["snmp"] = {"status": "no_device" if not r.get("device_id") else
                     ("not_in_inventory" if not r.get("interface_in_inventory") else "no_data")}
    if not pairs or sw.empty:
        return
    if not await _table_exists("snmp_if_metrics_5m"):
        for r in rows:
            if r["snmp"]["status"] == "no_data":
                r["snmp"] = {"status": "unavailable"}
        return
    params: dict = {"cap_first": sw.first, "cap_end": sw.end, "cap_pct": int(pct),
                    "cap_pairs": [(d, int(i)) for d, i in pairs]}
    sel = selector.sql("ts", params, prefix="cap_ssel")
    res = await run_query(
        f"""SELECT dev, if_index, n,
                   if(n = 0, 0., arrayElement(si, rnk)) AS in_p, if(n = 0, 0., arrayElement(so, rnk)) AS out_p,
                   arrayMax(si) AS in_max, arrayMax(so) AS out_max
            FROM (
                SELECT dev, if_index, count() AS n,
                       toUInt32(n - intDiv(n * (100 - %(cap_pct)s), 100)) AS rnk,
                       arraySort(groupArray(in_bps)) AS si, arraySort(groupArray(out_bps)) AS so
                FROM (
                    SELECT toString(device_id) AS dev, if_index, toDateTime(timestamp) AS ts,
                           round(sum(avg_in_bps * sample_count) / greatest(sum(sample_count), 1), 3) AS in_bps,
                           round(sum(avg_out_bps * sample_count) / greatest(sum(sample_count), 1), 3) AS out_bps
                    FROM {table_ref('snmp_if_metrics_5m')}
                    WHERE timestamp >= %(cap_first)s AND timestamp < %(cap_end)s
                      AND (toString(device_id), if_index) IN %(cap_pairs)s
                    GROUP BY dev, if_index, ts
                    HAVING {sel}
                )
                GROUP BY dev, if_index
            )""",
        params, label="capacity-snmp",
    )
    by = {(r[0], int(r[1])): r for r in res.result_rows}
    for row in rows:
        if row["snmp"]["status"] != "no_data":
            continue
        hit = by.get((row["device_id"], row["ifindex"]))
        if not hit or not hit[2]:
            continue
        n = int(hit[2])
        cov = round(min(100.0, n / row["expected_slots"] * 100.0), 3) if row["expected_slots"] else None
        ok = n >= min_samples(pct)
        snmp = {"status": "ok", "samples": n, "coverage_pct": cov,
                "in_p_bps": _f(hit[3]) if ok else None, "out_p_bps": _f(hit[4]) if ok else None,
                "in_max_bps": _f(hit[5]), "out_max_bps": _f(hit[6])}
        for side in ("in", "out"):
            flow = row[side]["p_bps"] if ok else row[side]["max_bps"]
            ref = snmp[f"{side}_p_bps"] if ok else snmp[f"{side}_max_bps"]
            snmp[f"{side}_delta_pct"] = (round((flow - ref) / ref * 100.0, 1)
                                         if flow is not None and ref else None)
        snmp["statistic"] = f"p{pct}" if ok else "max"
        row["snmp"] = snmp


async def counter_crosscheck(rows: list[dict], sw: SlotWindow, vis: FlowVisibility, pct: int) -> None:
    """Per row, utilization measured from sFlow counter samples
    (``flow_if_counters``, cumulative octets) when the exporter sends them."""
    for r in rows:
        r["counters"] = {"status": "no_data"}
    if not rows or sw.empty:
        return
    keys = sorted({(ip_param(r["exporter"]), int(r["ifindex"])) for r in rows})
    params: dict = {"cap_first": sw.first - SLOT, "cap_end": sw.end, "cap_pct": int(pct), "cap_keys": keys}
    vis_sql, vis_params = visibility_predicate(vis, param="cap_vis_cc")
    params.update(vis_params)
    res = await run_query(
        f"""SELECT exporter, ifindex, length(rates) AS n,
                   arraySort(arrayMap(x -> x.1, rates)) AS ri, arraySort(arrayMap(x -> x.2, rates)) AS ro,
                   toUInt32(n - intDiv(n * (100 - %(cap_pct)s), 100)) AS rnk,
                   if(n = 0, 0., arrayElement(ri, rnk)) AS in_p, if(n = 0, 0., arrayElement(ro, rnk)) AS out_p,
                   if(n = 0, 0., arrayMax(ri)) AS in_max, if(n = 0, 0., arrayMax(ro)) AS out_max, spd
            FROM (
                SELECT exporter, ifindex, spd,
                       arrayFilter(x -> x.3,
                           arrayMap(i -> (
                               (pts[i].2 - pts[i - 1].2) * 8 / (pts[i].1 - pts[i - 1].1),
                               (pts[i].3 - pts[i - 1].3) * 8 / (pts[i].1 - pts[i - 1].1),
                               pts[i].1 - pts[i - 1].1 > 0 AND pts[i].1 - pts[i - 1].1 <= 900
                                 AND pts[i].2 >= pts[i - 1].2 AND pts[i].3 >= pts[i - 1].3
                                 AND pts[i].4 >= %(cap_first_s)s),
                             arraySlice(arrayEnumerate(pts), 2))) AS rates
                FROM (
                    SELECT replaceRegexpOne(toString(e), '^::ffff:', '') AS exporter, ifindex,
                           arraySort(x -> x.1, groupArray((toFloat64(ts_last), toFloat64(in_last),
                                                            toFloat64(out_last), toUInt32(slot)))) AS pts,
                           max(speed) AS spd
                    FROM (
                        SELECT {EXPORTER} AS e, ifindex, toStartOfInterval(ts, INTERVAL {SLOT_S} SECOND) AS slot,
                               max(ts) AS ts_last, argMax(in_octets, ts) AS in_last,
                               argMax(out_octets, ts) AS out_last, argMax(if_speed, ts) AS speed
                        FROM {table_ref('flow_if_counters')}
                        WHERE ts >= %(cap_first)s AND ts < %(cap_end)s AND (e, ifindex) IN %(cap_keys)s
                          {f'AND {vis_sql}' if vis_sql else ''}
                        GROUP BY e, ifindex, slot
                    )
                    GROUP BY e, ifindex
                )
            )""",
        {**params, "cap_first_s": int(sw.first.timestamp())}, label="capacity-counters",
    )
    by = {(canonical_ip(r[0]), int(r[1])): r for r in res.result_rows}
    for row in rows:
        hit = by.get((row["exporter"], row["ifindex"]))
        if not hit or not hit[2]:
            continue
        n = int(hit[2])
        ok = n >= min_samples(pct)
        c = {"status": "ok", "samples": n, "statistic": f"p{pct}" if ok else "max",
             "in_p_bps": _f(hit[6]) if ok else None, "out_p_bps": _f(hit[7]) if ok else None,
             "in_max_bps": _f(hit[8]), "out_max_bps": _f(hit[9]), "if_speed": int(hit[10]) if hit[10] else None}
        for side in ("in", "out"):
            spd = row[side]["speed_bps"]
            v = c[f"{side}_p_bps"] if ok else c[f"{side}_max_bps"]
            c[f"{side}_utilization_pct"] = pct_of(v, spd)
        row["counters"] = c


# ── CSV ───────────────────────────────────────────────────────────────────────

def csv_safe(value) -> str:
    """Cell text that a spreadsheet will not evaluate as a formula."""
    if value is None:
        return ""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(header: list[str], rows: Iterable[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) if isinstance(v, str) else ("" if v is None else v) for v in r])
    return buf.getvalue()


__all__ = [
    "SLOT_S", "SETTINGS_KEY", "PERCENTILES", "DIRECTION_MODES", "BusinessHours", "CapacitySettings",
    "DEFAULT_SETTINGS", "SettingsError", "parse_settings", "load_settings", "save_settings",
    "SlotWindow", "complete_slots", "SlotRule", "SlotSelector", "build_selector", "expected_slots",
    "nearest_rank", "min_samples", "percentile_value", "series_stats", "p_available",
    "classify", "headroom", "pct_of", "DirectionPolicy", "load_direction_policy", "dedup_sql",
    "accounting_basis", "stats_sql", "rollup_lag", "IfaceMeta", "resolve_interfaces", "load_overrides",
    "compute_capacity", "CapacityResult", "summarize", "risk_key", "billing_grade", "month_bounds",
    "interface_trend", "forecast", "linear_fit", "snmp_crosscheck", "counter_crosscheck", "to_csv",
    "csv_safe", "SUPPORTED_KEYS", "unsupported_keys",
]
