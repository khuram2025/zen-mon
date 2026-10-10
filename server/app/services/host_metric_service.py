"""ClickHouse ingestion + query helpers for host (server) metrics.

Used by ``/api/v1/agents/results/host`` to fan a batched envelope out to
the right per-kind ClickHouse table, and by the admin metrics endpoints
to read it back for charts.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_ch_client
from app.schemas.agent import AgentResultsBatch, MetricPoint, MetricSeries
from app.services import inventory_normalize, versioning
from app.services.filesystem_monitoring import ch_capacity_filter

logger = logging.getLogger("zenplus.host_metric_service")


def get_clickhouse_client():
    """Compatibility seam returning the current thread's CH session."""
    return get_ch_client()


class HostMetricStorageError(RuntimeError):
    """A retryable failure while persisting authoritative host telemetry."""

    def __init__(self, part: str):
        super().__init__(f"host telemetry storage failed for {part}")
        self.part = part


class _DeduplicatingInsertClient:
    """Inject one stable ClickHouse retry token into an insert call."""

    def __init__(self, client: Any, token: str):
        self._client = client
        self._token = token

    def insert(self, *args, settings=None, **kwargs):
        insert_settings = dict(settings or {})
        insert_settings["insert_deduplication_token"] = self._token
        insert_settings["insert_deduplicate"] = 1
        return self._client.insert(*args, settings=insert_settings, **kwargs)


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


