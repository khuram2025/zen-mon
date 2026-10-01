-- NetFlow schema v2 (ClickHouse).
--
-- What this adds
--   * flow_records: full-width ifIndexes, IPv6 addresses (exporter, endpoints,
--     next hop, NAT), the per-record field-presence bitmask, observation domain,
--     IE 61 direction, export protocol, NBAR2/App-ID, firewall event, NAT ports
--     and flowEndReason — see docs/netflow-review-contract.md §3.1 / §4.2.
--   * Insert deduplication on flow_records so a collector retry that reuses its
--     insert_deduplication_token is a no-op in raw (the rollup tables get the
--     same block log in migrate-144).
--   * IPv6-aware keys and field-quality counters on the hourly conversation and
--     QoS rollups (§3.4).
--   * flow_interface_5m: per-interface, per-direction 5-minute volume with each
--     record's bytes spread over the time it was active (capacity, X-26).
--   * flow_exporter_minutes: per-exporter delivery minutes on the appliance
--     clock (liveness, coverage and clock-skew evidence).
--   * The tables the collector writes its own evidence into: flow_collector_stats,
--     flow_templates, flow_app_names, flow_if_counters.
--   * A one-off correction of NetFlow v5 rows stored with the unmasked sampling
--     header (X-02), as an asynchronous mutation.
--
-- Everything is metadata-only or guarded (IF [NOT] EXISTS), so this file can be
-- replayed by the schema sync. Nothing here back-fills: an appliance holds up to
-- 30 days (~900M rows) of raw flows, which would blow the updater's statement
-- timeout. app/services/netflow_rollup.py back-fills the new rollups from raw,
-- one hour at a time, and rebuilds the rollup hours that held X-02-inflated
-- traffic while raw data still covers them.
--
-- The updater stops the collector before migrations run, so replacing the
-- materialized views below cannot drop an insert on the floor.
--
-- Every volume/flow aggregate applies the same guard as the API (§3.3):
--     bytes / greatest(sampling_interval, 1) <= 1000000000000   corrupt counters
--     AND fw_event NOT IN (1, 3)      NSEL flow-create / flow-denied events carry
--                                     no volume (the teardown event does)
-- netflow_rollup.py compares raw and rollup counts under the same predicate.

-- ─── Raw records ─────────────────────────────────────────────────────────────
-- ADD COLUMN is metadata-only: existing parts read the DEFAULT until they are
-- next merged. The defaults describe a pre-v2 row truthfully — IPv4 only,
-- ifIndexes as the 16-bit columns held them, and fields_present = 0, which
-- every reader treats as "legacy row, field presence unknown".
ALTER TABLE zenplus.flow_records
    ADD COLUMN IF NOT EXISTS input_if            UInt32 DEFAULT input_snmp,
    ADD COLUMN IF NOT EXISTS output_if           UInt32 DEFAULT output_snmp,
    ADD COLUMN IF NOT EXISTS ip_version          UInt8 DEFAULT 4,
    ADD COLUMN IF NOT EXISTS exporter_ip6        IPv6,
    ADD COLUMN IF NOT EXISTS src_addr6           IPv6,
    ADD COLUMN IF NOT EXISTS dst_addr6           IPv6,
    ADD COLUMN IF NOT EXISTS next_hop6           IPv6,
    ADD COLUMN IF NOT EXISTS fields_present      UInt32 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS observation_domain  UInt32 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS flow_direction      UInt8 DEFAULT 255,
    ADD COLUMN IF NOT EXISTS export_protocol     LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS app_id              UInt64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS fw_event            UInt8 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS xlate_src_addr6     IPv6,
    ADD COLUMN IF NOT EXISTS xlate_dst_addr6     IPv6,
    ADD COLUMN IF NOT EXISTS xlate_src_port      UInt16 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS xlate_dst_port      UInt16 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS flow_end_reason     UInt8 DEFAULT 0;

-- Same mechanism as migrate-094: the collector sends a stable
-- insert_deduplication_token per batch, which a non-replicated MergeTree only
-- honours with a block log. This keeps raw exact across retries. It does NOT
-- keep the rollups exact: ClickHouse 24.10 still pushes a deduplicated block
-- through the materialized views, and a view's target table only skips it with
-- a block log of its own. migrate-144 gives the rollup tables that log (and the
-- healer inserts with insert_deduplicate = 0 because of it).
ALTER TABLE zenplus.flow_records
    MODIFY SETTING non_replicated_deduplication_window = 1000;

