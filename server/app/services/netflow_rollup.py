"""Backfill, heal and repair the NetFlow rollup tables.

Every rollup is populated from ``flow_records`` by a materialized view
(migrate-071/072/20260506, all rewritten by migrate-137):

    flow_conversations_1h   hourly, per exporter / address pair / protocol / port
    flow_qos_1h             hourly, per exporter / protocol / ToS / TCP flags
    flow_traffic_5m         5-minute traffic series (report sections)
    flow_interface_5m       5-minute per-interface volume, duration-spread
    flow_exporter_minutes   per-exporter delivery minutes, appliance clock

A view only sees inserts made after it exists, so raw history (up to the raw
retention, ~30M rows/day on a busy appliance) is invisible to a rollup created
by a migration, and a view that was missing or broken for a while leaves a hole.
The migrations cannot back-fill themselves: minutes of INSERT..SELECT would
blow the updater's statement timeout and make them unsafe to replay. This
sweeper converges each rollup instead, one hour at a time:

* **backfill** an hour that raw has and the rollup does not;
* **heal** an hour whose rollup count disagrees with raw (the hour in flight
  when a view was created, an insert that raced a backfill);
* **repair** an hour queued for a forced rebuild (X-02, below).

Convergence is judged per rollup on a count both sides can compute exactly
under the same volume guard the views apply: flows per flow-hour for the
conversation, QoS and traffic rollups; interface-sides per flow-hour for
flow_interface_5m (its ``flows`` column counts a record only in the slot the
record ended in, although its bytes are spread over earlier slots); guarded
records per RECEIVED hour for flow_exporter_minutes.

Never from truncated raw (X-11)
    A rebuild deletes the rollup hour and re-derives it from raw, so it must
    only ever read complete raw hours. Raw expires by TTL (admin-configurable
    down to 3 days), by storage purges and by mutations, while the rollups are
    kept far longer. Nothing is planned while ``flow_records`` has an unfinished
    mutation, and no hour older than the retention floor is touched: the
    configured raw retention or the live TTL (whichever is shorter) plus a
    margin, the oldest surviving raw hour, and the rollup's own TTL (so a
    back-filled hour is not TTL-deleted again straight away).

X-02 repair
    The pre-v2 collector stored NetFlow v5's sampling word unmasked and
    inflated sampled v5 traffic ~165x. migrate-137 corrects raw with an
    asynchronous mutation. Once no such mutation is pending, a one-off pass
    compares rollup and raw bytes for every hour holding sampled v5 records
    and queues the hours that disagree; rows that reach raw after the
    migration (a pre-v2 collector still running, a restored backup) are
    queued by hour and corrected with the same mutation. Queued hours are
    rebuilt while raw still covers them. The queue and the outcome live in the
    ``netflow_rollup_repair`` system setting (``repair_status()``): hours older
    than the raw retention at repair time could not be corrected, and the API
    discloses that.

Load
    Healer queries run with a bounded thread count, hours are converged one at
    a time with a pause in between, and each hour runs inside its own
    transaction-scoped Postgres advisory lock (the udt_sweeper pattern) so the
    two uvicorn workers never double-insert an hour — the loser of the race
    yields the pass. A newly created rollup converges over a month of raw in
    well under an hour; in steady state the views keep every count exact and a
    sweep only reads. Unchanged raw partitions reuse an exact census; a
    separate session advisory lock and shared next-due setting coordinate the
    expensive census across API workers without a long database transaction.

Deduplication
    migrate-144 gives every rollup table a block log so a collector retry
    (same insert_deduplication_token) is not counted twice through the views.
    The healer's own inserts carry no token and would be deduplicated by
    content, which silently drops an unchanged hour rebuilt after its DELETE,
    so they run with ``insert_deduplicate = 0`` (INSERT_SETTINGS).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine, get_ch_client
from app.services.netflow_census import RawCensusCache, FORMAT as CENSUS_FORMAT

logger = logging.getLogger("zenplus.netflow")

# Must match the volume guard in every view (migrate-137 §3.3) and the API's
# raw aggregates, or counts never converge.
VOLUME_GUARD = (
    "bytes / greatest(sampling_interval, 1) <= 1000000000000 "
    "AND fw_event NOT IN (1, 3)"
)
# NF-18: a duration is usable only when both switched times are present, in
# order, on the same clock (sysUptime ms vs epoch ms) and at most a day apart.
DURATION_VALID = (
    "first_switched_ms > 0 "
    "AND last_switched_ms >= first_switched_ms "
    "AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000) "
    "AND last_switched_ms - first_switched_ms <= 86400000"
)
# X-02: no correctly decoded v5 row can carry an interval above 14 bits.
X02_PREDICATE = "flow_version = 5 AND sampling_interval > 16383"

HOUR_S = 3600
SWEEP_INTERVAL_S = 6 * 3600
MIN_RESWEEP_S = 300
MUTATION_RECHECK_S = 60
STARTUP_DELAY_S = 20
# Late-exported flows for an hour keep arriving for a while after it ends;
# heals and forced rebuilds wait this long so they never race them.
HEAL_SETTLE_S = HOUR_S
# How far inside the raw retention line an hour must be before it may be
# rebuilt: TTL merges delete lazily, and a long sweep moves the line.
RETENTION_MARGIN_S = 6 * HOUR_S
MAX_HEALS_PER_SWEEP = 6
HEAL_PAUSE_S = 2.0
BACKFILL_PAUSE_S = 0.5
HEALER_MAX_THREADS = 4

NETFLOW_ROLLUP_ADVISORY_LOCK = 1515074392
NETFLOW_SWEEP_ADVISORY_LOCK = 1515074393
CENSUS_SETTINGS_KEY = "netflow_rollup_census"
REPAIR_SETTINGS_KEY = "netflow_rollup_repair"


# ─── ClickHouse access ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChTarget:
    """Where the rollups live. Tests point this at a private database."""

    database: str = "zenplus"
    client: Callable[[], Any] = get_ch_client


DEFAULT_TARGET = ChTarget()
_SETTINGS = {"max_threads": HEALER_MAX_THREADS}
# Every healer write into a rollup table (see rebuild_hour).
INSERT_SETTINGS = {"insert_deduplicate": 0}


def _rows(t: ChTarget, sql: str, params: dict | None = None) -> list[tuple]:
    return t.client().query(sql, parameters=params or {}, settings=_SETTINGS).result_rows


def _command(t: ChTarget, sql: str, params: dict | None = None, settings: dict | None = None) -> None:
    t.client().command(sql, parameters=params or {}, settings={**_SETTINGS, **(settings or {})})


async def _query(t: ChTarget, sql: str, params: dict | None = None) -> list[tuple]:
    return await asyncio.to_thread(_rows, t, sql, params)


async def _exec(t: ChTarget, sql: str, params: dict | None = None, settings: dict | None = None) -> None:
    await asyncio.to_thread(_command, t, sql, params, settings)


def _ch_time(epoch_s: int) -> str:
    """ClickHouse DateTime literal for a UTC epoch second (columns are UTC)."""
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso(epoch_s: int) -> str:
    return datetime.fromtimestamp(epoch_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Rollup SQL (mirrors of the views in migrate-137) ────────────────────────
#
# Each rollup is a (column, expression) list over an inner subquery that renames
# the raw columns, exactly as the view is written. The INSERT column list and
# the SELECT list are generated from the same tuples, so they cannot drift
# apart, and nothing depends on the table's physical column order.

_CONVERSATIONS_OUT: tuple[tuple[str, str], ...] = (
    ("timestamp", "hour_bucket"),
    ("exporter_ip", "exporter_ip"),
    ("src_addr", "src_addr"),
    ("dst_addr", "dst_addr"),
    ("protocol", "protocol"),
    ("dst_port", "dst_port"),
    ("exporter_ip6", "exporter_ip6"),
    ("src_addr6", "src_addr6"),
    ("dst_addr6", "dst_addr6"),
    ("bytes", "sum(raw_bytes)"),
    ("packets", "sum(raw_packets)"),
    ("flows", "count()"),
    ("src_ports", "groupUniqArrayState(10)(raw_src_port)"),
    ("input_snmp", "groupUniqArrayState(10)(raw_input_snmp)"),
    ("output_snmp", "groupUniqArrayState(10)(raw_output_snmp)"),
    ("first_seen", "min(raw_ts)"),
    ("last_seen", "max(raw_ts)"),
    ("received_at", "max(raw_received_at)"),
    ("duration_ms", "sum(raw_duration_ms)"),
    ("tcp_flags", "groupBitOr(toUInt64(raw_tcp_flags))"),
    ("input_ifs", "groupUniqArrayState(10)(raw_input_if)"),
    ("output_ifs", "groupUniqArrayState(10)(raw_output_if)"),
    ("flagged_flows", "sum(raw_flagged)"),
    ("legacy_flows", "sum(raw_legacy)"),
    ("duration_valid_flows", "sum(raw_duration_valid)"),
    ("duration_valid_ms", "sum(raw_duration_valid_ms)"),
    ("app_id", "max(raw_app_id)"),
)

_CONVERSATIONS_BODY = f"""
FROM (
    SELECT
        toStartOfHour(timestamp) AS hour_bucket,
        timestamp AS raw_ts,
        received_at AS raw_received_at,
        exporter_ip,
        src_addr,
        dst_addr,
        protocol,
        dst_port,
        exporter_ip6,
        src_addr6,
        dst_addr6,
        src_port AS raw_src_port,
        toUInt16(least(input_if, 65535)) AS raw_input_snmp,
        toUInt16(least(output_if, 65535)) AS raw_output_snmp,
        input_if AS raw_input_if,
        output_if AS raw_output_if,
        bytes AS raw_bytes,
        packets AS raw_packets,
        toInt64(last_switched_ms) - toInt64(first_switched_ms) AS raw_duration_ms,
        tcp_flags AS raw_tcp_flags,
        toUInt64(if(fields_present = 0, tcp_flags != 0, bitTest(fields_present, 0))) AS raw_flagged,
        toUInt64(fields_present = 0) AS raw_legacy,
        toUInt64({DURATION_VALID}) AS raw_duration_valid,
        if(raw_duration_valid = 1, raw_duration_ms, 0) AS raw_duration_valid_ms,
        app_id AS raw_app_id
    FROM {{db}}.flow_records
    WHERE {VOLUME_GUARD}
      AND timestamp >= %(lo)s AND timestamp < %(hi)s
)
GROUP BY hour_bucket, exporter_ip, src_addr, dst_addr, protocol, dst_port, exporter_ip6, src_addr6, dst_addr6
"""

_QOS_OUT: tuple[tuple[str, str], ...] = (
    ("timestamp", "hour_bucket"),
    ("exporter_ip", "exporter_ip"),
    ("protocol", "protocol"),
    ("tos", "tos"),
    ("tcp_flags", "tcp_flags"),
    ("exporter_ip6", "exporter_ip6"),
    ("flags_known", "flags_known"),
    ("tos_known", "tos_known"),
    ("bytes", "sum(raw_bytes)"),
    ("packets", "sum(raw_packets)"),
    ("flows", "count()"),
    ("empty_flows", "countIf(raw_packets = 0)"),
    ("duration_ms", "sum(raw_duration_ms)"),
    ("received_at", "max(raw_received_at)"),
    ("duration_valid_flows", "sum(raw_duration_valid)"),
    ("duration_valid_ms", "sum(raw_duration_valid_ms)"),
)

_QOS_BODY = f"""
FROM (
    SELECT
        toStartOfHour(timestamp) AS hour_bucket,
        received_at AS raw_received_at,
        exporter_ip,
        protocol,
        tos,
        tcp_flags,
        exporter_ip6,
        toUInt8(multiIf(fields_present = 0, 2, bitTest(fields_present, 0), 1, 0)) AS flags_known,
        toUInt8(multiIf(fields_present = 0, 2, bitTest(fields_present, 1), 1, 0)) AS tos_known,
        bytes AS raw_bytes,
        packets AS raw_packets,
        toInt64(last_switched_ms) - toInt64(first_switched_ms) AS raw_duration_ms,
        toUInt64({DURATION_VALID}) AS raw_duration_valid,
        if(raw_duration_valid = 1, raw_duration_ms, 0) AS raw_duration_valid_ms
    FROM {{db}}.flow_records
    WHERE {VOLUME_GUARD}
      AND timestamp >= %(lo)s AND timestamp < %(hi)s
)
GROUP BY hour_bucket, exporter_ip, protocol, tos, tcp_flags, exporter_ip6, flags_known, tos_known
"""

_TRAFFIC_OUT: tuple[tuple[str, str], ...] = (
    ("timestamp", "slot_bucket"),
    ("exporter_ip", "exporter_ip"),
    ("protocol", "protocol"),
    ("dst_port", "dst_port"),
    ("bytes", "sum(raw_bytes)"),
    ("packets", "sum(raw_packets)"),
    ("flow_count", "count()"),
)

_TRAFFIC_BODY = f"""
FROM (
    SELECT
        toStartOfFiveMinutes(timestamp) AS slot_bucket,
        exporter_ip,
        protocol,
        dst_port,
        bytes AS raw_bytes,
        packets AS raw_packets
    FROM {{db}}.flow_records
    WHERE {VOLUME_GUARD}
      AND timestamp >= %(lo)s AND timestamp < %(hi)s
)
GROUP BY slot_bucket, exporter_ip, protocol, dst_port
"""

_INTERFACE_OUT: tuple[tuple[str, str], ...] = (
    ("timestamp", "toDateTime(intDiv(slot_ms, 1000), 'UTC')"),
    ("exporter_ip", "exporter_ip"),
    ("exporter_ip6", "exporter_ip6"),
    ("ifindex", "ifindex"),
    ("direction", "direction"),
    ("flow_direction", "flow_direction"),
    ("bytes", "sum(raw_bytes * slot_share)"),
    ("packets", "sum(raw_packets * slot_share)"),
    ("flows", "sum(toUInt64(slot_ms = end_slot_ms))"),
)

# A slot hour S receives the bytes of records ending in S and, through
# spreading (at most one hour back), of records ending in the next hour, so it
# is rebuilt from raw [S, S + 2h) keeping only the slots inside S.
_INTERFACE_BODY = f"""
FROM (
    SELECT
        exporter_ip,
        exporter_ip6,
        ifindex,
        direction,
        flow_direction,
        raw_bytes,
        raw_packets,
        end_slot_ms,
        arrayJoin(range(intDiv(end_ms - span_ms, 300000) * 300000, end_slot_ms + 1, 300000)) AS slot_ms,
        if(span_ms = 0,
           toFloat64(slot_ms = end_slot_ms),
           (least(end_ms, slot_ms + 300000) - greatest(end_ms - span_ms, slot_ms)) / span_ms) AS slot_share
    FROM (
        SELECT
            exporter_ip,
            exporter_ip6,
            flow_direction,
            arrayJoin(arrayFilter(side -> side.2 != 0,
                [(toUInt8(0), input_if), (toUInt8(1), output_if)])) AS if_side,
            if_side.1 AS direction,
            if_side.2 AS ifindex,
            toFloat64(bytes) AS raw_bytes,
            toFloat64(packets) AS raw_packets,
            toUnixTimestamp64Milli(timestamp) AS end_ms,
            intDiv(end_ms, 300000) * 300000 AS end_slot_ms,
            if({DURATION_VALID},
               toInt64(least(last_switched_ms - first_switched_ms, 3600000)),
               toInt64(0)) AS span_ms
        FROM {{db}}.flow_records
        WHERE {VOLUME_GUARD}
          AND timestamp >= %(lo)s AND timestamp < %(raw_hi)s
    )
)
WHERE slot_ms >= %(slot_lo_ms)s AND slot_ms < %(slot_hi_ms)s
GROUP BY timestamp, exporter_ip, exporter_ip6, ifindex, direction, flow_direction
"""

_EXPORTER_MINUTES_OUT: tuple[tuple[str, str], ...] = (
    ("minute", "minute_bucket"),
    ("exporter_ip", "exporter_ip"),
    ("exporter_ip6", "exporter_ip6"),
    ("observation_domain", "observation_domain"),
    ("records", "countIf(counted)"),
    ("bytes", "sumIf(raw_bytes, counted)"),
    ("tcp_records", "countIf(counted AND protocol = 6)"),
    ("tcp_flagged_records", "countIf(counted AND protocol = 6 AND flagged)"),
    ("v6_records", "countIf(counted AND ip_version = 6)"),
    ("sampled_records", "countIf(counted AND raw_sampling > 1)"),
    ("legacy_records", "countIf(counted AND raw_fields_present = 0)"),
    ("max_sampling", "maxIf(raw_sampling, counted)"),
    ("first_received_at", "min(raw_received_at)"),
    ("last_received_at", "max(raw_received_at)"),
    ("min_flow_ts", "min(raw_ts)"),
    ("max_flow_ts", "max(raw_ts)"),
    ("lag_ms_min", "min(lag_ms)"),
    ("lag_ms_max", "max(lag_ms)"),
)

# Keyed by received_at, which raw is not sorted or partitioned by, so the
# rebuild is also bounded by the flow times that hour's records carry (from
# the census, widened to the collector's clock-clamp window) to keep it off a
# full-table scan.
_EXPORTER_MINUTES_BODY = f"""
FROM (
    SELECT
        toStartOfMinute(toDateTime(received_at, 'UTC')) AS minute_bucket,
        received_at AS raw_received_at,
        timestamp AS raw_ts,
        exporter_ip,
        exporter_ip6,
        observation_domain,
        protocol,
        ip_version,
        bytes AS raw_bytes,
        sampling_interval AS raw_sampling,
        fields_present AS raw_fields_present,
        if(fields_present = 0, tcp_flags != 0, bitTest(fields_present, 0)) AS flagged,
        ({VOLUME_GUARD}) AS counted,
        toUnixTimestamp64Milli(received_at) - toUnixTimestamp64Milli(timestamp) AS lag_ms
    FROM {{db}}.flow_records
    WHERE received_at >= %(lo)s AND received_at < %(hi)s
      AND timestamp >= fromUnixTimestamp64Milli(toInt64(%(ts_lo_ms)s), 'UTC')
      AND timestamp <= fromUnixTimestamp64Milli(toInt64(%(ts_hi_ms)s), 'UTC')
)
GROUP BY minute_bucket, exporter_ip, exporter_ip6, observation_domain
"""


def _insert_sql(database: str, table: str, out: tuple[tuple[str, str], ...], body: str) -> str:
    columns = ", ".join(col for col, _ in out)
    select = ",\n    ".join(expr if expr == col else f"{expr} AS {col}" for col, expr in out)
    return (
        f"INSERT INTO {database}.{table} ({columns})\n"
        f"SELECT\n    {select}\n{body.replace('{db}', database)}"
    )


# ─── Rollup registry ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RollupSpec:
    table: str
    time_col: str                      # the rollup's bucket column
    out: tuple[tuple[str, str], ...]
    body: str
    clock: str                         # 'flow' = raw timestamp hours, 'received' = raw received_at hours
    raw_count: str                     # census field the rollup count converges to
    raw_present: str                   # census field that says "raw has something for this hour"
    rollup_count: str                  # rollup expression compared with raw_count
    rollup_bytes: str | None           # UInt64 byte total, for the X-02 byte reconciliation
    absent_by: str                     # 'rows' | 'count': how "never rolled up" shows
    lookahead_h: int = 0               # raw hours after H that also feed H's rows
    rebuild_neighbor: bool = False     # a change to H also changes H - 1 (spreading)
    probe_column: str = ""             # a schema-v2 column: its absence means migrate-137 is pending

    def insert_sql(self, database: str) -> str:
        return _insert_sql(database, self.table, self.out, self.body)


CONVERSATIONS = RollupSpec(
    table="flow_conversations_1h", time_col="timestamp",
    out=_CONVERSATIONS_OUT, body=_CONVERSATIONS_BODY,
    clock="flow", raw_count="flows", raw_present="flows",
    rollup_count="sum(flows)", rollup_bytes="sum(bytes)", absent_by="rows",
    probe_column="duration_valid_flows",
)
QOS = RollupSpec(
    table="flow_qos_1h", time_col="timestamp",
    out=_QOS_OUT, body=_QOS_BODY,
    clock="flow", raw_count="flows", raw_present="flows",
    rollup_count="sum(flows)", rollup_bytes="sum(bytes)", absent_by="rows",
    probe_column="tos_known",
)
TRAFFIC_5M = RollupSpec(
    table="flow_traffic_5m", time_col="timestamp",
    out=_TRAFFIC_OUT, body=_TRAFFIC_BODY,
    clock="flow", raw_count="flows", raw_present="flows",
    rollup_count="sum(flow_count)", rollup_bytes="sum(bytes)", absent_by="rows",
)
INTERFACE_5M = RollupSpec(
    table="flow_interface_5m", time_col="timestamp",
    out=_INTERFACE_OUT, body=_INTERFACE_BODY,
    clock="flow", raw_count="if_sides", raw_present="if_sides",
    # Rows can exist in a slot hour purely from the next hour's spread-back,
    # so "never rolled up" is judged on the end-slot flow count, not on rows.
    rollup_count="sum(flows)", rollup_bytes=None, absent_by="count",
    lookahead_h=1, rebuild_neighbor=True,
)
EXPORTER_MINUTES = RollupSpec(
    table="flow_exporter_minutes", time_col="minute",
    out=_EXPORTER_MINUTES_OUT, body=_EXPORTER_MINUTES_BODY,
    clock="received", raw_count="counted", raw_present="total",
    rollup_count="sum(records)", rollup_bytes=None, absent_by="rows",
)

ROLLUPS: tuple[RollupSpec, ...] = (CONVERSATIONS, QOS, TRAFFIC_5M, INTERFACE_5M, EXPORTER_MINUTES)
X02_RECONCILED = (CONVERSATIONS, QOS, TRAFFIC_5M)

# New-collector rows are clamped to [received - 7200 s, received + 120 s];
# widen the census bounds by that so rows spooled into raw after the census
# are still inside a received-hour rebuild.
_CLAMP_BEFORE_MS = 7200 * 1000 + 60_000
_CLAMP_AFTER_MS = 120 * 1000 + 60_000


# ─── Census ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RolledHour:
    rows: int
    count: int
    bytes: int


def _flow_census_sql(database: str) -> str:
    # Aliases never reuse a raw column name: one would capture the column
    # inside the other aggregates' guard.
    return f"""
        SELECT toUInt32(toUnixTimestamp(toStartOfHour(timestamp))) AS h,
               countIf({VOLUME_GUARD}) AS n_flows,
               sumIf(toUInt64(input_if != 0) + toUInt64(output_if != 0), {VOLUME_GUARD}) AS n_if_sides,
               sumIf(bytes, {VOLUME_GUARD}) AS n_bytes,
               countIf(flow_version = 5 AND sampling_interval > 1) AS n_v5_sampled
        FROM {database}.flow_records
        WHERE timestamp >= %(lo)s AND timestamp < %(hi)s
        GROUP BY h
    """


def _received_census_sql(database: str) -> str:
    return f"""
        SELECT toUInt32(toUnixTimestamp(toStartOfHour(toDateTime(received_at, 'UTC')))) AS h,
               countIf({VOLUME_GUARD}) AS n_counted,
               count() AS n_total,
               sumIf(bytes, {VOLUME_GUARD}) AS n_bytes,
               toUnixTimestamp64Milli(min(timestamp)) AS n_ts_min_ms,
               toUnixTimestamp64Milli(max(timestamp)) AS n_ts_max_ms
        FROM {database}.flow_records
        WHERE received_at >= %(lo)s AND received_at < %(hi)s
        GROUP BY h
    """


_FLOW_FIELDS = ("flows", "if_sides", "bytes", "v5_sampled")
_RECEIVED_FIELDS = ("counted", "total", "bytes", "ts_min_ms", "ts_max_ms")


async def raw_census(t: ChTarget, clock: str, lo_s: int, hi_s: int) -> dict[int, dict[str, int]]:
    """Per-hour raw totals for [lo, hi) on the given clock ('flow' | 'received')."""
    sql, fields = (
        (_flow_census_sql(t.database), _FLOW_FIELDS) if clock == "flow"
        else (_received_census_sql(t.database), _RECEIVED_FIELDS)
    )
    rows = await _query(t, sql, {"lo": _ch_time(lo_s), "hi": _ch_time(hi_s)})
    return {int(r[0]): {f: int(v or 0) for f, v in zip(fields, r[1:])} for r in rows}


async def rollup_census(t: ChTarget, spec: RollupSpec, lo_s: int, hi_s: int) -> dict[int, RolledHour]:
    rows = await _query(
        t,
        f"""
        SELECT toUInt32(toUnixTimestamp(toStartOfHour({spec.time_col}))) AS h,
               count(), {spec.rollup_count}, {spec.rollup_bytes or '0'}
        FROM {t.database}.{spec.table}
        WHERE {spec.time_col} >= %(lo)s AND {spec.time_col} < %(hi)s
        GROUP BY h
        """,
        {"lo": _ch_time(lo_s), "hi": _ch_time(hi_s)},
    )
    return {int(r[0]): RolledHour(int(r[1]), int(r[2] or 0), int(r[3] or 0)) for r in rows}


def _partition_census_sql(database: str, clock: str) -> str:
    """Exact per-partition results for a bounded batch of raw partitions."""
    if clock == "flow":
        sql = _flow_census_sql(database)
        extras = f", count(), toUnixTimestamp64Milli(min(timestamp)), countIf({X02_PREDICATE})\n"
        window = "timestamp >= %(lo)s AND timestamp < %(hi)s"
    else:
        sql = _received_census_sql(database)
        extras = f", countIf({X02_PREDICATE})\n"
        window = "received_at >= %(lo)s AND received_at < %(hi)s"
    return sql.replace(f"FROM {database}.flow_records", extras + f"FROM {database}.flow_records").replace(
        "SELECT toUInt32", "SELECT _partition_id AS census_partition, toUInt32", 1).replace(
        window, "_partition_id IN %(partitions)s").replace("GROUP BY h", "GROUP BY census_partition, h")


def _raw_cache(t: ChTarget, stored: dict | None = None) -> RawCensusCache:
    return RawCensusCache(lambda sql, params: _query(t, sql, params), t.database, stored)


# ─── Retention floor ─────────────────────────────────────────────────────────


def retention_floor(
    now_s: int,
    *,
    oldest_raw_s: int | None,
    raw_retention_days: int | None,
    rollup_ttl_days: int | None,
) -> int | None:
    """Oldest hour start whose raw is complete and whose rollup rows will stick.

    Returns None when raw holds nothing. The oldest surviving raw hour itself is
    excluded: a TTL merge or a row-precise purge may have taken part of it.
    """
    if oldest_raw_s is None:
        return None
    floor = oldest_raw_s - oldest_raw_s % HOUR_S + HOUR_S
    for days in (raw_retention_days, rollup_ttl_days):
        if days:
            floor = max(floor, now_s - int(days) * 86400 + RETENTION_MARGIN_S)
    return -(-floor // HOUR_S) * HOUR_S


async def _table_ttl_days(t: ChTarget, table: str) -> tuple[bool, int | None]:
    """(has_ttl, days) for a table, parsed the way storage_service does."""
    from app.services.storage_service import _parse_ttl_days

    rows = await _query(
        t,
        "SELECT engine_full FROM system.tables WHERE database = %(db)s AND name = %(n)s",
        {"db": t.database, "n": table},
    )
    engine_full = str(rows[0][0]) if rows else ""
    return ("TTL" in engine_full, _parse_ttl_days(engine_full))


async def raw_retention_days(t: ChTarget) -> int | None:
    """How long raw flows are kept: the configured 'flows' retention or the
    live flow_records TTL, whichever is shorter (a lowered setting whose TTL
    change has not applied yet, or a TTL an operator set by hand). None when
    neither bounds it."""
    from app.services.storage_service import CATEGORIES, get_retention_settings

    configured: int | None = None
    try:
        async with AsyncSessionLocal() as db:
            settings = await get_retention_settings(db)
        value = settings["table_overrides"].get("flow_records", settings["categories"].get("flows"))
        if value:
            configured = max(int(value), int(CATEGORIES["flows"]["min_days"]))
    except Exception:
        # Without the setting the live TTL still bounds raw; never guess longer.
        logger.warning("netflow rollup: storage retention settings unreadable, using the live TTL")

    has_ttl, live = await _table_ttl_days(t, "flow_records")
    if has_ttl and live is None:
        # A TTL we cannot parse: assume the product default rather than "forever".
        live = int(CATEGORIES["flows"]["default_days"])
    known = [d for d in (configured, live) if d]
    return min(known) if known else None


# ─── Planning ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Planned:
    hour: int          # epoch seconds of the hour start
    reason: str        # 'backfill' | 'heal' | 'repair' | 'neighbor'


@dataclass
class Plan:
    items: list[Planned] = field(default_factory=list)
    next_ready_s: int | None = None     # earliest time deferred work becomes ready
    unrecoverable: list[int] = field(default_factory=list)   # queued hours raw no longer covers


def _absent(spec: RollupSpec, have: RolledHour | None) -> bool:
    if have is None:
        return True
    return (have.rows if spec.absent_by == "rows" else have.count) == 0


def ready_at(spec: RollupSpec, hour: int, reason: str) -> int:
    """Earliest time an hour may be rebuilt for the given reason."""
    window_end = hour + (1 + spec.lookahead_h) * HOUR_S
    if reason == "backfill" and spec.lookahead_h == 0:
        # An insert racing the backfill shows up as a count mismatch and is
        # healed; spread-back bytes (lookahead) would not, so those wait.
        return window_end
    return window_end + HEAL_SETTLE_S


def plan_rollup(
    spec: RollupSpec,
    raw: dict[int, dict[str, int]],
    rolled: dict[int, RolledHour],
    *,
    now_s: int,
    floor_s: int,
    queued: set[int] | frozenset[int] = frozenset(),
    max_heals: int = MAX_HEALS_PER_SWEEP,
) -> Plan:
    plan = Plan()
    heals = 0
    current_hour = now_s - now_s % HOUR_S
    for hour in sorted(set(raw) | set(queued)):
        r = raw.get(hour)
        have = rolled.get(hour)
        if hour < floor_s:
            if hour in queued:
                plan.unrecoverable.append(hour)
            continue
        present = bool(r and r[spec.raw_present] > 0)
        if hour in queued:
            if not present and hour < current_hour:
                # Raw has nothing left for the hour: a rebuild would only
                # delete the rollup's history.
                plan.unrecoverable.append(hour)
                continue
            reason = "repair"
        elif not present:
            continue
        elif _absent(spec, have):
            reason = "backfill"
        elif have is not None and have.count != r[spec.raw_count]:
            reason = "heal"
        else:
            continue
        ready = ready_at(spec, hour, reason)
        if ready > now_s:
            plan.next_ready_s = ready if plan.next_ready_s is None else min(plan.next_ready_s, ready)
            continue
        if reason == "heal":
            if heals >= max_heals:
                continue
            heals += 1
        plan.items.append(Planned(hour, reason))

    if spec.rebuild_neighbor:
        planned = {p.hour for p in plan.items}
        for p in list(plan.items):
            prev = p.hour - HOUR_S
            if prev >= floor_s and prev not in planned:
                plan.items.append(Planned(prev, "neighbor"))
                planned.add(prev)
        plan.items.sort(key=lambda p: p.hour)
    return plan


# ─── Converging one hour ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class HourState:
    raw_count: int
    raw_present: int
    rows: int
    count: int


def _window_params(spec: RollupSpec, hour: int, raw_hint: dict[str, int] | None) -> dict[str, Any]:
    params: dict[str, Any] = {"lo": _ch_time(hour), "hi": _ch_time(hour + HOUR_S)}
    if spec.lookahead_h:
        params["raw_hi"] = _ch_time(hour + (1 + spec.lookahead_h) * HOUR_S)
        params["slot_lo_ms"] = hour * 1000
        params["slot_hi_ms"] = (hour + HOUR_S) * 1000
    if spec.clock == "received":
        ts_lo = hour * 1000 - _CLAMP_BEFORE_MS
        ts_hi = (hour + HOUR_S) * 1000 + _CLAMP_AFTER_MS
        if raw_hint and raw_hint.get("total"):
            ts_lo = min(ts_lo, raw_hint["ts_min_ms"])
            ts_hi = max(ts_hi, raw_hint["ts_max_ms"])
        params["ts_lo_ms"] = ts_lo
        params["ts_hi_ms"] = ts_hi
    return params


async def hour_state(t: ChTarget, spec: RollupSpec, hour: int, params: dict[str, Any]) -> HourState:
    if spec.clock == "flow":
        raw = await _query(
            t,
            f"""
            SELECT countIf({VOLUME_GUARD}),
                   sumIf(toUInt64(input_if != 0) + toUInt64(output_if != 0), {VOLUME_GUARD})
            FROM {t.database}.flow_records
            WHERE timestamp >= %(lo)s AND timestamp < %(hi)s
            """,
            params,
        )
        values = {"flows": int(raw[0][0] or 0), "if_sides": int(raw[0][1] or 0)}
    else:
        raw = await _query(
            t,
            f"""
            SELECT countIf({VOLUME_GUARD}), count()
            FROM {t.database}.flow_records
            WHERE received_at >= %(lo)s AND received_at < %(hi)s
              AND timestamp >= fromUnixTimestamp64Milli(toInt64(%(ts_lo_ms)s), 'UTC')
              AND timestamp <= fromUnixTimestamp64Milli(toInt64(%(ts_hi_ms)s), 'UTC')
            """,
            params,
        )
        values = {"counted": int(raw[0][0] or 0), "total": int(raw[0][1] or 0)}
    rolled = await _query(
        t,
        f"""
        SELECT count(), {spec.rollup_count}
        FROM {t.database}.{spec.table}
        WHERE {spec.time_col} >= %(lo)s AND {spec.time_col} < %(hi)s
        """,
        params,
    )
    return HourState(
        raw_count=values[spec.raw_count],
        raw_present=values[spec.raw_present],
        rows=int(rolled[0][0] or 0),
        count=int(rolled[0][1] or 0),
    )


async def rebuild_hour(
    t: ChTarget, spec: RollupSpec, hour: int, params: dict[str, Any], *, has_rows: bool
) -> None:
    """Replace the rollup's rows for one hour with a fresh derivation from raw."""
    if has_rows:
        await _exec(
            t,
            f"ALTER TABLE {t.database}.{spec.table} "
            f"DELETE WHERE {spec.time_col} >= %(lo)s AND {spec.time_col} < %(hi)s",
            params,
            settings={"mutations_sync": 1},
        )
    # migrate-144 gives the rollup tables a block log so collector retries are
    # not counted twice. An untokened insert is then deduplicated by content,
    # and an unchanged hour rebuilt after the DELETE above is byte-identical to
    # the block it replaces: with deduplication on it would be dropped silently
    # and the hour left empty.
    await _exec(t, spec.insert_sql(t.database), params, settings=INSERT_SETTINGS)


