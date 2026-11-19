"""Server depth metrics for host alert rules (alerting plan G-9, G-10).

The agent has long collected more than CPU, memory, filesystem and disk
utilisation; these readings make the rest alertable through the ordinary host
rule lifecycle (``host_alert_service``). Each was checked against what the
agent actually sends before it was added (docs/alert-notification-policy.md
"Server rules"):

==========================  ================================================  =============
metric                      reading (dimension)                               lifecycle
==========================  ================================================  =============
``host_swap_pct``           swap used / swap total, % (server)                averaged
``host_disk_latency_ms``    the slower of the average read and write time     averaged
                            per I/O, ms (disk device). Linux agents only:
                            the Windows agent's disk times are whole seconds
                            (gopsutil), so its readings are ~1000x too low.
``host_nic_errors``         receive + transmit errors per second (NIC)        averaged
``host_clock_skew_s``       |agent clock - appliance clock|, s, from every    per sample
                            upload (agent_host_result_batches) (server)
``host_fs_days_left``       days until the filesystem is full at its          snapshot
                            least-squares growth over 7 days (mount)
``host_auto_service_stopped`` an automatic service stopped unexpectedly       snapshot
                            (service)
==========================  ================================================  =============

*Averaged* and *per sample* metrics are judged on their history by
``network_conditions.evaluate`` like CPU and memory. *Snapshot* metrics have
one current reading per pass; their breach and clear holds are kept in
``service_metric_alert_state`` (migrate-199) under the key
``host:<server>:<dimension>`` and judged by ``service_metric_alert_service.
decide`` — the same contract (a missing reading never raises, resolves or
counts toward a hold).

Automatic services. The agent reports a start mode of ``auto`` for every
automatic service: Windows delayed-start and trigger-start services are not
told apart (QueryServiceConfig reports both as SERVICE_AUTO_START), and an
enabled systemd oneshot unit that finished is ``stopped`` too. Such services
end cleanly, so a stop counts only when it was not clean: the last recorded
exit code (``host_service_state`` in ClickHouse) is not 0 and not 1077
(Windows "never started since boot"), or systemd reports the unit ``failed``.
A clean, deliberate stop of an automatic service is therefore not reported
here: name that service in a "Service stopped" rule. The rule's ``target``
holds service name patterns to leave out (comma separated, ``*`` and ``?``
wildcards, case-insensitive, matched against the name and display name).
"""

from __future__ import annotations

import fnmatch
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.host_alerts")

SWAP = "host_swap_pct"
DISK_LATENCY = "host_disk_latency_ms"
NIC_ERRORS = "host_nic_errors"
CLOCK_SKEW = "host_clock_skew_s"
FS_DAYS_LEFT = "host_fs_days_left"
AUTO_SERVICE_STOPPED = "host_auto_service_stopped"

# ClickHouse history for the averaged depth metrics: (server_id, dimension,
# epoch seconds, value) between %(since)s and %(until)s. A NULL value (a
# server without swap) is missing, never a reading of 0.
HISTORY_SQL = {
    SWAP: (
        "SELECT toString(server_id), '', toUnixTimestamp64Milli(timestamp) / 1000, "
        "if(swap_total_bytes > 0, 100.0 * swap_used_bytes / swap_total_bytes, NULL) "
        "FROM zenplus.host_memory_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s "
        "ORDER BY server_id, timestamp LIMIT {limit}"
    ),
    DISK_LATENCY: (
        "SELECT toString(server_id), device, toUnixTimestamp64Milli(timestamp) / 1000, "
        "greatest(avg_read_ms, avg_write_ms) "
        "FROM zenplus.host_disk_io_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s "
        "ORDER BY server_id, device, timestamp LIMIT {limit}"
    ),
    NIC_ERRORS: (
        "SELECT toString(server_id), if_name, toUnixTimestamp64Milli(timestamp) / 1000, "
        "rx_errors_ps + tx_errors_ps "
        "FROM zenplus.host_network_metrics "
        "WHERE timestamp >= %(since)s AND timestamp <= %(until)s "
        "ORDER BY server_id, if_name, timestamp LIMIT {limit}"
    ),
}
AVERAGED = frozenset(HISTORY_SQL)
# Judged on history (network_conditions.evaluate).
HISTORY_METRICS = AVERAGED | {CLOCK_SKEW}
# One reading per pass (service_metric_alert_service.decide).
SNAPSHOT_METRICS = frozenset({FS_DAYS_LEFT, AUTO_SERVICE_STOPPED})
ALL = HISTORY_METRICS | SNAPSHOT_METRICS