-- ─── Hourly conversation rollup ──────────────────────────────────────────────
-- IPv6 flows store 0.0.0.0 in the IPv4 columns, so without the *6 columns in
-- the key every IPv6 conversation of an hour would merge into one row.
-- ClickHouse only lets a sorting key grow by columns added in the same ALTER,
-- which is why the key change and the address columns travel together.
-- Replaying it with the key already extended is a no-op.
ALTER TABLE zenplus.flow_conversations_1h
    ADD COLUMN IF NOT EXISTS exporter_ip6 IPv6,
    ADD COLUMN IF NOT EXISTS src_addr6 IPv6,
    ADD COLUMN IF NOT EXISTS dst_addr6 IPv6,
    MODIFY ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port, exporter_ip6, src_addr6, dst_addr6);

-- input_snmp/output_snmp keep being filled (clamped to 65535, never
-- truncated) so readers that predate input_ifs keep working. Readers merge
-- input_ifs when it is non-empty, else the legacy state, which is all a
-- pre-v2 row has.
--
-- Rows written before this migration all came from the pre-v2 collector, so
-- their defaults are exact where they can be: every flow is legacy
-- (legacy_flows = flows). flagged_flows counts legacy flows with non-zero
-- flags. A pre-v2 row only kept the OR of its flows' flags, so the default
-- attributes all of the row's flows to it when that OR is non-zero — exact for
-- an exporter that either always or never exports flags, which is how
-- templates behave. The rows stay marked legacy, so readers can say the flag
-- basis is inferred. Duration validity was never recorded: 0 valid flows means
-- "average duration unknown" for those hours, not zero.
ALTER TABLE zenplus.flow_conversations_1h
    ADD COLUMN IF NOT EXISTS input_ifs AggregateFunction(groupUniqArray(10), UInt32),
    ADD COLUMN IF NOT EXISTS output_ifs AggregateFunction(groupUniqArray(10), UInt32),
    ADD COLUMN IF NOT EXISTS flagged_flows SimpleAggregateFunction(sum, UInt64) DEFAULT if(tcp_flags != 0, flows, 0),
    ADD COLUMN IF NOT EXISTS legacy_flows SimpleAggregateFunction(sum, UInt64) DEFAULT flows,
    ADD COLUMN IF NOT EXISTS duration_valid_flows SimpleAggregateFunction(sum, UInt64),
    ADD COLUMN IF NOT EXISTS duration_valid_ms SimpleAggregateFunction(sum, Int64),
    ADD COLUMN IF NOT EXISTS app_id SimpleAggregateFunction(max, UInt64);

-- ─── Hourly QoS / TCP-flag rollup ────────────────────────────────────────────
-- flags_known: 1 = the TCP-flags field was exported, 0 = tracked and absent
-- (the flag byte is not evidence of anything), 2 = legacy row, presence
-- unknown. tos_known: the same for the ToS byte (fields_present bit 1); a
-- record whose ToS was not exported stores 0, which is not DSCP 0 / Best
-- Effort. A column added to a sorting key may not carry a default in the
-- same ALTER, so the next statements set them. That is metadata-only (no
-- mutation) and makes every pre-v2 row read as 2 = legacy, which it is.
-- Replays: the key may only grow by columns this ALTER adds, so both stay
-- in one statement (an existing key is re-declared unchanged).
ALTER TABLE zenplus.flow_qos_1h
    ADD COLUMN IF NOT EXISTS exporter_ip6 IPv6,
    ADD COLUMN IF NOT EXISTS flags_known UInt8,
    ADD COLUMN IF NOT EXISTS tos_known UInt8,
    MODIFY ORDER BY (timestamp, exporter_ip, protocol, tos, tcp_flags, exporter_ip6, flags_known, tos_known);

ALTER TABLE zenplus.flow_qos_1h
    MODIFY COLUMN flags_known UInt8 DEFAULT 2;

ALTER TABLE zenplus.flow_qos_1h
    MODIFY COLUMN tos_known UInt8 DEFAULT 2;

ALTER TABLE zenplus.flow_qos_1h
    ADD COLUMN IF NOT EXISTS duration_valid_flows SimpleAggregateFunction(sum, UInt64),
    ADD COLUMN IF NOT EXISTS duration_valid_ms SimpleAggregateFunction(sum, Int64);