def _b(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        normalized = v.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return default


def _text_or_none(v: Any, max_length: int) -> Optional[str]:
    if not isinstance(v, str):
        return None
    value = v.strip()
    return value[:max_length] if value else None


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
            str(d.get("cmdline") or "")[:2048],
            _dt_or_none(d.get("started_at")),
            str(d.get("state") or ("running" if _i(d.get("pid")) > 0 else "unknown"))[:32],
            1 if _b(d.get("running"), _i(d.get("pid")) > 0) else 0,
            1 if _b(d.get("watchlisted")) else 0,
        ])
    if not data:
        return 0
    client.insert("host_process_metrics", data, column_names=[
        "server_id", "agent_id", "process_name", "timestamp",
        "pid", "cpu_pct", "memory_bytes", "thread_count", "handle_count", "user_name",
        "cmdline", "started_at", "state", "running", "watchlisted",
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

# PostgreSQL is only a bounded cache; the API applies the agent policy's exact
# freshness horizon.  Keep rows longer than the maximum supported 60-minute
# collection plus 60-minute upload cycle so a valid slow policy is never
# physically pruned before the API can evaluate it.
PROCESS_INVENTORY_RETENTION_SECONDS = 86_400

# Agent batches whose newest sample deviates from server time by more than
# this are shifted to server time (see ingest_host_metric_batch); the offset
# is recorded on the agent row as clock_skew_s for fleet visibility.
CLOCK_SKEW_TOLERANCE_S = 300


async def _upsert_process_inventory(server_id: str, rows: list[dict], db: AsyncSession) -> None:
    for p in rows:
        pid = _i(p.get("pid"))
        name = str(p.get("process_name") or p.get("name") or "")[:255]
        if not name:
            continue
        running = _b(p.get("running"), pid > 0)
        watchlisted = _b(p.get("watchlisted"))
        state = str(p.get("state") or ("running" if running else "unknown"))[:32]
        values = {
            "sid": server_id,
            "name": name,
            "cmd": _text_or_none(p.get("cmdline"), 2048),
            "user": _text_or_none(p.get("user_name"), 255),
            "cpu": _f(p.get("cpu_pct")),
            "mem": max(0, _i(p.get("memory_bytes"))),
            "started": _dt_or_none(p.get("started_at")),
            "state": state,
            "running": running,
            "watchlisted": watchlisted,
        }

        # A missing watchlist member intentionally has pid=0. Multiple missing
        # names cannot share the normal (server_id, pid) primary key, so retain
        # them in the name-keyed watchlist snapshot instead of discarding them.
        if pid <= 0:
            if not watchlisted:
                continue
            await db.execute(text(
                """INSERT INTO server_process_watchlist_inventory
                       (server_id, name, cmdline, user_name, cpu_pct, memory_bytes,
                        started_at, state, running, watchlisted, updated_at)
                   VALUES (:sid, :name, :cmd, :user, :cpu, :mem,
                           :started, :state, :running, :watchlisted, NOW())
                   ON CONFLICT (server_id, name) DO UPDATE SET
                       cmdline = EXCLUDED.cmdline,
                       user_name = EXCLUDED.user_name,
                       cpu_pct = EXCLUDED.cpu_pct,
                       memory_bytes = EXCLUDED.memory_bytes,
                       started_at = EXCLUDED.started_at,
                       state = EXCLUDED.state,
                       running = EXCLUDED.running,
                       watchlisted = EXCLUDED.watchlisted,
                       updated_at = NOW()"""
            ), values)
            await db.execute(text(
                """DELETE FROM server_process_inventory
                   WHERE server_id = :sid AND lower(name) = lower(:name)"""
            ), {"sid": server_id, "name": name})
            continue

        # When the name for a PID changes the OS has recycled the PID onto a
        # different process, so reset start time / cmdline instead of carrying
        # the previous process's values forward.
        await db.execute(text(
            """INSERT INTO server_process_inventory
                   (server_id, pid, name, cmdline, user_name, cpu_pct, memory_bytes,
                    started_at, state, running, watchlisted, updated_at)
               VALUES (:sid, :pid, :name, :cmd, :user, :cpu, :mem,
                       :started, :state, :running, :watchlisted, NOW())
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
                   state = EXCLUDED.state,
                   running = EXCLUDED.running,
                   watchlisted = EXCLUDED.watchlisted,
                   updated_at = NOW()"""
        ), {**values, "pid": pid})
        # If this watched process has returned, its earlier pid=0 absence row
        # must disappear immediately rather than remain visible until expiry.
        if watchlisted:
            await db.execute(text(
                """DELETE FROM server_process_watchlist_inventory
                   WHERE server_id = :sid AND lower(name) = lower(:name)"""
            ), {"sid": server_id, "name": name})

    # Long-lived cache retention is separate from the policy-aware live-view
    # filter. This bounds PID churn without deleting valid slow-policy data.
    for table in ("server_process_inventory", "server_process_watchlist_inventory"):
        await db.execute(
            text(
                f"DELETE FROM {table} WHERE server_id = :sid "
                "AND updated_at < NOW() - make_interval(secs => :ttl)"
            ),
            {"sid": server_id, "ttl": PROCESS_INVENTORY_RETENTION_SECONDS},
        )


# ── Inventory ingest: software snapshot and identity helpers ─────────
#
# Design: Documentation/Inventory/02-ARCHITECTURE-DATA-MODEL-API.md section 3.3.
# Everything the Inventory & Compliance module adds to ingest is best effort:
# an exception in the planning and key computation of the software list, the
# snapshot ordering guard, the change budget, the freshness stamps or the inline
# identity must never fail the upload, because HostMetricStorageError makes the
# agent retry the whole batch. The existing writes keep their error behaviour.

# The Windows agent caps the software list it sends at 1,000 entries
# (maxInstalledSoftware in ZenPlus_Agent/internal/collectors/native_windows.go),
# sorted by lower-cased name, and drops the alphabetical tail. A list that
# reaches the cap may be missing titles that are still installed, so absence
# from it proves nothing for the names after its last entry.
SOFTWARE_LIST_AGENT_CAP = 1000

# The server's own bound on one posted software list. Entries beyond it are
# dropped and the snapshot counts as truncated (it is far above what the agent
# sends, so only a faulty or hostile sender reaches it).
SOFTWARE_LIST_SERVER_CAP = 5000

# Software change events one server may write per 24 hours. Past it the capture
# triggers are silenced for the software section of an upload and a single
# marker event records that history was rate limited. The state tables stay
# current; only the history is bounded, because events live for 400 days and
# survive the server.
SOFTWARE_EVENT_BUDGET_24H = 2000
BUDGET_MARKER_SUBJECT = "(change budget exceeded)"

# A snapshot older than the newest one accepted is ignored while that newest one
# was accepted less than this long ago (by the server's clock). The agent spools
# a batch for at most 24 hours, so a genuinely reordered batch cannot arrive
# later; an agent whose clock was stepped back is accepted again after the same
# time instead of staying ignored until its clock catches up.
SNAPSHOT_ORDER_WINDOW_S = 24 * 3600

# Widths of the server_software_inventory columns. install_location is
# unbounded text; its cap is only a sanity limit.
_SW_NAME_MAX = 255
_SW_VERSION_MAX = 128
_SW_VENDOR_MAX = 255
_SW_ARCH_MAX = 16
_SW_SOURCE_MAX = 32
_SW_LOCATION_MAX = 1024
_SW_NAME_KEY_MAX = 160
_SW_PUBLISHER_KEY_MAX = 96
_SW_CATEGORY_MAX = 32

# Widths of the servers columns the OS section writes.
_OS_NAME_MAX = 255
_OS_VERSION_MAX = 128
_OS_KERNEL_MAX = 128
_OS_ARCH_MAX = 32
_OS_FQDN_MAX = 255
_CPU_MODEL_MAX = 255

# ON CONFLICT refreshes everything the snapshot carries and deliberately leaves
# first_seen_at alone. version never goes from a known value to NULL: a snapshot
# whose entry carries no version (a half-written uninstall key) is a gap, and
# the capture trigger treats NULL transitions as gaps too, so the stored version
# is kept and the next real version is compared against it.
_SOFTWARE_UPSERT_SQL = text(
    """INSERT INTO server_software_inventory
           (server_id, package_name, version, vendor, install_date,
            architecture, source, install_location,
            name_key, publisher_key, category, norm_rev, updated_at)
       VALUES (:sid, :name, :ver, :vendor, :install_date,
               :arch, :source, :location,
               :name_key, :pub_key, :category, :norm_rev, NOW())
       ON CONFLICT (server_id, package_name) DO UPDATE SET
           version = COALESCE(EXCLUDED.version, server_software_inventory.version),
           vendor = EXCLUDED.vendor,
           install_date = COALESCE(EXCLUDED.install_date, server_software_inventory.install_date),
           architecture = EXCLUDED.architecture,
           source = EXCLUDED.source,
           install_location = EXCLUDED.install_location,
           name_key = EXCLUDED.name_key,
           publisher_key = EXCLUDED.publisher_key,
           category = EXCLUDED.category,
           norm_rev = EXCLUDED.norm_rev,
           updated_at = NOW()"""
)

# Rows the snapshot did not touch keep their old updated_at; everything older
# than the grace window therefore dropped out of the list.
_SOFTWARE_PRUNE_SQL = text(
    "DELETE FROM server_software_inventory "
    "WHERE server_id = :sid AND updated_at < NOW() - INTERVAL '2 minutes'"
)
_SOFTWARE_STALE_SQL = text(
    "SELECT package_name FROM server_software_inventory "
    "WHERE server_id = :sid AND updated_at < NOW() - INTERVAL '2 minutes'"
)
_SOFTWARE_PRUNE_NAMED_SQL = text(
    "DELETE FROM server_software_inventory "
    "WHERE server_id = :sid AND updated_at < NOW() - INTERVAL '2 minutes' "
    "AND package_name = ANY(CAST(:names AS varchar[]))"
)

# Run before the software rows are written so that the capture triggers, which
# read servers.software_truncated when they write an event, tag the events of a
# capped snapshot (and not those of a complete one that follows a capped one).
_TRUNCATION_PRESTAMP_SQL = text(
    "UPDATE servers SET software_truncated = :truncated "
    "WHERE id = :sid AND software_truncated IS DISTINCT FROM :truncated"
)

# One writer per server decides which snapshot is the newest. The row lock this
# UPDATE takes also serialises overlapping uploads of the same server, so a
# later upload sees the earlier one's baseline. A stored time in the future by
# more than a day is a skewed clock and never blocks the next snapshot.
_SNAPSHOT_CLAIM_SQL = text(
    """UPDATE servers SET inventory_collected_at = :ts
        WHERE id = :sid
          AND (inventory_collected_at IS NULL
               OR inventory_collected_at <= :ts
               OR inventory_collected_at > NOW() + INTERVAL '1 day'
               OR inventory_updated_at IS NULL
               OR inventory_updated_at < NOW() - make_interval(secs => :window))
        RETURNING 1"""
)

_BUDGET_COUNT_SQL = text(
    "SELECT count(*) FROM (SELECT 1 FROM inventory_change_events "
    "WHERE asset_kind = 'server' AND asset_id = :sid AND category = 'software' "
    "AND observed_at > NOW() - INTERVAL '24 hours' LIMIT :limit) recent"
)
_BUDGET_MARKER_SQL = text(
    "INSERT INTO inventory_change_events "
    "(asset_kind, asset_id, asset_name, category, action, subject, detail) "
    "SELECT 'server', s.id, s.display_name, 'software', 'changed', CAST(:subject AS varchar), "
    "jsonb_build_object('budget_exceeded', true, 'limit', CAST(:limit AS integer)) "
    "FROM servers s WHERE s.id = :sid "
    "AND NOT EXISTS (SELECT 1 FROM inventory_change_events e "
    "WHERE e.asset_kind = 'server' AND e.asset_id = s.id AND e.category = 'software' "
    "AND e.subject = CAST(:subject AS varchar) "
    "AND e.observed_at > NOW() - INTERVAL '24 hours')"
)
_SUPPRESS_GUC = "zenplus.inventory_suppress"

# inventory_identity columns written by ingest: every Identity field except
# lifecycle_slug, which the lifecycle phase owns and the normaliser never sets.
_IDENTITY_COLUMNS = (
    "vendor_key", "vendor_label", "os_family", "os_product", "os_release",
    "os_version", "os_build", "os_edition", "os_source",
    "hw_vendor", "hw_model", "hw_serial", "hw_source",
)
_IDENTITY_SELECT_SQL = text(
    "SELECT s.os_name, s.os_version, s.kernel_or_build, s.architecture, s.cpu_model, "
    "s.hw_manufacturer, s.hw_model, s.hw_serial, i.norm_rev AS id_norm_rev, "
    + ", ".join(f"i.{c} AS id_{c}" for c in _IDENTITY_COLUMNS)
    + " FROM servers s LEFT JOIN inventory_identity i"
    " ON i.asset_kind = 'server' AND i.asset_id = s.id WHERE s.id = :sid"
)
# values_rev is written together with norm_rev and in the same statement: it is
# the rule-pack revision that rendered these values, and the identity capture
# trigger stays silent when it moves, so a new pack revision re-rendering an
# unchanged OS never reads as an OS change (whether or not the row was dirty).
_IDENTITY_UPSERT_SQL = text(
    "INSERT INTO inventory_identity (asset_kind, asset_id, "
    + ", ".join(_IDENTITY_COLUMNS)
    + ", norm_rev, values_rev, normalized_at) VALUES ('server', :sid, "
    + ", ".join(f":{c}" for c in _IDENTITY_COLUMNS)
    + ", :norm_rev, :values_rev, NOW()) ON CONFLICT (asset_kind, asset_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _IDENTITY_COLUMNS)
    + ", norm_rev = EXCLUDED.norm_rev, values_rev = EXCLUDED.values_rev, normalized_at = NOW()"
)

_PACKS_DEGRADED_LOGGED = False


class _SoftwarePlan(NamedTuple):
    """One software snapshot, ready to write.

    ``rows`` holds one parameter dict per distinct stored package name (the
    primary key), ``truncated`` says the list may have lost its tail (it reached
    the agent's cap or was cut at the server's), and ``key_errors`` counts the
    entries that fell back to NULL keys. ``boundary`` is set for a truncated
    list that is in the agent's order (sorted by lower-cased name): the lower-
    cased last name, before which absence from the list proves removal.
    """

    rows: list[dict[str, Any]]
    truncated: bool
    key_errors: int
    first_key_error: Optional[BaseException]
    boundary: Optional[str] = None
    first_error_name: Optional[str] = None


def _software_snapshot_truncated(sent: int) -> bool:
    """True when the list sent may have lost its tail to the agent's cap."""
    return sent >= SOFTWARE_LIST_AGENT_CAP


def _sanitize(value: str) -> str:
    """Make agent text storable.

    PostgreSQL rejects NUL in text, and a lone surrogate (which json.loads
    accepts) cannot be encoded as UTF-8: either one would fail the whole upload,
    and the agent would retry the same poisoned batch for ever.
    """
    return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _inventory_text(v: Any, max_length: int) -> Optional[str]:
    """Clip an evidence column; None unless the agent sent text."""
    if not isinstance(v, str):
        return None
    return _text_or_none(_sanitize(v), max_length)


def _os_field(v: Any, max_length: int) -> Optional[str]:
    """A servers column of the OS section: text only, made storable and clipped."""
    return _sanitize(v)[:max_length] if isinstance(v, str) else None


def _key_text(v: Any, max_length: int) -> Optional[str]:
    return v[:max_length] if isinstance(v, str) and v else None


def _inventory_packs_usable() -> bool:
    """False while a rule pack failed to load; logs once per process."""
    global _PACKS_DEGRADED_LOGGED
    if inventory_normalize.packs_healthy():
        return True
    if not _PACKS_DEGRADED_LOGGED:
        _PACKS_DEGRADED_LOGGED = True
        logger.error(
            "inventory rule packs are degraded (%s): agent uploads leave norm_rev "
            "unset so the normalise loop re-keys everything once the packs load",
            "; ".join(inventory_normalize.PACK_ERRORS)[:500],
        )
    return False


def _natural_key(version: Optional[str]) -> tuple:
    """Total order over version text: digit runs by value, other text lower-cased (``versioning.natural_key``).

    Used only where versioning.py refuses to order two versions (``9.6p1``,
    ``1.2.3-1``). It is not a claim about which release is newer; it is a
    deterministic choice that does not depend on the order the agent listed
    the entries in.
    """
    return versioning.natural_key(version)


def _highest_entry(group: list[dict[str, Any]]) -> dict[str, Any]:
    """The entry to keep out of several that share one package name.

    semverish decides when it can order every version in the group. The
    entries it ranks equal (or all of them, when it cannot rank the group) are
    then ordered by natural key, then by the raw text, so the survivor never
    depends on registry order; exactly identical versions keep the first entry.
    """
    versions = [e["ver"] for e in group]
    try:
        best = versioning.highest("semverish", versions)
    except Exception:
        best = None
    if best is None:
        candidates = range(len(group))
    else:
        candidates = [i for i, v in enumerate(versions)
                      if versioning.compare("semverish", v, best) == 0]
    chosen = max(candidates, key=lambda i: (_natural_key(versions[i]), versions[i] or ""))
    return group[chosen]


def _collapse_duplicate_names(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per stored package name, in order of first appearance.

    The table is keyed by (server_id, package_name), so duplicates inside one
    snapshot (the same product in HKLM, WOW6432Node and HKCU) would overwrite
    each other in listing order and make the version flap between uploads.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry["name"], []).append(entry)
    return [g[0] if len(g) == 1 else _highest_entry(g) for g in groups.values()]


def _software_entries(software: list) -> list[dict[str, Any]]:
    """Valid entries of a posted software list as upsert parameters, unkeyed.

    Accepts the same aliases as the original ingest and clips values to the
    same widths. Every string is made storable first (see ``_sanitize``).
    """
    entries: list[dict[str, Any]] = []
    for app in software:
        if not isinstance(app, dict):
            continue
        name = _sanitize(
            str(app.get("package_name") or app.get("name") or app.get("display_name") or "")).strip()
        if not name:
            continue
        entries.append({
            "name": name[:_SW_NAME_MAX],
            "ver": _sanitize(str(app.get("version") or app.get("display_version") or ""))[:_SW_VERSION_MAX] or None,
            "vendor": _sanitize(str(app.get("vendor") or app.get("publisher") or ""))[:_SW_VENDOR_MAX] or None,
            "install_date": _dt_or_none(app.get("install_date")),
            "arch": _inventory_text(app.get("architecture"), _SW_ARCH_MAX),
            "source": _inventory_text(app.get("source"), _SW_SOURCE_MAX),
            "location": _inventory_text(app.get("install_location"), _SW_LOCATION_MAX),
        })
    return entries


def _go_lower(name: str) -> str:
    """strings.ToLower of the Go agent: simple per-rune lower casing, no locale.

    Python's str.lower() differs in two places (the Turkish dotted capital I
    expands to two code points, and a final sigma is context sensitive), and
    PostgreSQL's lower() depends on the database locale; the order the agent
    sorted by is this one, compared in code-point order (Go compares bytes of
    UTF-8, which orders the same way).
    """
    return "".join("i" if ch == "\u0130" else ch.lower() for ch in name)


def _prune_boundary(entries: list[dict[str, Any]]) -> Optional[str]:
    """Lower-cased last name of a list that is in the agent's order, else None.

    The agent sorts by lower-cased name and cuts the tail, so for a capped list
    everything that sorts before its last name is complete. A list that is not
    in that order was not produced by the agent's cap, and nothing can be said
    about what it left out.
    """
    lowered = [_go_lower(e["name"]) for e in entries]
    if not lowered or any(a > b for a, b in zip(lowered, lowered[1:])):
        return None
    return lowered[-1]


def _plan_software(software: list, norm_rev: Optional[int]) -> _SoftwarePlan:
    """Parse, dedupe and key one software snapshot.

    A malformed entry is skipped and an entry whose key computation raises keeps
    NULL keys and a NULL norm_rev (the normalise loop retries it); neither
    raises. ``norm_rev`` is what to stamp on keyed rows: the current rule-pack
    revision, or None while the packs are degraded so the normalise loop re-keys
    them. The list is cut at ``SOFTWARE_LIST_SERVER_CAP`` entries and counts as
    truncated then. Runs on a worker thread: it is CPU work that must not stall
    the event loop (about 30 microseconds per entry).
    """
    over_cap = len(software) > SOFTWARE_LIST_SERVER_CAP
    if over_cap:
        software = software[:SOFTWARE_LIST_SERVER_CAP]
    entries = _software_entries(software)
    rows = _collapse_duplicate_names(entries)
    key_errors = 0
    first_error: Optional[BaseException] = None
    first_name: Optional[str] = None
    for row in rows:
        try:
            key = inventory_normalize.normalize_software(
                row["name"], row["vendor"], row["ver"], source=row["source"], strict=True)
            row["name_key"] = _key_text(key.name_key, _SW_NAME_KEY_MAX)
            row["pub_key"] = _key_text(key.publisher_key, _SW_PUBLISHER_KEY_MAX)
            row["category"] = _key_text(key.category, _SW_CATEGORY_MAX)
            row["norm_rev"] = norm_rev if row["name_key"] else None
        except Exception as exc:
            key_errors += 1
            first_error = first_error or exc
            first_name = first_name or row["name"]
            row.update(name_key=None, pub_key=None, category=None, norm_rev=None)
    truncated = over_cap or _software_snapshot_truncated(len(software))
    # A list cut at the server's cap was cut in listing order, not in the
    # agent's: only a list the agent capped itself has a boundary.
    boundary = _prune_boundary(entries) if truncated and not over_cap else None
    return _SoftwarePlan(rows, truncated, key_errors, first_error, boundary, first_name)


def _unkeyed_plan(software: list, error: BaseException) -> _SoftwarePlan:
    """Last resort when planning itself failed: the entries as sent, without keys.

    Duplicate names keep their first entry (no version ordering is attempted)
    and no boundary is claimed, so a capped list prunes nothing. The failure is
    logged by the caller, hence ``key_errors`` stays 0.
    """
    over_cap = len(software) > SOFTWARE_LIST_SERVER_CAP
    kept: dict[str, dict[str, Any]] = {}
    for entry in _software_entries(software[:SOFTWARE_LIST_SERVER_CAP]):
        kept.setdefault(entry["name"], entry)
    rows = [dict(e, name_key=None, pub_key=None, category=None, norm_rev=None) for e in kept.values()]
    truncated = over_cap or _software_snapshot_truncated(len(software))
    return _SoftwarePlan(rows, truncated, 0, error)


async def _plan_software_safely(server_id: str, software: list) -> Optional[_SoftwarePlan]:
    """The software plan of an upload; whatever goes wrong, the upload goes on.

    Any exception while checking the packs or planning gives the entries without
    keys (norm_rev NULL, so the normalise loop keys them later) and one log
    line. None means the list could not even be parsed: the software section is
    then left out of this upload.
    """
    try:
        norm_rev = inventory_normalize.RULEPACK_REV if _inventory_packs_usable() else None
        return await asyncio.to_thread(_plan_software, software, norm_rev)
    except Exception as exc:
        logger.exception(
            "Inventory planning failed for the %d-entry software list of server %s; "
            "rows are written without keys for the normalise loop", len(software), server_id)
        try:
            return _unkeyed_plan(software, exc)
        except Exception:
            logger.exception(
                "Software list of server %s could not be parsed; software section skipped", server_id)
            return None


def _snapshot_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _claim_snapshot(server_id: str, db: AsyncSession, collected_at: datetime) -> bool:
    """Record this snapshot as the newest, or report that a newer one was accepted.

    False means the snapshot is older than the newest accepted one, so writing
    its software, OS and identity would rewind the tables and make history in
    both directions. Equal times are accepted (a retry of the same batch is
    idempotent). On any failure the snapshot is accepted: ordering is a
    safeguard, never a reason to fail or drop an upload.
    """
    try:
        async with db.begin_nested():
            claimed = (await db.execute(_SNAPSHOT_CLAIM_SQL, {
                "sid": server_id, "ts": collected_at, "window": SNAPSHOT_ORDER_WINDOW_S,
            })).first()
    except Exception:
        logger.exception(
            "Inventory snapshot ordering check failed for server %s; accepting the snapshot", server_id)
        return True
    if claimed is None:
        logger.info(
            "Inventory snapshot of server %s collected at %s is older than one already accepted; "
            "software, OS and identity sections skipped", server_id, collected_at.isoformat())
        return False
    return True


async def _stamp_truncation_first(server_id: str, db: AsyncSession, truncated: bool) -> None:
    """Set servers.software_truncated to this snapshot's value before its rows are written.

    The capture triggers read the flag when they write an event, so this is what
    tags the events of a capped snapshot with snapshot_truncated, and what stops
    a complete snapshot that follows a capped one from inheriting the tag.
    """
    try:
        async with db.begin_nested():
            await db.execute(_TRUNCATION_PRESTAMP_SQL, {"sid": server_id, "truncated": truncated})
    except Exception:
        logger.exception("Inventory truncation flag stamp failed for server %s", server_id)


async def _silence_events_over_budget(server_id: str, db: AsyncSession) -> tuple[bool, Optional[str]]:
    """Silence software capture for the rest of the transaction when the server is over budget.

    Returns (silenced, previous value of the suppress setting). Over budget
    means SOFTWARE_EVENT_BUDGET_24H software events in the last 24 hours; the
    first time it happens in 24 hours one marker event says so (and one warning
    is logged; later uploads of the same storm log at info). Any failure leaves
    capture as it was.
    """
    try:
        async with db.begin_nested():
            recent = (await db.execute(
                _BUDGET_COUNT_SQL, {"sid": server_id, "limit": SOFTWARE_EVENT_BUDGET_24H})).scalar() or 0
            if recent < SOFTWARE_EVENT_BUDGET_24H:
                return False, None
            marker = await db.execute(_BUDGET_MARKER_SQL, {
                "sid": server_id, "subject": BUDGET_MARKER_SUBJECT, "limit": SOFTWARE_EVENT_BUDGET_24H})
            first = bool(marker.rowcount)
            previous = (await db.execute(
                text("SELECT current_setting(:name, true)"), {"name": _SUPPRESS_GUC})).scalar()
            await db.execute(
                text("SELECT set_config(:name, 'on', true)"), {"name": _SUPPRESS_GUC})
    except Exception:
        logger.exception("Inventory change budget check failed for server %s", server_id)
        return False, None
    (logger.warning if first else logger.info)(
        "Server %s reached %d software change events in 24 hours; its software changes in this "
        "upload are not recorded", server_id, SOFTWARE_EVENT_BUDGET_24H)
    return True, previous


async def _restore_capture(db: AsyncSession, previous: Optional[str]) -> None:
    try:
        async with db.begin_nested():
            await db.execute(
                text("SELECT set_config(:name, :value, true)"),
                {"name": _SUPPRESS_GUC, "value": previous or "off"})
    except Exception:
        logger.exception("Could not restore inventory change capture after the software section")


@contextlib.asynccontextmanager
async def _software_change_budget(server_id: str, db: AsyncSession):
    """Bound the history one server can write: silences capture for the body when over budget.

    The previous setting is restored afterwards, also when the body raises, so
    the identity events of the same upload are never affected.
    """
    silenced, previous = await _silence_events_over_budget(server_id, db)
    try:
        yield
    finally:
        if silenced:
            await _restore_capture(db, previous)


async def _prune_software(server_id: str, db: AsyncSession, plan: _SoftwarePlan) -> None:
    """Delete the stored rows the snapshot proves gone.

    A complete list proves every absent row gone. A list the agent capped only
    proves it for names that sort before its last entry (the agent cut the tail
    of its sorted list), compared in the agent's order, strictly: a stored row
    that lower-cases to the very last name might have been cut with its sibling.
    A capped list that is not in the agent's order, or was cut by the server,
    proves nothing and prunes nothing.
    """
    if not plan.truncated:
        await db.execute(_SOFTWARE_PRUNE_SQL, {"sid": server_id})
    elif plan.boundary is not None:
        stale = (await db.execute(_SOFTWARE_STALE_SQL, {"sid": server_id})).all()
        gone = [name for (name,) in stale if _go_lower(name) < plan.boundary]
        if gone:
            await db.execute(_SOFTWARE_PRUNE_NAMED_SQL, {"sid": server_id, "names": gone})


async def _stamp_inventory(server_id: str, db: AsyncSession, plan: Optional[_SoftwarePlan]) -> None:
    """Freshness, first-snapshot and truncation markers on the servers row.

    Runs after the software upserts, so the first snapshot of a server inserts
    its rows while inventory_baselined_at is still NULL (the capture triggers
    stay silent) and every later snapshot diffs normally. ``plan`` is the
    processed non-empty software snapshot, or None when only an OS section
    arrived, which stamps freshness alone.
    """
    assignments = ["inventory_updated_at = NOW()"]
    params: dict[str, Any] = {"sid": server_id}
    if plan is not None:
        assignments.append("inventory_baselined_at = COALESCE(inventory_baselined_at, NOW())")
        assignments.append("software_truncated = :truncated")
        params["truncated"] = plan.truncated
    try:
        async with db.begin_nested():
            await db.execute(
                text(f"UPDATE servers SET {', '.join(assignments)} WHERE id = :sid"), params)
    except Exception:
        logger.exception("Inventory freshness stamp failed for server %s", server_id)


async def _refresh_server_identity(server_id: str, db: AsyncSession) -> None:
    """Normalise the server's raw OS/hardware columns into inventory_identity.

    Runs in its own savepoint at the very end of the upload. The servers UPDATE
    triggers have already marked the identity row dirty (norm_rev NULL); writing
    the result is what cleans it, and the identity capture trigger turns a real
    os_version change into an event. An unchanged identity is not rewritten. The
    write sets values_rev with norm_rev, so a rule-pack revision change that only
    re-renders the same OS never reads as a change. The normaliser runs strict:
    if it fails, the row stays dirty and the normalise loop repairs it, instead
    of storing a fallback identity as clean.
    """
    try:
        if not _inventory_packs_usable():
            return
        async with db.begin_nested():
            row = (await db.execute(_IDENTITY_SELECT_SQL, {"sid": server_id})).mappings().first()
            if row is None:
                return
            identity = inventory_normalize.normalize_server(
                row["os_name"], row["os_version"], row["kernel_or_build"], row["architecture"],
                cpu_model=row["cpu_model"], hw_manufacturer=row["hw_manufacturer"],
                hw_model=row["hw_model"], hw_serial=row["hw_serial"], strict=True,
            )
            computed = dataclasses.asdict(identity)
            values = {c: computed[c] for c in _IDENTITY_COLUMNS}
            stored = {c: row[f"id_{c}"] for c in _IDENTITY_COLUMNS}
            if row["id_norm_rev"] == inventory_normalize.RULEPACK_REV and stored == values:
                return
            await db.execute(_IDENTITY_UPSERT_SQL, {
                "sid": server_id, "norm_rev": inventory_normalize.RULEPACK_REV,
                "values_rev": inventory_normalize.RULEPACK_REV, **values})
    except Exception:
        logger.exception(
            "Inline inventory identity failed for server %s; row left for the normalise loop",
            server_id)


async def _upsert_inventory(
    server_id: str,
    inv: Dict[str, Any],
    db: AsyncSession,
    *,
    collected_at: Optional[datetime] = None,
) -> None:
    """Write one inventory snapshot.

    ``collected_at`` is the agent-side time of the snapshot. With it, a software
    or OS section older than the newest snapshot already accepted for the server
    is skipped (the other sections still apply); without it nothing is compared.
    """
    software = inv.get("software") or inv.get("applications") or []
    os_section = inv.get("os")
    os_info: Dict[str, Any] = os_section if isinstance(os_section, dict) else {}

    plan: Optional[_SoftwarePlan] = None
    if isinstance(software, list) and software:
        plan = await _plan_software_safely(server_id, software)
    # A software list that held no usable entry counts as empty, and a list that
    # reached the agent's cap is incomplete: neither proves everything is gone.
    software_rows = plan.rows if plan is not None else []

    # Before anything is written: is this snapshot the newest one? A stale
    # software or OS section would rewind the tables and write history in both
    # directions. Hardware, services, filesystems and NICs keep their behaviour.
    current = True
    snapshot_time = _snapshot_time(collected_at)
    if snapshot_time is not None and (software_rows or os_info):
        current = await _claim_snapshot(server_id, db, snapshot_time)

    hardware = inv.get("hardware")
    if isinstance(hardware, dict):
        cpu = hardware.get("cpu") if isinstance(hardware.get("cpu"), dict) else {}
        memory = hardware.get("memory") if isinstance(hardware.get("memory"), dict) else {}
        raw_disks = hardware.get("physical_disks")
        disks: list[dict[str, Any]] = []
        if isinstance(raw_disks, list):
            for raw in raw_disks[:64]:
                if not isinstance(raw, dict):
                    continue
                disks.append({
                    "index": _i(raw.get("index")),
                    "device_id": str(raw.get("device_id") or "")[:255],
                    "model": str(raw.get("model") or "")[:255],
                    "manufacturer": str(raw.get("manufacturer") or "")[:255],
                    "interface_type": str(raw.get("interface_type") or "")[:64],
                    "media_type": str(raw.get("media_type") or "")[:64],
                    "size_bytes": max(0, _i(raw.get("size_bytes"))),
                    "status": str(raw.get("status") or "")[:64],
                })
        cpu_model = _inventory_text(cpu.get("model"), _CPU_MODEL_MAX)
        logical_count = max(0, _i(cpu.get("logical_count"))) or None
        physical_count = max(0, _i(cpu.get("physical_count"))) or None
        memory_total = max(0, _i(memory.get("total_physical_bytes"))) or None
        has_disks = isinstance(raw_disks, list)
        if cpu_model or logical_count or physical_count or memory_total or has_disks:
            await db.execute(text(
                """UPDATE servers SET
                       cpu_model = COALESCE(:cpu_model, cpu_model),
                       cpu_cores = COALESCE(:cpu_cores, cpu_cores),
                       cpu_physical_cores = COALESCE(:physical_cores, cpu_physical_cores),
                       memory_total_bytes = COALESCE(:memory_total, memory_total_bytes),
                       physical_disks = CASE WHEN :has_disks
                                            THEN CAST(:physical_disks AS jsonb)
                                            ELSE physical_disks END,
                       updated_at = NOW()
                   WHERE id = :sid"""
            ), {
                "sid": server_id,
                "cpu_model": cpu_model,
                "cpu_cores": logical_count,
                "physical_cores": physical_count,
                "memory_total": memory_total,
                "has_disks": has_disks,
                "physical_disks": json.dumps(disks),
            })

    if current and os_info:
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

    if software_rows and current:
        if plan.key_errors:
            logger.error(
                "Inventory keys not computed for %d of %d software entries on server %s "
                "(first failing title %.100r); rows left unkeyed for the normalise loop",
                plan.key_errors, len(plan.rows), server_id, plan.first_error_name,
                exc_info=plan.first_key_error,
            )
        await _stamp_truncation_first(server_id, db, plan.truncated)
        async with _software_change_budget(server_id, db):
            for row in plan.rows:
                await db.execute(_SOFTWARE_UPSERT_SQL, {"sid": server_id, **row})
            await _prune_software(server_id, db, plan)

    # Prune rows the snapshot no longer contains. Each section above stamps
    # updated_at = NOW() for everything present, so anything older than a
    # short grace window was dropped by the agent (volume unmounted, NIC
    # removed, service gone; software was handled above, where the truncation
    # rules apply). Without this, dead mounts keep driving disk health. Only
    # prune when the section was actually sent - an omitted section means
    # "no update", not "empty".
    prune_map = [
        (services, "server_service_inventory"),
        (filesystems, "server_filesystem_inventory"),
        (interfaces, "server_network_interface_inventory"),
    ]
    for sent, table in prune_map:
        if isinstance(sent, list) and sent:
            await db.execute(
                text(f"DELETE FROM {table} "
                     "WHERE server_id = :sid AND updated_at < NOW() - INTERVAL '2 minutes'"),
                {"sid": server_id},
            )

    # OS info
    if current and os_info:
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
            "n": _os_field(os_info.get("name"), _OS_NAME_MAX),
            "v": _os_field(os_info.get("version"), _OS_VERSION_MAX),
            "k": _os_field(os_info.get("kernel_or_build"), _OS_KERNEL_MAX),
            "a": _os_field(os_info.get("architecture"), _OS_ARCH_MAX),
            "f": _os_field(os_info.get("fqdn"), _OS_FQDN_MAX),
            "sid": server_id,
        })

    # Inventory freshness and identity, after every write above (see
    # _stamp_inventory). Uploads with neither a software nor an OS section are
    # not inventory snapshots and skip this, and so does a snapshot that was
    # found to be stale.
    if current and (software_rows or os_info):
        await _stamp_inventory(server_id, db, plan if software_rows else None)
        await _refresh_server_identity(server_id, db)


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