def still_needed(spec: RollupSpec, reason: str, state: HourState) -> bool:
    """Re-check a planned action against the hour as it is now (under the lock):
    the other worker may have converged it, or late rows may have arrived."""
    if reason == "backfill":
        # Rows that appeared since planning came from the view; inserting on
        # top would double-count them, so leave that for a heal.
        absent = (state.rows if spec.absent_by == "rows" else state.count) == 0
        return state.raw_present > 0 and absent
    if reason == "heal":
        return state.raw_present > 0 and state.count != state.raw_count
    if reason == "repair":
        return state.raw_present > 0
    return True     # neighbor: its content depends on the next hour, not on a count


# ─── X-02 repair bookkeeping (system_settings) ───────────────────────────────


def _empty_repair_state() -> dict[str, Any]:
    return {
        "x02": {
            "reconciled_at": None,
            "verified_from": None,
            "rows_found_after_upgrade": 0,
            "queue": {},
            "rebuilt": {},
            "unrecoverable": {},
        }
    }


async def _load_repair_state(db) -> dict[str, Any]:
    row = (await db.execute(
        text("SELECT value FROM system_settings WHERE key = :k"), {"k": REPAIR_SETTINGS_KEY}
    )).first()
    state = _empty_repair_state()
    if row and isinstance(row[0], dict):
        state["x02"].update(row[0].get("x02") or {})
    return state