-- ─── Per-interface 5-minute volume (capacity) ────────────────────────────────
-- One row per (slot, exporter, ifIndex, direction, IE 61 direction):
-- direction 0 = received on the interface (the record's input_if), 1 = sent
-- (output_if). Keeping the two apart is what lets capacity compare each
-- direction with its own link speed instead of summing a full-duplex link.
-- bytes/packets are Float64 because a record is spread over every slot it was
-- active in, in proportion to the overlap. flows counts records in the slot
-- they ended in, so sum(flows) over an hour equals the raw record count for
-- that hour (the rollup healer's convergence check).
CREATE TABLE IF NOT EXISTS zenplus.flow_interface_5m (
    timestamp       DateTime('UTC'),
    exporter_ip     IPv4,
    exporter_ip6    IPv6,
    ifindex         UInt32,
    direction       UInt8,
    flow_direction  UInt8,
    bytes           SimpleAggregateFunction(sum, Float64),
    packets         SimpleAggregateFunction(sum, Float64),
    flows           SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, exporter_ip6, ifindex, direction, flow_direction)
TTL timestamp + INTERVAL 400 DAY DELETE;

-- ─── Per-exporter delivery minutes (liveness / coverage / clock) ─────────────
-- Keyed by the minute the appliance RECEIVED the records, never by the
-- exporter-supplied flow time, so a skewed exporter clock cannot make a live
-- exporter look silent. A row exists for every minute an exporter delivered
-- anything. The record/byte counters apply the volume guard like every other
-- aggregate, so an NSEL exporter sending only create/deny events in a minute
-- still shows as delivering (records = 0) instead of silent.
CREATE TABLE IF NOT EXISTS zenplus.flow_exporter_minutes (
    minute               DateTime('UTC'),
    exporter_ip          IPv4,
    exporter_ip6         IPv6,
    observation_domain   UInt32,
    records              SimpleAggregateFunction(sum, UInt64),
    bytes                SimpleAggregateFunction(sum, UInt64),
    tcp_records          SimpleAggregateFunction(sum, UInt64),
    tcp_flagged_records  SimpleAggregateFunction(sum, UInt64),
    v6_records           SimpleAggregateFunction(sum, UInt64),
    sampled_records      SimpleAggregateFunction(sum, UInt64),
    legacy_records       SimpleAggregateFunction(sum, UInt64),
    max_sampling         SimpleAggregateFunction(max, UInt32),
    first_received_at    SimpleAggregateFunction(min, DateTime64(3, 'UTC')),
    last_received_at     SimpleAggregateFunction(max, DateTime64(3, 'UTC')),
    min_flow_ts          SimpleAggregateFunction(min, DateTime64(3, 'UTC')),
    max_flow_ts          SimpleAggregateFunction(max, DateTime64(3, 'UTC')),
    lag_ms_min           SimpleAggregateFunction(min, Int64),
    lag_ms_max           SimpleAggregateFunction(max, Int64)
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(minute)
ORDER BY (minute, exporter_ip, exporter_ip6, observation_domain)
TTL minute + INTERVAL 400 DAY DELETE;

-- ─── Collector evidence (written by the collector, not by views) ─────────────
-- Per-interval deltas, one row per (collector, exporter, domain, protocol) plus
-- one process-wide row per collector with exporter = ''. The deduplication
-- window makes the collector's retried inserts idempotent, as for flow_records.
CREATE TABLE IF NOT EXISTS zenplus.flow_collector_stats (
    ts                                DateTime('UTC'),
    collector_id                      LowCardinality(String),
    exporter                          String,
    observation_domain                UInt32,
    export_protocol                   LowCardinality(String),
    datagrams                         UInt64,
    datagram_bytes                    UInt64,
    records_decoded                   UInt64,
    records_written                   UInt64,
    records_dropped_implausible       UInt64,
    records_dropped_queue             UInt64,
    records_dropped_unknown_exporter  UInt64,
    records_unsupported               UInt64,
    parse_errors                      UInt64,
    data_sets_waiting_template        UInt64,
    options_records                   UInt64,
    sequence_gaps                     UInt64,
    clock_offset_ms                   Int64,
    clock_clamped_records             UInt64,
    sampling_interval                 UInt32,
    templates_active                  UInt32,
    insert_failures                   UInt64,
    insert_retries                    UInt64,
    spool_records                     UInt64,
    spool_bytes                       UInt64,
    queue_len                         UInt64,
    queue_cap                         UInt64,
    kernel_rcvbuf_errors              UInt64
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (ts, collector_id, exporter)
TTL ts + INTERVAL 30 DAY DELETE
SETTINGS non_replicated_deduplication_window = 1000;

-- The denominator of sequence_gaps: sequence units received in the interval
-- (v5 flows, v9 and sFlow datagrams, IPFIX data + options records as the
-- exporter counted them). records_decoded cannot stand in for it: a
-- bidirectional record is decoded into two rows but numbered as one.
ALTER TABLE zenplus.flow_collector_stats
    ADD COLUMN IF NOT EXISTS sequence_units UInt64;

-- Records stored unscaled because they named no sampler while the exporter
-- domain announced several samplers with different rates and no default
-- (X-08): their sampling rate is ambiguous, so no arbitrary rate is applied.
ALTER TABLE zenplus.flow_collector_stats
    ADD COLUMN IF NOT EXISTS sampling_ambiguous_records UInt64;

-- Latest definition of every template an exporter announced. A data template
-- and an options template that reuse an ID share one row (is_options tells
-- which is current), mirroring how the collector's cache evicts one for the
-- other.
CREATE TABLE IF NOT EXISTS zenplus.flow_templates (
    exporter            String,
    observation_domain  UInt32,
    export_protocol     LowCardinality(String),
    template_id         UInt16,
    field_ids           Array(UInt16),
    field_lengths       Array(UInt16),
    enterprise_ids      Array(UInt32),
    is_options          UInt8,
    has_tcp_flags       UInt8,
    has_ipv6            UInt8,
    has_ifindex32       UInt8,
    has_direction       UInt8,
    has_app_id          UInt8,
    has_sampling        UInt8,
    has_nat             UInt8,
    has_fw_event        UInt8,
    first_seen          DateTime('UTC'),
    last_seen           DateTime('UTC'),
    collector_id        LowCardinality(String)
)
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY (exporter, observation_domain, export_protocol, template_id);

-- NBAR2 / App-ID names learned from options data (IE 95 -> 96/94/372).
CREATE TABLE IF NOT EXISTS zenplus.flow_app_names (
    app_id       UInt64,
    name         String,
    description  String,
    category     String,
    exporter     String,
    updated_at   DateTime('UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (app_id);

-- sFlow counter samples, cumulative counters exactly as the agent sent them.
CREATE TABLE IF NOT EXISTS zenplus.flow_if_counters (
    ts                 DateTime('UTC'),
    collector_id       LowCardinality(String),
    exporter_ip        IPv4,
    exporter_ip6       IPv6,
    ifindex            UInt32,
    if_speed           UInt64,
    if_direction       UInt8,
    oper_up            UInt8,
    in_octets          UInt64,
    out_octets         UInt64,
    in_ucast           UInt64,
    out_ucast          UInt64,
    in_errors          UInt64,
    out_errors         UInt64,
    in_discards        UInt64,
    out_discards       UInt64,
    sample_interval_s  UInt32
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (exporter_ip, exporter_ip6, ifindex, ts)
TTL ts + INTERVAL 90 DAY DELETE
SETTINGS non_replicated_deduplication_window = 1000;

-- ─── Materialized views ──────────────────────────────────────────────────────
-- The conversation, QoS and 5-minute traffic views are replaced in place,
-- under their original names. Renaming them (e.g. *_mv2) would leave the
-- original names to migrate-071/072/20260506, and the schema sync re-runs any
-- recorded migration whose objects are missing: it would recreate the v1
-- views next to the v2 ones and every flow would be counted twice. Keeping the
-- names keeps those migrations satisfied. The new tables and columns above are
-- what tell the sync this file has run.
--
-- Every view renames raw columns in an inner subquery before aggregating, so a
-- bucket alias such as `... AS timestamp` can never capture min/max(timestamp)
-- (migrate-071) and GROUP BY resolves to the projected bucket (migrate-099).
-- app/services/netflow_rollup.py mirrors these SELECTs column for column. Its
-- tests insert the same raw rows through both and require identical results.

DROP VIEW IF EXISTS zenplus.flow_conversations_1h_mv;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_conversations_1h_mv
TO zenplus.flow_conversations_1h
AS SELECT
    hour_bucket AS timestamp,
    exporter_ip,
    src_addr,
    dst_addr,
    protocol,
    dst_port,
    exporter_ip6,
    src_addr6,
    dst_addr6,
    sum(raw_bytes) AS bytes,
    sum(raw_packets) AS packets,
    count() AS flows,
    groupUniqArrayState(10)(raw_src_port) AS src_ports,
    groupUniqArrayState(10)(raw_input_snmp) AS input_snmp,
    groupUniqArrayState(10)(raw_output_snmp) AS output_snmp,
    min(raw_ts) AS first_seen,
    max(raw_ts) AS last_seen,
    max(raw_received_at) AS received_at,
    sum(raw_duration_ms) AS duration_ms,
    groupBitOr(toUInt64(raw_tcp_flags)) AS tcp_flags,
    groupUniqArrayState(10)(raw_input_if) AS input_ifs,
    groupUniqArrayState(10)(raw_output_if) AS output_ifs,
    sum(raw_flagged) AS flagged_flows,
    sum(raw_legacy) AS legacy_flows,
    sum(raw_duration_valid) AS duration_valid_flows,
    sum(raw_duration_valid_ms) AS duration_valid_ms,
    max(raw_app_id) AS app_id
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
        toUInt64(first_switched_ms > 0
            AND last_switched_ms >= first_switched_ms
            AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)
            AND last_switched_ms - first_switched_ms <= 86400000) AS raw_duration_valid,
        if(raw_duration_valid = 1, raw_duration_ms, 0) AS raw_duration_valid_ms,
        app_id AS raw_app_id
    FROM zenplus.flow_records
    WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
      AND fw_event NOT IN (1, 3)
)
GROUP BY hour_bucket, exporter_ip, src_addr, dst_addr, protocol, dst_port, exporter_ip6, src_addr6, dst_addr6;

DROP VIEW IF EXISTS zenplus.flow_qos_1h_mv;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_qos_1h_mv
TO zenplus.flow_qos_1h
AS SELECT
    hour_bucket AS timestamp,
    exporter_ip,
    protocol,
    tos,
    tcp_flags,
    exporter_ip6,
    flags_known,
    tos_known,
    sum(raw_bytes) AS bytes,
    sum(raw_packets) AS packets,
    count() AS flows,
    countIf(raw_packets = 0) AS empty_flows,
    sum(raw_duration_ms) AS duration_ms,
    max(raw_received_at) AS received_at,
    sum(raw_duration_valid) AS duration_valid_flows,
    sum(raw_duration_valid_ms) AS duration_valid_ms
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
        toUInt64(first_switched_ms > 0
            AND last_switched_ms >= first_switched_ms
            AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)
            AND last_switched_ms - first_switched_ms <= 86400000) AS raw_duration_valid,
        if(raw_duration_valid = 1, raw_duration_ms, 0) AS raw_duration_valid_ms
    FROM zenplus.flow_records
    WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
      AND fw_event NOT IN (1, 3)
)
GROUP BY hour_bucket, exporter_ip, protocol, tos, tcp_flags, exporter_ip6, flags_known, tos_known;

-- The 5-minute traffic series (report sections) predates the volume guard.
-- Same shape as before. Only the guard is new.
DROP VIEW IF EXISTS zenplus.flow_traffic_5m_mv;

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_traffic_5m_mv
TO zenplus.flow_traffic_5m
AS SELECT
    slot_bucket AS timestamp,
    exporter_ip,
    protocol,
    dst_port,
    sum(raw_bytes) AS bytes,
    sum(raw_packets) AS packets,
    count() AS flow_count
FROM (
    SELECT
        toStartOfFiveMinutes(timestamp) AS slot_bucket,
        exporter_ip,
        protocol,
        dst_port,
        bytes AS raw_bytes,
        packets AS raw_packets
    FROM zenplus.flow_records
    WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
      AND fw_event NOT IN (1, 3)
)
GROUP BY slot_bucket, exporter_ip, protocol, dst_port;

-- Duration spreading. A record ends at `timestamp` and was active for
-- span_ms = last_switched_ms - first_switched_ms, clamped to one hour (an
-- invalid duration — absent, inverted, mixed uptime/epoch clocks or over a
-- day — spreads nothing: span 0). Its bytes are shared across every 5-minute
-- slot overlapping [end - span, end] in proportion to the overlap, and all go
-- to the end slot when span is 0. Without this a 30-minute flow lands in one
-- slot as a spike six times its real rate (X-26). ifIndex 0 means "not
-- exported" and is skipped rather than reported as an interface.
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_interface_5m_mv
TO zenplus.flow_interface_5m
AS SELECT
    toDateTime(intDiv(slot_ms, 1000), 'UTC') AS timestamp,
    exporter_ip,
    exporter_ip6,
    ifindex,
    direction,
    flow_direction,
    sum(raw_bytes * slot_share) AS bytes,
    sum(raw_packets * slot_share) AS packets,
    sum(toUInt64(slot_ms = end_slot_ms)) AS flows
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
            if(first_switched_ms > 0
                AND last_switched_ms >= first_switched_ms
                AND (first_switched_ms >= 1000000000000) = (last_switched_ms >= 1000000000000)
                AND last_switched_ms - first_switched_ms <= 86400000,
               toInt64(least(last_switched_ms - first_switched_ms, 3600000)),
               toInt64(0)) AS span_ms
        FROM zenplus.flow_records
        WHERE bytes / greatest(sampling_interval, 1) <= 1000000000000
          AND fw_event NOT IN (1, 3)
    )
)
GROUP BY timestamp, exporter_ip, exporter_ip6, ifindex, direction, flow_direction;

-- Liveness and clock evidence (received/flow times, lag) cover every record
-- delivered. Counters and sampling apply the volume guard (see the table).
CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.flow_exporter_minutes_mv
TO zenplus.flow_exporter_minutes
AS SELECT
    minute_bucket AS minute,
    exporter_ip,
    exporter_ip6,
    observation_domain,
    countIf(counted) AS records,
    sumIf(raw_bytes, counted) AS bytes,
    countIf(counted AND protocol = 6) AS tcp_records,
    countIf(counted AND protocol = 6 AND flagged) AS tcp_flagged_records,
    countIf(counted AND ip_version = 6) AS v6_records,
    countIf(counted AND raw_sampling > 1) AS sampled_records,
    countIf(counted AND raw_fields_present = 0) AS legacy_records,
    maxIf(raw_sampling, counted) AS max_sampling,
    min(raw_received_at) AS first_received_at,
    max(raw_received_at) AS last_received_at,
    min(raw_ts) AS min_flow_ts,
    max(raw_ts) AS max_flow_ts,
    min(lag_ms) AS lag_ms_min,
    max(lag_ms) AS lag_ms_max
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
        (bytes / greatest(sampling_interval, 1) <= 1000000000000 AND fw_event NOT IN (1, 3)) AS counted,
        toUnixTimestamp64Milli(received_at) - toUnixTimestamp64Milli(timestamp) AS lag_ms
    FROM zenplus.flow_records
)
GROUP BY minute_bucket, exporter_ip, exporter_ip6, observation_domain;

