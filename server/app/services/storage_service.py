"""Storage management: usage overview, retention/purge, backup/restore.

Backs the Settings -> Storage tab. Three concerns live here:

1. Usage + retention. ClickHouse tables are classified into categories
   (raw metrics, 5-minute rollups, hourly rollups, flows, APM, events) and
   retention is enforced through table TTLs (``ALTER TABLE .. MODIFY TTL``).
   Immediate space reclamation — manual or the emergency auto-purge — drops
   whole partitions via ``DROP PARTITION ID``, oldest first.

2. Auto-purge. ``storage_sweeper_loop`` watches the /data volume; above the
   configured threshold it drops the oldest purgeable partitions (never newer
   than the configured minimum keep window) until usage falls back under the
   target. Every action is recorded in ``storage_events``.

3. Backup/restore. A backup is a directory under /data/backups/appliance/<id>
   holding a pg_dump custom-format archive, the ClickHouse schema, a config
   tarball and a manifest; optionally plus a native ClickHouse ``BACKUP
   DATABASE`` snapshot on the dedicated 'backups' disk. Restore replays any
   subset of those components. Privileged steps (pg_dump/pg_restore as the
   postgres user, service restarts) go through the root-owned
   /usr/local/sbin/zenplus-storage-helper, granted via sudoers.

The sweeper uses a Postgres advisory lock so it is safe with multiple
Uvicorn workers.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_ch_client
from app.services import storage_policy, storage_watchdog
from app.services.storage_layout import fs_usage, shares_filesystem
from app.services.storage_policy import (  # noqa: F401  (re-exported)
    CATEGORIES, EXCLUDED_PREFIXES, EXCLUDED_TABLES, STATE_TABLES, TABLE_CATEGORIES,
    classify_table,
)

logger = logging.getLogger("zenplus.storage")

# ─── Constants ───────────────────────────────────────────────────────────────

DATA_MOUNT = "/data"
CH_DATABASE = "zenplus"
BACKUP_ROOT = "/data/backups/appliance"
CH_BACKUP_HOST_ROOT = "/data/backups/clickhouse"   # host view of the CH 'backups' disk
STORAGE_HELPER = "/usr/local/sbin/zenplus-storage-helper"

RETENTION_SETTINGS_KEY = "storage.retention"
BACKUP_SCHEDULE_KEY = "storage.backup_schedule"

STORAGE_SWEEP_ADVISORY_LOCK = 1515074390

SWEEP_INTERVAL_S = 300
BACKUP_STALE_AFTER_H = 3

DEFAULT_AUTO_PURGE = {
    "enabled": True,
    "threshold_pct": 85,
    "target_pct": 75,
    "min_keep_days": 7,
    "min_free_gib": 5.0,
}

DEFAULT_BACKUP_SCHEDULE = {
    "enabled": False,
    "frequency": "weekly",      # daily | weekly
    "weekday": 6,               # 0=Mon .. 6=Sun (used for weekly)
    "hour_utc": 2,
    "include_clickhouse": False,
    "keep_last": 5,
}


# ─── Small helpers ───────────────────────────────────────────────────────────

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _mount_usage(path: str) -> dict[str, Any]:
    usage = fs_usage(path)
    if usage is None:
        raise OSError(f"cannot stat {path}")
    return {"mount": path, **usage}


def _run_helper(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run the root storage helper via sudo (non-interactive)."""
    return subprocess.run(
        ["sudo", "-n", STORAGE_HELPER, *args],
        capture_output=True, text=True, timeout=timeout,
    )


async def write_storage_event(
    db: AsyncSession,
    event_type: str,
    *,
    actor: str = "system",
    freed_bytes: int = 0,
    details: Optional[dict] = None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO storage_events (event_type, actor, freed_bytes, details) "
            "VALUES (:t, :a, :f, CAST(:d AS jsonb))"
        ),
        {"t": event_type, "a": actor, "f": freed_bytes, "d": json.dumps(details or {})},
    )


# ─── Settings ────────────────────────────────────────────────────────────────

async def _get_setting(db: AsyncSession, key: str) -> Optional[dict]:
    row = (
        await db.execute(
            text("SELECT value FROM system_settings WHERE key = :key"), {"key": key}
        )
    ).first()
    return row[0] if row else None


async def _upsert_setting(db: AsyncSession, key: str, value: dict) -> None:
    await db.execute(
        text(
            "INSERT INTO system_settings (key, value) "
            "VALUES (:key, CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb), "
            "updated_at = NOW()"
        ),
        {"key": key, "value": json.dumps(value)},
    )


async def get_retention_settings(db: AsyncSession) -> dict:
    raw = await _get_setting(db, RETENTION_SETTINGS_KEY) or {}
    auto = {**DEFAULT_AUTO_PURGE, **(raw.get("auto_purge") or {})}
    return {
        "categories": raw.get("categories") or {},
        "table_overrides": raw.get("table_overrides") or {},
        "auto_purge": auto,
    }


async def get_backup_schedule(db: AsyncSession) -> dict:
    raw = await _get_setting(db, BACKUP_SCHEDULE_KEY) or {}
    return {**DEFAULT_BACKUP_SCHEDULE, **raw}


# ─── ClickHouse inventory ────────────────────────────────────────────────────

_TTL_RE = [
    re.compile(r"TTL\s+toDateTime\(\w+\)\s*\+\s*toIntervalDay\((\d+)\)"),
    re.compile(r"TTL\s+\w+\s*\+\s*toIntervalDay\((\d+)\)"),
    re.compile(r"TTL\s+toDateTime\(\w+\)\s*\+\s*INTERVAL\s+(\d+)\s+DAY", re.I),
    re.compile(r"TTL\s+\w+\s*\+\s*INTERVAL\s+(\d+)\s+DAY", re.I),
]


def _parse_ttl_days(engine_full: str) -> Optional[int]:
    for rx in _TTL_RE:
        m = rx.search(engine_full or "")
        if m:
            return int(m.group(1))
    return None


