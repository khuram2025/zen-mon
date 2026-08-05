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

# Tables that must never be touched by retention/purge: ClickHouse's own
# bookkeeping and datasets with their own first-class retention subsystem
# (network captures manage purge_after themselves).
EXCLUDED_TABLES = {"schema_migrations"}
EXCLUDED_PREFIXES = ("capture_", "network_capture")

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

DEFAULT_AUTO_PURGE = {
    "enabled": True,
    "threshold_pct": 85,
    "target_pct": 75,
    "min_keep_days": 7,
}

DEFAULT_BACKUP_SCHEDULE = {
    "enabled": False,
    "frequency": "weekly",      # daily | weekly
    "weekday": 6,               # 0=Mon .. 6=Sun (used for weekly)
    "hour_utc": 2,
    "include_clickhouse": False,
    "keep_last": 5,
}


def classify_table(name: str) -> Optional[str]:
    """Map a ClickHouse table to a retention category (None = unmanaged)."""
    if name in EXCLUDED_TABLES or name.endswith("_mv"):
        return None
    if any(name.startswith(p) for p in EXCLUDED_PREFIXES):
        return None
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


# ─── Small helpers ───────────────────────────────────────────────────────────

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _mount_usage(path: str) -> dict[str, Any]:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return {
        "mount": path,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "usage_percent": round(used / total * 100, 1) if total else 0.0,
    }


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


async def emergency_auto_purge(db: AsyncSession, auto: dict) -> Optional[dict]:
    """Drop oldest purgeable partitions until /data is back under target."""
    usage = _mount_usage(DATA_MOUNT)
    if usage["usage_percent"] < auto["threshold_pct"]:
        return None

    inventory = await ch_inventory()
    tables_meta = {t["table"]: t for t in inventory}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=int(auto["min_keep_days"])
    )
    candidates = await asyncio.to_thread(
        _list_purgeable_partitions_sync, list(tables_meta), cutoff
    )
    if not candidates:
        await write_storage_event(
            db, "auto_purge", details={
                "usage_percent": usage["usage_percent"],
                "note": "threshold exceeded but no purgeable partitions older "
                        f"than {auto['min_keep_days']} days",
            },
        )
        return {"freed_bytes": 0, "dropped_partitions": [], "exhausted": True}

    target = float(auto["target_pct"])
    dropped, freed = [], 0
    for p in candidates:
        try:
            await asyncio.to_thread(_drop_partition_sync, p["table"], p["partition_id"])
            dropped.append({"table": p["table"], "partition_id": p["partition_id"],
                            "size_bytes": p["size_bytes"]})
            freed += p["size_bytes"]
        except Exception:
            logger.exception("auto-purge drop failed: %s %s", p["table"], p["partition_id"])
            continue
        if _mount_usage(DATA_MOUNT)["usage_percent"] <= target:
            break

    after = _mount_usage(DATA_MOUNT)
    result = {
        "dropped_partitions": dropped,
        "freed_bytes": freed,
        "usage_before_pct": usage["usage_percent"],
        "usage_after_pct": after["usage_percent"],
        "exhausted": after["usage_percent"] > target,
    }
    await write_storage_event(db, "auto_purge", freed_bytes=freed, details=result)
    logger.warning(
        "auto-purge: freed %s (%d partitions), /data %s%% -> %s%%",
        human_size(freed), len(dropped),
        usage["usage_percent"], after["usage_percent"],
    )
    return result


# ─── OS disk overview & cleanup ──────────────────────────────────────────────

def _os_usage_extras() -> dict:
    out = {"journal_bytes": 0, "apt_cache_bytes": 0}
    try:
        r = _run_helper("os-usage", timeout=60)
        if r.returncode == 0:
            out.update(json.loads(r.stdout.strip()))
    except Exception:
        pass
    return out


async def os_overview() -> dict:
    mounts = []
    seen = set()
    for path, label in (("/", "System (OS)"), (DATA_MOUNT, "Data volume")):
        try:
            u = _mount_usage(path)
            dev = os.stat(path).st_dev
            if dev in seen:
                continue
            seen.add(dev)
            mounts.append({**u, "label": label})
        except OSError:
            continue
    extras = await asyncio.to_thread(_os_usage_extras)
    return {"mounts": mounts, **extras}


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


def _ch_restore_table_sync(table: str, backup_name: str) -> str:
    client = get_ch_client()
    client.command(f"DROP TABLE IF EXISTS {CH_DATABASE}.{table} SYNC")
    res = client.query(
        f"RESTORE TABLE {CH_DATABASE}.{table} "
        f"FROM Disk('backups', '{backup_name}') ASYNC"
    ).result_rows
    return str(res[0][0]) if res else ""


def _ch_apply_schema_sync(schema_sql: str) -> int:
    """Create any missing tables from a schema dump (existing tables kept)."""
    client = get_ch_client()
    created = 0
    for stmt in schema_sql.split(";\n"):
        stmt = stmt.strip().rstrip(";")
        if not stmt:
            continue
        stmt = re.sub(r"^CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", stmt)
        stmt = re.sub(
            r"^CREATE MATERIALIZED VIEW ",
            "CREATE MATERIALIZED VIEW IF NOT EXISTS ", stmt,
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
            with open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            restored.append(dest)
    return restored


async def run_restore(backup_id: str, components: list[str], actor: str) -> None:
    """Execute a restore, updating last_restore_* on the backup row."""
    from app.core.database import AsyncSessionLocal

    backup_dir = os.path.join(BACKUP_ROOT, backup_id)
    try:
        with open(os.path.join(backup_dir, "manifest.json"), "r", encoding="utf-8") as f:
            manifest = json.load(f)
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
            with open(os.path.join(backup_dir, "clickhouse_schema.sql"), "r",
                      encoding="utf-8") as f:
                schema_sql = f.read()
            done["clickhouse_schema_statements"] = await asyncio.to_thread(
                _ch_apply_schema_sync, schema_sql
            )

        if "clickhouse_data" in components:
            ch_name = manifest.get("clickhouse_backup_name")
            if not ch_name:
                raise RuntimeError("backup does not include ClickHouse data")
            restored_tables = []
            base = [t for t in manifest.get("clickhouse_tables", []) if not t.endswith("_mv")]
            mvs = [t for t in manifest.get("clickhouse_tables", []) if t.endswith("_mv")]
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
                    "UPDATE storage_backups SET last_restore_status='completed', "
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
                    "UPDATE storage_backups SET last_restore_status='failed', "
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
    if settings["auto_purge"].get("enabled"):
        try:
            await emergency_auto_purge(db, settings["auto_purge"])
        except Exception:
            logger.exception("auto-purge failed")

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