async def _save_repair_state(db, state: dict[str, Any]) -> None:
    await db.execute(
        text(
            "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb), updated_at = NOW()"
        ),
        {"k": REPAIR_SETTINGS_KEY, "v": json.dumps(state)},
    )


async def repair_status() -> dict[str, Any]:
    """X-02 repair outcome for disclosure: when raw was reconciled, from which
    hour rollups could be verified, what is still queued and what could not be
    corrected because raw had already expired."""
    async with AsyncSessionLocal() as db:
        state = await _load_repair_state(db)
    x02 = state["x02"]
    return {
        "reconciled_at": x02["reconciled_at"],
        "verified_from": x02["verified_from"],
        "pending_hours": {k: len(v) for k, v in x02["queue"].items() if v},
        "rebuilt_hours": x02["rebuilt"],
        "unrecoverable_hours": x02["unrecoverable"],
        "rows_found_after_upgrade": x02["rows_found_after_upgrade"],
    }


def _enqueue(state: dict[str, Any], table: str, hours) -> int:
    queue = state["x02"]["queue"]
    current = set(queue.get(table) or [])
    added = set(int(h) for h in hours) - current
    if added:
        queue[table] = sorted(current | added)
    return len(added)


def _dequeue(state: dict[str, Any], table: str, hour: int, outcome: str) -> None:
    queue = state["x02"]["queue"]
    if hour not in (queue.get(table) or []):
        return      # the other worker settled it first
    remaining = [h for h in queue[table] if h != hour]
    if remaining:
        queue[table] = remaining
    else:
        queue.pop(table, None)
    tally = state["x02"][outcome]
    tally[table] = int(tally.get(table, 0)) + 1


