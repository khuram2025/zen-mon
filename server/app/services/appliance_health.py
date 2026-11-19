"""The appliance's own health: CPU, memory, load, services and data stores.

Settings -> System status shows this beside storage. Everything here is
read-only and cheap:

* CPU, memory, swap, load and uptime come from ``/proc``;
* service state, memory, CPU time and restart counts come from one
  ``systemctl show`` call, which needs no privilege;
* the three data stores (PostgreSQL, ClickHouse, Redis) are asked directly,
  each with a short deadline, so "the unit is active but does not answer" is
  reported as down.

A reading that cannot be taken is ``None`` and is shown as unavailable. It is
never replaced by zero.

History: one sample a minute is kept in Redis for 24 hours (a sorted set,
about 150 KiB). Every API worker measures CPU over its own minute; the worker
that claims the minute writes the sample, so there is one row per minute
whatever the worker count. Without Redis the page still shows current values
and says that history is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

PROC = Path("/proc")

SAMPLE_INTERVAL_S = 60
HISTORY_HOURS = 24
HISTORY_BUCKET_S = 300
HISTORY_KEY = "zenplus:appliance_health:samples:v1"
HISTORY_CLAIM_KEY = "zenplus:appliance_health:claim:"
# A current reading older than this is not "now" any more.
SAMPLE_FRESH_S = 150

# When a reading is worth a line at the top of the page. Deliberately few and
# conservative: this page explains the appliance, it does not page anyone.
MEMORY_WARN_PCT = 90.0
MEMORY_CRIT_PCT = 97.0
SWAP_WARN_PCT = 50.0
LOAD_PER_CORE_WARN = 1.5
CPU_SUSTAINED_WARN_PCT = 90.0
IOWAIT_SUSTAINED_WARN_PCT = 25.0
SUSTAINED_WINDOW_S = 900

PROBE_TIMEOUT_S = 4.0
STATUS_CACHE_S = 5.0


# ── CPU ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CpuTimes:
    """Aggregate jiffies of the first ``cpu`` line of /proc/stat."""
    total: int
    idle: int
    iowait: int
    steal: int


def parse_cpu_times(stat_text: str) -> Optional[CpuTimes]:
    for line in stat_text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        try:
            values = [int(v) for v in parts[1:]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        # user nice system idle iowait irq softirq steal [guest guest_nice]
        # guest time is already inside user/nice, so only the first eight count.
        values = (values + [0] * 8)[:8]
        return CpuTimes(total=sum(values), idle=values[3], iowait=values[4], steal=values[7])
    return None


def cpu_usage(prev: Optional[CpuTimes], cur: Optional[CpuTimes]) -> Optional[dict]:
    """Share of the interval the CPUs were busy, waiting for disk, or stolen
    by the hypervisor. None when either reading is missing or the counters
    did not advance (or went backwards: a reboot between readings)."""
    if prev is None or cur is None:
        return None
    total = cur.total - prev.total
    idle = cur.idle - prev.idle
    iowait = cur.iowait - prev.iowait
    steal = cur.steal - prev.steal
    if total <= 0 or min(idle, iowait, steal) < 0:
        return None
    busy = total - idle - iowait

    def pct(v: int) -> float:
        return round(max(0.0, min(100.0, v * 100.0 / total)), 1)

    return {"usage_pct": pct(busy), "iowait_pct": pct(iowait), "steal_pct": pct(steal)}


def read_cpu_times(proc: Path = PROC) -> Optional[CpuTimes]:
    try:
        return parse_cpu_times((proc / "stat").read_text())
    except OSError:
        return None


def cpu_model(proc: Path = PROC) -> Optional[str]:
    try:
        for line in (proc / "cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip() or None
    except (OSError, IndexError):
        pass
    return None


# ── Memory ───────────────────────────────────────────────────────────────────

def parse_meminfo(text: str) -> dict:
    """Memory and swap in bytes. "Used" is total minus *available* (what the
    kernel says programs can still get), so page cache does not count as used.
    Every field is None when its source line is missing."""
    kb: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields:
            try:
                kb[name.strip()] = int(fields[0])
            except ValueError:
                continue

    def b(key: str) -> Optional[int]:
        return kb[key] * 1024 if key in kb else None

    total, available = b("MemTotal"), b("MemAvailable")
    used = total - available if total is not None and available is not None else None
    swap_total, swap_free = b("SwapTotal"), b("SwapFree")
    swap_used = swap_total - swap_free if swap_total is not None and swap_free is not None else None
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "used_pct": round(used * 100.0 / total, 1) if used is not None and total else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        # No swap configured is a fact, not a missing reading: 0 of 0.
        "swap_used_pct": (round(swap_used * 100.0 / swap_total, 1)
                          if swap_used is not None and swap_total else None),
    }


def read_memory(proc: Path = PROC) -> dict:
    try:
        return parse_meminfo((proc / "meminfo").read_text())
    except OSError:
        return parse_meminfo("")


def read_uptime(proc: Path = PROC) -> Optional[float]:
    try:
        return float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_load() -> Optional[list[float]]:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def read_disk(paths: Iterable[str] = ("/data", "/")) -> Optional[dict]:
    """Usage of the filesystem behind /data (the data volume, whatever backs
    it), as ``df`` reports it: used / (used + available to the service)."""
    for path in paths:
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        if used + avail <= 0:
            continue
        return {"used_bytes": used, "free_bytes": avail,
                "used_pct": round(used * 100.0 / (used + avail), 1)}
    return None


# ── Services ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ServiceSpec:
    id: str             # generic, shown in the API: never the name of the software
    label: str          # what it does, in product words: the only name the page shows
    units: tuple[str, ...] = ()   # systemd unit names or patterns
    probe: Optional[str] = None   # a data store asked directly
    core: bool = True   # monitoring stops without it


# The page and the API name services by what they do. Which software does it
# (the database engines, the web server) is an implementation detail and is
# not part of the response: ids, labels and messages stay generic.
SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("api", "Web application", ("zenplus-api.service",)),
    ServiceSpec("monitoring_engine", "Monitoring engine", ("zenplus-poller.service",)),
    ServiceSpec("config_db", "Configuration database", ("postgresql@*.service",), probe="postgresql"),
    # Runs in a container; it is asked directly, not through a unit.
    ServiceSpec("metrics_db", "Metrics database", (), probe="clickhouse"),
    ServiceSpec("cache", "Cache and live updates", ("redis-server.service",), probe="redis"),
    ServiceSpec("web_server", "Web server", ("nginx.service",)),
    ServiceSpec("storage_protection", "Storage protection",
                ("zenplus-storage-watchdog.service",), core=False),
    ServiceSpec("flow_collector", "Flow collector",
                ("zenplus-netflow-collector.service",), core=False),
    ServiceSpec("syslog_receiver", "Syslog receiver", ("zenplus-syslog.service",), core=False),
    ServiceSpec("backup_workers", "Configuration backup workers",
                ("zenplus-ncm-worker.service",), core=False),
)

_UNIT_PROPERTIES = ("Id,LoadState,ActiveState,SubState,UnitFileState,MemoryCurrent,"
                    "CPUUsageNSec,ActiveEnterTimestampMonotonic,NRestarts")


def parse_systemctl_show(text: str) -> list[dict]:
    """Blocks of ``Key=Value`` lines separated by blank lines, one per unit."""
    units: list[dict] = []
    block: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if block.get("Id"):
                units.append(block)
            block = {}
            continue
        key, sep, value = line.partition("=")
        if sep:
            block[key.strip()] = value.strip()
    return units


def _int_or_none(value: Optional[str]) -> Optional[int]:
    # systemd prints "[not set]" (or 2^64-1) when accounting is off for a unit.
    try:
        n = int(value or "")
    except ValueError:
        return None
    return None if n < 0 or n >= 2 ** 63 else n


def read_units(specs: Iterable[ServiceSpec] = SERVICES, timeout: float = 5.0) -> Optional[list[dict]]:
    """Every unit the specs name, or None when systemd cannot be asked (a
    development host, a container): the caller then reports "unknown"."""
    names = sorted({u for s in specs for u in s.units})
    if not names:
        return []
    try:
        proc = subprocess.run(
            ["systemctl", "show", *names, "--no-pager", "-p", _UNIT_PROPERTIES],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not proc.stdout.strip():
        return None
    return parse_systemctl_show(proc.stdout)


def _unit_matches(pattern: str, unit_id: str) -> bool:
    if "*" not in pattern:
        return pattern == unit_id
    head, _, tail = pattern.partition("*")
    return unit_id.startswith(head) and unit_id.endswith(tail) and len(unit_id) >= len(head) + len(tail)


def _unit_for(spec: ServiceSpec, units: list[dict]) -> Optional[dict]:
    """The spec's unit. Of several matches (two PostgreSQL clusters) the active
    one wins; a pattern that matched nothing is simply absent from the list."""
    matches = [u for u in units if any(_unit_matches(p, u.get("Id", "")) for p in spec.units)]
    loaded = [u for u in matches if u.get("LoadState") == "loaded"]
    if not loaded:
        return matches[0] if matches else None
    return next((u for u in loaded if u.get("ActiveState") == "active"), loaded[0])


def service_row(spec: ServiceSpec, units: Optional[list[dict]], probe: Optional[dict],
                uptime_s: Optional[float]) -> dict:
    """One row of the services table.

    state: ``running`` | ``down`` | ``stopped`` (an optional service that is
    switched off) | ``not_installed`` | ``unknown`` (could not be checked).
    """
    unit = _unit_for(spec, units) if units is not None and spec.units else None
    row: dict[str, Any] = {
        "id": spec.id, "label": spec.label, "core": spec.core,
        "state": "unknown", "detail": None, "uptime_s": None, "memory_bytes": None,
        "cpu_seconds": None, "restarts": None,
    }
    unit_state: Optional[str] = None
    if unit is not None:
        if unit.get("LoadState") != "loaded":
            unit_state = "not_installed"
        elif unit.get("ActiveState") == "active":
            unit_state = "running"
            row["memory_bytes"] = _int_or_none(unit.get("MemoryCurrent"))
            cpu_ns = _int_or_none(unit.get("CPUUsageNSec"))
            row["cpu_seconds"] = round(cpu_ns / 1e9, 1) if cpu_ns is not None else None
            entered = _int_or_none(unit.get("ActiveEnterTimestampMonotonic"))
            if entered and uptime_s is not None:
                row["uptime_s"] = max(0, int(uptime_s - entered / 1e6))
        elif unit.get("ActiveState") in ("activating", "reloading"):
            unit_state = "starting"
        else:
            enabled = (unit.get("UnitFileState") or "").startswith("enabled")
            # A core service is never legitimately off. An optional one that
            # the administrator disabled is "stopped", not a fault.
            unit_state = "down" if (spec.core or enabled or unit.get("ActiveState") == "failed") else "stopped"
            row["detail"] = "failed" if unit.get("ActiveState") == "failed" else "not running"
        if unit.get("LoadState") == "loaded":
            row["restarts"] = _int_or_none(unit.get("NRestarts"))
    elif spec.units and units is not None:
        unit_state = "not_installed"

    if spec.id == "api":
        # This process is answering the question.
        unit_state = "running" if unit_state in (None, "not_installed", "starting") else unit_state

    if probe is not None:
        if probe.get("ok"):
            row["state"] = "running"
            if probe.get("uptime_s") is not None and row["uptime_s"] is None:
                row["uptime_s"] = int(probe["uptime_s"])
            if probe.get("memory_bytes") is not None and row["memory_bytes"] is None:
                row["memory_bytes"] = int(probe["memory_bytes"])
        else:
            row["state"] = "down"
            row["detail"] = "not answering" if unit_state == "running" else (row["detail"] or "not answering")
    elif unit_state is not None:
        row["state"] = "starting" if unit_state == "starting" else unit_state
    return row


# ── Data store probes ────────────────────────────────────────────────────────

async def probe_postgresql() -> dict:
    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = (await db.execute(text(
            "SELECT pg_database_size(current_database()), "
            "EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))"))).one()
    return {"ok": True, "size_bytes": int(row[0]), "uptime_s": float(row[1])}


def _probe_clickhouse_sync() -> dict:
    from app.core.database import get_ch_client
    from app.core.config import get_settings

    client = get_ch_client()
    uptime = client.query("SELECT uptime()").result_rows[0][0]
    out: dict[str, Any] = {"ok": True, "uptime_s": float(uptime)}
    try:
        rows = client.query(
            "SELECT database = 'system' AS diagnostic, sum(bytes_on_disk) FROM system.parts "
            "WHERE active GROUP BY diagnostic").result_rows
        sizes = {bool(d): int(b) for d, b in rows}
        out["size_bytes"] = sizes.get(False, 0)
        out["diagnostic_bytes"] = sizes.get(True, 0)
        memory = client.query(
            "SELECT value FROM system.asynchronous_metrics WHERE metric = 'MemoryResident'").result_rows
        if memory:
            out["memory_bytes"] = int(memory[0][0])
    except Exception:  # sizes are extra; the server answered
        logger.debug("ClickHouse size query failed", exc_info=True)
    out["database"] = get_settings().CLICKHOUSE_DB
    return out


async def probe_clickhouse() -> dict:
    return await asyncio.to_thread(_probe_clickhouse_sync)


def _redis():
    import redis.asyncio as aioredis
    from app.core.config import get_settings

    return aioredis.from_url(get_settings().REDIS_URL, socket_connect_timeout=1.5, socket_timeout=1.5,
                             decode_responses=True)


async def probe_redis() -> dict:
    client = _redis()
    try:
        await client.ping()
        info = await client.info()
    finally:
        await client.aclose()
    return {"ok": True, "uptime_s": info.get("uptime_in_seconds"), "memory_bytes": info.get("used_memory")}


PROBES: dict[str, Callable[[], Awaitable[dict]]] = {
    "postgresql": probe_postgresql, "clickhouse": probe_clickhouse, "redis": probe_redis,
}


async def run_probe(name: str, probes: Optional[dict] = None, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """A probe's answer, or ``{"ok": False}``. The reason goes to the log, not
    to the page: raw driver errors carry addresses and are not for display."""
    fn = (probes or PROBES).get(name)
    if fn is None:
        return {"ok": False}
    try:
        return await asyncio.wait_for(fn(), timeout)
    except Exception as exc:  # noqa: BLE001 - any failure means "does not answer"
        logger.info("appliance health: %s does not answer (%s)", name, type(exc).__name__)
        return {"ok": False}


# ── Ingestion ────────────────────────────────────────────────────────────────

async def read_ingestion() -> dict:
    """Whether results are being taken in: the controller's recovery state and
    the low-space pause for agent and application uploads. None = not known."""
    out: dict[str, Any] = {"monitoring": None, "uploads_paused": None}
    try:
        from app.core.database import AsyncSessionLocal
        from app.services import controller_recovery as cr

        async with AsyncSessionLocal() as db:
            recovery = await asyncio.wait_for(cr.load(db), PROBE_TIMEOUT_S)
        if recovery:
            now = time.time()
            ready = recovery.ready_at is not None and now - recovery.last_tick <= cr.LEASE_GAP_S
            if not ready or recovery.overdue(now):
                out["monitoring"] = "interrupted"
            elif now < recovery.ready_at + cr.RECONNECT_S:
                out["monitoring"] = "reconnecting"
            else:
                out["monitoring"] = "ready"
    except Exception:  # noqa: BLE001
        logger.debug("appliance health: ingestion state unavailable", exc_info=True)
    try:
        from app.services import storage_backpressure

        out["uploads_paused"] = bool(storage_backpressure.ingest_pause())
    except Exception:  # noqa: BLE001
        logger.debug("appliance health: upload pause state unavailable", exc_info=True)
    return out


# ── Software ─────────────────────────────────────────────────────────────────

def read_software() -> dict:
    """Installed version, when it was installed and how the last update ended."""
    out: dict[str, Any] = {"version": None, "installed_at": None, "last_update": None}
    try:
        from app.api.v1 import system_updates as su

        version, installed_at = su._get_version_info()
        out["version"] = None if version == "unknown" else version
        out["installed_at"] = installed_at or None
        if su.HISTORY_FILE.exists():
            for record in reversed(json.loads(su.HISTORY_FILE.read_text())):
                if isinstance(record, dict) and record.get("status") in ("success", "failed", "rolled_back"):
                    out["last_update"] = {
                        "version": record.get("version"),
                        "status": record.get("status"),
                        "at": record.get("finished_at") or record.get("completed_at")
                              or record.get("started_at") or record.get("timestamp"),
                    }
                    break
    except Exception:  # noqa: BLE001
        logger.debug("appliance health: software details unavailable", exc_info=True)
    return out


def read_clock() -> dict:
    out: dict[str, Any] = {"now": datetime.now(timezone.utc).isoformat(), "timezone": None,
                           "ntp_synchronized": None}
    try:
        from app.api.v1.system_time import read_timedatectl

        info = read_timedatectl()
        out["timezone"] = info.get("timezone")
        out["ntp_synchronized"] = info.get("ntp_synchronized")
    except Exception:  # noqa: BLE001
        logger.debug("appliance health: clock details unavailable", exc_info=True)
    return out


# ── History ──────────────────────────────────────────────────────────────────

class RedisSampleStore:
    """One JSON sample a minute in a sorted set scored by its timestamp."""

    async def claim(self, minute: int) -> bool:
        client = _redis()
        try:
            return bool(await client.set(f"{HISTORY_CLAIM_KEY}{minute}", "1", nx=True,
                                         ex=SAMPLE_INTERVAL_S * 3))
        finally:
            await client.aclose()

    async def add(self, sample: dict) -> None:
        client = _redis()
        try:
            pipe = client.pipeline()
            pipe.zadd(HISTORY_KEY, {json.dumps(sample, separators=(",", ":")): sample["t"]})
            pipe.zremrangebyscore(HISTORY_KEY, "-inf", sample["t"] - (HISTORY_HOURS + 1) * 3600)
            pipe.expire(HISTORY_KEY, (HISTORY_HOURS + 2) * 3600)
            await pipe.execute()
        finally:
            await client.aclose()

    async def read(self, since: float) -> list[dict]:
        client = _redis()
        try:
            raw = await client.zrangebyscore(HISTORY_KEY, since, "+inf")
        finally:
            await client.aclose()
        out = []
        for item in raw:
            try:
                sample = json.loads(item)
            except ValueError:
                continue
            if isinstance(sample, dict) and isinstance(sample.get("t"), (int, float)):
                out.append(sample)
        return out


def make_sample(now: float, cpu: Optional[dict], memory: dict, load: Optional[list[float]],
                disk: Optional[dict]) -> dict:
    return {
        "t": int(now),
        "cpu": cpu["usage_pct"] if cpu else None,
        "iow": cpu["iowait_pct"] if cpu else None,
        "mem": memory.get("used_pct"),
        "swap": memory.get("swap_used_pct"),
        "load": load[0] if load else None,
        "disk": disk["used_pct"] if disk else None,
        "disk_used": disk["used_bytes"] if disk else None,
    }


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def bucket_history(samples: list[dict], now: float, hours: int = HISTORY_HOURS,
                   bucket_s: int = HISTORY_BUCKET_S) -> dict:
    """Samples as fixed buckets from the first sample in the window to now.

    A bucket without samples (the appliance or the API was down) keeps null
    readings, so the chart shows a gap instead of joining the line across it.
    """
    start = now - hours * 3600
    rows = sorted((s for s in samples if start <= s["t"] <= now + 60), key=lambda s: s["t"])
    if not rows:
        return {"available": True, "bucket_s": bucket_s, "hours": hours, "since": None,
                "samples": 0, "points": [], "disk_change": None}
    first = int(rows[0]["t"] // bucket_s * bucket_s)
    last = int(now // bucket_s * bucket_s)
    grouped: dict[int, list[dict]] = {}
    for s in rows:
        grouped.setdefault(int(s["t"] // bucket_s * bucket_s), []).append(s)

    def numbers(group: list[dict], key: str) -> list[float]:
        return [float(s[key]) for s in group if isinstance(s.get(key), (int, float))]

    points = []
    for t in range(first, last + 1, bucket_s):
        group = grouped.get(t, [])
        cpu = numbers(group, "cpu")
        mem = numbers(group, "mem")
        disk = numbers(group, "disk")
        points.append({
            "t": t,
            "cpu": _mean(cpu),
            "cpu_max": round(max(cpu), 1) if cpu else None,
            "iowait": _mean(numbers(group, "iow")),
            "mem": _mean(mem),
            "mem_max": round(max(mem), 1) if mem else None,
            "swap": _mean(numbers(group, "swap")),
            "load": (round(sum(numbers(group, "load")) / len(numbers(group, "load")), 2)
                     if numbers(group, "load") else None),
            "disk": round(disk[-1], 1) if disk else None,
        })

    measured = [s for s in rows if isinstance(s.get("disk_used"), (int, float))]
    change = None
    if len(measured) >= 2 and measured[-1]["t"] - measured[0]["t"] >= 3600:
        span = measured[-1]["t"] - measured[0]["t"]
        change = {"bytes": int(measured[-1]["disk_used"] - measured[0]["disk_used"]),
                  "hours": round(span / 3600, 1)}
    return {
        "available": True, "bucket_s": bucket_s, "hours": hours,
        "since": datetime.fromtimestamp(rows[0]["t"], timezone.utc).isoformat(),
        "samples": len(rows), "points": points, "disk_change": change,
    }


def sustained(samples: list[dict], key: str, now: float, window_s: int = SUSTAINED_WINDOW_S) -> Optional[float]:
    """Mean of a reading over the last ``window_s``, when most of that window
    was actually sampled (a single busy minute is not "sustained")."""
    values = [float(s[key]) for s in samples
              if now - window_s <= s["t"] <= now + 60 and isinstance(s.get(key), (int, float))]
    needed = max(3, int(window_s / SAMPLE_INTERVAL_S * 0.6))
    return round(sum(values) / len(values), 1) if len(values) >= needed else None


# The latest minute this worker measured; the status call serves it as "now".
_latest: dict[str, Any] = {"at": 0.0, "cpu": None}


async def sampler_loop(store: Optional[RedisSampleStore] = None,
                       interval: float = SAMPLE_INTERVAL_S) -> None:
    """Measure once a minute; write the sample when this worker claims the minute."""
    store = store or RedisSampleStore()
    prev = read_cpu_times()
    while True:
        try:
            # Align to the minute so every worker measures the same interval.
            delay = interval
            if interval >= 1:
                delay = interval - (time.time() % interval)
                if delay < interval * 0.1:
                    # Woken a moment before the boundary: do not measure a sliver.
                    delay += interval
            await asyncio.sleep(delay)
            cur = read_cpu_times()
            cpu = cpu_usage(prev, cur)
            prev = cur
            now = time.time()
            _latest.update(at=now, cpu=cpu)
            try:
                # The nearest boundary: a wake-up a millisecond early or late is the same minute.
                if await store.claim(int(round(now / interval))):
                    await store.add(make_sample(now, cpu, read_memory(), read_load(), read_disk()))
            except Exception as exc:  # noqa: BLE001 - Redis away: keep measuring
                logger.debug("appliance health: sample not stored (%s)", type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("appliance health sampler pass failed")
            await asyncio.sleep(interval)


def _measure_cpu_now(window: float = 0.5) -> Optional[dict]:
    first = read_cpu_times()
    time.sleep(window)
    return cpu_usage(first, read_cpu_times())


async def current_cpu(now: Optional[float] = None) -> tuple[Optional[dict], Optional[int]]:
    """(usage, window in seconds): the last full minute when this worker has
    one, otherwise a half-second measurement taken now."""
    now = now or time.time()
    if _latest["cpu"] is not None and now - _latest["at"] <= SAMPLE_FRESH_S:
        return _latest["cpu"], SAMPLE_INTERVAL_S
    usage = await asyncio.to_thread(_measure_cpu_now)
    return usage, (1 if usage else None)


# ── Findings ─────────────────────────────────────────────────────────────────

def findings(*, services: list[dict], memory: dict, load: Optional[list[float]], cores: Optional[int],
             ingestion: dict, clock: dict, cpu_15m: Optional[float] = None,
             iowait_15m: Optional[float] = None) -> list[dict]:
    """What deserves a line at the top of the page, worst first."""
    out: list[dict] = []

    def add(severity: str, code: str, message: str) -> None:
        out.append({"severity": severity, "code": code, "message": message})

    for s in services:
        if s["state"] == "down":
            if s["core"]:
                add("critical", f"service_down:{s['id']}",
                    f"{s['label']} is {s['detail'] or 'not running'}.")
            else:
                add("warning", f"service_down:{s['id']}",
                    f"{s['label']} is {s['detail'] or 'not running'}.")
    if ingestion.get("monitoring") == "interrupted":
        add("critical", "monitoring_interrupted",
            "Monitoring results are not being processed. Status and alerts may be out of date.")
    if ingestion.get("uploads_paused"):
        add("warning", "uploads_paused",
            "Agent and application uploads are paused until free space recovers.")
    used = memory.get("used_pct")
    if used is not None and used >= MEMORY_CRIT_PCT:
        add("critical", "memory_critical", f"Memory is almost exhausted ({used:.0f}% in use).")
    elif used is not None and used >= MEMORY_WARN_PCT:
        add("warning", "memory_high", f"Memory use is high ({used:.0f}%).")
    swap = memory.get("swap_used_pct")
    if swap is not None and swap >= SWAP_WARN_PCT:
        add("warning", "swap_high",
            f"{swap:.0f}% of swap is in use: the appliance is short of memory and will be slow.")
    if cpu_15m is not None and cpu_15m >= CPU_SUSTAINED_WARN_PCT:
        add("warning", "cpu_sustained", f"CPU has averaged {cpu_15m:.0f}% over the last 15 minutes.")
    elif load and cores and load[2] / cores >= LOAD_PER_CORE_WARN:
        add("warning", "load_high",
            f"More work is queued than the {cores} CPU core{'s' if cores != 1 else ''} can run "
            f"(15-minute load {load[2]:.1f}).")
    if iowait_15m is not None and iowait_15m >= IOWAIT_SUSTAINED_WARN_PCT:
        add("warning", "iowait_high",
            f"The CPUs have spent {iowait_15m:.0f}% of the last 15 minutes waiting for the disk.")
    if clock.get("ntp_synchronized") is False:
        add("warning", "clock_unsynchronized",
            "The appliance clock is not synchronised with a time server.")
    order = {"critical": 0, "warning": 1}
    return sorted(out, key=lambda f: order.get(f["severity"], 2))


# ── The status document ──────────────────────────────────────────────────────

_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
_status_lock = asyncio.Lock()


async def _recent_samples(store: RedisSampleStore, now: float) -> Optional[list[dict]]:
    try:
        return await asyncio.wait_for(store.read(now - SUSTAINED_WINDOW_S), 2.0)
    except Exception:  # noqa: BLE001
        return None


async def collect_status(*, probes: Optional[dict] = None, store: Optional[RedisSampleStore] = None,
                         units: Any = "read") -> dict:
    """Everything the page shows as "now". ``units`` and ``probes`` are
    replaceable so tests never ask a real systemd or database."""
    now = time.time()
    store = store or RedisSampleStore()
    unit_list = await asyncio.to_thread(read_units) if units == "read" else units
    names = [s.probe for s in SERVICES if s.probe]
    probe_results, ingestion, (cpu, window), recent, software, clock = await asyncio.gather(
        asyncio.gather(*(run_probe(n, probes) for n in names)),
        read_ingestion() if probes is None else _static({"monitoring": None, "uploads_paused": None}),
        current_cpu(now),
        _recent_samples(store, now),
        asyncio.to_thread(read_software),
        asyncio.to_thread(read_clock),
    )
    probed = dict(zip(names, probe_results))
    uptime = read_uptime()
    memory = read_memory()
    load = read_load()
    cores = os.cpu_count()
    services = [service_row(s, unit_list, probed.get(s.probe) if s.probe else None, uptime) for s in SERVICES]
    # An optional service that is not installed on this appliance is not a row.
    services = [s for s in services if s["core"] or s["state"] != "not_installed"]

    pg, ch, rd = probed.get("postgresql", {}), probed.get("clickhouse", {}), probed.get("redis", {})
    data_stores = [
        {"id": "config_db", "label": "Configuration database",
         "holds": "Devices, users, settings, alert rules and inventory",
         "size_bytes": pg.get("size_bytes"), "kind": "disk"},
        {"id": "metrics_db", "label": "Metrics database",
         "holds": "Monitoring history, flows, traces and events",
         "size_bytes": ch.get("size_bytes"), "kind": "disk"},
        {"id": "metrics_db_logs", "label": "Metrics database logs",
         "holds": "The database's own diagnostic logs",
         "size_bytes": ch.get("diagnostic_bytes"), "kind": "disk"},
        {"id": "cache", "label": "Cache and live updates",
         "holds": "Sessions, live status and short-lived queues",
         "size_bytes": rd.get("memory_bytes"), "kind": "memory"},
    ]
    cpu_15m = sustained(recent, "cpu", now) if recent else None
    iowait_15m = sustained(recent, "iow", now) if recent else None
    found = findings(services=services, memory=memory, load=load, cores=cores, ingestion=ingestion,
                     clock=clock, cpu_15m=cpu_15m, iowait_15m=iowait_15m)
    state = ("critical" if any(f["severity"] == "critical" for f in found)
             else "warning" if found else "ok")
    return {
        "sampled_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "state": state,
        "findings": found,
        "host": {"cpu_model": cpu_model(), "cores": cores,
                 "uptime_s": int(uptime) if uptime is not None else None,
                 "boot_time": (datetime.fromtimestamp(now - uptime, timezone.utc).isoformat()
                               if uptime is not None else None)},
        "cpu": {"usage_pct": cpu["usage_pct"] if cpu else None,
                "iowait_pct": cpu["iowait_pct"] if cpu else None,
                "steal_pct": cpu["steal_pct"] if cpu else None,
                "window_s": window, "cores": cores, "load": load,
                "average_15m_pct": cpu_15m},
        "memory": memory,
        "disk": read_disk(),
        "services": services,
        "data_stores": data_stores,
        "ingestion": ingestion,
        "software": software,
        "clock": clock,
    }


async def _static(value: dict) -> dict:
    return value


async def status() -> dict:
    """``collect_status`` with a few seconds of reuse, so several open pages
    (or several administrators) cost one round of probes."""
    now = time.monotonic()
    if _status_cache["value"] is not None and now - _status_cache["at"] < STATUS_CACHE_S:
        return _status_cache["value"]
    async with _status_lock:
        now = time.monotonic()
        if _status_cache["value"] is not None and now - _status_cache["at"] < STATUS_CACHE_S:
            return _status_cache["value"]
        value = await collect_status()
        _status_cache.update(at=time.monotonic(), value=value)
        return value


async def history(hours: int = HISTORY_HOURS, store: Optional[RedisSampleStore] = None) -> dict:
    hours = max(1, min(int(hours), HISTORY_HOURS))
    now = time.time()
    store = store or RedisSampleStore()
    try:
        samples = await asyncio.wait_for(store.read(now - hours * 3600), 3.0)
    except Exception as exc:  # noqa: BLE001
        logger.info("appliance health: history unavailable (%s)", type(exc).__name__)
        return {"available": False, "bucket_s": HISTORY_BUCKET_S, "hours": hours, "since": None,
                "samples": 0, "points": [], "disk_change": None}
    # Short windows keep the minute resolution; a day is drawn in 5-minute steps.
    bucket = SAMPLE_INTERVAL_S if hours <= 3 else HISTORY_BUCKET_S
    return bucket_history(samples, now, hours=hours, bucket_s=bucket)