# Incident dimension per metric: the dedupe key part and the metadata key.
DIMENSION_KEYS = {
    "host_filesystem_pct": "mount", FS_DAYS_LEFT: "mount",
    DISK_LATENCY: "disk", NIC_ERRORS: "nic", AUTO_SERVICE_STOPPED: "service",
}
DIMENSION_NOUNS = {"mount": "Filesystem", "disk": "Disk", "nic": "Interface", "service": "Service"}


# ── Clock skew (PostgreSQL) ─────────────────────────────────────────────────

async def fetch_clock_skew_history(db: AsyncSession, since: float, until: float) -> dict[str, dict[str, list]]:
    """{server_id: {"": [(epoch s, |skew| s), ...]}} from every completed
    upload in the window. Each upload measures the agent clock against the
    appliance's (host_metric_service._correct_agent_clock_skew)."""
    rows = (await db.execute(text(
        """SELECT server_id::text, extract(epoch FROM completed_at), abs(clock_skew_s)
             FROM agent_host_result_batches
            WHERE completed_at >= :since AND completed_at <= :until
            ORDER BY server_id, completed_at"""),
        {"since": datetime.fromtimestamp(since, timezone.utc),
         "until": datetime.fromtimestamp(until, timezone.utc)})).all()
    out: dict[str, dict[str, list]] = {}
    for sid, stamp, skew in rows:
        out.setdefault(str(sid), {}).setdefault("", []).append((float(stamp), float(skew)))
    return out


async def windows_server_ids(db: AsyncSession) -> set[str]:
    """Servers whose agent's disk latency is not usable (see the module doc)."""
    rows = (await db.execute(text("SELECT id::text FROM servers WHERE os_type = 'windows'"))).all()
    return {r[0] for r in rows}


# ── Snapshot readings ───────────────────────────────────────────────────────

GONE = "gone"   # the dimension is no longer watched: resolve without a notice


@dataclass
class Snap:
    """One current reading of a snapshot metric. ``value`` None = unknown
    (keep the incident as it is); ``state`` GONE = no longer watched."""
    value: Optional[float]
    detail: str = ""
    state: str = "current"
    context: dict = field(default_factory=dict)


# Filesystem forecast. Growth is the least-squares slope over GROWTH_WINDOW
# (host_metric_detail, the server page's "days until full"); the query covers
# every server, so it runs at most once per FORECAST_CACHE_S.
FORECAST_CACHE_S = 900
_forecast_cache: dict = {"at": None, "rows": None}


def _forecast_sql() -> str:
    from app.services.filesystem_monitoring import ch_capacity_filter
    return f"""
        SELECT toString(server_id), mount,
               tupleElement(simpleLinearRegression(toUnixTimestamp(timestamp), toFloat64(used_bytes)), 1),
               argMax(used_bytes, timestamp), argMax(total_bytes, timestamp), count(),
               dateDiff('second', min(timestamp), max(timestamp)),
               toUnixTimestamp(max(timestamp))
        FROM zenplus.host_filesystem_metrics
        WHERE timestamp >= %(since)s AND timestamp <= %(until)s AND {ch_capacity_filter()}
        GROUP BY server_id, mount"""