class _Locked:
    """One short transaction holding the rollup advisory lock (or not)."""

    def __init__(self) -> None:
        self.db = None
        self.acquired = False

    async def __aenter__(self) -> "_Locked":
        self._cm = AsyncSessionLocal()
        self.db = await self._cm.__aenter__()
        self.acquired = bool((await self.db.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": NETFLOW_ROLLUP_ADVISORY_LOCK}
        )).scalar())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                await self.db.commit()
        finally:
            await self._cm.__aexit__(exc_type, exc, tb)


# ─── X-02 gate ───────────────────────────────────────────────────────────────


def x02_mutation_sql(database: str) -> str:
    """The raw correction migrate-137 issues, reissued for rows that reached raw
    after it ran. Idempotent: corrected rows no longer match the predicate."""
    return (
        f"ALTER TABLE {database}.flow_records UPDATE "
        "bytes = intDiv(bytes, sampling_interval) * greatest(bitAnd(sampling_interval, 16383), 1), "
        "packets = intDiv(packets, sampling_interval) * greatest(bitAnd(sampling_interval, 16383), 1), "
        "sampling_interval = greatest(bitAnd(sampling_interval, 16383), 1) "
        f"WHERE {X02_PREDICATE}"
    )


async def pending_raw_mutations(t: ChTarget) -> list[tuple[str, str, str]]:
    """Unfinished mutations on flow_records: (id, command, latest failure)."""
    rows = await _query(
        t,
        "SELECT mutation_id, command, latest_fail_reason FROM system.mutations "
        "WHERE database = %(db)s AND table = 'flow_records' AND NOT is_done",
        {"db": t.database},
    )
    return [(str(r[0]), str(r[1]), str(r[2] or "")) for r in rows]