def _ch_inventory_sync() -> list[dict]:
    """Managed-table inventory: size, rows, span, TTL, partition granularity."""
    client = get_ch_client()
    tables = client.query(
        "SELECT name, engine, engine_full, partition_key "
        "FROM system.tables WHERE database = %(db)s",
        parameters={"db": CH_DATABASE},
    ).result_rows
    parts = client.query(
        "SELECT table, sum(bytes_on_disk), sum(rows), min(min_time), max(max_time) "
        "FROM system.parts WHERE active AND database = %(db)s GROUP BY table",
        parameters={"db": CH_DATABASE},
    ).result_rows
    stats = {r[0]: r[1:] for r in parts}

    ttl_columns = client.query(
        "SELECT table, groupArray(name) FROM system.columns "
        "WHERE database = %(db)s AND type LIKE 'DateTime%%' GROUP BY table",
        parameters={"db": CH_DATABASE},
    ).result_rows
    time_cols = {r[0]: r[1] for r in ttl_columns}

    out = []
    for name, engine, engine_full, partition_key in tables:
        category = classify_table(name)
        if category is None or "MergeTree" not in engine:
            continue
        cols = time_cols.get(name) or []
        # Prefer the canonical event-time column over ingest-time columns.
        time_col = next(
            (c for c in ("timestamp", "observed_at", "seen_at") if c in cols),
            cols[0] if cols else None,
        )
        if not time_col:
            continue
        size, rows, min_t, max_t = stats.get(name, (0, 0, None, None))
        granularity = (
            "daily" if "toYYYYMMDD" in (partition_key or "")
            else "monthly" if "toYYYYMM" in (partition_key or "")
            else "none"
        )
        out.append({
            "table": name,
            "category": category,
            "engine": engine,
            "time_column": time_col,
            "partition_granularity": granularity,
            "size_bytes": int(size or 0),
            "size_human": human_size(int(size or 0)),
            "rows": int(rows or 0),
            "oldest": min_t.isoformat() if isinstance(min_t, datetime) else None,
            "newest": max_t.isoformat() if isinstance(max_t, datetime) else None,
            "current_ttl_days": _parse_ttl_days(engine_full),
        })
    out.sort(key=lambda t: -t["size_bytes"])
    return out


async def ch_inventory() -> list[dict]:
    return await asyncio.to_thread(_ch_inventory_sync)


def _apply_ttl_sync(table: str, time_col: str, days: int) -> None:
    client = get_ch_client()
    client.command(
        f"ALTER TABLE {CH_DATABASE}.{table} "
        f"MODIFY TTL toDateTime({time_col}) + toIntervalDay({int(days)})"
    )


async def apply_retention(db: AsyncSession, settings: dict, actor: str) -> list[dict]:
    """Apply configured retention as ClickHouse TTLs. Returns changes made."""
    inventory = await ch_inventory()
    categories = settings.get("categories") or {}
    overrides = settings.get("table_overrides") or {}
    changes = []
    for t in inventory:
        days = overrides.get(t["table"], categories.get(t["category"]))
        if not days:
            continue
        days = int(days)
        min_days = CATEGORIES[t["category"]]["min_days"]
        if days < min_days:
            days = min_days
        if t["current_ttl_days"] == days:
            continue
        try:
            await asyncio.to_thread(_apply_ttl_sync, t["table"], t["time_column"], days)
            changes.append({"table": t["table"], "from": t["current_ttl_days"], "to": days})
        except Exception as e:
            logger.exception("MODIFY TTL failed for %s", t["table"])
            changes.append({"table": t["table"], "error": str(e)})
    if changes:
        await write_storage_event(
            db, "retention_applied", actor=actor, details={"changes": changes}
        )
    return changes


# ─── Purge (partition drops) ─────────────────────────────────────────────────

def _list_purgeable_partitions_sync(
    tables: list[str], older_than: datetime
) -> list[dict]:
    """Partitions whose *entire* content is older than the cutoff."""
    if not tables:
        return []
    client = get_ch_client()
    rows = client.query(
        "SELECT table, partition_id, sum(bytes_on_disk), sum(rows), "
        "       min(min_time), max(max_time) "
        "FROM system.parts "
        "WHERE active AND database = %(db)s AND table IN %(tables)s "
        "GROUP BY table, partition_id "
        "HAVING max(max_time) < %(cutoff)s "
        "ORDER BY max(max_time) ASC",
        parameters={"db": CH_DATABASE, "tables": tables, "cutoff": older_than},
    ).result_rows
    return [
        {
            "table": r[0],
            "partition_id": r[1],
            "size_bytes": int(r[2]),
            "size_human": human_size(int(r[2])),
            "rows": int(r[3]),
            "oldest": r[4].isoformat() if isinstance(r[4], datetime) else None,
            "newest": r[5].isoformat() if isinstance(r[5], datetime) else None,
        }
        for r in rows
    ]


def _drop_partition_sync(table: str, partition_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", partition_id):
        raise ValueError(f"unsafe partition id: {partition_id!r}")
    client = get_ch_client()
    client.command(f"ALTER TABLE {CH_DATABASE}.{table} DROP PARTITION ID '{partition_id}'")


def _delete_older_than_sync(table: str, time_col: str, older_than: datetime) -> None:
    """Row-precise purge for data newer than any full partition (background mutation)."""
    client = get_ch_client()
    client.command(
        f"ALTER TABLE {CH_DATABASE}.{table} DELETE "
        f"WHERE toDateTime({time_col}) < %(cutoff)s",
        parameters={"cutoff": older_than},
    )


async def plan_purge(
    tables: list[str], older_than_days: int
) -> list[dict]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=older_than_days)
    return await asyncio.to_thread(_list_purgeable_partitions_sync, tables, cutoff)