def fetch_fs_forecast(now: float) -> list[tuple]:
    """(server, mount, slope bytes/s, used, total, samples, span s, newest
    epoch) per filesystem. Raises when ClickHouse cannot be read."""
    from app.core.database import get_clickhouse_client
    from app.services.host_metric_detail import GROWTH_WINDOW
    rows = get_clickhouse_client().query(_forecast_sql(), parameters={
        "since": datetime.fromtimestamp(now - GROWTH_WINDOW.total_seconds(), timezone.utc),
        "until": datetime.fromtimestamp(now, timezone.utc),
    }).result_rows
    return [tuple(r) for r in rows]


def cached_fs_forecast(now: float, fetch=None) -> list[tuple]:
    at = _forecast_cache["at"]
    if at is None or _forecast_cache["rows"] is None or abs(now - at) >= FORECAST_CACHE_S:
        _forecast_cache["rows"] = (fetch or fetch_fs_forecast)(now)
        _forecast_cache["at"] = now
    return _forecast_cache["rows"]


def reset_forecast_cache() -> None:
    _forecast_cache.update(at=None, rows=None)


def days_left(slope, used, total, samples, span_s) -> Optional[float]:
    """Days until full; inf when the volume is not filling; None when there is
    too little history (under a day, or fewer than 10 samples) to say."""
    from app.services.host_metric_detail import MIN_GROWTH_SPAN, days_until_full
    if (samples or 0) < 10 or (span_s or 0) < MIN_GROWTH_SPAN.total_seconds():
        return None
    try:
        per_day = float(slope) * 86400
    except (TypeError, ValueError):
        return None
    if math.isnan(per_day) or per_day <= 0 or not total:
        return math.inf
    out = days_until_full(float(total), float(used or 0), per_day)
    return math.inf if out is None else out


def fs_days_left_readings(rows: list[tuple], freshness: dict[str, int], default_fresh: int,
                          now: float) -> dict[str, dict[str, Snap]]:
    """{server: {mount: Snap}} from the forecast rows; a filesystem whose newest
    sample is older than the agent's telemetry freshness is unknown."""
    out: dict[str, dict[str, Snap]] = {}
    for sid, mount, slope, used, total, samples, span, newest in rows:
        sid = str(sid)
        fresh = freshness.get(sid, default_fresh) + FORECAST_CACHE_S
        if newest is None or float(newest) + fresh <= now:
            out.setdefault(sid, {})[str(mount)] = Snap(None, state="no_data")
            continue
        days = days_left(slope, used, total, samples, span)
        if days is None:
            out.setdefault(sid, {})[str(mount)] = Snap(None, state="insufficient_history")
            continue
        growth = float(slope) * 86400 if slope is not None and not math.isnan(float(slope)) else 0.0
        ctx = {"growth_bytes_per_day": round(growth), "used_bytes": int(used or 0),
               "total_bytes": int(total or 0)}
        detail = ("not filling" if math.isinf(days)
                  else f"full in {days:.1f} days at the growth of the last 7 days")
        out.setdefault(sid, {})[str(mount)] = Snap(days, detail, context=ctx)
    return out


# Automatic services
CLEAN_EXIT_CODES = frozenset({0, 1077})
STOPPED_STATES = frozenset({"stopped", "failed"})
# The agent resends an unchanged service list every 10 minutes.
SERVICE_SNAPSHOT_MAX_AGE_S = 900


def exclude_patterns(target: Optional[str]) -> list[str]:
    """The rule target as service name patterns ("gupdate*, edgeupdate*")."""
    return [p.strip().lower() for p in re.split(r"[,;\n]+", target or "") if p.strip()]


def excluded(name: str, display: Optional[str], patterns: list[str]) -> bool:
    names = [n.lower() for n in (name, display or "") if n]
    return any(fnmatch.fnmatchcase(n, p) for p in patterns for n in names)


