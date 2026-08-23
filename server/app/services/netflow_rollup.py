"""Backfill and heal the hourly NetFlow rollup tables.

migrate-071/072 create ``zenplus.flow_conversations_1h`` (pair-keyed) and
``zenplus.flow_qos_1h`` (ToS/flag-keyed) with materialized views that populate
them from every ``flow_records`` insert — but an MV only sees inserts made
after its own creation. Everything already in ``flow_records`` when the
migration lands (up to 30 days of raw history) would be invisible to
rollup-backed queries. The migrations cannot backfill it themselves: a fleet
appliance would need minutes of INSERT..SELECT, which both blows the updater's
statement timeout and would make the migration non-replay-safe.

So this sweeper converges each rollup instead, one hour-bucket at a time:

* an hour that exists in raw but has no rollup rows is backfilled;
* an hour whose rollup flow count disagrees with the raw count is rebuilt
  (DELETE + re-insert). In steady state the MV makes the counts match
  exactly, so a mismatch only appears for the hour that was in flight when
  the MV was created, or after an MV outage.

Hours younger than ``HEAL_MIN_AGE_HOURS`` are left alone — late-exported
flows are still arriving and the MV is handling them. Hours older than
``HEAL_MAX_AGE_HOURS`` are never rebuilt: raw rows near the 30-day TTL may be
partially deleted, and rebuilding from partial raw would destroy history the
90-day rollups are meant to outlive.

Each hour is converged inside its own transaction-scoped Postgres advisory
lock (the pattern udt_sweeper.py documents), so the two uvicorn workers never
double-insert an hour; the loser of the race skips the whole pass.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, get_ch_client

logger = logging.getLogger("zenplus.netflow")

RAW_TABLE = "zenplus.flow_records"
# Must match the corrupt-flow guard in the MVs (migrate-071/072) and _scope()
# in app/api/v1/netflow.py, or counts will never converge.
SANITY_GUARD = "bytes / greatest(sampling_interval, 1) <= 1000000000000"

SWEEP_INTERVAL_S = 6 * 3600
STARTUP_DELAY_S = 20
PAUSE_BETWEEN_HOURS_S = 2.0
HEAL_MIN_AGE_HOURS = 2
HEAL_MAX_AGE_HOURS = 27 * 24
MAX_HEALS_PER_SWEEP = 6

NETFLOW_ROLLUP_ADVISORY_LOCK = 1515074392

# The INSERTs mirror the SELECTs in the corresponding materialized views. The
# inner subqueries rename raw columns so aggregates like min/max(raw_ts)
# cannot be captured by the ``AS timestamp`` bucket alias.
_CONVERSATIONS_INSERT = f"""
INSERT INTO zenplus.flow_conversations_1h
SELECT
    hour_bucket, exporter_ip, src_addr, dst_addr, protocol, dst_port,
    sum(raw_bytes), sum(raw_packets), count(),
    groupUniqArrayState(10)(raw_src_port),
    groupUniqArrayState(10)(raw_input_snmp),
    groupUniqArrayState(10)(raw_output_snmp),
    min(raw_ts), max(raw_ts), max(raw_received_at),
    sum(raw_duration_ms),
    groupBitOr(toUInt64(raw_tcp_flags))
FROM (
    SELECT
        toStartOfHour(timestamp) AS hour_bucket,
        timestamp AS raw_ts,
        received_at AS raw_received_at,
        exporter_ip, src_addr, dst_addr, protocol, dst_port,
        src_port AS raw_src_port,
        input_snmp AS raw_input_snmp,
        output_snmp AS raw_output_snmp,
        bytes AS raw_bytes,
        packets AS raw_packets,
        toInt64(last_switched_ms) - toInt64(first_switched_ms) AS raw_duration_ms,
        tcp_flags AS raw_tcp_flags
    FROM {RAW_TABLE}
    WHERE {SANITY_GUARD}
      AND timestamp >= %(hour)s AND timestamp < %(hour)s + INTERVAL 1 HOUR
)
GROUP BY hour_bucket, exporter_ip, src_addr, dst_addr, protocol, dst_port
"""

_QOS_INSERT = f"""
INSERT INTO zenplus.flow_qos_1h
SELECT
    hour_bucket, exporter_ip, protocol, tos, tcp_flags,
    sum(raw_bytes), sum(raw_packets), count(),
    countIf(raw_packets = 0),
    sum(raw_duration_ms),
    max(raw_received_at)
