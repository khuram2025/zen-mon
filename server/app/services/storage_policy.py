"""Storage pressure policy shared by the API and the storage watchdog.

Standard library only: the watchdog (``app.services.storage_watchdog``) runs
as its own systemd service and must keep protecting free space while the API,
PostgreSQL or both are down, so neither it nor this module may import the
application's database layers.

Three concerns:

* Classification. ClickHouse tables are application telemetry (retention
  categories), ClickHouse diagnostic logs in ``system`` (disposable, bounded
  by TTL), legacy copies of those logs left behind by schema rotation or
  recovery, or protected (never touched).
* Pressure evaluation. Per backing filesystem (``/`` and ``/data`` are
  deduplicated by device), from percentage, minimum free bytes and growth
  runway, with reclamation that is scheduled but not yet visible on disk
  counted as pending so one pressure episode does not trigger repeated
  over-deletion.
* Purge planning. Bounded passes, cheapest-to-lose first: legacy diagnostic
  copies, then the oldest diagnostic log partitions, then whole diagnostic
  tables, then application partitions entirely older than the minimum keep
  window, by category priority and age.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

GIB = 1024 ** 3

# ─── Application telemetry categories ────────────────────────────────────────

CH_DATABASE = "zenplus"

# Tables never touched by retention/purge: bookkeeping and datasets with their
# own retention subsystem (network captures manage purge_after themselves).
EXCLUDED_TABLES = {"schema_migrations"}
EXCLUDED_PREFIXES = ("capture_", "network_capture")

# NetFlow state tables: templates and NBAR names stay valid however old.
STATE_TABLES = {"flow_templates", "flow_app_names"}

# NetFlow tables kept longer by design than their name suggests.
TABLE_CATEGORIES = {
    "flow_interface_5m": "flow_history",
    "flow_exporter_minutes": "flow_history",
    "flow_if_counters": "flow_history",
}

CATEGORIES: dict[str, dict[str, Any]] = {
    "raw_metrics": {
        "label": "Raw metrics",
        "description": "Per-poll SNMP, ping, service and host agent samples",
        "default_days": 30, "min_days": 3,
    },
    "rollups_5m": {
        "label": "5-minute rollups",
        "description": "Aggregated 5-minute series used by most charts",
        "default_days": 90, "min_days": 14,
    },
    "rollups_1h": {
        "label": "Hourly rollups",
        "description": "Long-range hourly series for capacity trends",
        "default_days": 400, "min_days": 30,
    },
    "flows": {
        "label": "NetFlow records",
        "description": "Raw flow records from the NetFlow collector",
        "default_days": 30, "min_days": 3,
    },
    "flow_history": {
        "label": "NetFlow history",
        "description": "Per-interface capacity, exporter delivery coverage and sFlow interface counters",
        "default_days": 400, "min_days": 30,
    },
    "apm": {
        "label": "APM traces",
        "description": "Spans, exceptions and service-graph data",
        "default_days": 30, "min_days": 3,
    },
    "events": {
        "label": "Events & logs",
        "description": "Status change history, SNMP traps and event summaries",
        "default_days": 180, "min_days": 30,
    },
}

# Emergency deletion order for application data: high-volume raw data first,
# long-range history last.
CATEGORY_PURGE_PRIORITY = ("flows", "raw_metrics", "apm", "rollups_5m", "events",
                           "rollups_1h", "flow_history")


def classify_table(name: str) -> Optional[str]:
    """Map an application ClickHouse table to a retention category."""
    if name in EXCLUDED_TABLES or name in STATE_TABLES or name.endswith("_mv"):
        return None
    if any(name.startswith(p) for p in EXCLUDED_PREFIXES):
        return None
    if name in TABLE_CATEGORIES:
        return TABLE_CATEGORIES[name]
    if name.endswith("_5m"):
        return "rollups_5m"
    if name.endswith("_1h"):
        return "rollups_1h"
    if name == "flow_records" or name.startswith("host_network_flows"):
        return "flows"
    if name.startswith("apm_"):
        return "apm"
    if name.endswith(("_status_log", "_traps", "_event_log_summary", "_service_state")):
        return "events"
    return "raw_metrics"


# ─── ClickHouse diagnostic logs ──────────────────────────────────────────────

# Retention in days per system log; mirrors docker/clickhouse/zenplus_logging.xml
# (enforced by a test). Logs not listed here are left to ClickHouse.
DIAGNOSTIC_LOG_RETENTION_DAYS: dict[str, int] = {
    "text_log": 3,
    "trace_log": 3,
    "processors_profile_log": 3,
    "query_views_log": 3,
    "query_thread_log": 3,
    "query_metric_log": 3,
    "s3queue_log": 3,
    "asynchronous_insert_log": 3,
    "query_log": 7,
    "part_log": 7,
    "metric_log": 7,
    "asynchronous_metric_log": 7,
    "blob_storage_log": 7,
    "error_log": 30,
    "backup_log": 30,
}
# Small and useful after an incident: kept even under pressure.
PROTECTED_DIAGNOSTIC_LOGS = {"error_log", "crash_log", "session_log", "backup_log"}

# Logs that docker/clickhouse/zenplus_profile.xml switches off. ClickHouse never
# writes them again, so it never re-creates them with the new TTL: the existing
# table is a frozen legacy copy, removed once its newest day passes retention.
DISABLED_DIAGNOSTIC_LOGS = {"processors_profile_log", "query_views_log"}

_LEGACY_RE = re.compile(r"^(?P<base>[a-z_]+?_log)_\d+$")
# Copies taken by a storage recovery are evidence about that incident: never
# deleted automatically (not even under pressure); an operator decides.
_EVIDENCE_RE = re.compile(r"^(?P<base>[a-z_]+?_log)_pre_recovery_\d{8}(?:_\d+)?$")


# Every diagnostic log name this policy knows. Anything else in ``system``,
# including custom or future ``*_log`` tables and copies of them, is never
# classified as diagnostic and therefore never touched.
KNOWN_DIAGNOSTIC_LOGS = (set(DIAGNOSTIC_LOG_RETENTION_DAYS) | PROTECTED_DIAGNOSTIC_LOGS
                         | DISABLED_DIAGNOSTIC_LOGS)


def diagnostic_kind(table: str) -> Optional[str]:
    """current | legacy | evidence for a known ``system`` log table, else None."""
    m = _EVIDENCE_RE.match(table)
    if m:
        return "evidence" if m.group("base") in KNOWN_DIAGNOSTIC_LOGS else None
    if table in DISABLED_DIAGNOSTIC_LOGS:
        return "legacy"
    if table in KNOWN_DIAGNOSTIC_LOGS:
        return "current"
    m = _LEGACY_RE.match(table)
    if m and m.group("base") in KNOWN_DIAGNOSTIC_LOGS:
        return "legacy"
    return None


def diagnostic_log(table: str) -> Optional[tuple[str, bool]]:
    """(base log name, is_not_current) for a ``system`` log table, else None.

    Legacy copies, disabled logs and recovery evidence all report True; use
    ``diagnostic_kind`` to tell evidence (never deleted) from legacy.
    """
    kind = diagnostic_kind(table)
    if kind is None:
        return None
    if kind == "current":
        return table, False
    m = _EVIDENCE_RE.match(table) or _LEGACY_RE.match(table)
    return (m.group("base") if m else table), True


def diagnostic_retention_days(base: str) -> int:
    return DIAGNOSTIC_LOG_RETENTION_DAYS.get(base, 30 if base in PROTECTED_DIAGNOSTIC_LOGS else 7)


# ─── Policy ──────────────────────────────────────────────────────────────────

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "threshold_pct": 85,          # start purging above this usage
    "target_pct": 75,             # purge down to this usage
    "min_keep_days": 7,           # application data newer than this is never purged
    "min_free_gib": 5.0,          # also act when free space falls below this
    "critical_free_gib": 2.0,     # below this, pause non-essential ingestion
    "critical_pct": 97,
    "runway_hours": 6,            # purge diagnostics when projected full sooner
}

# Pass limits keep each pass short and its effect measurable.
MAX_ACTIONS_PER_PASS = 12
# DROP PARTITION removes data in the background (~10 minutes in ClickHouse);
# until then the bytes are "pending" and must not trigger more deletion.
PENDING_RECLAIM_WINDOW_S = 15 * 60


def normalize_policy(raw: Optional[dict]) -> dict[str, Any]:
    """Merge a stored policy onto defaults, clamping every value to sane bounds."""
    p = dict(DEFAULT_POLICY)
    for key, value in (raw or {}).items():
        if key in p and value is not None:
            p[key] = value
    try:
        p["enabled"] = bool(p["enabled"])
        p["threshold_pct"] = min(98.0, max(50.0, float(p["threshold_pct"])))
        p["target_pct"] = min(p["threshold_pct"] - 1, max(40.0, float(p["target_pct"])))
        p["min_keep_days"] = int(min(365, max(1, int(p["min_keep_days"]))))
        p["min_free_gib"] = min(1024.0, max(0.5, float(p["min_free_gib"])))
        p["critical_free_gib"] = min(p["min_free_gib"], max(0.25, float(p["critical_free_gib"])))
        p["critical_pct"] = min(99.5, max(p["threshold_pct"], float(p["critical_pct"])))
        p["runway_hours"] = min(168.0, max(0.0, float(p["runway_hours"])))
    except (TypeError, ValueError):
        return dict(DEFAULT_POLICY)
    return p


# ─── Pressure evaluation ─────────────────────────────────────────────────────

@dataclass
class FsSample:
    mount: str
    device: int
    total_bytes: int      # f_blocks
    used_bytes: int       # f_blocks - f_bfree
    avail_bytes: int      # f_bavail (what non-root processes can still write)

    @property
    def capacity(self) -> int:
        # df semantics: blocks reserved for root are neither used nor available.
        return self.used_bytes + self.avail_bytes

    @property
    def reserved_bytes(self) -> int:
        return max(0, self.total_bytes - self.capacity)

    @property
    def usage_pct(self) -> float:
        return round(self.used_bytes / self.capacity * 100, 2) if self.capacity else 0.0


def dedupe_filesystems(samples: Iterable[FsSample]) -> list[FsSample]:
    seen: dict[int, FsSample] = {}
    for s in samples:
        if s.device not in seen:
            seen[s.device] = s
        else:  # shared mount: keep both names for display
            seen[s.device].mount = f"{seen[s.device].mount} {s.mount}".strip()
    return list(seen.values())


def growth_rate(samples: list[tuple[float, int]], window_s: float = 900) -> Optional[float]:
    """Bytes/second of used-space growth over the trailing window (None if too short)."""
    if len(samples) < 2:
        return None
    end_t, end_used = samples[-1]
    start = next(((t, u) for t, u in samples if t >= end_t - window_s), samples[0])
    span = end_t - start[0]
    if span < window_s * 0.6:
        return None
    return (end_used - start[1]) / span


@dataclass
class Pressure:
    state: str                    # ok | growth | pressure | critical
    need_bytes: int = 0           # still to schedule, after pending reclamation
    tiers: tuple[str, ...] = ()   # which candidate tiers may be used
    reasons: list[str] = field(default_factory=list)
    runway_hours: Optional[float] = None
    rate_bytes_per_hour: Optional[float] = None


def evaluate_pressure(fs: FsSample, policy: dict, pending_bytes: int,
                      rate_bps: Optional[float]) -> Pressure:
    """Decide what, if anything, the watchdog must free on one filesystem."""
    cap = fs.capacity
    used = max(0, fs.used_bytes - pending_bytes)
    avail = fs.avail_bytes + pending_bytes
    pct = used / cap * 100 if cap else 0.0
    min_free = int(policy["min_free_gib"] * GIB)
    critical_free = int(policy["critical_free_gib"] * GIB)
    reasons: list[str] = []
    runway = None
    if rate_bps and rate_bps > 0:
        runway = avail / rate_bps / 3600      # includes space about to return
    rate_h = rate_bps * 3600 if rate_bps is not None else None

    need = 0
    if pct >= policy["threshold_pct"]:
        need = max(need, int(used - cap * policy["target_pct"] / 100))
        reasons.append(f"usage {pct:.1f}% ≥ {policy['threshold_pct']:.0f}%")
    if avail < min_free:
        # Recover with margin so the trigger does not flap at the boundary.
        need = max(need, int(min_free * 1.5) - avail)
        reasons.append(f"free {avail / GIB:.1f} GiB < {policy['min_free_gib']:.1f} GiB")

    critical = (fs.avail_bytes < critical_free or fs.usage_pct >= policy["critical_pct"])
    if need > 0:
        state = "critical" if critical else "pressure"
        tiers = ("legacy", "diagnostic", "diagnostic_truncate", "application")
        return Pressure(state, need, tiers, reasons, runway, rate_h)
    if critical:
        # Pending reclamation should bring relief; keep ingestion protected
        # until it shows up on disk.
        return Pressure("critical", 0, (), ["free space critically low; reclamation pending"],
                        runway, rate_h)
    if runway is not None and policy["runway_hours"] and runway < policy["runway_hours"]:
        # Fast growth below the thresholds: shed diagnostics only, never
        # monitoring history.
        reasons.append(f"projected full in {runway:.1f} h")
        need = max(0, int(rate_bps * policy["runway_hours"] * 3600) - pending_bytes)
        return Pressure("growth", need, ("legacy", "diagnostic"), reasons, runway, rate_h)
    return Pressure("ok", 0, (), [], runway, rate_h)


# ─── Candidates and planning ─────────────────────────────────────────────────

@dataclass
class Candidate:
    tier: str                 # legacy | diagnostic | diagnostic_truncate | application
    database: str
    table: str
    partition_id: Optional[str]
    bytes: int
    newest: Optional[date]
    priority: int = 0

    @property
    def pressure_eligible(self) -> bool:
        # Legacy error/crash/session/backup copies may expire by age, but
        # must retain their incident history during emergency reclamation.
        kind = diagnostic_log(self.table) if self.database == "system" else None
        return not kind or kind[0] not in PROTECTED_DIAGNOSTIC_LOGS

    @property
    def action(self) -> str:
        if self.tier == "legacy":
            return "drop_table"
        if self.tier == "diagnostic_truncate":
            return "truncate_table"
        return "drop_partition"

    def sql(self) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.database + self.table):
            raise ValueError(f"unsafe identifier {self.database}.{self.table}")
        name = f"{self.database}.{self.table}"
        if self.action == "drop_table":
            return f"DROP TABLE IF EXISTS {name} SYNC"
        if self.action == "truncate_table":
            return f"TRUNCATE TABLE IF EXISTS {name} SYNC"
        if not self.partition_id or not re.fullmatch(r"[A-Za-z0-9_-]+", self.partition_id):
            raise ValueError(f"unsafe partition id {self.partition_id!r}")
        return f"ALTER TABLE {name} DROP PARTITION ID '{self.partition_id}'"

    @property
    def reclaims_immediately(self) -> bool:
        return self.action in ("drop_table", "truncate_table")


TIER_ORDER = {"legacy": 0, "diagnostic": 1, "diagnostic_truncate": 2, "application": 3}


def build_candidates(partitions: list[dict], today: date, policy: dict) -> list[Candidate]:
    """Candidates from active-partition rows.

    Each row: database, table, partition_id, bytes, newest (date or None).
    """
    keep_cutoff = today - timedelta(days=int(policy["min_keep_days"]))
    out: list[Candidate] = []
    legacy_bytes: dict[str, int] = {}
    legacy_newest: dict[str, Optional[date]] = {}
    diag_tables: dict[str, int] = {}
    for p in partitions:
        db, table = p["database"], p["table"]
        size = int(p.get("bytes") or 0)
        newest = p.get("newest")
        if db == "system":
            kind = diagnostic_log(table)
            if not kind:
                continue
            base, legacy = kind
            if diagnostic_kind(table) == "evidence":
                continue
            if legacy:
                legacy_bytes[table] = legacy_bytes.get(table, 0) + size
                legacy_newest[table] = max(filter(None, [legacy_newest.get(table), newest]), default=None)
                continue
            if base in PROTECTED_DIAGNOSTIC_LOGS:
                continue
            diag_tables[table] = diag_tables.get(table, 0) + size
            # Never today's partition: it is being written to right now.
            if newest is not None and newest < today:
                out.append(Candidate("diagnostic", db, table, p["partition_id"], size, newest))
        elif db == CH_DATABASE:
            category = classify_table(table)
            if category is None or newest is None or newest >= keep_cutoff:
                continue
            out.append(Candidate("application", db, table, p["partition_id"], size, newest,
                                 CATEGORY_PURGE_PRIORITY.index(category)
                                 if category in CATEGORY_PURGE_PRIORITY else 99))
    for table, size in legacy_bytes.items():
        out.append(Candidate("legacy", "system", table, None, size, legacy_newest.get(table)))
    for table, size in diag_tables.items():
        out.append(Candidate("diagnostic_truncate", "system", table, None, size, None))

    def key(c: Candidate):
        if c.tier == "application":
            # Least valuable category first (raw flows before long-range
            # rollups), oldest first within it.
            return (TIER_ORDER[c.tier], c.priority, c.newest or date.min, -c.bytes)
        if c.tier == "diagnostic_truncate":
            return (TIER_ORDER[c.tier], date.min, 0, -c.bytes)   # largest first
        return (TIER_ORDER[c.tier], c.newest or date.min, 0, -c.bytes)
    return sorted(out, key=key)


def legacy_expired(candidates: list[Candidate], today: date) -> list[Candidate]:
    """Legacy diagnostic copies whose newest data is past their log's retention.

    Dropped routinely (no pressure needed): the live table already holds
    everything within retention.
    """
    out = []
    for c in candidates:
        if c.tier != "legacy":
            continue
        base = (diagnostic_log(c.table) or (c.table, True))[0]
        # An unknown age is not proof that protected incident history expired.
        if base in PROTECTED_DIAGNOSTIC_LOGS and c.newest is None:
            continue
        if c.newest is None or c.newest < today - timedelta(days=diagnostic_retention_days(base)):
            out.append(c)
    return out


# Below this usage, monitoring history is only deleted when doing so can reach
# the target. On a volume shared with the OS, other files often fill the disk;
# deleting history there loses data without relieving it. At or above it a
# full disk is imminent and any space helps keep ingestion alive.
APPLICATION_LAST_RESORT_PCT = 95.0


def application_allowed(candidates: list[Candidate], pressure: Pressure, usage_pct: float) -> bool:
    if "application" not in pressure.tiers:
        return False
    if pressure.state == "critical" or usage_pct >= APPLICATION_LAST_RESORT_PCT:
        return True
    # A table's truncate candidate already covers its dated partitions.
    reclaimable = sum(c.bytes for c in candidates
                      if c.pressure_eligible and c.tier in ("legacy", "diagnostic_truncate", "application"))
    return reclaimable >= pressure.need_bytes


def plan_pass(candidates: list[Candidate], pressure: Pressure,
              max_actions: int = MAX_ACTIONS_PER_PASS,
              allow_application: bool = True) -> list[Candidate]:
    """Smallest prefix of allowed candidates that covers the need, bounded."""
    if pressure.need_bytes <= 0:
        return []
    plan, scheduled = [], 0
    for c in candidates:
        if not c.pressure_eligible or c.tier not in pressure.tiers or c.bytes <= 0:
            continue
        if c.tier == "application" and not allow_application:
            continue
        if c.tier == "diagnostic_truncate":
            # Only what earlier partition drops of the same table left behind.
            already = sum(p.bytes for p in plan if p.database == c.database and p.table == c.table)
            remaining = c.bytes - already
            if remaining <= 0:
                continue
            c = Candidate(c.tier, c.database, c.table, None, remaining, c.newest, c.priority)
        plan.append(c)
        scheduled += c.bytes
        if scheduled >= pressure.need_bytes or len(plan) >= max_actions:
            break
    return plan


def pending_bytes(pending: list[dict], now: float) -> int:
    """Scheduled-but-unreclaimed bytes still inside their reclamation window."""
    return sum(int(p["bytes"]) for p in pending
               if now - float(p["at"]) < PENDING_RECLAIM_WINDOW_S)


def newest_date(max_date: Any, max_time: Any) -> Optional[date]:
    """Newest data day of a partition from system.parts max_date / max_time."""
    best: Optional[date] = None
    for v in (max_date, max_time):
        if isinstance(v, datetime):
            v = v.date()
        if isinstance(v, date) and v.year > 2000:
            best = v if best is None or v > best else best
    return best