# ``agent_health`` describes the uploader itself (queue, spool and errors), not
# the monitored Windows host.  In particular, receiving it must not make an
# otherwise empty Server module look healthy or advance ``last_metric_at``.
HOST_TELEMETRY_KINDS = frozenset(KIND_INSERTERS) - {"agent_health"}
HEALTH_TELEMETRY_KINDS = frozenset({"cpu", "memory", "filesystem"})


def _correct_agent_clock_skew(
    metric_rows: list[dict],
    sent_at: datetime,
    agent_id: str,
    *,
    received_at: datetime | None = None,
) -> int:
    """Correct host clock skew without turning spool age into clock skew.

    ``sent_at`` is refreshed immediately before every upload, including a
    replay from the on-disk spool.  Comparing the newest sample to wall clock
    used to shift every delayed batch to "now" after an appliance outage or
    upgrade.  That destroyed the historical timestamps precisely when the
    spool was doing its job.  The transport timestamp is the clock probe; the
    sample timestamps retain their original spacing and age.
    """
    now = _ts(received_at or datetime.now(timezone.utc))
    agent_sent_at = _ts(sent_at)
    offset = now - agent_sent_at
    clock_skew_s = -int(offset.total_seconds())
    if metric_rows and abs(offset.total_seconds()) > CLOCK_SKEW_TOLERANCE_S:
        for row in metric_rows:
            row["timestamp"] = _ts(row.get("timestamp")) + offset
        logger.warning(
            "clock skew of %+ds corrected for agent=%s "
            "(using batch sent_at; sample age preserved)",
            clock_skew_s,
            agent_id,
        )
    return clock_skew_s


