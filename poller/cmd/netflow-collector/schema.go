package main

// Schema the collector ensures on startup (and again after an insert reports
// a missing table or column), so a new collector never fails against a
// database the migrations have not reached yet. It only covers the tables the
// collector itself writes; rollup tables and every materialized view belong to
// the ClickHouse migrations (scripts/migrate-*-clickhouse.sql), which the
// collector never touches.
//
// The DDL mirrors scripts/init-clickhouse.sql (flow_records) and
// scripts/migrate-137-netflow-schema-v2-clickhouse.sql (schema v2 columns and
// the collector evidence tables) statement for statement, with the database
// taken from the connection (CLICKHOUSE_DB) instead of a zenplus. prefix, so
// a fresh install converges to the same schema whichever runs first.
var schemaStatements = []string{
	// Final (schema v2) shape, identical to scripts/init-clickhouse.sql, so a
	// fresh install is the same whether the collector or the installer creates
	// the table first.
	`CREATE TABLE IF NOT EXISTS flow_records (
    timestamp          DateTime64(3, 'UTC'),
    received_at        DateTime64(3, 'UTC'),
    collector_id       LowCardinality(String),
    exporter_ip        IPv4,
    flow_version       UInt8,
    flow_sequence      UInt32,
    engine_type        UInt8,
    engine_id          UInt8,
    sampling_interval  UInt32,
    src_addr           IPv4,
    dst_addr           IPv4,
    next_hop           IPv4,
    input_snmp         UInt16,
    output_snmp        UInt16,
    packets            UInt64,
    bytes              UInt64,
    first_switched_ms  UInt64,
    last_switched_ms   UInt64,
    src_port           UInt16,
    dst_port           UInt16,
    tcp_flags          UInt8,
    protocol           UInt8,
    tos                UInt8,
    src_as             UInt32,
    dst_as             UInt32,
    src_mask           UInt8,
    dst_mask           UInt8,
    input_if            UInt32 DEFAULT input_snmp,
    output_if           UInt32 DEFAULT output_snmp,
    ip_version          UInt8 DEFAULT 4,
    exporter_ip6        IPv6,
    src_addr6           IPv6,
    dst_addr6           IPv6,
    next_hop6           IPv6,
    fields_present      UInt32 DEFAULT 0,
    observation_domain  UInt32 DEFAULT 0,
    flow_direction      UInt8 DEFAULT 255,
    export_protocol     LowCardinality(String) DEFAULT '',
    app_id              UInt64 DEFAULT 0,
    fw_event            UInt8 DEFAULT 0,
    xlate_src_addr6     IPv6,
    xlate_dst_addr6     IPv6,
    xlate_src_port      UInt16 DEFAULT 0,
    xlate_dst_port      UInt16 DEFAULT 0,
    flow_end_reason     UInt8 DEFAULT 0
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, non_replicated_deduplication_window = 1000`,

	// A table created before schema v2 gets the v2 columns exactly as
	// migrate-137 adds them (same list, same order): metadata-only ADD COLUMN,
	// and pre-v2 rows read the defaults, which describe them truthfully (IPv4,
	// 16-bit ifIndex, fields_present = 0 = presence unknown).
	`ALTER TABLE flow_records
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
    ADD COLUMN IF NOT EXISTS flow_end_reason     UInt8 DEFAULT 0`,

	// The insert_deduplication_token of a retried batch is only honoured by a
	// non-replicated MergeTree with a block log (precedent: migrate-094).
	`ALTER TABLE flow_records
    MODIFY SETTING non_replicated_deduplication_window = 1000`,

	`CREATE TABLE IF NOT EXISTS flow_collector_stats (
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
SETTINGS non_replicated_deduplication_window = 1000`,

	// The denominator of sequence_gaps (see migrate-137).
	`ALTER TABLE flow_collector_stats
    ADD COLUMN IF NOT EXISTS sequence_units UInt64`,

	// Records left unscaled because their sampling rate was ambiguous (X-08,
	// see migrate-137).
	`ALTER TABLE flow_collector_stats
    ADD COLUMN IF NOT EXISTS sampling_ambiguous_records UInt64`,

	`CREATE TABLE IF NOT EXISTS flow_templates (
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
ORDER BY (exporter, observation_domain, export_protocol, template_id)`,

	`CREATE TABLE IF NOT EXISTS flow_app_names (
    app_id       UInt64,
    name         String,
    description  String,
    category     String,
    exporter     String,
    updated_at   DateTime('UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (app_id)`,

	`CREATE TABLE IF NOT EXISTS flow_if_counters (
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
SETTINGS non_replicated_deduplication_window = 1000`,
}