async def _queue_unmasked_rows(t: ChTarget, cache: RawCensusCache | None = None) -> bool:
    """Queue every hour holding unmasked v5 rows and correct them. True when
    such rows were found (the caller then waits for the mutation)."""
    if cache is not None:
        flow = cache.census("flow", 0, 2**32)
        received = cache.census("received", 0, 2**32)
        count = sum(row["unmasked"] for row in flow.values())
        if not count:
            return False
        flow_hours = [hour for hour, row in flow.items() if row["unmasked"]]
        received_hours = [hour for hour, row in received.items() if row["unmasked"]]
    else:
        found = await _query(t, f"SELECT count() FROM {t.database}.flow_records WHERE {X02_PREDICATE}")
        count = int(found[0][0] or 0)
        if not count:
            return False
        flow_hours = [int(r[0]) for r in await _query(
            t,
            f"SELECT DISTINCT toUInt32(toUnixTimestamp(toStartOfHour(timestamp))) "
            f"FROM {t.database}.flow_records WHERE {X02_PREDICATE}",
        )]
        received_hours = [int(r[0]) for r in await _query(
            t,
            f"SELECT DISTINCT toUInt32(toUnixTimestamp(toStartOfHour(toDateTime(received_at, 'UTC')))) "
            f"FROM {t.database}.flow_records WHERE {X02_PREDICATE}",
        )]
    async with _Locked() as lock:
        if not lock.acquired:
            return True
        state = await _load_repair_state(lock.db)
        for spec in ROLLUPS:
            _enqueue(state, spec.table, received_hours if spec.clock == "received" else flow_hours)
        state["x02"]["rows_found_after_upgrade"] = int(state["x02"]["rows_found_after_upgrade"]) + count
        await _save_repair_state(lock.db, state)
        # Issued after the hours are safely queued: once it completes, the
        # damaged rows can no longer be told apart from correct ones.
        await _exec(t, x02_mutation_sql(t.database), settings={"mutations_sync": 0})
    logger.warning(
        "netflow rollup: %d NetFlow v5 record(s) with an unmasked sampling header reached raw "
        "after migrate-137 (a pre-v2 collector, or restored data); correcting them and "
        "rebuilding %d flow hour(s) of rollups", count, len(flow_hours),
    )
    return True