FROM (
    SELECT
        toStartOfHour(timestamp) AS hour_bucket,
        received_at AS raw_received_at,
        exporter_ip, protocol, tos, tcp_flags,
        bytes AS raw_bytes,
        packets AS raw_packets,
        toInt64(last_switched_ms) - toInt64(first_switched_ms) AS raw_duration_ms
    FROM {RAW_TABLE}
    WHERE {SANITY_GUARD}
      AND timestamp >= %(hour)s AND timestamp < %(hour)s + INTERVAL 1 HOUR
)
GROUP BY hour_bucket, exporter_ip, protocol, tos, tcp_flags
"""


@dataclass(frozen=True)
class _Rollup:
    name: str          # bare table name, for existence probe and logs
    table: str         # qualified table name
    insert_sql: str


ROLLUPS = (
    _Rollup("flow_conversations_1h", "zenplus.flow_conversations_1h", _CONVERSATIONS_INSERT),
    _Rollup("flow_qos_1h", "zenplus.flow_qos_1h", _QOS_INSERT),
)


def _ch(sql: str, params: dict | None = None):
    return get_ch_client().query(sql, parameters=params or {})


async def _query(sql: str, params: dict | None = None):
    return await asyncio.to_thread(_ch, sql, params)


async def _table_exists(name: str) -> bool:
    res = await _query(
        "SELECT count() FROM system.tables WHERE database = 'zenplus' AND name = %(n)s",
        {"n": name},
    )
    return bool(res.result_rows and int(res.result_rows[0][0]))


async def _plan(rollup: _Rollup) -> list[tuple[str, str]]:
    """Return [(hour_iso, 'backfill' | 'heal')] oldest-first.

    Compares guarded raw row counts against rollup flow sums per hour bucket.
    The current (still-filling) hour is excluded; the MV owns it.
    """
    raw = await _query(
        f"""
        SELECT toStartOfHour(timestamp) AS h, count() AS n
        FROM {RAW_TABLE}
        WHERE {SANITY_GUARD} AND timestamp < toStartOfHour(now())
        GROUP BY h
        """
    )
    rolled = await _query(
        f"SELECT timestamp AS h, sum(flows) AS n FROM {rollup.table} GROUP BY h"
    )
    rollup_counts = {r[0]: int(r[1]) for r in rolled.result_rows}

    now_res = await _query("SELECT now('UTC')")
    now_utc = now_res.result_rows[0][0]

    plan: list[tuple[str, str]] = []
    heals = 0
    for hour, raw_count in sorted(raw.result_rows):
        have = rollup_counts.get(hour)
        if have is None:
            plan.append((hour.strftime("%Y-%m-%d %H:%M:%S"), "backfill"))
            continue
        if have == int(raw_count):
            continue
        age_h = (now_utc - hour).total_seconds() / 3600
        if HEAL_MIN_AGE_HOURS <= age_h <= HEAL_MAX_AGE_HOURS and heals < MAX_HEALS_PER_SWEEP:
            plan.append((hour.strftime("%Y-%m-%d %H:%M:%S"), "heal"))
            heals += 1
    return plan


async def _current_action(rollup: _Rollup, hour: str) -> str | None:
    """What this hour needs right now: 'backfill', 'heal', or None."""
    rolled = await _query(
        f"SELECT count(), sum(flows) FROM {rollup.table} WHERE timestamp = %(hour)s",
        {"hour": hour},
    )
    rollup_rows = int(rolled.result_rows[0][0])
    rollup_flows = int(rolled.result_rows[0][1] or 0)
    raw = await _query(
        f"""
        SELECT count() FROM {RAW_TABLE}
        WHERE {SANITY_GUARD}
          AND timestamp >= %(hour)s AND timestamp < %(hour)s + INTERVAL 1 HOUR
        """,
        {"hour": hour},
    )
    raw_rows = int(raw.result_rows[0][0])
    if raw_rows == 0:
        return None
    if rollup_rows == 0:
        return "backfill"
    return "heal" if rollup_flows != raw_rows else None


async def _converge_hour(rollup: _Rollup, hour: str, action: str) -> None:
    if action == "heal":
        logger.warning("netflow rollup: rebuilding mismatched hour %s in %s", hour, rollup.name)
        await _query(
            f"ALTER TABLE {rollup.table} DELETE WHERE timestamp = %(hour)s "
            "SETTINGS mutations_sync = 1",
            {"hour": hour},
        )
    await _query(rollup.insert_sql, {"hour": hour})


async def _sweep_rollup(rollup: _Rollup) -> None:
    if not await _table_exists(rollup.name):
        logger.info("netflow rollup: %s absent (migration not applied yet), skipping", rollup.name)
        return
    plan = await _plan(rollup)
    if not plan:
        return
    logger.info(
        "netflow rollup: converging %d hour(s) into %s (%d backfill, %d heal)",
        len(plan),
        rollup.name,
        sum(1 for _, a in plan if a == "backfill"),
        sum(1 for _, a in plan if a == "heal"),
    )
    done = 0
    for hour, action in plan:
        # Fresh short transaction per hour: the xact-scoped advisory lock is
        # held across this hour's ClickHouse work only, so a competing worker
        # is blocked for seconds, not for the whole (possibly hours-long)
        # first backfill.
        async with AsyncSessionLocal() as db:
            got = (await db.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"),
                {"k": NETFLOW_ROLLUP_ADVISORY_LOCK},
            )).scalar()
            if not got:
                logger.info("netflow rollup: another worker is converging, yielding")
                return
            # Re-check under the lock: the other worker may have converged
            # this hour already, or late-exported flows may have given a
            # planned-backfill hour its first MV rows (a plain INSERT on top
            # of those would double-count — leave it for a future heal).
            current = await _current_action(rollup, hour)
            if current == "backfill" or (current == "heal" and action == "heal"):
                await _converge_hour(rollup, hour, current)
                done += 1
            await db.commit()
        await asyncio.sleep(PAUSE_BETWEEN_HOURS_S)
    if done:
        logger.info("netflow rollup: converged %d hour(s) into %s", done, rollup.name)


async def _sweep_once() -> None:
    for rollup in ROLLUPS:
        await _sweep_rollup(rollup)


async def netflow_rollup_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            await _sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow rollup sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