def _insert_metric_groups(
    agent_id: str,
    server_id: str,
    batch_id: str,
    by_kind: dict[str, list[dict]],
) -> int:
    """Persist one batch on a worker thread with that thread's CH session.

    ``clickhouse_connect`` sessions reject concurrent operations.  Host ingest
    previously ran up to nine blocking inserts on the asyncio event loop using
    one process-global session, which could stall heartbeats and APM traffic
    while fleets replayed their spools after an appliance restart.
    """
    client = get_ch_client()
    accepted = 0
    for kind, inserter in KIND_INSERTERS.items():
        rows = by_kind.get(kind) or []
        if not rows:
            continue
        try:
            # A failure after an earlier kind committed returns 503 and replays
            # the whole batch.  Every kind therefore needs a deterministic
            # block token; migrate-094 enables the corresponding deduplication
            # window on these non-replicated MergeTree tables.
            token = hashlib.sha256(
                f"zenplus-host-v1\0{agent_id}\0{batch_id}\0{kind}".encode("utf-8")
            ).hexdigest()
            accepted += inserter(
                _DeduplicatingInsertClient(client, token),
                agent_id,
                server_id,
                rows,
            )
        except Exception as exc:
            logger.exception("ClickHouse insert failed for kind=%s", kind)
            raise HostMetricStorageError(kind) from exc
    return accepted


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

    # Clock-skew correction uses the transmit timestamp, not the newest metric.
    # A metric can legitimately be hours old after an appliance upgrade while
    # ``sent_at`` still reflects the agent's current wall clock.
    metric_rows = [r for k, rows in by_kind.items() if k != "inventory" for r in rows]
    clock_skew_s = _correct_agent_clock_skew(
        metric_rows, batch.sent_at, agent_id,
    )

    # Keep blocking ClickHouse I/O off the API event loop. A storage/schema
    # failure is retryable and must not be reported as a final per-sample
    # rejection: the Windows agent deliberately acknowledges and removes every
    # batch that receives a successful HTTP response with rejected samples.
    accepted = await asyncio.to_thread(
        _insert_metric_groups, agent_id, server_id, batch.batch_id, by_kind,
    )

    if by_kind.get("process"):
        try:
            await _upsert_process_inventory(server_id, by_kind["process"], db)
        except Exception as exc:
            logger.exception("Process inventory upsert failed")
            raise HostMetricStorageError("process_inventory") from exc

    # Inventory snapshot (either from sample kind or top-level field). The time
    # that orders snapshots is the agent's own clock when it assembled the data:
    # batch.collected_at for the top-level field (the agent keeps it stable when
    # a spooled batch is replayed, whereas sent_at is refreshed on every upload
    # attempt and cannot tell an old snapshot from a new one), and the sample's
    # own timestamp for an inventory metric. Neither goes through the clock-skew
    # correction above, so all of one agent's snapshots stay on one clock.
    software_updated = False
    if batch.inventory:
        try:
            await _upsert_inventory(
                server_id, batch.inventory, db, collected_at=batch.collected_at)
            software_updated = software_updated or bool(
                batch.inventory.get("software") or batch.inventory.get("applications"))
        except Exception as exc:
            logger.exception("Inventory upsert failed")
            raise HostMetricStorageError("inventory") from exc

    for row in by_kind.get("inventory", []):
        try:
            await _upsert_inventory(server_id, row, db, collected_at=row.get("timestamp"))
            software_updated = software_updated or bool(
                row.get("software") or row.get("applications"))
        except Exception as exc:
            logger.exception("Inventory metric upsert failed")
            raise HostMetricStorageError("inventory") from exc

    # Health is only authoritative when this batch contains an input used by
    # the health calculation.  An agent-health-only batch used to default the
    # server to healthy even when it had never delivered CPU or memory data.
    if HEALTH_TELEMETRY_KINDS.intersection(by_kind):
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


