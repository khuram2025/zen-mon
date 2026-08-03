"""ClickHouse ingestion + query helpers for host (server) metrics.

Used by ``/api/v1/agents/results/host`` to fan a batched envelope out to
the right per-kind ClickHouse table, and by the admin metrics endpoints
to read it back for charts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_clickhouse_client
from app.schemas.agent import AgentResultsBatch, MetricPoint, MetricSeries
from app.services.filesystem_monitoring import ch_capacity_filter

logger = logging.getLogger("zenplus.host_metric_service")


# ── Inserts ──────────────────────────────────────────────────────────

def _ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def _dt_or_none(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v:
        return None
    if len(v) == 8 and v.isdigit():
        try:
            return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


def _arr_f(v: Any) -> list:
    if isinstance(v, (list, tuple)):
        return [_f(x) for x in v]
    return []


def _insert_cpu(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data: list[list] = []
    for d in rows:
        data.append([
            server_id, agent_id, _ts(d.get("timestamp")),
            _f(d.get("cpu_total_pct")),
            _f(d.get("cpu_user_pct")),
            _f(d.get("cpu_system_pct")),
            _f(d.get("cpu_iowait_pct")),
            _f(d.get("cpu_steal_pct")),
            _f(d.get("cpu_idle_pct")),
            _arr_f(d.get("per_core")),
            _f(d.get("load_1")),
            _f(d.get("load_5")),
            _f(d.get("load_15")),
        ])
    if not data:
        return 0
    client.insert("host_cpu_metrics", data, column_names=[
        "server_id", "agent_id", "timestamp",
        "cpu_total_pct", "cpu_user_pct", "cpu_system_pct",
        "cpu_iowait_pct", "cpu_steal_pct", "cpu_idle_pct",
        "per_core", "load_1", "load_5", "load_15",
    ])
    return len(data)


def _insert_memory(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        total = _i(d.get("total_bytes"))
        used = _i(d.get("used_bytes"))
        pct = _f(d.get("used_pct"))
        if not pct and total:
            pct = (used / total) * 100.0
        data.append([
            server_id, agent_id, _ts(d.get("timestamp")),
            total, used,
            _i(d.get("available_bytes")),
            _i(d.get("cached_bytes")),
            _i(d.get("committed_bytes")),
            _i(d.get("swap_total_bytes")),
            _i(d.get("swap_used_bytes")),
            pct,
        ])
    if not data:
        return 0
    client.insert("host_memory_metrics", data, column_names=[
        "server_id", "agent_id", "timestamp",
        "total_bytes", "used_bytes", "available_bytes", "cached_bytes",
        "committed_bytes", "swap_total_bytes", "swap_used_bytes", "used_pct",
    ])
    return len(data)


def _insert_filesystem(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        total = _i(d.get("total_bytes"))
        used = _i(d.get("used_bytes"))
        pct = _f(d.get("used_pct"))
        if not pct and total:
            pct = (used / total) * 100.0
        data.append([
            server_id, agent_id,
            str(d.get("mount") or ""), str(d.get("fs_type") or ""),
            _ts(d.get("timestamp")),
            total, used, _i(d.get("free_bytes")), pct,
            _i(d.get("inodes_total")), _i(d.get("inodes_used")),
        ])
    if not data:
        return 0
    client.insert("host_filesystem_metrics", data, column_names=[
        "server_id", "agent_id", "mount", "fs_type", "timestamp",
        "total_bytes", "used_bytes", "free_bytes", "used_pct",
        "inodes_total", "inodes_used",
    ])
    return len(data)


def _insert_disk_io(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        data.append([
            server_id, agent_id,
            str(d.get("device") or ""),
            _ts(d.get("timestamp")),
            _f(d.get("read_bytes_ps")),
            _f(d.get("write_bytes_ps")),
            _f(d.get("read_iops")),
            _f(d.get("write_iops")),
            _f(d.get("queue_length")),
            _f(d.get("util_pct")),
            _f(d.get("avg_read_ms")),
            _f(d.get("avg_write_ms")),
        ])
    if not data:
        return 0
    client.insert("host_disk_io_metrics", data, column_names=[
        "server_id", "agent_id", "device", "timestamp",
        "read_bytes_ps", "write_bytes_ps", "read_iops", "write_iops",
        "queue_length", "util_pct", "avg_read_ms", "avg_write_ms",
    ])
    return len(data)


def _insert_network(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        data.append([
            server_id, agent_id,
            str(d.get("if_name") or ""),
            _ts(d.get("timestamp")),
            _f(d.get("rx_bytes_ps")),
            _f(d.get("tx_bytes_ps")),
            _f(d.get("rx_packets_ps")),
            _f(d.get("tx_packets_ps")),
            _f(d.get("rx_errors_ps")),
            _f(d.get("tx_errors_ps")),
            _f(d.get("rx_dropped_ps")),
            _f(d.get("tx_dropped_ps")),
            1 if d.get("is_up") else 0,
        ])
    if not data:
        return 0
    client.insert("host_network_metrics", data, column_names=[
        "server_id", "agent_id", "if_name", "timestamp",
        "rx_bytes_ps", "tx_bytes_ps", "rx_packets_ps", "tx_packets_ps",
        "rx_errors_ps", "tx_errors_ps", "rx_dropped_ps", "tx_dropped_ps",
        "is_up",
    ])
    return len(data)


def _insert_process(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        data.append([
            server_id, agent_id,
            str(d.get("process_name") or "")[:255],
            _ts(d.get("timestamp")),
            _i(d.get("pid")),
            _f(d.get("cpu_pct")),
            _i(d.get("memory_bytes")),
            _i(d.get("thread_count")),
            _i(d.get("handle_count")),
            str(d.get("user_name") or "")[:255],
        ])
    if not data:
        return 0
    client.insert("host_process_metrics", data, column_names=[
        "server_id", "agent_id", "process_name", "timestamp",
        "pid", "cpu_pct", "memory_bytes", "thread_count", "handle_count", "user_name",
    ])
    return len(data)


def _insert_service_state(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        data.append([
            server_id, agent_id,
            str(d.get("service_name") or "")[:255],
            _ts(d.get("timestamp")),
            str(d.get("state") or "unknown")[:64],
            str(d.get("start_mode") or "unknown")[:64],
            _i(d.get("pid")),
            _i(d.get("exit_code")),
        ])
    if not data:
        return 0
    client.insert("host_service_state", data, column_names=[
        "server_id", "agent_id", "service_name", "timestamp",
        "state", "start_mode", "pid", "exit_code",
    ])
    return len(data)


def _insert_event_log(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        sample_ids = d.get("sample_ids") or []
        if not isinstance(sample_ids, list):
            sample_ids = []
        sample_ids = [_i(x) for x in sample_ids[:50]]
        data.append([
            server_id, agent_id,
            str(d.get("log_name") or "System")[:128],
            str(d.get("level") or "information")[:32],
            _ts(d.get("timestamp")),
            _i(d.get("event_count")),
            sample_ids,
        ])
    if not data:
        return 0
    client.insert("host_event_log_summary", data, column_names=[
        "server_id", "agent_id", "log_name", "level", "timestamp",
        "event_count", "sample_ids",
    ])
    return len(data)


def _insert_agent_health(client, agent_id: str, server_id: str, rows: list[dict]) -> int:
    data = []
    for d in rows:
        data.append([
            agent_id, server_id, _ts(d.get("timestamp")),
            _f(d.get("cpu_pct")), _i(d.get("memory_bytes")),
            _i(d.get("queue_depth")), _i(d.get("spool_bytes")),
            _i(d.get("upload_lag_ms")),
            1 if d.get("config_apply_ok") else 0,
            str(d.get("last_error") or ""),
        ])
    if not data:
        return 0
    client.insert("agent_health_metrics", data, column_names=[
        "agent_id", "server_id", "timestamp",
        "cpu_pct", "memory_bytes", "queue_depth", "spool_bytes",
        "upload_lag_ms", "config_apply_ok", "last_error",
    ])
    return len(data)


# ── Inventory upsert (Postgres) ──────────────────────────────────────

# Processes not refreshed within this window are treated as exited and pruned
# from the inventory snapshot. Keep in sync with the freshness filter in
# server/app/api/v1/servers.py (server_processes). 5× the default 60s upload
# interval, so a couple of missed uploads won't drop still-running processes.
PROCESS_STALE_SECONDS = 300

# Agent batches whose newest sample deviates from server time by more than
# this are shifted to server time (see ingest_host_metric_batch); the offset
# is recorded on the agent row as clock_skew_s for fleet visibility.
CLOCK_SKEW_TOLERANCE_S = 300


async def _upsert_process_inventory(server_id: str, rows: list[dict], db: AsyncSession) -> None:
    for p in rows:
        pid = _i(p.get("pid"))
        name = str(p.get("process_name") or p.get("name") or "")[:255]
        if pid <= 0 or not name:
            continue
        # When the name for a PID changes the OS has recycled the PID onto a
        # different process, so reset start time / cmdline instead of carrying
        # the previous process's values forward.
        await db.execute(text(
            """INSERT INTO server_process_inventory
                   (server_id, pid, name, cmdline, user_name, cpu_pct, memory_bytes, started_at, updated_at)
               VALUES (:sid, :pid, :name, :cmd, :user, :cpu, :mem, :started, NOW())
               ON CONFLICT (server_id, pid) DO UPDATE SET
                   name = EXCLUDED.name,
                   cmdline = CASE WHEN server_process_inventory.name IS DISTINCT FROM EXCLUDED.name
                                  THEN EXCLUDED.cmdline
                                  ELSE COALESCE(EXCLUDED.cmdline, server_process_inventory.cmdline) END,
                   user_name = EXCLUDED.user_name,
                   cpu_pct = EXCLUDED.cpu_pct,
                   memory_bytes = EXCLUDED.memory_bytes,
                   started_at = CASE WHEN server_process_inventory.name IS DISTINCT FROM EXCLUDED.name
                                     THEN EXCLUDED.started_at
                                     ELSE COALESCE(EXCLUDED.started_at, server_process_inventory.started_at) END,
                   updated_at = NOW()"""
        ), {
            "sid": server_id,
            "pid": pid,
            "name": name,
            "cmd": p.get("cmdline"),
            "user": (p.get("user_name") or "")[:255] or None,
            "cpu": _f(p.get("cpu_pct")),
            "mem": _i(p.get("memory_bytes")),
            "started": _dt_or_none(p.get("started_at")),
        })

    # Prune processes we haven't heard about within the freshness window so
    # exited processes don't linger in the snapshot and the table stays bounded.
    await db.execute(
        text(
            "DELETE FROM server_process_inventory "
            "WHERE server_id = :sid "
            "AND updated_at < NOW() - make_interval(secs => :ttl)"
        ),
        {"sid": server_id, "ttl": PROCESS_STALE_SECONDS},
    )


async def _upsert_inventory(server_id: str, inv: Dict[str, Any], db: AsyncSession) -> None:
    os_info = inv.get("os")
    if isinstance(os_info, dict):
        boot_raw = os_info.get("boot_time")
        boot_dt = None
        if boot_raw:
            try:
                boot_dt = datetime.fromisoformat(str(boot_raw).replace("Z", "+00:00"))
            except ValueError:
                boot_dt = None
        elif _i(os_info.get("uptime_seconds")):
            boot_dt = datetime.now(timezone.utc) - timedelta(
                seconds=_i(os_info.get("uptime_seconds")))
        if boot_dt is not None:
            await db.execute(text(
                """UPDATE servers SET boot_time = :bt, updated_at = NOW()
                   WHERE id = :sid
                     AND (boot_time IS NULL OR abs(extract(epoch FROM (boot_time - :bt))) > 120)"""
            ), {"sid": server_id, "bt": boot_dt})

    services = inv.get("services") or []
    if isinstance(services, list) and services:
        for s in services:
            await db.execute(text(
                """INSERT INTO server_service_inventory
                       (server_id, service_name, display_name, start_mode, state, pid, description, updated_at)
                   VALUES (:sid, :name, :dn, :sm, :st, :pid, :desc, NOW())
                   ON CONFLICT (server_id, service_name) DO UPDATE SET
                       display_name = EXCLUDED.display_name,
                       start_mode   = EXCLUDED.start_mode,
                       state        = EXCLUDED.state,
                       pid          = EXCLUDED.pid,
                       description  = EXCLUDED.description,
                       updated_at   = NOW()"""
            ), {
                "sid": server_id,
                "name": str(s.get("service_name") or s.get("name") or "")[:255],
                "dn": (s.get("display_name") or "")[:255] or None,
                "sm": (s.get("start_mode") or "")[:32] or None,
                "st": (s.get("state") or "")[:32] or None,
                "pid": _i(s.get("pid")) or None,
                "desc": s.get("description"),
            })

    filesystems = inv.get("filesystems") or []
    if isinstance(filesystems, list):
        for fs in filesystems:
            total = _i(fs.get("total_bytes"))
            used = _i(fs.get("used_bytes"))
            pct = _f(fs.get("used_pct"))
            if not pct and total:
                pct = (used / total) * 100.0
            await db.execute(text(
                """INSERT INTO server_filesystem_inventory
                       (server_id, mount, fs_type, device, total_bytes, used_bytes, free_bytes, used_pct, updated_at)
                   VALUES (:sid, :mount, :fs, :dev, :tot, :used, :free, :pct, NOW())
                   ON CONFLICT (server_id, mount) DO UPDATE SET
                       fs_type=EXCLUDED.fs_type,
                       device=EXCLUDED.device,
                       total_bytes=EXCLUDED.total_bytes,
                       used_bytes=EXCLUDED.used_bytes,
                       free_bytes=EXCLUDED.free_bytes,
                       used_pct=EXCLUDED.used_pct,
                       updated_at=NOW()"""
            ), {
                "sid": server_id, "mount": str(fs.get("mount") or "")[:255],
                "fs": (fs.get("fs_type") or "")[:64] or None,
                "dev": (fs.get("device") or "")[:255] or None,
                "tot": total, "used": used, "free": _i(fs.get("free_bytes")), "pct": pct,
            })

    interfaces = inv.get("network_interfaces") or []
    if isinstance(interfaces, list):
        import json as _json
        for nic in interfaces:
            await db.execute(text(
                """INSERT INTO server_network_interface_inventory
                       (server_id, if_name, mac_address, ip_addresses, speed_mbps, is_up, mtu, updated_at)
                   VALUES (:sid, :name, :mac, COALESCE(:ips, '[]'::jsonb), :spd, :up, :mtu, NOW())
                   ON CONFLICT (server_id, if_name) DO UPDATE SET
                       mac_address = EXCLUDED.mac_address,
                       ip_addresses = EXCLUDED.ip_addresses,
                       speed_mbps = EXCLUDED.speed_mbps,
                       is_up = EXCLUDED.is_up,
                       mtu = EXCLUDED.mtu,
                       updated_at = NOW()"""
            ), {
                "sid": server_id,
                "name": str(nic.get("if_name") or nic.get("name") or "")[:128],
                "mac": (nic.get("mac_address") or "")[:64] or None,
                "ips": _json.dumps(nic.get("ip_addresses") or []),
                "spd": _i(nic.get("speed_mbps")) or None,
                "up": bool(nic.get("is_up", True)),
                "mtu": _i(nic.get("mtu")) or None,
            })

    software = inv.get("software") or inv.get("applications") or []
    if isinstance(software, list):
        for app in software:
            if not isinstance(app, dict):
                continue
            name = str(app.get("package_name") or app.get("name") or app.get("display_name") or "").strip()
            if not name:
                continue
            await db.execute(text(
                """INSERT INTO server_software_inventory
                       (server_id, package_name, version, vendor, install_date, updated_at)
                   VALUES (:sid, :name, :ver, :vendor, :install_date, NOW())
                   ON CONFLICT (server_id, package_name) DO UPDATE SET
                       version = EXCLUDED.version,
                       vendor = EXCLUDED.vendor,
                       install_date = COALESCE(EXCLUDED.install_date, server_software_inventory.install_date),
                       updated_at = NOW()"""
            ), {
                "sid": server_id,
                "name": name[:255],
                "ver": str(app.get("version") or app.get("display_version") or "")[:128] or None,
                "vendor": str(app.get("vendor") or app.get("publisher") or "")[:255] or None,
                "install_date": _dt_or_none(app.get("install_date")),
            })

    # Prune rows the snapshot no longer contains. Each section above stamps
    # updated_at = NOW() for everything present, so anything older than a
    # short grace window was dropped by the agent (software uninstalled,
    # volume unmounted, NIC removed). Without this, uninstalled software
    # fails "prohibited" baselines forever and dead mounts keep driving
    # disk health. Only prune when the section was actually sent — an
    # omitted section means "no update", not "empty".
    prune_map = [
        (services, "server_service_inventory"),
        (filesystems, "server_filesystem_inventory"),
        (interfaces, "server_network_interface_inventory"),
        (software, "server_software_inventory"),
    ]
    for sent, table in prune_map:
        if isinstance(sent, list) and sent:
            await db.execute(
                text(f"DELETE FROM {table} "
                     "WHERE server_id = :sid AND updated_at < NOW() - INTERVAL '2 minutes'"),
                {"sid": server_id},
            )

    # OS info
    os_info = inv.get("os") or {}
    if os_info:
        await db.execute(text(
            """UPDATE servers SET
                 os_name = COALESCE(:n, os_name),
                 os_version = COALESCE(:v, os_version),
                 kernel_or_build = COALESCE(:k, kernel_or_build),
                 architecture = COALESCE(:a, architecture),
                 fqdn = COALESCE(:f, fqdn),
                 updated_at = NOW()
               WHERE id = :sid"""
        ), {
            "n": os_info.get("name"), "v": os_info.get("version"),
            "k": os_info.get("kernel_or_build"), "a": os_info.get("architecture"),
            "f": os_info.get("fqdn"),
            "sid": server_id,
        })


# ── Main entry point ─────────────────────────────────────────────────

KIND_INSERTERS = {
    "cpu": _insert_cpu,
    "memory": _insert_memory,
    "filesystem": _insert_filesystem,
    "disk_io": _insert_disk_io,
    "network": _insert_network,
    "process": _insert_process,
    "service_state": _insert_service_state,
    "event_log": _insert_event_log,
    "agent_health": _insert_agent_health,
}


async def ingest_host_metric_batch(
    agent_id: str,
    server_id: str,
    batch: AgentResultsBatch,
    db: AsyncSession,
) -> tuple[int, int, list[str], int]:
    """Fan-out a metrics envelope to ClickHouse + inventory tables.

    Returns (accepted_count, rejected_count, errors, clock_skew_s).
    """
    accepted = 0
    rejected = 0
    errors: list[str] = []

    # Group by kind
    by_kind: dict[str, list[dict]] = {}
    for sample in batch.metrics:
        kind = sample.kind
        row = dict(sample.data) if sample.data else {}
        row.setdefault("timestamp", sample.timestamp)
        row["timestamp"] = _ts(row.get("timestamp"))
        if kind == "inventory":
            # handled separately
            by_kind.setdefault("inventory", []).append(row)
            continue
        if kind not in KIND_INSERTERS:
            rejected += 1
            errors.append(f"unknown kind: {kind}")
            continue
        by_kind.setdefault(kind, []).append(row)

    # Clock-skew correction: agent timestamps are wall-clock on the host and
    # hosts routinely have wrong clocks/timezones. If the newest sample in the
    # batch deviates from server time by more than the tolerance, shift every
    # sample by that offset — this keeps spooled-sample spacing intact while
    # anchoring the batch to server time, so "latest" windows stay truthful.
    clock_skew_s = 0
    metric_rows = [r for k, rows in by_kind.items() if k != "inventory" for r in rows]
    if metric_rows:
        newest = max(r["timestamp"] for r in metric_rows)
        offset = datetime.now(timezone.utc) - newest
        clock_skew_s = -int(offset.total_seconds())
        if abs(offset.total_seconds()) > CLOCK_SKEW_TOLERANCE_S:
            for r in metric_rows:
                r["timestamp"] = r["timestamp"] + offset
            logger.warning(
                "clock skew of %+ds corrected for agent=%s (batch shifted to server time)",
                clock_skew_s, agent_id,
            )

    client = get_clickhouse_client()
    for kind, rows in by_kind.items():
        if kind == "inventory":
            continue
        try:
            n = KIND_INSERTERS[kind](client, agent_id, server_id, rows)
            accepted += n
        except Exception as exc:
            rejected += len(rows)
            errors.append(f"{kind}: {exc}")
            logger.exception("ClickHouse insert failed for kind=%s", kind)

    if by_kind.get("process"):
        try:
            await _upsert_process_inventory(server_id, by_kind["process"], db)
        except Exception as exc:
            errors.append(f"process_inventory: {exc}")
            logger.exception("Process inventory upsert failed")

    # Inventory snapshot (either from sample kind or top-level field).
    software_updated = False
    if batch.inventory:
        try:
            await _upsert_inventory(server_id, batch.inventory, db)
            software_updated = software_updated or bool(
                batch.inventory.get("software") or batch.inventory.get("applications"))
        except Exception as exc:
            errors.append(f"inventory: {exc}")
            logger.exception("Inventory upsert failed")

    for row in by_kind.get("inventory", []):
        try:
            await _upsert_inventory(server_id, row, db)
            software_updated = software_updated or bool(
                row.get("software") or row.get("applications"))
        except Exception as exc:
            errors.append(f"inventory: {exc}")

    # Health: derive server status + reasons from the fresh telemetry.
    try:
        from app.services.server_health_service import compute_server_health, store_server_health
        status, reasons, issues = await compute_server_health(db, server_id, by_kind)
        await store_server_health(db, server_id, status, reasons, issues)
    except Exception as exc:
        errors.append(f"health: {exc}")
        logger.exception("Health computation failed")

    # Compliance: a fresh software list may change baseline outcomes.
    if software_updated:
        try:
            from app.services.baseline_service import evaluate_server
            await evaluate_server(db, server_id, commit=False)
        except Exception as exc:
            errors.append(f"baseline: {exc}")
            logger.exception("Baseline evaluation failed")

    return accepted, rejected, errors, clock_skew_s


# ── Query helpers (for the admin metrics endpoints) ──────────────────

def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pick_table(metric: str, range_seconds: int) -> tuple[str, str]:
    """Return (table, granularity) for a given metric over a window length."""
    use_rollup = range_seconds > 6 * 3600
    if metric.startswith("cpu"):
        return ("host_cpu_metrics_5m" if use_rollup else "host_cpu_metrics",
                "5m" if use_rollup else "raw")
    if metric.startswith("memory"):
        return ("host_memory_metrics_5m" if use_rollup else "host_memory_metrics",
                "5m" if use_rollup else "raw")
    if metric.startswith("filesystem"):
        return ("host_filesystem_metrics_5m" if use_rollup else "host_filesystem_metrics",
                "5m" if use_rollup else "raw")
    if metric.startswith("network"):
        return ("host_network_metrics_5m" if use_rollup else "host_network_metrics",
                "5m" if use_rollup else "raw")
    if metric.startswith("disk_io"):
        return ("host_disk_io_metrics", "raw")
    return ("host_cpu_metrics", "raw")


def query_server_metrics(
    server_id: str,
    from_time: datetime,
    to_time: datetime,
    metrics: list[str],
) -> list[MetricSeries]:
    """Time-series queries used by the server detail charts."""
    client = get_clickhouse_client()
    out: list[MetricSeries] = []
    range_s = max(1, int((_utc(to_time) - _utc(from_time)).total_seconds()))
    params = {
        "sid": server_id,
        "f": _utc(from_time).strftime("%Y-%m-%d %H:%M:%S"),
        "t": _utc(to_time).strftime("%Y-%m-%d %H:%M:%S"),
    }

    for metric in metrics:
        try:
            if metric == "cpu_total_pct":
                table, gran = _pick_table("cpu", range_s)
                if gran == "raw":
                    sql = f"""SELECT timestamp, cpu_total_pct AS v
                              FROM zenplus.{table}
                              WHERE server_id = %(sid)s
                                AND timestamp >= %(f)s AND timestamp <= %(t)s
                              ORDER BY timestamp LIMIT 5000"""
                else:
                    sql = f"""SELECT timestamp, avg_total_pct AS v
                              FROM zenplus.{table}
                              WHERE server_id = %(sid)s
                                AND timestamp >= %(f)s AND timestamp <= %(t)s
                              ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="%", label="CPU total",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
            elif metric == "memory_used_pct":
                table, gran = _pick_table("memory", range_s)
                col = "used_pct" if gran == "raw" else "avg_used_pct"
                sql = f"""SELECT timestamp, {col} AS v
                          FROM zenplus.{table}
                          WHERE server_id = %(sid)s
                            AND timestamp >= %(f)s AND timestamp <= %(t)s
                          ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="%", label="Memory used",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
            elif metric == "memory_used_bytes":
                table, gran = _pick_table("memory", range_s)
                col = "used_bytes" if gran == "raw" else "avg_used_bytes"
                sql = f"""SELECT timestamp, {col} AS v
                          FROM zenplus.{table}
                          WHERE server_id = %(sid)s
                            AND timestamp >= %(f)s AND timestamp <= %(t)s
                          ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="B", label="Memory used (bytes)",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
            elif metric == "network_rx_bps":
                sql = """SELECT timestamp, sum(rx_bytes_ps) AS v
                         FROM zenplus.host_network_metrics
                         WHERE server_id = %(sid)s
                           AND timestamp >= %(f)s AND timestamp <= %(t)s
                         GROUP BY timestamp ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="bps", label="Network RX",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
            elif metric == "network_tx_bps":
                sql = """SELECT timestamp, sum(tx_bytes_ps) AS v
                         FROM zenplus.host_network_metrics
                         WHERE server_id = %(sid)s
                           AND timestamp >= %(f)s AND timestamp <= %(t)s
                         GROUP BY timestamp ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="bps", label="Network TX",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
            elif metric == "disk_read_bps":
                sql = """SELECT timestamp, sum(read_bytes_ps) AS v
                         FROM zenplus.host_disk_io_metrics
                         WHERE server_id = %(sid)s
                           AND timestamp >= %(f)s AND timestamp <= %(t)s
                         GROUP BY timestamp ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="B/s", label="Disk read",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
            elif metric == "disk_write_bps":
                sql = """SELECT timestamp, sum(write_bytes_ps) AS v
                         FROM zenplus.host_disk_io_metrics
                         WHERE server_id = %(sid)s
                           AND timestamp >= %(f)s AND timestamp <= %(t)s
                         GROUP BY timestamp ORDER BY timestamp LIMIT 5000"""
                res = client.query(sql, parameters=params).result_rows
                out.append(MetricSeries(
                    metric=metric, unit="B/s", label="Disk write",
                    points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                            for r in res],
                ))
        except Exception as exc:
            logger.warning("metric query failed for %s: %s", metric, exc)
            out.append(MetricSeries(metric=metric, unit=None, label=None, points=[]))

    return out


def query_fleet_latest_metrics(window_minutes: int = 10) -> dict[str, dict]:
    """Current cpu/mem/disk/net per server for the inventory list.

    One aggregate per server over the last ``window_minutes`` — cheap enough
    to refresh on every list poll (the raw tables are (server_id, ts)-ordered).
    """
    client = get_clickhouse_client()
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, dict] = {}

    def _merge(rows, key):
        for r in rows:
            sid = str(r[0])
            out.setdefault(sid, {})[key] = float(r[1] or 0)

    try:
        _merge(client.query(
            """SELECT server_id, avg(cpu_total_pct) FROM zenplus.host_cpu_metrics
               WHERE timestamp >= %(s)s GROUP BY server_id""",
            parameters={"s": since}).result_rows, "cpu_pct")
        _merge(client.query(
            """SELECT server_id, avg(used_pct) FROM zenplus.host_memory_metrics
               WHERE timestamp >= %(s)s GROUP BY server_id""",
            parameters={"s": since}).result_rows, "memory_pct")
        _merge(client.query(
            f"""SELECT server_id, max(used_pct) FROM zenplus.host_filesystem_metrics
               WHERE timestamp >= %(s)s AND {ch_capacity_filter()}
               GROUP BY server_id""",
            parameters={"s": since}).result_rows, "disk_max_pct")
        _merge(client.query(
            """SELECT server_id, sum(rx_bytes_ps + tx_bytes_ps) / uniqExact(timestamp)
               FROM zenplus.host_network_metrics
               WHERE timestamp >= %(s)s GROUP BY server_id""",
            parameters={"s": since}).result_rows, "net_bps")
    except Exception as exc:
        logger.warning("fleet latest metrics query failed: %s", exc)
    return out


def query_server_memory_total(server_id: str, window_minutes: int = 10) -> int:
    """Latest total physical RAM (bytes) for one server.

    Used to render per-process memory% on the processes tab. Returns 0 when no
    recent sample exists (caller treats 0 as "unknown" and hides the percentage).
    """
    client = get_clickhouse_client()
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = client.query(
            """SELECT argMax(total_bytes, timestamp) FROM zenplus.host_memory_metrics
               WHERE server_id = %(sid)s AND timestamp >= %(s)s""",
            parameters={"sid": server_id, "s": since}).result_rows
        if rows and rows[0][0]:
            return int(rows[0][0])
    except Exception as exc:
        logger.warning("server memory total query failed: %s", exc)
    return 0


def query_process_history(
    server_id: str,
    process_name: str,
    from_time: datetime,
    to_time: datetime,
) -> list[MetricSeries]:
    """CPU% and memory over time for all processes sharing a name.

    PIDs churn constantly (a browser spawns a process per tab/renderer), so
    history is aggregated by ``process_name``: at each sample we sum across that
    name's PIDs, then bucket over time so the chart stays readable for wide
    ranges. Reads raw host_process_metrics — there is no process rollup table.
    """
    client = get_clickhouse_client()
    range_s = max(1, int((_utc(to_time) - _utc(from_time)).total_seconds()))
    bucket = max(60, range_s // 240)  # ~240 points target, never finer than 60s
    params = {
        "sid": server_id,
        "name": process_name,
        "f": _utc(from_time).strftime("%Y-%m-%d %H:%M:%S"),
        "t": _utc(to_time).strftime("%Y-%m-%d %H:%M:%S"),
        "b": bucket,
    }
    specs = [
        ("cpu_pct", "sum(cpu_pct)", "%", "CPU total"),
        ("memory_bytes", "sum(memory_bytes)", "B", "Memory"),
    ]
    out: list[MetricSeries] = []
    for metric, agg, unit, label in specs:
        try:
            sql = f"""SELECT toStartOfInterval(timestamp, toIntervalSecond(%(b)s)) AS ts,
                             avg(s) AS v
                      FROM (
                          SELECT timestamp, {agg} AS s
                          FROM zenplus.host_process_metrics
                          WHERE server_id = %(sid)s AND process_name = %(name)s
                            AND timestamp >= %(f)s AND timestamp <= %(t)s
                          GROUP BY timestamp
                      )
                      GROUP BY ts ORDER BY ts LIMIT 5000"""
            res = client.query(sql, parameters=params).result_rows
            out.append(MetricSeries(
                metric=metric, unit=unit, label=label,
                points=[MetricPoint(timestamp=_utc(r[0]), value=float(r[1] or 0))
                        for r in res],
            ))
        except Exception as exc:
            logger.warning("process history query failed for %s/%s: %s", process_name, metric, exc)
            out.append(MetricSeries(metric=metric, unit=unit, label=label, points=[]))
    return out


def query_top_pressure(kind: str, limit: int = 5) -> list[dict]:
    """Top-N hosts by CPU/memory/disk pressure (used by Overview page)."""
    client = get_clickhouse_client()
    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        if kind == "cpu":
            sql = """SELECT server_id, avg(cpu_total_pct) AS v
                     FROM zenplus.host_cpu_metrics
                     WHERE timestamp >= %(since)s
                     GROUP BY server_id
                     ORDER BY v DESC LIMIT %(limit)s"""
        elif kind == "memory":
            sql = """SELECT server_id, avg(used_pct) AS v
                     FROM zenplus.host_memory_metrics
                     WHERE timestamp >= %(since)s
                     GROUP BY server_id
                     ORDER BY v DESC LIMIT %(limit)s"""
        elif kind == "disk":
            sql = f"""SELECT server_id, max(used_pct) AS v
                     FROM zenplus.host_filesystem_metrics
                     WHERE timestamp >= %(since)s AND {ch_capacity_filter()}
                     GROUP BY server_id
                     ORDER BY v DESC LIMIT %(limit)s"""
        elif kind == "network":
            # Average per-sample throughput, not the sum over the window.
            sql = """SELECT server_id, sum(rx_bytes_ps + tx_bytes_ps) / uniqExact(timestamp) AS v
                     FROM zenplus.host_network_metrics
                     WHERE timestamp >= %(since)s
                     GROUP BY server_id
                     ORDER BY v DESC LIMIT %(limit)s"""
        else:
            return []
        res = client.query(sql, parameters={"since": since, "limit": limit}).result_rows
        return [{"server_id": str(r[0]), "value": float(r[1] or 0)} for r in res]
    except Exception as exc:
        logger.warning("top pressure query failed (%s): %s", kind, exc)
        return []