-- ─── X-02: NetFlow v5 sampling header stored unmasked ────────────────────────
-- The v5 header's sampling word is 2 mode bits over a 14-bit interval. The
-- pre-v2 collector stored the whole word (1-in-100 in deterministic mode is
-- 0x4064 = 16484) and multiplied bytes and packets by it, inflating sampled
-- v5 traffic ~165x. Those multiplications were exact, so dividing by the
-- stored word and multiplying by the real interval restores the correct
-- estimate. An interval field of 0 means unsampled (1), as the collector now
-- decodes it. No correctly decoded v5 row can hold more than 16383, so the
-- WHERE clause both selects exactly the damaged rows and makes a replay a
-- no-op. Asynchronous: the correction runs in the background and
-- netflow_rollup.py waits for it before rebuilding the affected rollup hours.
ALTER TABLE zenplus.flow_records
    UPDATE
        bytes = intDiv(bytes, sampling_interval) * greatest(bitAnd(sampling_interval, 16383), 1),
        packets = intDiv(packets, sampling_interval) * greatest(bitAnd(sampling_interval, 16383), 1),
        sampling_interval = greatest(bitAnd(sampling_interval, 16383), 1)
    WHERE flow_version = 5 AND sampling_interval > 16383
    SETTINGS mutations_sync = 0;