def service_reading(state: str, exit_code: Optional[int], last_state: Optional[str]) -> Snap:
    """The reading for one automatic service in the newest snapshot."""
    state = (state or "").lower()
    if state not in STOPPED_STATES:
        return Snap(0.0, f"running ({state or 'unknown state'})" if state != "running" else "running")
    if state == "failed":
        return Snap(1.0, "stopped: the service failed", context={"service_state": state,
                                                                 "exit_code": exit_code})
    if exit_code is None or (last_state or "").lower() not in STOPPED_STATES:
        # No recorded stop to read the exit code from: unknown, not healthy.
        return Snap(None, state="no_exit_code")
    if int(exit_code) in CLEAN_EXIT_CODES:
        return Snap(0.0, f"stopped cleanly (exit code {int(exit_code)})")
    return Snap(1.0, f"stopped unexpectedly (exit code {int(exit_code)})",
                context={"service_state": state, "exit_code": int(exit_code)})


async def load_service_snapshots(db: AsyncSession) -> tuple[dict[str, float], list]:
    """({server: newest snapshot epoch}, [(server, name, display, state)] of the
    automatic services in each server's newest snapshot). A snapshot is
    written in one transaction, so its rows share one updated_at."""
    latest = {r[0]: float(r[1]) for r in (await db.execute(text(
        "SELECT server_id::text, extract(epoch FROM max(updated_at)) FROM server_service_inventory"
        " GROUP BY server_id"))).all()}
    rows = (await db.execute(text(
        """SELECT i.server_id::text, i.service_name, i.display_name, lower(COALESCE(i.state, ''))
             FROM server_service_inventory i
             JOIN (SELECT server_id, max(updated_at) AS at FROM server_service_inventory
                    GROUP BY server_id) l ON l.server_id = i.server_id
            WHERE i.updated_at >= l.at - interval '5 seconds'
              AND lower(COALESCE(i.start_mode, '')) = 'auto'"""))).all()
    return latest, [tuple(r) for r in rows]


def fetch_exit_codes(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, str]]:
    """{(server, service): (last exit code, last state)} from the service
    change log, for the stopped candidates only. Raises when unreadable."""
    if not pairs:
        return {}
    from app.core.database import get_clickhouse_client
    import uuid
    sids = tuple(uuid.UUID(s) for s in sorted({p[0] for p in pairs}))
    names = tuple(sorted({p[1] for p in pairs}))
    rows = get_clickhouse_client().query(
        """SELECT toString(server_id), service_name, argMax(exit_code, timestamp), argMax(state, timestamp)
             FROM zenplus.host_service_state
            WHERE timestamp >= now() - INTERVAL 60 DAY
              AND server_id IN %(sids)s AND service_name IN %(names)s
            GROUP BY server_id, service_name""",
        parameters={"sids": sids, "names": names}).result_rows
    return {(str(r[0]), str(r[1])): (int(r[2]), str(r[3])) for r in rows if (str(r[0]), str(r[1])) in pairs}


def auto_service_readings(latest: dict[str, float], rows: list, codes: dict, *, patterns: list[str],
                          freshness: dict[str, int], default_fresh: int,
                          now: float) -> dict[str, Optional[dict[str, Snap]]]:
    """{server: {service: Snap}}; None for a server whose newest service
    snapshot is too old to judge (every open incident there stays open)."""
    out: dict[str, Optional[dict[str, Snap]]] = {}
    for sid, at in latest.items():
        fresh = freshness.get(sid, default_fresh) + SERVICE_SNAPSHOT_MAX_AGE_S
        out[sid] = {} if at + fresh > now else None
    for sid, name, display, state in rows:
        per = out.get(sid)
        if per is None:
            continue
        if excluded(name, display, patterns):
            per[name] = Snap(None, state=GONE)
            continue
        code, last_state = codes.get((sid, name), (None, None))
        snap = service_reading(state, code, last_state)
        if display and display != name:
            snap.context["display_name"] = display
        per[name] = snap
    return out


def stopped_candidates(rows: list, patterns: list[str]) -> set[tuple[str, str]]:
    return {(sid, name) for sid, name, display, state in rows
            if (state or "").lower() == "stopped" and not excluded(name, display, patterns)}