async def execute_purge(
    db: AsyncSession,
    tables_meta: dict[str, dict],
    tables: list[str],
    older_than_days: int,
    *,
    mode: str = "partitions",
    actor: str = "system",
    event_type: str = "manual_purge",
) -> dict:
    """Drop all fully-expired partitions; optionally follow with a precise
    row-level DELETE mutation (``mode='precise'``)."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=older_than_days)
    plan = await asyncio.to_thread(_list_purgeable_partitions_sync, tables, cutoff)
    dropped, freed, errors = [], 0, []
    for p in plan:
        try:
            await asyncio.to_thread(_drop_partition_sync, p["table"], p["partition_id"])
            dropped.append({"table": p["table"], "partition_id": p["partition_id"],
                            "size_bytes": p["size_bytes"]})
            freed += p["size_bytes"]
        except Exception as e:
            logger.exception("DROP PARTITION failed: %s %s", p["table"], p["partition_id"])
            errors.append(f"{p['table']}/{p['partition_id']}: {e}")
    mutations = []
    if mode == "precise":
        for table in tables:
            meta = tables_meta.get(table)
            if not meta:
                continue
            try:
                await asyncio.to_thread(
                    _delete_older_than_sync, table, meta["time_column"], cutoff
                )
                mutations.append(table)
            except Exception as e:
                logger.exception("ALTER DELETE failed: %s", table)
                errors.append(f"{table}: {e}")
    result = {
        "dropped_partitions": dropped,
        # Partition bytes scheduled for deletion. ClickHouse removes dropped
        # parts in the background (about 10 minutes); this is not yet free space.
        "scheduled_bytes": freed,
        "freed_bytes": freed,
        "freed_human": human_size(freed),
        "delete_mutations": mutations,
        "errors": errors,
    }
    await write_storage_event(
        db, event_type, actor=actor, freed_bytes=freed,
        details={"older_than_days": older_than_days, "mode": mode, **result},
    )
    return result


# ─── Free-space watchdog integration ─────────────────────────────────────────
# Pressure-based purging lives in the independent watchdog service
# (storage_watchdog), which keeps working when the API or PostgreSQL is down.
# The API gives it the last valid policy, imports its audit trail into
# storage_events, and runs the same tick itself (under the same lock) on
# appliances where the service is not installed yet.

WATCHDOG_STALE_S = 180


def watchdog_policy(retention: dict) -> dict:
    auto = retention.get("auto_purge") or {}
    return storage_policy.normalize_policy({
        "enabled": auto.get("enabled"),
        "threshold_pct": auto.get("threshold_pct"),
        "target_pct": auto.get("target_pct"),
        "min_keep_days": auto.get("min_keep_days"),
        "min_free_gib": auto.get("min_free_gib"),
    })


def write_watchdog_policy(retention: dict) -> bool:
    try:
        os.makedirs(storage_watchdog.STATE_DIR, exist_ok=True)
        storage_watchdog._atomic_write("policy.json", {
            "policy": watchdog_policy(retention),
            "written_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except OSError:
        logger.warning("cannot write the storage watchdog policy to %s", storage_watchdog.STATE_DIR)
        return False


def watchdog_status() -> dict:
    """Watchdog state for the Storage page (bulky sample series trimmed)."""
    state = storage_watchdog._read_json("state.json")
    if not state:
        return {"installed": False, "alive": False}
    age = datetime.now(timezone.utc).timestamp() - float(state.get("heartbeat") or 0)
    trimmed = {k: v for k, v in state.items()
               if k not in ("samples", "table_snapshots", "pending", "open_passes", "last_event_at")}
    return {**trimmed, "installed": True, "alive": age < WATCHDOG_STALE_S,
            "heartbeat_age_s": int(age)}


def _fallback_watchdog_tick() -> Optional[dict]:
    return storage_watchdog.run_tick_once(get_ch_client)


async def import_watchdog_audit(db: AsyncSession, limit: int = 200) -> int:
    """Copy new watchdog audit records into storage_events (idempotent)."""
    records = [r for r in storage_watchdog.read_audit(limit) if r.get("id") and r.get("type")]
    if not records:
        return 0
    ids = [r["id"] for r in records]
    existing = {
        row[0] for row in (await db.execute(
            text("SELECT details->>'watchdog_id' FROM storage_events "
                 "WHERE details->>'watchdog_id' = ANY(:ids)"), {"ids": ids})).all()
    }
    added = 0
    for r in records:
        if r["id"] in existing:
            continue
        details = dict(r.get("details") or {})
        measured = details.get("measured_free_change_bytes")
        freed = int(measured) if r["type"] == "watchdog_reclaim" and measured and measured > 0 else 0
        try:
            created = datetime.fromisoformat(r["at"])
        except (KeyError, ValueError):
            created = datetime.now(timezone.utc)
        await db.execute(
            text("INSERT INTO storage_events (created_at, event_type, actor, freed_bytes, details) "
                 "VALUES (:c, :t, 'watchdog', :f, CAST(:d AS jsonb))"),
            {"c": created, "t": r["type"], "f": freed,
             "d": json.dumps({**details, "watchdog_id": r["id"]}, default=str)},
        )
        added += 1
    return added


# ─── OS disk overview & cleanup ──────────────────────────────────────────────

def _os_usage_extras() -> dict:
    fields = ("journal_bytes", "apt_cache_bytes", "var_log_bytes")
    try:
        r = _run_helper("os-usage", timeout=60)
        if r.returncode != 0:
            raise ValueError("storage helper failed")
        measured = json.loads(r.stdout.strip())
        if not isinstance(measured, dict) or any(
            type(measured.get(k)) is not int or measured[k] < 0 for k in fields
        ):
            raise ValueError("invalid storage helper measurement")
        return {**{k: measured[k] for k in fields}, "os_usage_error": None}
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning("System log and package cache measurement unavailable")
        return {**{k: None for k in fields}, "os_usage_error":
                "System log and package cache sizes are unavailable. Refresh or check the storage helper."}


async def os_overview() -> dict:
    mounts = []
    shared = shares_filesystem("/", DATA_MOUNT)
    if shared:
        targets = [("/", "System & data volume", ["system", "data"])]
    else:
        targets = [("/", "System volume", ["system"]), (DATA_MOUNT, "Data volume", ["data"])]
    for path, label, roles in targets:
        try:
            u = _mount_usage(path)
        except OSError:
            continue
        mounts.append({
            **u, "label": label, "roles": roles,
            # Blocks reserved for root: neither used nor available to the app.
            "reserved_bytes": max(0, u["total_bytes"] - u["used_bytes"] - u["free_bytes"]),
        })
    extras = await asyncio.to_thread(_os_usage_extras)
    return {"mounts": mounts, "data_shares_os": shared,
            "sampled_at": datetime.now(timezone.utc).isoformat(), **extras}


def _pg_data_path() -> str:
    return f"{DATA_MOUNT}/postgresql" if os.path.isdir(f"{DATA_MOUNT}/postgresql") \
        else "/var/lib/postgresql"


def _mount_of(path: str) -> str:
    return DATA_MOUNT if shares_filesystem(path, DATA_MOUNT) else "/"


def _clickhouse_breakdown_sync() -> dict:
    return storage_watchdog.ch_breakdown(get_ch_client())


async def clickhouse_breakdown() -> dict:
    """Application vs diagnostic, active vs inactive, detached — plus top tables.

    Uses the watchdog's recent sample when fresh (it also tracks growth), and
    queries ClickHouse directly otherwise.
    """
    state = storage_watchdog._read_json("state.json") or {}
    bd = state.get("breakdown")
    if bd and datetime.now(timezone.utc).timestamp() - float(bd.get("at") or 0) < 600:
        return {**bd, "source": "watchdog"}
    raw = await asyncio.to_thread(_clickhouse_breakdown_sync)
    top = storage_watchdog.top_tables(raw)
    return {"at": datetime.now(timezone.utc).timestamp(),
            **storage_watchdog.summarize_breakdown(raw), "top_tables": top, "growth": [],
            "source": "live"}


def _diagnostic_logs_sync() -> list[dict]:
    """Every ``system`` log table with its kind, retention, TTL and size.

    Sizes come from system.parts for all tables (active and inactive parts),
    not from the top-25 breakdown, keyed by the table's own name.
    """
    client = get_ch_client()
    stats = {r[0]: (int(r[1] or 0), storage_policy.newest_date(r[2], r[3])) for r in client.query(
        "SELECT table, sum(bytes_on_disk), maxIf(max_date, active), maxIf(max_time, active) FROM system.parts "
        "WHERE database = 'system' GROUP BY table"
    ).result_rows}
    rows = client.query(
        "SELECT name, engine_full FROM system.tables WHERE database = 'system' "
        "AND engine LIKE '%MergeTree%'"
    ).result_rows
    out = []
    for name, engine_full in rows:
        kind = storage_policy.diagnostic_kind(name)
        if not kind:
            continue
        base = storage_policy.diagnostic_log(name)[0]
        out.append({
            "table": name,
            "log": base,
            "kind": kind,                      # current | legacy | evidence
            "legacy": kind == "legacy",
            "evidence": kind == "evidence",
            "disabled": name in storage_policy.DISABLED_DIAGNOSTIC_LOGS,
            "protected": base in storage_policy.PROTECTED_DIAGNOSTIC_LOGS,
            "retention_days": storage_policy.diagnostic_retention_days(base),
            "ttl_active": " TTL " in f" {engine_full or ''} ",
            "bytes": stats.get(name, (0, None))[0],
            "expires_after": ((stats[name][1] + timedelta(
                days=storage_policy.diagnostic_retention_days(base) + 1)).isoformat()
                if kind == "legacy" and name in stats and stats[name][1] is not None else None),
        })
    return sorted(out, key=lambda r: -r["bytes"])


async def diagnostic_logs() -> list[dict]:
    return await asyncio.to_thread(_diagnostic_logs_sync)


async def space_consumers(db: AsyncSession, ch: Optional[dict], extras: dict) -> list[dict]:
    """Known consumers per mount; the dashboard shows the rest as unclassified."""
    try:
        pg_bytes = int((await db.execute(
            text("SELECT pg_database_size(current_database())"))).scalar() or 0)
    except Exception:
        pg_bytes = 0
    # Sizes recorded when each backup finished; walking a multi-GB native
    # ClickHouse backup on every poll would be far too slow.
    try:
        backup_bytes = int((await db.execute(text(
            "SELECT COALESCE(sum(size_bytes), 0) FROM storage_backups "
            "WHERE status = 'completed'"))).scalar() or 0)
    except Exception:
        backup_bytes = 0
    ch = ch or {}
    journal = int(extras.get("journal_bytes") or 0)
    var_log = int(extras.get("var_log_bytes") or 0)
    ch_path = f"{DATA_MOUNT}/clickhouse"
    rows = [
        ("clickhouse", "Monitoring data (ClickHouse)", ch.get("application_bytes", 0), ch_path,
         "Shorten retention or purge old data"),
        ("clickhouse_diagnostic", "ClickHouse diagnostic logs", ch.get("diagnostic_bytes", 0),
         ch_path, "Bounded by the diagnostic-log retention below"),
        ("clickhouse_inactive", "Merged parts awaiting removal", ch.get("inactive_bytes", 0),
         ch_path, "Removed automatically about 8 minutes after each merge"),
        ("clickhouse_detached", "Detached parts", ch.get("detached_bytes", 0), ch_path,
         "Left by recoveries; never removed automatically"),
        ("postgres", "Configuration database (PostgreSQL)", pg_bytes, _pg_data_path(), None),
        ("backups", "Backups", backup_bytes, f"{DATA_MOUNT}/backups", "Delete old backups"),
        ("journal", "System journal", journal, "/var/log/journal", "Vacuum the journal"),
        ("os_logs", "Other system logs (/var/log)", max(0, var_log - journal), "/var/log",
         "Rotated by size"),
        ("apt_cache", "Package cache", int(extras.get("apt_cache_bytes") or 0), "/var/cache/apt",
         "Clear the package cache"),
    ]
    return [
        {"id": cid, "label": label, "bytes": int(size or 0), "human": human_size(int(size or 0)),
         "mount": _mount_of(path), "hint": hint}
        for cid, label, size, path, hint in rows
        if cid not in ("journal", "os_logs", "apt_cache") or not extras.get("os_usage_error")
    ]


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.year > 2000 else None   # partition keys without time report 1970


def capacity_forecast(tables: list[dict], retention: dict, free_bytes: int) -> dict:
    """Estimate ingest per day and where metrics storage levels off.

    Daily footprint per table is its size over the span of data it holds;
    with a retention of N days a table levels off at roughly N days of
    footprint. Tables without any retention grow without bound.
    """
    categories = retention.get("categories") or {}
    overrides = retention.get("table_overrides") or {}
    per_category: dict[str, dict] = {}
    total_daily = growing_daily = 0.0
    remaining = 0.0
    unbounded: set[str] = set()
    for t in tables:
        oldest, newest = _parse_ts(t.get("oldest")), _parse_ts(t.get("newest"))
        cat = per_category.setdefault(t["category"], {
            "daily_bytes": 0.0, "projected_bytes": 0.0, "unbounded": False, "estimated": True,
        })
        if not oldest or not newest or t["size_bytes"] <= 0:
            cat["projected_bytes"] += t["size_bytes"]
            continue
        span_days = max(1.0, (newest - oldest).total_seconds() / 86400)
        daily = t["size_bytes"] / span_days
        days = overrides.get(t["table"]) or categories.get(t["category"]) or t.get("current_ttl_days")
        cat["daily_bytes"] += daily
        total_daily += daily
        if not days:
            cat["unbounded"] = True
            unbounded.add(t["category"])
            cat["projected_bytes"] += t["size_bytes"]
            growing_daily += daily
            continue
        projected = daily * float(days)
        cat["projected_bytes"] += max(projected, t["size_bytes"])
        if projected > t["size_bytes"]:
            remaining += projected - t["size_bytes"]
            growing_daily += daily
    current = sum(t["size_bytes"] for t in tables)
    steady = sum(c["projected_bytes"] for c in per_category.values())
    fits = not unbounded and remaining <= free_bytes
    days_until_full = None
    if not fits and growing_daily > 0:
        days_until_full = int(free_bytes / growing_daily)
    return {
        "daily_bytes": int(total_daily),
        "current_bytes": int(current),
        "steady_state_bytes": int(steady),
        "growth_remaining_bytes": int(remaining),
        "free_bytes": int(free_bytes),
        "fits": fits,
        "days_until_full": days_until_full,
        "unbounded_categories": sorted(unbounded),
        "categories": {k: {**v, "daily_bytes": int(v["daily_bytes"]),
                           "projected_bytes": int(v["projected_bytes"])}
                       for k, v in per_category.items()},
    }


async def os_cleanup(db: AsyncSession, actions: list[str], journal_max_mb: int, actor: str) -> dict:
    before = _mount_usage("/")
    performed, errors = [], []
    if "vacuum_journal" in actions:
        r = await asyncio.to_thread(_run_helper, "vacuum-journal", str(int(journal_max_mb)))
        (performed if r.returncode == 0 else errors).append(
            "vacuum_journal" if r.returncode == 0 else f"vacuum_journal: {r.stderr.strip()}"
        )
    if "apt_clean" in actions:
        r = await asyncio.to_thread(_run_helper, "apt-clean")
        (performed if r.returncode == 0 else errors).append(
            "apt_clean" if r.returncode == 0 else f"apt_clean: {r.stderr.strip()}"
        )
    after = _mount_usage("/")
    freed = max(0, after["free_bytes"] - before["free_bytes"])
    result = {
        "performed": performed,
        "errors": errors,
        "freed_bytes": freed,
        "freed_human": human_size(freed),
        "root_usage_percent": after["usage_percent"],
    }
    await write_storage_event(db, "os_cleanup", actor=actor, freed_bytes=freed, details=result)
    return result


# ─── Backups ─────────────────────────────────────────────────────────────────

CONFIG_BACKUP_PATHS = [
    "/opt/zenplus/.env",
    "/opt/zenplus/poller/config.yaml",
    "/etc/nginx/sites-available/zenplus",
    "/etc/nginx/conf.d/zenplus.conf",
    "/etc/zenplus/tls/server.crt",
    "/etc/zenplus/tls/chain.crt",
    "/etc/zenplus/tls/state.json",
]
CONFIG_BACKUP_GLOBS = [
    "/etc/systemd/system/zenplus-*.service",
    "/etc/systemd/system/zenplus-*.timer",
]
# Only these are written back on a config restore; the rest of the tarball is
# reference material (root-owned paths are never auto-overwritten).
CONFIG_RESTORE_WHITELIST = {"opt/zenplus/.env", "opt/zenplus/poller/config.yaml"}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _app_version() -> str:
    try:
        with open("/opt/zenplus/.version", "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _ch_schema_sync() -> tuple[str, list[str]]:
    client = get_ch_client()
    rows = client.query(
        "SELECT name, create_table_query FROM system.tables "
        "WHERE database = %(db)s AND engine != 'MaterializedView' "
        "ORDER BY name",
        parameters={"db": CH_DATABASE},
    ).result_rows
    mvs = client.query(
        "SELECT name, create_table_query FROM system.tables "
        "WHERE database = %(db)s AND engine = 'MaterializedView' ORDER BY name",
        parameters={"db": CH_DATABASE},
    ).result_rows
    # Base tables first so MVs can attach to them on restore.
    statements, names = [], []
    for name, ddl in list(rows) + list(mvs):
        if ddl:
            statements.append(ddl.rstrip(";") + ";")
            names.append(name)
    return "\n\n".join(statements) + "\n", names


def _ch_backup_start_sync(backup_id: str) -> str:
    """Start a native ClickHouse backup; returns the CH-side backup UUID."""
    client = get_ch_client()
    res = client.query(
        f"BACKUP DATABASE {CH_DATABASE} "
        f"TO Disk('backups', 'appliance/{backup_id}') ASYNC"
    ).result_rows
    return str(res[0][0]) if res else ""


def _ch_operation_status_sync(op_id: str) -> tuple[str, str]:
    client = get_ch_client()
    rows = client.query(
        "SELECT status, error FROM system.backups WHERE id = %(id)s",
        parameters={"id": op_id},
    ).result_rows
    return (str(rows[0][0]), str(rows[0][1])) if rows else ("UNKNOWN", "")


async def _wait_ch_operation(op_id: str, *, timeout_s: int = 3600) -> None:
    """Poll system.backups until a BACKUP/RESTORE ASYNC operation finishes."""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_s)
    while datetime.now(timezone.utc) < deadline:
        status, error = await asyncio.to_thread(_ch_operation_status_sync, op_id)
        if status in ("BACKUP_CREATED", "RESTORED"):
            return
        if status.endswith("FAILED") or status == "UNKNOWN":
            raise RuntimeError(f"ClickHouse operation {op_id} failed: {error or status}")
        await asyncio.sleep(3)
    raise TimeoutError(f"ClickHouse operation {op_id} timed out")


def _write_config_tar(dest: str) -> list[str]:
    included = []
    paths = [p for p in CONFIG_BACKUP_PATHS if os.path.isfile(p)]
    for g in CONFIG_BACKUP_GLOBS:
        paths.extend(sorted(glob.glob(g)))
    with tarfile.open(dest, "w:gz") as tar:
        for p in paths:
            try:
                tar.add(p, arcname=p.lstrip("/"))
                included.append(p)
            except (OSError, PermissionError):
                pass
    return included


async def run_backup(backup_id: str, kind: str, include_clickhouse: bool, actor: str) -> None:
    """Execute a backup end-to-end, updating the storage_backups row.

    Runs as a fire-and-forget task; owns its DB sessions.
    """
    from app.core.database import AsyncSessionLocal

    backup_dir = os.path.join(BACKUP_ROOT, backup_id)
    ch_tables: list[str] = []
    try:
        os.makedirs(backup_dir, exist_ok=True)

        # 1. PostgreSQL (custom-format dump via the root helper, as postgres)
        pg_path = os.path.join(backup_dir, "postgres.dump")
        r = await asyncio.to_thread(_run_helper, "pg-dump", pg_path)
        if r.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {(r.stderr or r.stdout).strip()}")

        # 2. ClickHouse schema (always cheap, always included)
        schema_sql, ch_tables = await asyncio.to_thread(_ch_schema_sync)
        with open(os.path.join(backup_dir, "clickhouse_schema.sql"), "w",
                  encoding="utf-8") as f:
            f.write(schema_sql)

        # 3. Config tarball
        included = await asyncio.to_thread(
            _write_config_tar, os.path.join(backup_dir, "config.tar.gz")
        )

        # 4. Optional native ClickHouse data snapshot
        ch_backup_name = None
        if include_clickhouse:
            # Preflight: the snapshot is roughly the size of the live tables.
            ch_size = sum(t["size_bytes"] for t in await ch_inventory())
            free = _mount_usage(DATA_MOUNT)["free_bytes"]
            if free < ch_size * 1.15:
                raise RuntimeError(
                    f"Not enough free space for a full backup: metrics data is "
                    f"{human_size(ch_size)} but only {human_size(free)} is free on "
                    f"{DATA_MOUNT}. Purge old data or expand storage first."
                )
            op_id = await asyncio.to_thread(_ch_backup_start_sync, backup_id)
            await _wait_ch_operation(op_id)
            ch_backup_name = f"appliance/{backup_id}"

        manifest = {
            "id": backup_id,
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": actor,
            "app_version": _app_version(),
            "include_clickhouse": include_clickhouse,
            "clickhouse_backup_name": ch_backup_name,
            "clickhouse_tables": ch_tables,
            "components": {
                "postgres": "postgres.dump",
                "clickhouse_schema": "clickhouse_schema.sql",
                "config": "config.tar.gz",
            },
            "config_files": included,
        }
        with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        size = _dir_size(backup_dir)
        if ch_backup_name:
            size += _dir_size(os.path.join(CH_BACKUP_HOST_ROOT, "appliance", backup_id))

        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE storage_backups SET status='completed', size_bytes=:s, "
                    "path=:p, finished_at=NOW() WHERE id=:id"
                ),
                {"s": size, "p": backup_dir, "id": backup_id},
            )
            await write_storage_event(
                db, "backup_created", actor=actor,
                details={"backup_id": backup_id, "kind": kind,
                         "include_clickhouse": include_clickhouse,
                         "size_bytes": size},
            )
            await db.commit()
        logger.info("backup %s completed (%s)", backup_id, human_size(size))
    except Exception as e:
        logger.exception("backup %s failed", backup_id)
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE storage_backups SET status='failed', error=:e, "
                    "finished_at=NOW() WHERE id=:id"
                ),
                {"e": str(e)[:2000], "id": backup_id},
            )
            await write_storage_event(
                db, "backup_failed", actor=actor,
                details={"backup_id": backup_id, "error": str(e)[:500]},
            )
            await db.commit()


# ─── Backup trust validation ─────────────────────────────────────────────────
#
# An uploaded archive's manifest.json and clickhouse_schema.sql are attacker
# controlled (admin-only, but still a different trust level than our own
# backup writer). Table names are interpolated into DROP/RESTORE statements and
# the schema file is executed, so both are validated against exactly what
# ``run_backup`` produces before anything runs. Backups are not yet signed: an
# appliance HMAC over the manifest and component digests is the remaining
# step and is tracked separately.

class BackupValidationError(ValueError):
    """The backup's manifest/schema/archive is not one this appliance wrote."""