def telemetry_freshness_seconds(
    metric_interval_s: Any,
    upload_interval_s: Any,
    *,
    minimum_seconds: int = 300,
) -> int:
    """Freshness horizon that covers one collection plus one upload cycle."""
    metric = max(5, min(_i(metric_interval_s, 30), 3600))
    upload = max(5, min(_i(upload_interval_s, 60), 3600))
    return max(minimum_seconds, metric + upload + 60)


def query_fleet_latest_metrics(
    window_minutes: int = 10,
    freshness_seconds_by_server: Optional[dict[str, int]] = None,
) -> dict[str, dict]:
    """Current cpu/mem/disk/net per server for the inventory list.

    One current/summary value per server over the last ``window_minutes`` —
    cheap enough to refresh on every list poll (the raw tables are ordered by
    server and timestamp).
    """
    client = get_clickhouse_client()
    out: dict[str, dict] = {}

    def _merge(rows, key):
        for r in rows:
            sid = str(r[0])
            out.setdefault(sid, {})[key] = float(r[1] or 0)

    # Keep each signal independent. A missing/newly-migrating table must not
    # blank every other KPI on the server page. CPU and memory are snapshots,
    # so use the newest sample rather than a ten-minute average labelled as
    # the current value.
    now = datetime.now(timezone.utc)
    scopes: list[tuple[int, Optional[tuple[str, ...]]]]
    if freshness_seconds_by_server:
        grouped: dict[int, list[str]] = {}
        for server_id, seconds in freshness_seconds_by_server.items():
            bounded = max(60, min(_i(seconds, window_minutes * 60), 7260))
            grouped.setdefault(bounded, []).append(str(server_id))
        scopes = [
            (seconds, tuple(server_ids))
            for seconds, server_ids in grouped.items()
        ]
    else:
        scopes = [(max(60, window_minutes * 60), None)]

    for freshness_s, server_ids in scopes:
        since = (now - timedelta(seconds=freshness_s)).strftime("%Y-%m-%d %H:%M:%S")
        scope_sql = " AND toString(server_id) IN %(ids)s" if server_ids else ""
        params: dict[str, Any] = {"s": since}
        if server_ids:
            params["ids"] = server_ids
        queries = [
            (
                "cpu_pct",
                f"""SELECT server_id, argMax(cpu_total_pct, timestamp)
                    FROM zenplus.host_cpu_metrics
                    WHERE timestamp >= %(s)s{scope_sql} GROUP BY server_id""",
            ),
            (
                "memory_pct",
                f"""SELECT server_id, argMax(used_pct, timestamp)
                    FROM zenplus.host_memory_metrics
                    WHERE timestamp >= %(s)s{scope_sql} GROUP BY server_id""",
            ),
            (
                "disk_max_pct",
                f"""SELECT server_id, max(used_pct) FROM zenplus.host_filesystem_metrics
                    WHERE timestamp >= %(s)s{scope_sql} AND {ch_capacity_filter()}
                    GROUP BY server_id""",
            ),
            (
                "net_bps",
                f"""SELECT server_id, sum(rx_bytes_ps + tx_bytes_ps) / uniqExact(timestamp)
                    FROM zenplus.host_network_metrics
                    WHERE timestamp >= %(s)s{scope_sql} GROUP BY server_id""",
            ),
        ]
        for key, sql in queries:
            try:
                _merge(client.query(sql, parameters=params).result_rows, key)
            except Exception as exc:
                logger.warning("fleet latest %s query failed: %s", key, exc)
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