async def _reconcile_x02(
    t: ChTarget,
    flow_raw: dict[int, dict[str, int]],
    censuses: dict[str, dict[int, RolledHour]],
    floor_s: int,
    now_s: int,
) -> None:
    """One-off: queue every hour whose rollup bytes disagree with the corrected raw.

    Only hours holding sampled v5 records can have been inflated, and only
    rollups that existed before migrate-137 hold pre-correction bytes (the new
    ones are back-filled after the correction finishes).
    """
    candidates = [h for h, r in flow_raw.items() if r["v5_sampled"] > 0]
    async with _Locked() as lock:
        if not lock.acquired:
            return
        state = await _load_repair_state(lock.db)
        if state["x02"]["reconciled_at"]:
            return
        queued = 0
        for spec in X02_RECONCILED:
            rolled = censuses.get(spec.table) or {}
            stale = [
                h for h in candidates
                if (rolled.get(h) is not None and rolled[h].bytes != flow_raw[h]["bytes"])
            ]
            queued += _enqueue(state, spec.table, stale)
        state["x02"]["reconciled_at"] = _iso(now_s)
        state["x02"]["verified_from"] = _iso(floor_s)
        await _save_repair_state(lock.db, state)
    if queued:
        logger.warning(
            "netflow rollup: %d rollup hour(s) hold traffic inflated by the unmasked NetFlow v5 "
            "sampling header; rebuilding them from corrected raw", queued,
        )
    logger.info(
        "netflow rollup: X-02 reconciliation done; rollup hours before %s predate raw retention "
        "and could not be verified", _iso(floor_s),
    )