CH_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
BACKUP_ID_RE = re.compile(rf"^{_UUID_PATTERN}$")
# run_backup writes f"appliance/{backup_id}" (backup_id = str(uuid.uuid4())).
CH_BACKUP_NAME_RE = re.compile(rf"^appliance/({_UUID_PATTERN})$")
BACKUP_KIND_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
MAX_MANIFEST_TABLES = 5000

_IDENT = r"[A-Za-z_][A-Za-z0-9_]{0,127}"
_SCHEMA_HEAD_RE = re.compile(
    r"^(?i:CREATE)\s+((?i:TABLE|MATERIALIZED\s+VIEW|VIEW|DICTIONARY))\s+"
    r"(?:(?i:IF\s+NOT\s+EXISTS)\s+)?"
    rf"(?:`{CH_DATABASE}`|{CH_DATABASE})\s*\.\s*(?:`({_IDENT})`|({_IDENT}))(?=[\s(]|$)"
)
# MaterializedViews without TO own an implicit inner table. Its DDL may appear
# in a dump but recreating the view recreates it, so it is skipped, not run.
_SCHEMA_INNER_TABLE_RE = re.compile(
    r"^(?i:CREATE\s+TABLE)\s+(?:(?i:IF\s+NOT\s+EXISTS)\s+)?"
    rf"(?:`{CH_DATABASE}`|{CH_DATABASE})\s*\.\s*`\.inner(?:_id)?\.[A-Za-z0-9_.-]+`(?=[\s(])"
)
_ALLOWED_ENGINE_RE = re.compile(
    r"^(?:(?:Replicated)?(?:Replacing|Summing|Aggregating|Collapsing|"
    r"VersionedCollapsing|Graphite|Coalescing)?MergeTree|Memory|Log|TinyLog|"
    r"StripeLog|Null)$"
)
_ENGINE_RE = re.compile(r"\bENGINE\s*=\s*([A-Za-z0-9_]+)", re.IGNORECASE)
# Table functions / dynamic disks that reach files, the network or other
# servers. Aggregate combinators (sumMerge) do not match because of \b.
_FORBIDDEN_CALL_RE = re.compile(
    r"\b(?:url|urlCluster|file|fileCluster|s3|s3Cluster|gcs|oss|cosn|"
    r"azureBlobStorage|azureBlobStorageCluster|hdfs|hdfsCluster|remote|"
    r"remoteSecure|cluster|clusterAllReplicas|mysql|postgresql|mongodb|redis|"
    r"sqlite|odbc|jdbc|executable|executablePool|input|deltaLake|hudi|iceberg|"
    r"icebergS3|merge|dictionary|generateRandom|disk|loop)\s*\(",
    re.IGNORECASE,
)
# Any database-qualified reference in FROM/JOIN/TO/AS position must be ours.
_QUALIFIED_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|TO|AS)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\.\s*`?[A-Za-z_.]",
    re.IGNORECASE,
)


def split_clickhouse_schema(schema_sql: str) -> list[str]:
    """Split a schema dump exactly the way ``_ch_schema_sync`` joins it."""
    out = []
    for stmt in schema_sql.split(";\n"):
        stmt = stmt.strip().rstrip(";").strip()
        if stmt:
            out.append(stmt)
    return out


def validate_clickhouse_schema(schema_sql: str) -> list[tuple[str, str, str]]:
    """Return ``[(kind, name, statement)]`` or raise BackupValidationError.

    Only CREATE TABLE / MATERIALIZED VIEW / VIEW / DICTIONARY statements for
    the zenplus database are accepted, with MergeTree-family/Memory/Log/Null
    engines and no table functions, dynamic disks, foreign databases or
    stacked statements. The whole file is refused if any statement fails, so a
    restore never runs half of a hostile schema.
    """
    if not isinstance(schema_sql, str):
        raise BackupValidationError("ClickHouse schema is not text")
    accepted: list[tuple[str, str, str]] = []
    for index, stmt in enumerate(split_clickhouse_schema(schema_sql), start=1):
        if _SCHEMA_INNER_TABLE_RE.match(stmt):
            continue
        head = _SCHEMA_HEAD_RE.match(stmt)
        if not head:
            raise BackupValidationError(
                f"schema statement {index} is not a CREATE TABLE/VIEW/DICTIONARY "
                f"for database {CH_DATABASE}"
            )
        kind = re.sub(r"\s+", " ", head.group(1)).upper()
        name = head.group(2) or head.group(3)
        if ";" in stmt:
            raise BackupValidationError(f"schema statement {index} ({name}) has stacked statements")
        for engine in _ENGINE_RE.findall(stmt):
            if not _ALLOWED_ENGINE_RE.match(engine):
                raise BackupValidationError(
                    f"schema statement {index} ({name}) uses unsupported engine {engine}"
                )
        if kind == "TABLE" and not _ENGINE_RE.search(stmt):
            raise BackupValidationError(f"schema statement {index} ({name}) has no ENGINE")
        if _FORBIDDEN_CALL_RE.search(stmt):
            raise BackupValidationError(
                f"schema statement {index} ({name}) uses a table function or external disk"
            )
        for database in _QUALIFIED_REF_RE.findall(stmt):
            if database != CH_DATABASE:
                raise BackupValidationError(
                    f"schema statement {index} ({name}) references database {database}"
                )
        if kind == "DICTIONARY":
            if not re.search(r"\bSOURCE\s*\(\s*CLICKHOUSE\s*\(", stmt, re.IGNORECASE) or \
                    re.search(r"\b(?:HOST|PORT|USER|PASSWORD)\b", stmt, re.IGNORECASE):
                raise BackupValidationError(
                    f"schema statement {index} ({name}) has a non-local dictionary source"
                )
        accepted.append((kind, name, stmt))
    return accepted


def validate_backup_manifest(manifest: Any, backup_id: str) -> dict:
    """Check a manifest against what ``run_backup`` writes for ``backup_id``."""
    if not isinstance(manifest, dict):
        raise BackupValidationError("manifest is not a JSON object")
    mid = manifest.get("id")
    if mid is not None and str(mid) != backup_id:
        raise BackupValidationError("manifest id does not match the backup")
    kind = manifest.get("kind")
    if kind is not None and (not isinstance(kind, str) or not BACKUP_KIND_RE.match(kind)):
        raise BackupValidationError("manifest kind is invalid")
    ch_name = manifest.get("clickhouse_backup_name")
    if ch_name is not None:
        if not isinstance(ch_name, str) or not CH_BACKUP_NAME_RE.match(ch_name) \
                or ch_name != f"appliance/{backup_id}":
            raise BackupValidationError("manifest clickhouse_backup_name is invalid")
    tables = manifest.get("clickhouse_tables", [])
    if tables is None:
        tables = []
    if not isinstance(tables, list) or len(tables) > MAX_MANIFEST_TABLES:
        raise BackupValidationError("manifest clickhouse_tables is invalid")
    for table in tables:
        if not isinstance(table, str) or not CH_IDENTIFIER_RE.match(table):
            raise BackupValidationError(
                f"manifest lists an invalid ClickHouse table name: {str(table)[:80]!r}"
            )
    return manifest


def validate_uploaded_backup_id(value: Any) -> str:
    """The upload's manifest id must be a canonical lowercase UUID string."""
    text_id = str(value or "")
    if not BACKUP_ID_RE.match(text_id):
        raise BackupValidationError("manifest id is not a backup UUID")
    return text_id


def _check_archive_member(member: tarfile.TarInfo, prefix: str) -> None:
    name = member.name
    parts = name.split("/")
    if name.startswith("/") or "\\" in name or ".." in parts or "\x00" in name:
        raise BackupValidationError(f"unsafe path in archive: {name[:120]}")
    if member.issym():
        raise BackupValidationError(f"symbolic link in archive: {name[:120]}")
    if member.islnk():
        link = member.linkname
        if (link.startswith("/") or ".." in link.split("/") or
                not link.startswith(prefix + "/")):
            raise BackupValidationError(f"hard link leaves the backup: {name[:120]}")
    elif not (member.isreg() or member.isdir()):
        raise BackupValidationError(f"special file in archive: {name[:120]}")


def safe_extract_backup_members(
    tar: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    dest: str,
    *,
    prefix: str,
    rename_to: Optional[str] = None,
) -> int:
    """Extract only ``prefix/...`` members into ``dest``.

    Every member is validated first (no absolute/.. paths, symlinks, devices,
    FIFOs, or hard links outside the backup). Extraction then uses the
    ``data`` filter where the runtime provides it (Python 3.12, and 3.8.17+/
    3.10.12+ backports), which also re-checks the resolved path.
    """
    selected = [m for m in members if m.name == prefix or m.name.startswith(prefix + "/")]
    for m in selected:
        _check_archive_member(m, prefix)
    extracted = 0
    use_filter = hasattr(tarfile, "data_filter")
    for m in selected:
        if rename_to is not None:
            m.name = rename_to + m.name[len(prefix):]
            if m.islnk():
                m.linkname = rename_to + m.linkname[len(prefix):]
        if use_filter:
            tar.extract(m, dest, filter="data")
        else:
            target = os.path.realpath(os.path.join(dest, m.name))
            root = os.path.realpath(dest)
            if os.path.commonpath([root, target]) != root:
                raise BackupValidationError(f"unsafe path in archive: {m.name[:120]}")
            tar.extract(m, dest)
        extracted += 1
    return extracted


def _read_backup_manifest(backup_dir: str, backup_id: str) -> dict:
    with open(os.path.join(backup_dir, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return validate_backup_manifest(manifest, backup_id)


def _read_backup_schema(backup_dir: str) -> Optional[str]:
    path = os.path.join(backup_dir, "clickhouse_schema.sql")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def preflight_restore(backup_id: str, components: list[str]) -> dict:
    """Validate a backup's manifest (and schema when used) before a restore."""
    backup_dir = os.path.join(BACKUP_ROOT, backup_id)
    manifest = _read_backup_manifest(backup_dir, backup_id)
    if "clickhouse_schema" in components or "clickhouse_data" in components:
        schema_sql = _read_backup_schema(backup_dir)
        if schema_sql is not None:
            validate_clickhouse_schema(schema_sql)
        elif "clickhouse_schema" in components:
            raise BackupValidationError("backup has no clickhouse_schema.sql")
    return manifest


def _ch_existing_tables_sync() -> set[str]:
    client = get_ch_client()
    rows = client.query(
        "SELECT name FROM system.tables WHERE database = %(db)s",
        parameters={"db": CH_DATABASE},
    ).result_rows
    return {str(r[0]) for r in rows}


def _restorable_tables(manifest: dict, schema_sql: Optional[str]) -> list[str]:
    """Manifest tables, restricted to the backup's own schema or live tables."""
    tables = list(manifest.get("clickhouse_tables") or [])
    known: set[str] = set()
    if schema_sql is not None:
        known |= {name for _kind, name, _stmt in validate_clickhouse_schema(schema_sql)}
    unknown = [t for t in tables if t not in known]
    if unknown:
        known |= _ch_existing_tables_sync()
        unknown = [t for t in tables if t not in known]
    if unknown:
        raise BackupValidationError(
            "manifest lists ClickHouse tables that are neither in the backup schema "
            f"nor in database {CH_DATABASE}: {', '.join(sorted(unknown)[:10])}"
        )
    return tables


def _ch_restore_table_sync(table: str, backup_name: str) -> str:
    if not CH_IDENTIFIER_RE.match(table) or not CH_BACKUP_NAME_RE.match(backup_name):
        raise BackupValidationError("refusing an invalid ClickHouse restore target")
    client = get_ch_client()
    client.command(f"DROP TABLE IF EXISTS {CH_DATABASE}.{table} SYNC")
    res = client.query(
        f"RESTORE TABLE {CH_DATABASE}.{table} "
        f"FROM Disk('backups', '{backup_name}') ASYNC"
    ).result_rows
    return str(res[0][0]) if res else ""


def _ch_apply_schema_sync(schema_sql: str) -> int:
    """Create any missing tables from a schema dump (existing tables kept).

    The whole dump is validated first; nothing runs if any statement is not a
    plain CREATE for the zenplus database.
    """
    statements = validate_clickhouse_schema(schema_sql)
    client = get_ch_client()
    created = 0
    for _kind, _name, stmt in statements:
        stmt = re.sub(
            r"^CREATE\s+(TABLE|MATERIALIZED\s+VIEW|VIEW|DICTIONARY)\s+(?!IF\s+NOT\s+EXISTS\b)",
            lambda m: f"CREATE {m.group(1)} IF NOT EXISTS ", stmt, count=1,
            flags=re.IGNORECASE,
        )
        client.command(stmt)
        created += 1
    return created


def _extract_config_files(tar_path: str) -> list[str]:
    restored = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.lstrip("./")
            if name not in CONFIG_RESTORE_WHITELIST or not member.isfile():
                continue
            src = tar.extractfile(member)
            if src is None:
                continue
            dest = "/" + name
            # A hardened appliance keeps configuration root-owned. A restore
            # of its own backup need not rewrite files that are already equal.
            # Changed protected files still require an administrator to apply
            # them; the API must not gain arbitrary configuration write access.
            content = src.read()
            if os.path.isfile(dest):
                with open(dest, "rb") as current:
                    if current.read() == content:
                        restored.append(dest)
                        continue
            with open(dest, "wb") as out:
                out.write(content)
            restored.append(dest)
    return restored


async def run_restore(backup_id: str, components: list[str], actor: str) -> None:
    """Execute a restore, updating last_restore_* on the backup row."""
    from app.core.database import AsyncSessionLocal

    backup_dir = os.path.join(BACKUP_ROOT, backup_id)
    try:
        # Validate the manifest (and the schema when ClickHouse is involved)
        # before any component is touched, so a hostile upload is refused
        # rather than half-applied.
        manifest = await asyncio.to_thread(preflight_restore, backup_id, components)
        done: dict[str, Any] = {}

        if "postgres" in components:
            pg_path = os.path.join(backup_dir, "postgres.dump")
            if not os.path.isfile(pg_path):
                raise RuntimeError("backup has no postgres.dump")
            r = await asyncio.to_thread(_run_helper, "pg-restore", pg_path)
            if r.returncode != 0:
                raise RuntimeError(f"pg_restore failed: {(r.stderr or r.stdout).strip()[-800:]}")
            done["postgres"] = True

        if "clickhouse_schema" in components:
            schema_sql = _read_backup_schema(backup_dir) or ""
            done["clickhouse_schema_statements"] = await asyncio.to_thread(
                _ch_apply_schema_sync, schema_sql
            )

        if "clickhouse_data" in components:
            ch_name = manifest.get("clickhouse_backup_name")
            if not ch_name:
                raise RuntimeError("backup does not include ClickHouse data")
            restored_tables = []
            tables = await asyncio.to_thread(
                _restorable_tables, manifest, _read_backup_schema(backup_dir)
            )
            base = [t for t in tables if not t.endswith("_mv")]
            mvs = [t for t in tables if t.endswith("_mv")]
            for table in base + mvs:
                op_id = await asyncio.to_thread(_ch_restore_table_sync, table, ch_name)
                if op_id:
                    await _wait_ch_operation(op_id)
                restored_tables.append(table)
            done["clickhouse_tables"] = len(restored_tables)

        if "config" in components:
            done["config_files"] = await asyncio.to_thread(
                _extract_config_files, os.path.join(backup_dir, "config.tar.gz")
            )

        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    # A PostgreSQL dump includes its own backup row while that
                    # backup is still running. Restoring it must not strand the
                    # row in 'running' and block every subsequent backup.
                    "UPDATE storage_backups SET status='completed', last_restore_status='completed', "
                    "last_restore_error=NULL WHERE id=:id"
                ),
                {"id": backup_id},
            )
            await write_storage_event(
                db, "restore_completed", actor=actor,
                details={"backup_id": backup_id, "components": components, **{
                    k: v for k, v in done.items() if not isinstance(v, list)
                }},
            )
            await db.commit()
        logger.info("restore of backup %s completed: %s", backup_id, components)
    except Exception as e:
        logger.exception("restore of backup %s failed", backup_id)
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE storage_backups SET status='completed', last_restore_status='failed', "
                    "last_restore_error=:e WHERE id=:id"
                ),
                {"e": str(e)[:2000], "id": backup_id},
            )
            await write_storage_event(
                db, "restore_failed", actor=actor,
                details={"backup_id": backup_id, "error": str(e)[:500]},
            )
            await db.commit()


async def delete_backup_files(backup_id: str) -> None:
    def _rm() -> None:
        shutil.rmtree(os.path.join(BACKUP_ROOT, backup_id), ignore_errors=True)
        shutil.rmtree(
            os.path.join(CH_BACKUP_HOST_ROOT, "appliance", backup_id),
            ignore_errors=True,
        )
    await asyncio.to_thread(_rm)


# ─── Scheduled backups + sweeper loop ────────────────────────────────────────

async def _maybe_run_scheduled_backup(db: AsyncSession, schedule: dict) -> Optional[str]:
    if not schedule.get("enabled"):
        return None
    now = datetime.now(timezone.utc)
    if now.hour != int(schedule.get("hour_utc", 2)):
        return None
    if schedule.get("frequency") == "weekly" and now.weekday() != int(schedule.get("weekday", 6)):
        return None
    # Already ran in this window?
    row = (
        await db.execute(
            text(
                "SELECT 1 FROM storage_backups WHERE created_by='schedule' "
                "AND created_at > :since AND status != 'failed' LIMIT 1"
            ),
            {"since": now - timedelta(hours=23)},
        )
    ).first()
    if row:
        return None

    backup_id = str(uuid.uuid4())
    include_ch = bool(schedule.get("include_clickhouse"))
    await db.execute(
        text(
            "INSERT INTO storage_backups (id, created_by, kind, status, include_clickhouse) "
            "VALUES (:id, 'schedule', :kind, 'running', :ch)"
        ),
        {"id": backup_id, "kind": "full" if include_ch else "config", "ch": include_ch},
    )
    await db.commit()
    asyncio.create_task(
        run_backup(backup_id, "full" if include_ch else "config", include_ch, "schedule")
    )

    # Prune old scheduled backups beyond keep_last.
    keep = max(1, int(schedule.get("keep_last", 5)))
    old = (
        await db.execute(
            text(
                "SELECT id FROM storage_backups WHERE created_by='schedule' "
                "AND status='completed' ORDER BY created_at DESC OFFSET :keep"
            ),
            {"keep": keep},
        )
    ).all()
    for (old_id,) in old:
        await delete_backup_files(str(old_id))
        await db.execute(
            text("DELETE FROM storage_backups WHERE id=:id"), {"id": old_id}
        )
    if old:
        await db.commit()
    return backup_id


async def storage_sweep_tick(db: AsyncSession) -> None:
    locked = (
        await db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": STORAGE_SWEEP_ADVISORY_LOCK},
        )
    ).scalar()
    if not locked:
        return

    # Fail backups whose worker died (API restart mid-backup).
    await db.execute(
        text(
            "UPDATE storage_backups SET status='failed', "
            "error='backup interrupted (service restart)', finished_at=NOW() "
            "WHERE status='running' AND created_at < :stale"
        ),
        {"stale": datetime.now(timezone.utc) - timedelta(hours=BACKUP_STALE_AFTER_H)},
    )

    settings = await get_retention_settings(db)
    write_watchdog_policy(settings)
    if not watchdog_status().get("alive"):
        try:
            await asyncio.to_thread(_fallback_watchdog_tick)
        except Exception:
            logger.exception("in-process storage watchdog tick failed")
    try:
        await import_watchdog_audit(db)
    except Exception:
        logger.exception("importing the storage watchdog audit trail failed")

    try:
        schedule = await get_backup_schedule(db)
        await _maybe_run_scheduled_backup(db, schedule)
    except Exception:
        logger.exception("scheduled backup check failed")

    await db.commit()


async def storage_sweeper_loop() -> None:
    from app.core.database import AsyncSessionLocal

    await asyncio.sleep(45)  # let the app settle
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await storage_sweep_tick(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("storage sweeper tick failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