def query_latest_process_snapshot(
    server_id: str,
    window_minutes: int = 5,
    limit: int = 200,
) -> list[dict]:
    """Rebuild a current process snapshot from raw metrics.

    PostgreSQL is the normal fast path for the process tab. This fallback
    covers upgraded controllers that already have ClickHouse process samples
    but missed the inventory upsert, instead of presenting an empty process
    table while history data exists.
    """
    client = get_clickhouse_client()
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        rows = client.query(
            """SELECT pid,
                      process_name AS name,
                      argMax(cmdline, timestamp) AS cmdline,
                      argMax(user_name, timestamp) AS user_name,
                      argMax(cpu_pct, timestamp) AS cpu_pct,
                      argMax(memory_bytes, timestamp) AS memory_bytes,
                      argMax(started_at, timestamp) AS started_at,
                      argMax(state, timestamp) AS state,
                      argMax(running, timestamp) AS running,
                      argMax(watchlisted, timestamp) AS watchlisted,
                      max(timestamp) AS updated_at
               FROM zenplus.host_process_metrics
               WHERE server_id = %(sid)s AND timestamp >= %(s)s
                 AND notEmpty(process_name)
               GROUP BY pid, process_name
               ORDER BY (pid <= 0 AND watchlisted = 1) DESC, cpu_pct DESC
               LIMIT %(limit)s""",
            parameters={
                "sid": server_id,
                "s": since,
                # Reserve room for explicit pid=0 watchlist states in addition
                # to the requested top running-process slice.
                "limit": max(2, min(limit * 2, 400)),
            },
        ).result_rows
    except Exception as exc:
        logger.warning("latest process snapshot query failed for %s: %s", server_id, exc)
        return []

    snapshots = [
        {
            "pid": int(row[0]),
            "name": str(row[1]),
            "cmdline": str(row[2] or "") or None,
            "user_name": str(row[3] or "") or None,
            "cpu_pct": float(row[4] or 0),
            "memory_bytes": int(row[5] or 0),
            "started_at": _utc(row[6]) if row[6] is not None else None,
            "state": str(row[7] or "unknown"),
            "running": bool(row[8]),
            "watchlisted": bool(row[9]),
            "updated_at": _utc(row[10]),
        }
        for row in rows
    ]

    # A watched name can transition between a real PID and a pid=0
    # ``not_running`` sample inside the query window. Keep the newest state for
    # that name so fallback mode never renders both "running" and "missing".
    newest_by_name: dict[str, dict] = {}
    for item in snapshots:
        key = item["name"].casefold()
        newest = newest_by_name.get(key)
        if (
            newest is None
            or item["updated_at"] > newest["updated_at"]
            or (
                item["updated_at"] == newest["updated_at"]
                and item["running"] and not newest["running"]
            )
        ):
            newest_by_name[key] = item
    current = [
        item for item in snapshots
        if (
            (item["pid"] > 0 and newest_by_name[item["name"].casefold()]["running"])
            or (item["pid"] <= 0 and not newest_by_name[item["name"].casefold()]["running"])
        )
    ]
    running = [item for item in current if item["pid"] > 0][:limit]
    missing = [item for item in current if item["pid"] <= 0]
    return running + missing


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