# ─── Sweep ───────────────────────────────────────────────────────────────────


async def _schema_ready(t: ChTarget) -> dict[str, bool]:
    """Which rollups can be converged: table present and schema v2 applied."""
    rows = await _query(
        t,
        "SELECT table, name FROM system.columns WHERE database = %(db)s "
        "AND table IN %(tables)s",
        {"db": t.database, "tables": ["flow_records", *[s.table for s in ROLLUPS]]},
    )
    columns = {(str(r[0]), str(r[1])) for r in rows}
    if ("flow_records", "fw_event") not in columns:
        return {s.table: False for s in ROLLUPS}
    return {
        s.table: (s.table, s.time_col) in columns
        and (not s.probe_column or (s.table, s.probe_column) in columns)
        for s in ROLLUPS
    }


async def _converge(
    t: ChTarget,
    spec: RollupSpec,
    plan: Plan,
    raw: dict[int, dict[str, int]],
    *,
    floor_at: Callable[[int], int | None],
) -> tuple[int, bool]:
    """Work through one rollup's plan. Returns (hours done, yielded)."""
    done = 0
    for item in plan.items:
        # The retention line keeps moving while a long back-fill runs; an hour
        # that has crossed it is left alone.
        floor_now = floor_at(_now_s())
        if floor_now is None or item.hour < floor_now:
            continue
        params = _window_params(spec, item.hour, raw.get(item.hour))
        started = time.monotonic()
        # Fresh short transaction per hour: the xact-scoped advisory lock is
        # held across this hour's ClickHouse work only, so a competing worker
        # is blocked for seconds, not for a whole back-fill.
        async with _Locked() as lock:
            if not lock.acquired:
                logger.info("netflow rollup: another worker is converging, yielding")
                return done, True
            state = None
            if item.reason == "repair":
                state = await _load_repair_state(lock.db)
                if item.hour not in set(state["x02"]["queue"].get(spec.table) or []):
                    continue    # the other worker already rebuilt it
            current = await hour_state(t, spec, item.hour, params)
            if still_needed(spec, item.reason, current):
                if item.reason == "heal":
                    logger.warning(
                        "netflow rollup: rebuilding mismatched %s hour %s (rollup %d, raw %d)",
                        spec.table, _iso(item.hour), current.count, current.raw_count,
                    )
                await rebuild_hour(t, spec, item.hour, params, has_rows=current.rows > 0)
                done += 1
            if state is not None:
                _dequeue(state, spec.table, item.hour,
                         "rebuilt" if current.raw_present > 0 else "unrecoverable")
                await _save_repair_state(lock.db, state)
        pause = BACKFILL_PAUSE_S if item.reason == "backfill" else HEAL_PAUSE_S
        # Duty-cycle the back-fill: never busier than half the time, however
        # slow the appliance.
        await asyncio.sleep(max(pause, time.monotonic() - started))
    return done, False


def _now_s() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def sweep_once(t: ChTarget = DEFAULT_TARGET, cache: RawCensusCache | None = None) -> float:
    """One convergence pass over every rollup. Returns seconds until the next."""
    ready = await _schema_ready(t)
    if not any(ready.values()):
        logger.info("netflow rollup: schema v2 (migrate-137) not applied yet, skipping")
        return SWEEP_INTERVAL_S

    # Raw that is being rewritten (the X-02 correction, a purge, a TTL
    # materialisation) cannot be compared with anything.
    pending = await pending_raw_mutations(t)
    if pending:
        failing = [p for p in pending if p[2]]
        if failing:
            logger.warning(
                "netflow rollup: waiting on %d flow_records mutation(s); %s is failing: %s",
                len(pending), failing[0][0], failing[0][2][:300],
            )
        else:
            logger.info("netflow rollup: waiting on %d flow_records mutation(s)", len(pending))
        return MUTATION_RECHECK_S
    cache = cache if cache is not None else _raw_cache(t)
    await cache.prepare({clock: _partition_census_sql(t.database, clock) for clock in ("flow", "received")})
    if await _queue_unmasked_rows(t, cache):
        return MUTATION_RECHECK_S

    now_s = _now_s()
    current_hour = now_s - now_s % HOUR_S
    all_flow = cache.census("flow", 0, 2**32)
    oldest_raw_s = min((row["ts_min_ms"] // 1000 for row in all_flow.values()), default=None)
    retention = await raw_retention_days(t)
    base_floor = retention_floor(now_s, oldest_raw_s=oldest_raw_s,
                                 raw_retention_days=retention, rollup_ttl_days=None)
    if base_floor is None or base_floor >= current_hour:
        return SWEEP_INTERVAL_S

    flow_raw = cache.census("flow", base_floor, current_hour)
    received_raw = cache.census("received", base_floor, current_hour)

    censuses: dict[str, dict[int, RolledHour]] = {}
    for spec in ROLLUPS:
        if ready[spec.table]:
            censuses[spec.table] = await rollup_census(t, spec, base_floor, current_hour)

    async with AsyncSessionLocal() as db:
        repair = await _load_repair_state(db)
    if not repair["x02"]["reconciled_at"] and all(ready[s.table] for s in X02_RECONCILED):
        await _reconcile_x02(t, flow_raw, censuses, base_floor, now_s)
        async with AsyncSessionLocal() as db:
            repair = await _load_repair_state(db)

    next_s = float(SWEEP_INTERVAL_S)
    for spec in ROLLUPS:
        if not ready[spec.table]:
            continue
        has_ttl, ttl_days = await _table_ttl_days(t, spec.table)

        def floor_at(at_s: int, ttl=ttl_days if has_ttl else None) -> int | None:
            return retention_floor(at_s, oldest_raw_s=oldest_raw_s, raw_retention_days=retention,
                                   rollup_ttl_days=ttl)

        floor_s = floor_at(now_s)
        raw = flow_raw if spec.clock == "flow" else received_raw
        queued = set(repair["x02"]["queue"].get(spec.table) or [])
        plan = plan_rollup(spec, raw, censuses[spec.table], now_s=now_s,
                           floor_s=floor_s or current_hour, queued=queued)
        if plan.unrecoverable:
            await _drop_unrecoverable(spec, plan.unrecoverable)
        if plan.next_ready_s is not None:
            next_s = min(next_s, max(MIN_RESWEEP_S, plan.next_ready_s - now_s))
        if not plan.items:
            continue
        logger.info(
            "netflow rollup: converging %d hour(s) of %s (%s)", len(plan.items), spec.table,
            ", ".join(f"{sum(1 for p in plan.items if p.reason == r)} {r}"
                      for r in ("backfill", "heal", "repair", "neighbor")
                      if any(p.reason == r for p in plan.items)),
        )
        done, yielded = await _converge(t, spec, plan, raw, floor_at=floor_at)
        if done:
            logger.info("netflow rollup: converged %d hour(s) of %s", done, spec.table)
        if yielded:
            return SWEEP_INTERVAL_S
    return next_s


async def _drop_unrecoverable(spec: RollupSpec, hours: list[int]) -> None:
    async with _Locked() as lock:
        if not lock.acquired:
            return
        state = await _load_repair_state(lock.db)
        for hour in hours:
            _dequeue(state, spec.table, hour, "unrecoverable")
        await _save_repair_state(lock.db, state)
    logger.warning(
        "netflow rollup: %d hour(s) of %s held inflated NetFlow v5 traffic but raw no longer "
        "covers them; they cannot be corrected", len(hours), spec.table,
    )


class _SweepLease:
    """One session lock for census/scheduling, separate from per-hour locks.

    AUTOCOMMIT avoids holding a database transaction across history scans.
    An unlock failure invalidates the physical connection before pool return.
    """
    def __init__(self):
        self.connection = None
        self.acquired = False

    async def __aenter__(self):
        self.connection = await engine.connect()
        try:
            await self.connection.execution_options(isolation_level="AUTOCOMMIT")
            self.acquired = bool((await self.connection.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": NETFLOW_SWEEP_ADVISORY_LOCK}
            )).scalar())
        except BaseException:
            await self.connection.invalidate()
            await self.connection.close()
            raise
        return self

    async def _release(self):
        try:
            if self.acquired:
                unlocked = (await self.connection.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": NETFLOW_SWEEP_ADVISORY_LOCK}
                )).scalar()
                if not unlocked:
                    await self.connection.invalidate()
        except BaseException:
            await self.connection.invalidate()
            raise
        finally:
            await self.connection.close()

    async def __aexit__(self, *exc):
        await asyncio.shield(self._release())


async def coordinated_sweep(t: ChTarget = DEFAULT_TARGET) -> float:
    """Single appliance-wide pass with durable cache and next-due scheduling.

    A worker losing the lock retries shortly. Once the winner finishes, the
    shared next-due time prevents the loser repeating the same scans. Failed
    passes publish neither a completion time nor a new cache.
    """
    async with _SweepLease() as lease:
        if not lease.acquired:
            return float(MUTATION_RECHECK_S)
        key = CENSUS_SETTINGS_KEY + ":" + t.database
        saved = (await lease.connection.execute(
            text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}
        )).scalar()
        saved = saved if isinstance(saved, dict) else {}
        stored = saved.get("census")
        now = _now_s()
        due = saved.get("next_due", 0)
        valid = isinstance(stored, dict) and stored.get("format") == CENSUS_FORMAT
        if valid and type(due) in (int, float) and now < due <= now + SWEEP_INTERVAL_S:
            return max(float(MUTATION_RECHECK_S), due - now)
        cache = _raw_cache(t, stored)
        delay = await sweep_once(t, cache)
        state = {"next_due": _now_s() + delay, "census": cache.export()}
        await lease.connection.execute(
            text("INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                 "ON CONFLICT (key) DO UPDATE SET value = CAST(EXCLUDED.value AS jsonb), updated_at = NOW()"),
            {"k": key, "v": json.dumps(state, separators=(",", ":"))},
        )
        logger.info("netflow rollup: census reused %d unchanged partition(s), scanned %d", cache.hits, cache.scanned)
        return delay


async def netflow_rollup_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        delay: float = SWEEP_INTERVAL_S
        try:
            delay = await coordinated_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow rollup sweep failed")
            delay = MUTATION_RECHECK_S
        await asyncio.sleep(delay)
