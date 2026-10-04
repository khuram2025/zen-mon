# NetFlow development review — implementation contract

Branch `fix/netflow-development-review` (from `release/1.23.22`). This document is the
shared contract for every work package (WP) that fixes the NetFlow development review
(`/home/zen/NetFlow-development-review.md`, findings NF-01..NF-34, features FE-01..FE-12),
the verified extra defects (X-xx, see `docs/netflow-review-assessment.md`) and the
competitor-driven features (CR-xx). Where a WP needs something another WP provides, the
name, shape and semantics are fixed here so both sides can be built in parallel.

If a WP must deviate from this contract, it records the deviation in its report and keeps
the old name working (alias) rather than silently changing a shared shape.

---

## 0. Scope

In scope:

* All review findings NF-01..NF-34 and all verified extra defects.
* Review features FE-01..FE-12.
* Competitor features: CR-01 decoder correctness + field registry, CR-02 IPv6 end to end,
  CR-03 sFlow v5 (flow + counter samples), CR-04 permissions + scoping, CR-05 collector and
  exporter health, CR-06 onboarding help (config snippets + live exporter check; no
  approval queue), CR-07 flow alert rules in the platform alert engine, CR-08 per-direction
  capacity + configurable percentile billing, CR-09 capacity trend and runout forecast,
  CR-10 threat-intelligence IP reputation (offline capable), CR-11 persistent detection
  engine with baselines and finding lifecycle, CR-12 NBAR2/App-ID + configurable
  application rules, CR-13 managed IP groups, CR-14 host profile joined with UDT identity,
  CR-15 "what changed" explainer, CR-16 boundary-aware counting, CR-17 complete forensics
  and retention disclosure, CR-18 Cisco ASA/FTD NSEL + NAT fields, CR-19 collector
  durability (not scale-out), CR-20 AS / next-hop views, CR-25 narrow-screen layout.

Out of scope (documented as future work): CBQoS polling (CR-21), flow replicator (CR-22),
cloud flow logs (CR-23), API tokens / MCP (CR-24), distributed collectors on sensors
(scale-out part of CR-19), DDoS mitigation.

## 1. Principles (apply everywhere)

1. **Unknown is not zero.** A metric is either a value with its basis, or `null` with a
   machine-readable reason. Loading, failed, unsupported, no-coverage and genuinely-zero are
   five different UI states.
2. **One clock: the appliance.** Relative windows, freshness and exporter state are
   computed on the server (`datetime.now(timezone.utc)`), and the UI uses `serverNow()`
   from `dashboard/src/lib/serverClock.ts`. Explicit custom timestamps are never shifted.
3. **Half-open windows** `[from, to)` on every raw, rollup and hybrid path.
4. **Scope before filters.** Visibility (exporter ownership + tags) restricts every query;
   user filters only narrow what is visible.
5. **One definition per concept.** One filter compiler, one classifier, one capacity
   statistic, one speed rule (`COALESCE(configured, override, snmp)`), one RST ratio.
6. **Never fabricate.** No `0.0.0.0` endpoints, no truncated ifIndex, no zero TCP flags
   when the field was not exported.
7. **Disclose** sampling, retention, data source (raw / rollup / hybrid), coverage,
   truncation and counting mode.

## 2. Layout and ownership

Backend package (created by the split in WP0): `server/app/api/v1/netflow/`

| Module | Owner WP | Contents |
|---|---|---|
| `__init__.py` | WP0, then A3 | router assembly (prefix `/netflow`, router-level `require_permission("netflow.view")` added by A3), legacy re-exports |
| `common.py` | A3a | window/time, query execution, errors, envelope, constants shared by all |
| `filters.py` (new) | A3a | `FlowFilter`, dependency, compiler, table capability map |
| `sql.py` (new) | A3a | address/exporter/ifindex expressions, source planner (raw/rollup/hybrid) |
| `access.py` (new) | A3b | `FlowVisibility`, exporter ownership resolver, permission helpers |
| `meta.py` (new) | A3a | `/netflow/coverage`, `/netflow/retention` (§11) |
| `overview.py` | B1 | overview, timeseries (+compare), heatmap, exporter-health card data (device-status successor) |
| `explain.py` | B1 | what-changed explainer (CR-15) |
| `breakdowns.py` | B2 | top talkers/endpoints/conversations, protocols, ports, applications, dscp, tcp-flags, network classes, countries, ip-groups traffic, interfaces, ASNs/next-hops (CR-20) |
| `forensics.py` | B3 | forensics search/paging/export |
| `saved_views.py` | B3 | governed saved views |
| `capacity.py` | B4 | capacity, trends, forecast, billing |
| `anomalies.py` | B5 | findings API, detector status, exceptions, threat-intel feeds |
| `health.py`, `exporters.py` | B6 | collector & exporter health, exporter status, mapping, exporter settings, onboarding |
| `settings.py`, `hosts.py` | B7 | IP groups CRUD, application rules CRUD, host profile |
| `server/app/api/v1/alert_rules.py` (netflow parts), `services/netflow_alert_service.py`, `services/report_sections.py` (netflow parts) | B8 | flow alert rules, management report |

Shared backend services:

| Module | Owner | Contents |
|---|---|---|
| `services/netflow_rollup.py` | A2 | rollup MVs mirror + healer (all rollups incl. new ones) |
| `services/netflow_classify.py` (new) | A3b | single classifier (Python + SQL), built-in + DB rules + NBAR names |
| `services/netflow_groups.py` (new) | A3b | DB-backed IP groups, JSON import, predicates |
| `services/interface_speed.py` (new) | A3b | `effective_speed()` shared with link utilization |
| `services/netflow_tasks.py` (new) | A3a | background loop registry (see §9.4) |
| `services/netflow_capacity.py` | B4 | capacity statistics contract |
| `services/netflow_detection.py`, `services/netflow_ti.py` | B5 | detection engine, threat intel |
| `services/netflow_health.py` | B6 | exporter state, collector stats reader |

Frontend: `dashboard/src/pages/netflow/` (split by WP0), plus

| Path | Owner | Contents |
|---|---|---|
| `dashboard/src/lib/netflow/*.ts` (new) | A4 | filters, window, api, format, links, status helpers |
| `dashboard/src/components/netflow/*.tsx` (new) | A4 | QueryPanel/state views, StatusTile, DataQualityBar, FilterChips, SaveViewDialog shell |
| `pages/netflow/OverviewPage.tsx`, `components/*` (overview cards), `ExplainDrawer.tsx` | B1 |
| `pages/netflow/SectionPage.tsx`, `details/*` | B2 |
| `pages/netflow/ForensicsPage.tsx`, `SavedViewsPage.tsx` | B3 |
| `pages/netflow/CapacityPage.tsx` | B4 |
| `pages/netflow/AnomaliesPage.tsx`, `pages/dashboards/NetworkSecurityDashboard.tsx` (netflow parts) | B5 |
| `pages/netflow/HealthPage.tsx`, `SetupPage.tsx`, `components/ExporterDeviceDialog.tsx` | B6 |
| `pages/netflow/SettingsPage.tsx`, `HostProfilePage.tsx` | B7 |
| alert rule wizard netflow parts, reports netflow parts | B8 |
| `App.tsx` routes, `components/layout/navigation.ts` NetFlow node, `styles/globals.css` + `tailwind.config.js` status tokens | A4 (only) |

Collector: `poller/**`, `scripts/netflow-simulator.py`, `poller/systemd/zenplus-netflow-collector.service` — A1 only.
ClickHouse: `scripts/migrate-137-netflow-schema-v2-clickhouse.sql`, `scripts/init-clickhouse.sql` (netflow parts) — A2 only.
PostgreSQL: `scripts/migrate-138-netflow-governance.sql` and the SQLAlchemy models for its tables (`server/app/models/netflow_*.py`) — A3b only. Reserved numbers if a
slice genuinely needs its own migration: 139 (B4), 140 (B5), 141 (B6), 142 (B7), 143 (B8),
144 (A1, ClickHouse). Never reuse a number; `scripts/migrations.lock` is updated once at
integration, not by slices.

Tests: each WP adds its own files (`server/tests/test_netflow_<area>.py`,
`dashboard/scripts/test-netflow-<area>.mjs`, `poller/internal/.../*_test.go`). Nobody
edits another WP's test file.

## 3. ClickHouse schema v2 — `scripts/migrate-137-netflow-schema-v2-clickhouse.sql` (A2)

Replay-safe only (`IF NOT EXISTS` / `IF EXISTS`, no backfill INSERT). Metadata-only ALTERs:
no `MODIFY COLUMN` type changes on `flow_records` (no mutation). The updater stops the
collector before migrations, so MV swaps are safe.

### 3.1 `zenplus.flow_records` new columns

| Column | Type / default | Meaning |
|---|---|---|
| `input_if` | `UInt32 DEFAULT input_snmp` | full-width ingress ifIndex (use instead of `input_snmp`) |
| `output_if` | `UInt32 DEFAULT output_snmp` | full-width egress ifIndex |
| `ip_version` | `UInt8 DEFAULT 4` | 4 or 6 |
| `exporter_ip6` | `IPv6` (default `::`) | IPv6 exporter address; `::` when the exporter is IPv4 |
| `src_addr6`, `dst_addr6`, `next_hop6` | `IPv6` | IPv6 addresses; `::` for IPv4 flows |
| `fields_present` | `UInt32 DEFAULT 0` | bitmask §4.2; 0 = legacy row, presence unknown |
| `observation_domain` | `UInt32 DEFAULT 0` | v9 source ID / IPFIX domain / v5 `engine_type<<8|engine_id` / sFlow sub-agent |
| `flow_direction` | `UInt8 DEFAULT 255` | IE 61: 0 ingress-monitored, 1 egress-monitored, 255 unknown |
| `export_protocol` | `LowCardinality(String) DEFAULT ''` | `netflow5`, `netflow9`, `ipfix`, `sflow5`; `''` legacy |
| `app_id` | `UInt64 DEFAULT 0` | NBAR2/App-ID: `engine_id << 32 | selector` (0 = none) |
| `fw_event` | `UInt8 DEFAULT 0` | 0 none, 1 created, 2 deleted/teardown, 3 denied, 4 alert, 5 update |
| `xlate_src_addr6`, `xlate_dst_addr6` | `IPv6` | post-NAT addresses (IPv4-mapped for IPv4); `::` none |
| `xlate_src_port`, `xlate_dst_port` | `UInt16 DEFAULT 0` | post-NAT ports |
| `flow_end_reason` | `UInt8 DEFAULT 0` | IE 136 |

`flow_version` stays (5/9/10; 0 for sFlow). Ports carry ICMP `type*256+code` in `dst_port`
for ICMP/ICMPv6 (normalised by the collector, also from IE 32/176/177/178/179).

Enable insert deduplication for collector retries:
`ALTER TABLE zenplus.flow_records MODIFY SETTING non_replicated_deduplication_window = 1000`
(precedent: migrate-094).

### 3.2 Unified expressions (the only way API code refers to addresses)

Defined in `netflow/sql.py` (A3), valid on `flow_records` **and** every rollup that has
the `*6` columns:

```
EXPORTER = if(exporter_ip6 = toIPv6('::'), IPv4ToIPv6(exporter_ip), exporter_ip6)
SRC      = if(src_addr6    = toIPv6('::'), IPv4ToIPv6(src_addr),    src_addr6)
DST      = if(dst_addr6    = toIPv6('::'), IPv4ToIPv6(dst_addr),    dst_addr6)
display(expr) = replaceRegexpOne(toString(expr), '^::ffff:', '')   -- only on result rows
```

Bind IP parameters as IPv6 text (`::ffff:a.b.c.d` for IPv4). CIDR predicates: IPv4 CIDR →
`(src_addr6 = toIPv6('::') AND src_addr BETWEEN lo AND hi)`; IPv6 CIDR →
`(SRC BETWEEN toIPv6(lo) AND toIPv6(hi))`. Never `toString()` a column inside WHERE/GROUP BY.

### 3.3 Volume guard (applies to every volume/flow aggregate, raw and MVs)

```
bytes / greatest(sampling_interval, 1) <= 1000000000000   -- BUG-01 (raw only; MVs apply it at insert)
AND fw_event NOT IN (1, 3)                                -- NSEL create/deny events carry no volume
```

Forensics shows every row (guard not applied) and can filter `fw_event`.

### 3.4 Rollups

`flow_conversations_1h` (existing):
```
ADD COLUMN IF NOT EXISTS exporter_ip6 IPv6, ADD COLUMN IF NOT EXISTS src_addr6 IPv6,
ADD COLUMN IF NOT EXISTS dst_addr6 IPv6,
MODIFY ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port, exporter_ip6, src_addr6, dst_addr6)
ADD COLUMN IF NOT EXISTS input_ifs  AggregateFunction(groupUniqArray(10), UInt32)
ADD COLUMN IF NOT EXISTS output_ifs AggregateFunction(groupUniqArray(10), UInt32)
ADD COLUMN IF NOT EXISTS flagged_flows SimpleAggregateFunction(sum, UInt64)   -- flows whose TCP flags were exported (or legacy non-zero)
ADD COLUMN IF NOT EXISTS legacy_flows  SimpleAggregateFunction(sum, UInt64)   -- fields_present = 0
ADD COLUMN IF NOT EXISTS duration_valid_flows SimpleAggregateFunction(sum, UInt64)
ADD COLUMN IF NOT EXISTS duration_valid_ms    SimpleAggregateFunction(sum, Int64)
ADD COLUMN IF NOT EXISTS app_id SimpleAggregateFunction(max, UInt64)
```
The legacy `input_snmp`/`output_snmp` states stay populated (UInt16-clamped) for rows
written before the upgrade; readers merge `input_ifs` when non-empty, else the legacy state.

`flow_qos_1h` (existing):
```
ADD COLUMN IF NOT EXISTS exporter_ip6 IPv6, ADD COLUMN IF NOT EXISTS flags_known UInt8,
MODIFY ORDER BY (timestamp, exporter_ip, protocol, tos, tcp_flags, exporter_ip6, flags_known)
ADD COLUMN IF NOT EXISTS duration_valid_flows SimpleAggregateFunction(sum, UInt64)
ADD COLUMN IF NOT EXISTS duration_valid_ms    SimpleAggregateFunction(sum, Int64)
```
`flags_known`: 1 when the TCP-flags field was present (`fields_present` bit 0), 2 when
legacy (`fields_present = 0`), 0 when tracked and absent.

Duration validity (NF-18): `first_switched_ms > 0 AND last_switched_ms >= first_switched_ms
AND (first_switched_ms >= 1e12) = (last_switched_ms >= 1e12) AND last_switched_ms -
first_switched_ms <= 86400000`.

New MVs replace the old ones under new names (so the ledger sees new objects):
`flow_conversations_1h_mv2`, `flow_qos_1h_mv2`; `DROP VIEW IF EXISTS` the old
`flow_conversations_1h_mv`, `flow_qos_1h_mv`. Both apply the volume guard.

New `zenplus.flow_interface_5m` (capacity; TTL 400 days):
```
timestamp DateTime('UTC')            -- 5-minute slot start
exporter_ip IPv4, exporter_ip6 IPv6, ifindex UInt32,
direction UInt8                      -- 0 = in (rx, via input_if), 1 = out (tx, via output_if)
flow_direction UInt8                 -- copied from the record (255 unknown)
bytes SimpleAggregateFunction(sum, Float64)    -- duration-spread bytes
packets SimpleAggregateFunction(sum, Float64)
flows SimpleAggregateFunction(sum, UInt64)     -- records ending in the slot
ENGINE AggregatingMergeTree ORDER BY (timestamp, exporter_ip, exporter_ip6, ifindex, direction, flow_direction)
```
Spreading: a record covering `[end - d, end]` with `d = clamp(last_switched_ms -
first_switched_ms, 0, 3600000)` (invalid duration → 0) contributes
`bytes * overlap(slot)/d` to each 5-minute slot it overlaps (all to the end slot when d = 0).
ifIndex 0 is skipped. MV `flow_interface_5m_mv` + healer backfill.

New `zenplus.flow_exporter_minutes` (liveness/coverage; TTL 400 days):
```
minute DateTime('UTC')               -- toStartOfMinute(received_at): APPLIANCE clock
exporter_ip IPv4, exporter_ip6 IPv6, observation_domain UInt32,
records SimpleAggregateFunction(sum, UInt64), bytes SimpleAggregateFunction(sum, UInt64),
tcp_records, tcp_flagged_records, v6_records, sampled_records, legacy_records  (sum, UInt64)
max_sampling SimpleAggregateFunction(max, UInt32),
first_received_at SimpleAggregateFunction(min, DateTime64(3,'UTC')),
last_received_at  SimpleAggregateFunction(max, DateTime64(3,'UTC')),
min_flow_ts / max_flow_ts SimpleAggregateFunction(min/max, DateTime64(3,'UTC')),
lag_ms_min / lag_ms_max SimpleAggregateFunction(min/max, Int64)   -- received_at - timestamp
ORDER BY (minute, exporter_ip, exporter_ip6, observation_domain)
```
Coverage of a flow-time interval = minutes in which the exporter delivered records
(approximation documented; exporter clock skew is reported separately).

New tables written by the collector (A1 writes, A2 creates):

* `zenplus.flow_collector_stats` — one row per (collector, exporter, domain, version) per
  60 s interval, **deltas** not cumulative totals; plus one row per collector with
  `exporter = ''` for process-wide values. Columns: `ts DateTime('UTC')`, `collector_id
  LowCardinality(String)`, `exporter String` (canonical IP text), `observation_domain
  UInt32`, `export_protocol LowCardinality(String)`, `datagrams`, `datagram_bytes`,
  `records_decoded`, `records_written`, `records_dropped_implausible`,
  `records_dropped_queue`, `records_dropped_unknown_exporter`, `records_unsupported`,
  `parse_errors`, `data_sets_waiting_template`, `options_records`, `sequence_gaps` (lost
  datagrams for v9/sFlow, lost records for v5/IPFIX), `clock_offset_ms Int64` (median
  received − export header time), `clock_clamped_records`, `sampling_interval UInt32`,
  `templates_active UInt32`, `insert_failures`, `insert_retries`, `spool_records`,
  `spool_bytes`, `queue_len`, `queue_cap`, `kernel_rcvbuf_errors` (process row only),
  `sequence_units`, `sampling_ambiguous_records` — all
  `UInt64` unless stated. MergeTree, `ORDER BY (ts, collector_id, exporter)`, TTL 30 days.
* `zenplus.flow_templates` — `ReplacingMergeTree(last_seen)`, `ORDER BY (exporter,
  observation_domain, export_protocol, template_id)`: `field_ids Array(UInt16)`,
  `field_lengths Array(UInt16)`, `enterprise_ids Array(UInt32)`, `is_options UInt8`,
  `has_tcp_flags`, `has_ipv6`, `has_ifindex32`, `has_direction`, `has_app_id`,
  `has_sampling`, `has_nat`, `has_fw_event UInt8`, `first_seen`, `last_seen
  DateTime('UTC')`, `collector_id`.
* `zenplus.flow_app_names` — `ReplacingMergeTree(updated_at)`, `ORDER BY (app_id)`:
  `app_id UInt64`, `name String`, `description String`, `category String`, `exporter
  String`, `updated_at DateTime('UTC')` (from IE 95→96/94/372 options data).
* `zenplus.flow_if_counters` — sFlow counter samples: `ts DateTime('UTC')`, `collector_id`,
  `exporter_ip IPv4`, `exporter_ip6 IPv6`, `ifindex UInt32`, `if_speed UInt64`,
  `if_direction UInt8`, `oper_up UInt8`, `in_octets`, `out_octets`, `in_ucast`,
  `out_ucast`, `in_errors`, `out_errors`, `in_discards`, `out_discards` (`UInt64`,
  cumulative counters as sent), `sample_interval_s UInt32`. MergeTree `ORDER BY
  (exporter_ip, exporter_ip6, ifindex, ts)`, TTL 90 days.

`scripts/init-clickhouse.sql` and the collector's embedded `ensureSchema` DDL must produce
the same final schema on a fresh install (A2 + A1). The collector's `ensureSchema` must
also `ADD COLUMN IF NOT EXISTS` the §3.1 columns so a new collector never fails against an
un-migrated database; it never touches MVs.

## 4. Collector contract (A1)

### 4.1 `netflow.Record` (poller/internal/netflow)

Existing fields keep their names; widen `InputSNMP`/`OutputSNMP` to `uint32`. Add:
`ExporterAddr netip.Addr` (canonical, unmapped), `SrcAddr6`, `DstAddr6`, `NextHop6
net.IP` (16-byte or nil), `IPVersion uint8`, `FieldsPresent uint32`, `ObservationDomain
uint32`, `FlowDirection uint8` (255 unknown), `ExportProtocol string`, `AppID uint64`,
`FwEvent uint8`, `XlateSrc`, `XlateDst net.IP`, `XlateSrcPort`, `XlateDstPort uint16`,
`FlowEndReason uint8`. Storage: IPv4 flows keep `SrcAddr/DstAddr` (4-byte) and write `::`
to the `*6` columns; IPv6 flows write `0.0.0.0` to the IPv4 columns and the real address
to the `*6` columns. IPv4 exporters write `exporter_ip` and `::`; IPv6 exporters write
`0.0.0.0` and `exporter_ip6`. A record with **no** source or destination address is
dropped and counted (`records_unsupported`), never stored as `0.0.0.0`.

### 4.2 `fields_present` bits (shared by collector, MVs, API)

```
bit 0  TCP_FLAGS        bit 1  TOS              bit 2  L4_PORTS         bit 3  INPUT_IF
bit 4  OUTPUT_IF        bit 5  FLOW_TIMES       bit 6  AS               bit 7  MASKS
bit 8  NEXT_HOP         bit 9  DIRECTION        bit 10 APP_ID           bit 11 ICMP_TYPE
bit 12 NAT              bit 13 FW_EVENT         bit 14 SAMPLING_INLINE  bit 15 VLAN
bit 27 SAMPLING_AMBIGUOUS
bit 28 CLOCK_CLAMPED    bit 29 SAMPLED_SFLOW    bit 30 BIFLOW_REVERSE   bit 31 TRACKED
```
Every record written by the new collector has bit 31 set. v5 sets bits 0-8 (+5). A reader
treats `fields_present = 0` as "legacy, unknown".

### 4.3 Decoding rules

* v5 sampling: `interval = hdr & 0x3FFF`, `mode = hdr >> 14`; interval 0 → 1.
* v9/IPFIX: cases 27/28/62/63 (IPv6), 29/30 (v6 masks), 61 (direction), 95 (applicationId;
  engine byte + selector, first 4 selector bytes), 32/176/177/178/179 (ICMP type/code →
  `dst_port`), 34/35/305 inline sampling, 136 flowEndReason, 225/226/227/228/281/282 (NAT),
  233 + NSEL 40005 (firewall event), 231/232/298/299 (initiator/responder octets/packets),
  323/152/153 event times, 252/253 physical interfaces (fallback when 10/14 absent).
  IE 85/86 (running totals) are used only when IE 1/2 are absent and never overwrite them.
* Bidirectional counters (responder octets/packets, or IN_BYTES and OUT_BYTES both present
  and different): emit the forward record, and a second record with swapped
  addresses/ports/interfaces carrying the reverse counters (bit 30).
* Template/sampler caches keyed by the full canonical exporter address (never `0.0.0.0`),
  observation domain and template ID; a data-template ID and options-template ID sharing a
  number evict each other. Sampler learned per (exporter, domain, sampler ID) when present.
  A per-ID sampler applies only to records naming its ID; a record naming none uses the
  domain rate announced without a sampler ID, else the one rate all the domain's samplers
  share. When they disagree it is stored unscaled with bit 27 and counted in
  `flow_collector_stats.sampling_ambiguous_records` (X-08) — never scaled by whichever
  sampler was announced last.
* Clock policy (`NETFLOW_CLOCK_POLICY=clamp|trust`, default `clamp`): a flow end time more
  than 120 s in the future or more than 7200 s before `received_at` is clamped to
  `received_at` (bit 28, counted); original switched times are preserved.
* sFlow v5 (`NETFLOW_SFLOW_LISTEN`, default `:6343`): flow samples → one record per sample
  (`packets = sampling_rate`, `bytes = frame_length * sampling_rate`, `sampling_interval =
  sampling_rate`, ifIndexes, 5-tuple/flags/ToS from the sampled header incl. 802.1Q and
  IPv6, bit 29); counter samples → `flow_if_counters`; sequence gaps counted.

### 4.4 Delivery

Bounded retries with backoff and a per-insert timeout; a bounded spool (memory, optional
disk under `NETFLOW_SPOOL_DIR`) drained FIFO; `insert_deduplication_token` per batch;
graceful shutdown drains the queue and flushes with a fresh context; collector keeps
listening when ClickHouse is down at start (spools). `records_written` counts after a
successful insert. Kernel receive buffer set via `NETFLOW_RCVBUF`; `/proc/net/snmp`
`RcvbufErrors` exposed. Health JSON on `NETFLOW_HEALTH_LISTEN` includes every
`flow_collector_stats` counter (cumulative) and `status: ok|degraded|down`.

## 5. PostgreSQL — `scripts/migrate-138-netflow-governance.sql` (A3)

All `IF NOT EXISTS`; `gen_random_uuid()` ids; `created_at/updated_at TIMESTAMPTZ DEFAULT
NOW()`; `created_by/updated_by UUID REFERENCES users(id) ON DELETE SET NULL`.

* `netflow_saved_views` add: `visibility VARCHAR(16) NOT NULL DEFAULT 'shared'`
  (`private|shared`), `kind VARCHAR(16) NOT NULL DEFAULT 'forensics'`
  (`forensics|overview|section`), `query_version SMALLINT NOT NULL DEFAULT 1`, `updated_by`;
  index on `owner_id`.
* `netflow_ip_groups(id, name VARCHAR(120) UNIQUE NOT NULL, description TEXT, cidrs TEXT[]
  NOT NULL, color VARCHAR(16), tags JSONB NOT NULL DEFAULT '[]', parent_id UUID NULL,
  created_by, updated_by, created_at, updated_at)`.
* `netflow_app_rules(id, application VARCHAR(80) NOT NULL, service VARCHAR(80), category
  VARCHAR(64), protocol SMALLINT NULL, port_lo INT NULL, port_hi INT NULL, cidrs TEXT[] NULL,
  priority INT NOT NULL DEFAULT 100, enabled BOOLEAN NOT NULL DEFAULT TRUE, …audit cols)`
  with range checks.
* `netflow_exporter_settings(exporter_ip INET PRIMARY KEY, display_name VARCHAR(120), role
  VARCHAR(16) NOT NULL DEFAULT 'edge' CHECK (role IN ('edge','core','internal','excluded')),
  count_direction VARCHAR(16) NOT NULL DEFAULT 'auto' CHECK (count_direction IN
  ('auto','ingress','egress','both')), expected_interval_s INT NULL, note TEXT,
  …audit cols)`.
* `netflow_interface_overrides(exporter_ip INET, ifindex BIGINT, display_name VARCHAR(120),
  speed_in_bps BIGINT NULL, speed_out_bps BIGINT NULL, role VARCHAR(16) NULL CHECK (role IN
  ('wan','lan','uplink','internal','ignore')), note TEXT, …audit cols, PRIMARY
  KEY(exporter_ip, ifindex))`.
* `netflow_findings(id, fingerprint VARCHAR(80) UNIQUE NOT NULL, kind VARCHAR(48), severity
  VARCHAR(16), confidence VARCHAR(8), status VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (status
  IN ('open','acknowledged','suppressed','resolved')), title TEXT, description TEXT, src_ip
  INET NULL, dst_ip INET NULL, exporter_ip INET NULL, first_seen TIMESTAMPTZ NOT NULL,
  last_seen TIMESTAMPTZ NOT NULL, occurrences INT NOT NULL DEFAULT 1, evidence JSONB NOT NULL
  DEFAULT '{}', metrics JSONB NOT NULL DEFAULT '{}', alert_id UUID NULL, assigned_to UUID
  NULL, acknowledged_by UUID NULL, acknowledged_at TIMESTAMPTZ NULL, suppressed_until
  TIMESTAMPTZ NULL, suppressed_by UUID NULL, suppress_reason TEXT, resolved_at TIMESTAMPTZ
  NULL, notes JSONB NOT NULL DEFAULT '[]', created_at, updated_at)`; indexes (status,
  last_seen DESC), (kind), (src_ip).
* `netflow_detector_exceptions(id, kind VARCHAR(48) NULL, src_cidr CIDR NULL, dst_cidr
  CIDR NULL, dst_port INT NULL, reason TEXT NOT NULL, expires_at TIMESTAMPTZ NULL,
  created_by, created_at)`.
* `netflow_ti_feeds(id, name VARCHAR(120) UNIQUE NOT NULL, source VARCHAR(16) NOT NULL CHECK
  (source IN ('url','upload','builtin')), url TEXT NULL, category VARCHAR(48) NOT NULL
  DEFAULT 'malicious', enabled BOOLEAN NOT NULL DEFAULT FALSE, refresh_hours INT NOT NULL
  DEFAULT 24, last_refreshed_at TIMESTAMPTZ NULL, last_status TEXT, entry_count INT NOT NULL
  DEFAULT 0, …audit cols)` and `netflow_ti_entries(feed_id UUID REFERENCES netflow_ti_feeds
  ON DELETE CASCADE, prefix CIDR NOT NULL, PRIMARY KEY(feed_id, prefix))`.
* `netflow_export_jobs(id, requested_by, status VARCHAR(16) CHECK (status IN
  ('queued','running','done','failed','cancelled','expired')), query JSONB NOT NULL,
  row_count BIGINT, byte_size BIGINT, file_path TEXT, error TEXT, created_at, started_at,
  finished_at, expires_at)`.
* Permissions: add `netflow.manage` ("Manage NetFlow settings, mappings, rules and shared
  views") and `netflow.export` ("Export complete raw flow result sets") to
  `core/permissions.py`; the migration grants both to `operator` and `editor` roles (JSONB
  append guarded by `NOT permissions ? '…'`), admins get them via `system.admin`.
* System settings keys (JSON in `system_settings`, created lazily by code, not by the
  migration): `netflow_detectors`, `netflow_capacity`, `netflow_classification`,
  `netflow_counting`.

A3 is split in two parallel agents: **A3a** (common, sql, filters, meta, tasks, errors,
envelope, window) and **A3b** (access, permissions, migration 138 + models, classifier,
IP groups, interface speed, out-of-module consumers, devices speed audit). A3a imports
`FlowVisibility` from `access.py` and the classifier/group SQL builders from A3b's
services; A3b writes those first.

## 6. Access contract (A3b)

* Router-level `Depends(require_permission("netflow.view"))`.
* `FlowVisibility` (in `access.py`): `restricted: bool`, `exporters: frozenset[str] | None`
  (canonical IP text; `None` = unrestricted), `user`, `permissions`. Dependency
  `flow_visibility`. Ownership: explicit `netflow_exporter_devices` mapping wins, else an
  exact device management-IP match; the exporter is visible iff its owner device passes
  the user's tag scope. Unowned exporters are hidden from restricted users (fail-closed).
  Cached 15 s per scope, invalidated on mapping/tag writes.
* Every ClickHouse query built through `compile_filter` gets the visibility predicate on
  `EXPORTER`. An empty visible set returns no rows (never an unfiltered query).
* Detail endpoints for an out-of-scope exporter/host return **404**.
* Writes: `netflow.manage` for mappings, exporter settings, interface overrides, IP groups,
  application rules, detector settings/exceptions, TI feeds, shared-view moderation;
  saved-view owners edit their own views; `netflow.export` for complete exports;
  `alerts.acknowledge` or `netflow.manage` to acknowledge/assign findings; `netflow.manage`
  to suppress. Every write calls `write_audit_log`.
* Mapping writes also require the target device to be visible to the caller.
* Out-of-module consumers (A3b): link-utilization NetFlow overlay and `has_netflow`,
  manual-maps NOC link rates / conversations widget; (B8): report traffic sections.

```python
@dataclass(frozen=True)
class FlowVisibility:                  # netflow/access.py (A3b)
    restricted: bool
    exporters: frozenset[str] | None   # canonical IP text; None = unrestricted
    user_id: str | None
    permissions: frozenset[str]
    def allows(self, exporter: str) -> bool: ...
async def flow_visibility(user=Depends(require_permission("netflow.view")), db=Depends(get_db)) -> FlowVisibility
async def resolve_exporter_owners(db, ips: list[str] | None = None) -> dict[str, ExporterOwner]  # device_id, hostname, source 'mapped'|'ip-match'
def invalidate_visibility_cache() -> None
```

Classifier (A3b, `services/netflow_classify.py`): `classify(protocol, src_port, dst_port,
app_id=0) -> Classification(service_port|None, direction 'request'|'response'|'unknown'|
'n/a', service, application, category, ip_protocol, basis, confidence,
port_applicable)`; `service_port_sql(src_expr, dst_expr, proto_expr) -> str`;
`application_sql(...) -> str`; `known_applications() -> list[str]`;
`invalidate_rules_cache()`. Built-in rules are protocol-aware; DB rules
(`netflow_app_rules`) and NBAR names (`flow_app_names`) extend them; Python and SQL
classify identically.

IP groups (A3b, `services/netflow_groups.py`): `load_groups() -> list[Group]` (DB, importing
`/opt/zenplus/data/netflow-ip-groups.json` once when the table is empty), `group_names()`,
`group_predicate(name, src_expr, dst_expr) -> str | None`, `classify_ip(ip) -> str | None`,
`invalidate_groups_cache()`.

## 7. Time and window contract (A3a)

```python
@dataclass(frozen=True)
class Window:
    start: datetime        # aware UTC, inclusive
    end: datetime          # aware UTC, exclusive
    mode: str              # 'relative' | 'fixed'
    tz: str                # IANA name used for hour/dow/day buckets, default 'UTC'
```
`resolve_window(hours=None, minutes=None, from_ts=None, to_ts=None, tz=None, *, max_span,
now=None) -> Window`:

* `from`/`to` both or neither (400 "A custom range needs both from and to"); years outside
  [2000, now.year+1] → 400; `to <= from` → 400; `to` clamped to server now + 60 s; span >
  `max_span` → 400 naming the limit.
* When `from`/`to` are present `hours` is ignored (not validated). `hours` accepts up to
  2232 (93 days). Relative windows end at server now.
* `tz` validated against `zoneinfo.available_timezones()` (400 otherwise).
* `max_span`: 93 days for rollup-capable endpoints, 31 days for raw-only (forensics
  search, anomalies ad-hoc run).

## 8. Filter contract (A3a backend, A4 frontend)

Canonical query/URL keys (same on both sides):

| Key | Type / validation | Semantics |
|---|---|---|
| `exporter` | IPv4/IPv6 | exporter address |
| `iface` | `N` or `N@exporter` (N 0..4294967295) | ifIndex on either direction; qualified form restricts to that exporter |
| `iface_dir` | `in` / `out` / `any` | which direction the ifIndex must match |
| `talker` | IP or CIDR | source **or** destination |
| `src`, `dst` | IP or CIDR | one side |
| `not_talker`, `not_src`, `not_dst` | IP or CIDR | exclusions |
| `protocol` | 0..255 | IP protocol (`proto` accepted as a read alias) |
| `port` | 0..65535 | either side |
| `src_port`, `dst_port` | 0..65535 | one side |
| `dst_ports` | ≤32 comma-separated ports | destination in set |
| `service_port` | 0..65535 | classified service port (both directions of a service) |
| `app` | known application name | classifier output (built-in, DB rules, NBAR names) |
| `dscp` | 0..63 | `tos >> 2` (`dscp_eq` read alias) |
| `netclass` | enum incl. IPv6 classes | either side in class |
| `tcpflag` | `syn_only|ack_only|rst|fin|psh|urg|no_flags` | TCP only, flagged records only (`no_flags` = exported and zero) |
| `hour`, `dow` | 0..23, 1..7 | evaluated in `tz` |
| `tz` | IANA | see §7 |
| `group` | IP-group name (must exist) | either side in group |
| `asn` | UInt32 | `src_as` or `dst_as` |
| `ip_version` | 4 / 6 | |
| `direction` | `ingress` / `egress` | `flow_direction` |
| `min_bytes`, `max_bytes` | ≥0 | per record |
| `fw_event` | `created|teardown|denied|alert|update` | forensics only |
| `count` | `all` (default) / `edge` | counting mode (FE-12/CR-16): `edge` keeps only exporters whose `netflow_exporter_settings.role` is `edge` (no settings row = edge) and, per exporter, only records matching its `count_direction` (`auto` = ingress-monitored records when the exporter emits both directions, else all) |

Unknown values → 400 with a message; never silently ignored. Numeric zero is a valid value
everywhere (never truthiness tests). `compile_filter(f, table, vis, drop=frozenset())`
emits the predicate for a table; the table capability map decides rollup eligibility
(e.g. `dscp`/`tcpflag` → QoS rollup or raw; `iface` → interface rollup or raw; ports other
than `dst_port` → raw). `drop` removes dimensions a widget must not self-filter on (e.g.
the DSCP card drops `dscp`).

Frontend (`lib/netflow/filters.ts`): `FlowFilter` type with exactly these keys,
`parseFlowFilter(URLSearchParams)`, `writeFlowFilter(sp, f)` (canonical keys, removes
empty), `flowFilterToApiParams(f)`, `validateFlowFilter(f) -> Record<key, message>`,
`describeFlowFilter(f) -> Chip[]`, `FLOW_FILTER_KEYS`. Legacy read aliases: `proto`,
`dscp_eq`. Saved queries use `{v: 2, kind, filter, window: {mode:'relative', range} |
{mode:'fixed', from, to}, sort?, order?, columns?}`; v1 (old forensics blobs) is migrated on
read.

## 9. Query execution, errors, envelope, background tasks (A3a)

### 9.1 Execution
`run_query(sql, params, *, budget_s=20.0, label) -> result` runs in the thread pool with
ClickHouse settings `max_execution_time=budget_s`. Budgets: interactive widgets 20 s,
forensics page 30 s, exports 300 s, background jobs 120 s.

### 9.2 Errors
Never return database text. Failures log the exception with an `error_id`
(`nf-` + 8 hex) and return JSON `{"detail": "<human message> (ref nf-1a2b3c4d)",
"error_id": "nf-1a2b3c4d", "code": "query_failed" | "query_timeout" | "invalid_filter" |
"invalid_window" | "unavailable"}` — 500 for failures, 504 for budget timeouts, 400 for
validation. `detail` is always a string.

### 9.3 Envelope
Any read endpoint accepts `envelope=1` and then returns `{"data": <legacy shape>, "meta":
{...}}`; without it the legacy shape is unchanged (dashboards, maps and device pages keep
working). NetFlow pages always send `envelope=1`.

```
meta = {
  "window": {"from", "to", "mode", "tz", "effective_from", "effective_to",
             "source": "raw"|"rollup"|"hybrid"|"interface_rollup"|"pg", "granularity_s"},
  "server_time": iso8601,
  "query_ms": int,
  "restricted": bool,                         # visibility scope applied
  "filters": {canonical applied filters},
  "retention": {"raw_days", "rollup_days", "interface_days", "limited": bool},
  "truncated": {"limit", "returned", "more": bool} | null,
  "sampling": {"sampled_share": float|null, "max_interval": int|null} | null,
  "warnings": [{"code", "message"}]
}
```
Timestamps are RFC3339 UTC with `Z`.

### 9.4 Background tasks
`services/netflow_tasks.py` exports `start_netflow_tasks(app)` (called once from
`main.py` lifespan in place of the current rollup-loop task). It starts, when the module
and function exist: `netflow_rollup.netflow_rollup_loop` (A2),
`netflow_detection.netflow_detection_loop` (B5), `netflow_ti.netflow_ti_loop` (B5),
`netflow_health.netflow_health_loop` (B6), `netflow_alert_service.netflow_alert_loop`
(B8), `netflow_forensics_export.netflow_export_loop` (B3). Each loop takes its own PG
advisory lock (two uvicorn workers).

## 10. Frontend foundation (A4)

* `lib/netflow/filters.ts` (§8), `lib/netflow/window.ts`: `windowParams(range)` → relative
  `{hours|minutes}` or fixed `{from, to}` (calendar ranges → fixed), never `hours` with
  custom bounds; `tz` = user display timezone (browser `Intl`); `freezeWindow(range)` for
  evidence links; `windowLabel(meta.window)` with timezone.
* `lib/netflow/api.ts`: `nf.get<T>(path, params)` always sends `envelope=1` and returns
  `{data, meta}`; `nfKeys` query-key factory; `useNfQuery` wrapper (keepPreviousData,
  poll interval from window span and mode, never polls fixed windows).
* `lib/netflow/format.ts`: decimal SI units everywhere for traffic (`kB/MB/GB`, `kbps/…`),
  exact value available for titles; `formatBps` clamps sub-1 values; compact counts;
  `formatDuration(ms)` (ms → s → min → h); `ipDisplay`.
* `lib/netflow/links.ts`: `buildNetflowLink(path, {filter, window, extra})`.
* `lib/netflow/status.ts`: `panelStatus(query, opts)` → `loading|forbidden|error|stale|
  no_coverage|unsupported|empty|ok`.
* `components/netflow/QueryPanel.tsx` (skeleton, error with retry + ref id, stale chip with
  last-success time, forbidden), `StatusTile.tsx` (tone fill + border + icon + word, `*-fg`
  text), `DataQualityBar.tsx` (renders `meta` + `/netflow/coverage`), `FilterChips.tsx`,
  `SaveViewDialog.tsx` (shell used by B3).
* Status foreground tokens `--danger-fg`, `--warning-fg`, `--success-fg`, `--info-fg` in
  both themes (≥4.5:1 on their tinted fills), Tailwind colours `danger-fg` etc.
* Routes: `/netflow/health`, `/netflow/setup`, `/netflow/settings`, `/netflow/hosts/:ip`
  with stub pages the slices fill; NetFlow nav node gets `permission: 'netflow.view'` and
  entries Overview, Forensics, Anomalies, Capacity, Saved Views, Collector health, Settings.

## 11. Shared API shapes other slices consume

* `GET /netflow/coverage` (A3a, `meta.py`) → `{window, exporters: [{exporter, minutes_with_data,
  minutes_in_window, records, tcp_records, tcp_flagged_records, v6_records,
  sampled_records, max_sampling, legacy_records, first_received_at, last_received_at}],
  totals: {…same sums…, flag_coverage_pct, sampled_share}, retention}`.
* `GET /netflow/retention` (A3a, `meta.py`) → `{raw_days, rollup_days, interface_days,
  exporter_minutes_days, earliest_raw, earliest_rollup, earliest_interface}`.
* `GET /netflow/exporters/status` (B6) → `{server_time, thresholds: {stale_s, silent_s},
  exporters: [{exporter, display_name, device_id, device_hostname, device_status,
  last_received_at, receipt_age_s, state: 'receiving'|'late'|'silent'|'never',
  records_15m, expected_interval_s, clock_offset_ms, clock_suspect, export_protocols,
  loss_pct_1h, flags_exported: bool|null}]}` — independent of page filters and window.
* `GET /netflow/findings` (B5) → `{items: [finding], counts: {open, acknowledged,
  suppressed, resolved, by_severity}, detectors: [{kind, status: 'ok'|'unavailable'|
  'no_coverage'|'failed'|'disabled', reason, last_run_at}]}`.

## 12. Acceptance mapping

| Item | WP |
|---|---|
| NF-01, NF-23 (exporter state/insights) | B6 (status API) + B1 (UI) |
| NF-02, NF-17, NF-28, NF-18, NF-19, NF-30 | B1 (A3 provides hybrid planner) |
| NF-03 | A4 components + every page slice |
| NF-04 | A3 (+ B8 reports) |
| NF-05, NF-12, NF-34, NF-10, NF-11, NF-08 (forensics side), NF-26 | B3 |
| NF-06, NF-32 (collector), NF-33 (collector), NF-07 (collector) | A1 (+A2 schema) |
| NF-07 (detectors/UI), NF-09, NF-21, NF-22 | B5 |
| NF-08 (links), NF-20, NF-24, NF-25, NF-27 | B2 (+A3 classifier) |
| NF-13, NF-14, NF-15, NF-16 | B4 |
| NF-29 | A4 tokens + page slices |
| NF-31 | A3 |
| FE-01, FE-08, FE-11 (UI), FE-12 | B6 |
| FE-02 | A3 + A4 |
| FE-03 | A3 (coverage) + A4 (DataQualityBar) + slices |
| FE-04, FE-10 | B3 |
| FE-05 | B4 |
| FE-06 | B5 |
| FE-07 | B8 |
| FE-09 | B7 (+A3 classifier/groups) |

## 13. Test environment (scratch, never production)

* ClickHouse 24.10 `127.0.0.1:18123` (HTTP) / `19000` (native), user `default`, password
  `scratchpw`, database `zenplus` (shared) — create your own database for destructive
  experiments.
* PostgreSQL 16 `127.0.0.1:55441`, user `postgres` (trust) / app role `zenplus`/`zenplus`,
  database `zenplus`, fully migrated.
* Reference API (old code) `:8095`. Each WP runs its own uvicorn on its assigned port
  (A3 8101, B1 8111, B2 8112, B3 8113, B4 8114, B5 8115, B6 8116, B7 8117, B8 8118) and its
  own vite dev server (port = API port − 2900, e.g. 5211 for B1) using a private config file
  in the scratchpad; kill only your own processes by PID.
* Scratch collector binary under the scratchpad (A1 rebuilds `nf-collector`), UDP 12055,
  health 127.0.0.1:18091. `scripts/netflow-simulator.py` drives v5/v9/IPFIX (A1 adds sFlow,
  NSEL and App-ID scenarios).
* Heavy commands through the scratchpad `bin/heavy.sh` lock.

## 14. Overview composition (B1 ↔ B2)

The overview page (`pages/netflow/OverviewPage.tsx`, B1) renders self-fetching cards that
B2 provides under `pages/netflow/cards/`. B2 creates `cards/types.ts` and a compiling
skeleton of every card **first**, then deepens them; B1 composes them and never edits
them; B2 never edits `OverviewPage.tsx`.

```ts
// pages/netflow/cards/types.ts (B2)
import type { FlowFilter } from '@/lib/netflow/filters'
import type { NfWindow } from '@/lib/netflow/window'
export interface OverviewContext {
  filter: FlowFilter                                  // applied page filter (from the URL)
  window: NfWindow                                    // resolved page window
  exporter?: string                                   // exporter deep-dive page
  setFilter: (patch: Partial<FlowFilter>) => void     // click-to-filter (writes the URL)
  sectionLink: (section: string, extra?: Record<string, string>) => string
}
export interface OverviewCardProps { ctx: OverviewContext; compact?: boolean }
// pages/netflow/cards/index.ts exports:
// TopTalkersCard, TopEndpointsCard, ConversationsCard, ApplicationsCard, ProtocolsCard,
// DscpCard, TcpFlagsCard, NetworkClassesCard, InterfacesCard, CountriesCard, AsnCard,
// IpGroupsCard — each `(props: OverviewCardProps) => JSX.Element`, each with its own
// loading/error/empty/unsupported states (QueryPanel) and whole-scope denominators.
```

Old per-card components under `pages/netflow/components/` that become unused are deleted
at integration, not by the slices.

B1 also consumes: `/netflow/exporters/status` (B6, §11), `/netflow/findings` counts (B5,
§11), `/netflow/coverage` (A3a). If a producer is not ready yet, B1 codes against the §11
shape and handles 404 as "unavailable".

## 15. Deviations as built

Recorded at integration (wave C) from the work-package reports; one line per deviation.
Everything else in §§0–14 holds as written.

**A2 — ClickHouse schema and rollups**
* View names kept (`flow_conversations_1h_mv`, `flow_qos_1h_mv` replaced in place) instead of `*_mv2`: with new names the schema sync re-ran migrate-071/072 and resurrected the old views next to the new ones (every flow counted twice).
* `flow_traffic_5m_mv` also applies the §3.3 volume guard; the healer and the X-02 repair cover `flow_traffic_5m`.
* Pre-v2 rows read as legacy: `flags_known DEFAULT 2`, `legacy_flows DEFAULT flows`, `flagged_flows` inferred from the stored flag byte.
* The X-02 correction divides by `greatest(bitAnd(sampling_interval, 16383), 1)` (interval bits 0 = unsampled).
* `flow_exporter_minutes` has a row for every minute anything arrived; its counters apply the guard, its liveness and clock fields cover all records.
* `flow_collector_stats` and `flow_if_counters` also carry the deduplication window; `flow_templates.template_id` is `UInt16`; all new tables are partitioned by day.

**A3a — common, filters, planner, meta**
* `flow_traffic_5m` and `flow_exporter_minutes` reads report `meta.window.source = "rollup"`.
* An unreachable or busy ClickHouse is 503 with `code: "unavailable"` (the contract names only 500/504/400).
* Error bodies may carry a `fields` map; FastAPI's 422 becomes a 400 with a string `detail`.
* `/coverage` adds `minutes_with_receipts`, `coverage_pct`, per-exporter `flag_coverage_pct` / `sampled_share` and `window.measured_from/to`; `/retention` adds `qos_days`, `earliest_qos`, `earliest_exporter_minutes`, `known`, `configured` and `sampling_repair`.
* New storage category `flow_history` (interface 5-minute, exporter minutes, sFlow counters; default 400 days); `flow_templates` and `flow_app_names` are excluded from retention and purge.

**A3b — access, classifier, groups, governance**
* Additive classifier API (`classification_sql`, `classification_tuple_sql`, `service_name`, `load_rules`), extra CHECK constraints and indexes in migrate-138, system setting `netflow_ip_groups_import`, 60 s caches for rules and groups.
* Link utilization's `has_netflow` flag reads `flow_interface_5m` instead of raw flows.

**A4 — frontend foundation**
* `panelStatus` returns `loading` while a previous selection stays on screen; `placeholderStatus` lets QueryPanel dim it.
* A saved relative window may name a calendar period (`month`, `last_month`, `quarter`); views of kind `section`/`overview` store the page they reopen as `query.path`.
* `windowParams` always sends `tz`; the end of "This month/quarter" snaps to 5 minutes for spans over two days.
* `app`, `netclass` and `group` are checked for shape only on the client (the API validates names).
* The Settings menu entry is not gated by `netflow.manage` (the page is read-only without it).

**A1 — collector**
* `flow_collector_stats.ts` is the interval start to the second; readers group by `toStartOfMinute(ts)`.
* Rows ClickHouse rejects are counted under `records_dropped_implausible` (health JSON shows them as `records_rejected`).
* `flow_templates.first_seen` is when this collector process first saw the definition.
* sFlow health fields `sflow_listen`, `sflow_listening`, `sflow_error`, `if_counters`, `app_names` are top-level; `options_records` counts sFlow counter samples; bit 15 (VLAN) is never set.
* Templates and samplers persist to `templates.json` in `NETFLOW_SPOOL_DIR` (no new variable); memory-only without a spool directory.
* An allowlist with no valid entry fails closed; a stats interval below 5 s falls back to 60 s.

**B1 — overview**
* The heatmap reads the hourly rollup (raw edges, same planner as the totals) in whole-hour time zones and raw records in half-hour zones or under a filter the rollup cannot evaluate; the part of a window older than the data kept is left out and disclosed. The overview polls it every 15 minutes.
* Delivery coverage (`/coverage`, continuity, chart gap/idle, comparison coverage, heatmap, the rate check) counts a minute as covered when it falls within the exporter's expected export interval after a delivery (configured, else learned, else 60 s, as `/exporters/status`), and measures the running minute once records arrived in it.
* `/overview`: `rate_status` gains `no_receipts` (nothing delivered over the rate minutes: `current_bps` null); `totals_status` (`ok`/`beyond_retention`, totals null when nothing retained answers the window for the filter) and `raw_only_filters`.
* `/timeseries` raises a caller's `bucket` so a series has at most 2000 buckets (`bucket_raised` warning); `POST /explain` rejects unknown filter keys (400 `invalid_filter`).
* Panel state `expired` (A4 `status.ts`/QueryPanel): an enveloped result whose effective window is empty because of retention renders the retention reason, never "no flows matched". Calendar ranges stay pending (queries wait) until the appliance clock is calibrated.

**B2 — breakdowns**
* `limit` is capped at 200; breakdowns return the legacy list unless `with_totals=1` (`{items, total, distinct, …}`), which the NetFlow pages always send.
* (wave C fix) `/interfaces` states no utilization percentage (it counts records the page's way and averages over gaps); `utilization` points to Capacity and the UI links each interface there. Link utilization's NetFlow overlay adds `interface_exporters` and links `iface=N@exporter` with its window.

**B3 — forensics and saved views**
* Admins see other users' private views (in their own group); everyone else gets 404.
* `pinned` is one flag per view (pinning a shared view pins it for everyone).
* Results are 100 rows per page on phones (500 on desktop).
* The legacy `GET /forensics` keeps its list and CSV shapes, now on the filter contract and visibility.
* The filter builder combines conditions with AND plus exclusions (no general OR).

**B4 — capacity**
* The legacy response shape carries the headline direction's corrected values.
* Traffic-content filters cannot narrow link utilization; they are dropped and reported as `capacity_filters_not_applied`.
* Low-coverage state rule: an interface without enough samples is never "healthy" (`method.state_rule`).
* (wave C fix) A speed known in one direction only is never "healthy": the state is `unknown` (a measured warning/critical/over stays), `headline.unknown_directions` names the side, and the state tiles count it where the coverage card does.
* (wave C fix) The Capacity page and billing leave out each exporter's provisional slots (`exclude_provisional`): slots after `floor5(now − T − 60 s)` where T is the longest valid flow span the exporter reported in the last 65 min (≤ 1 h; 30 min assumed without durations; 0 for sFlow); evidence cached 10 min. Rows carry `settled_until` and their own `expected_slots`; `window.provisional` and the `provisional_slots_excluded` warning disclose it. Interface alerts keep their own settle delay.
* (wave C fix) The rollup-lag check reads per-exporter high-water marks from `flow_interface_5m` and `flow_exporter_minutes`; raw records are read only to confirm a suspected lag, from a bounded slice (≤ 15 min, cached 60 s).
* (wave C fix) `speed.override_shadowed` lists the directions whose NetFlow override is not in use because the device circuit speed takes precedence; bulk speed changes are audited per interface (`<device>:<ifIndex>`), the trend's `speed_history` carries per-direction old/new values and each day's `speed` is the speed in effect that day.

**B5 — detection**
* `/findings` adds `total`, `engine`, `complete`, `counts.active`, `by_kind`, `by_kind_all`, `window`, `quiet_minutes`; `counts.by_severity` counts active (open + acknowledged) findings and `counts.by_severity_open` (integration) open ones.
* `/findings` applies only exporter, talker, src and dst; an ad-hoc run only exporter; the rest is disclosed through `meta.filters`.
* A malformed path id is 400 (not 422); an uploaded TI feed is disabled unless `enabled` is sent; the evidence timeline counts packets by flow end time.

**B6 — health, exporters, setup**
* Settings validation errors are 400 with a field map; windowed health, counting and exporter-list endpoints accept `tz`.
* Extra endpoints: `/collector/health`, `/exporters/{ip}/health`, `/exporters/{ip}/check`, `/exporters/counting`, `/exporters/settings`, `/setup/info`, `/exporter-devices/{ip}/suggestions|impact|history`; a "Device links" dialog on the Health page.

**B7 — settings and host profile**
* Settings routes live under `/netflow/settings/*` (B2 owns `/netflow/ip-groups`); validation errors carry `fields`, a stale edit is 409 `code: "conflict"`.

**B8 — alerting and reports**
* Small shared-file changes: narrow-screen donut and "<0.1%" legend in the report viewer, `period` prefill in the schedule dialog, a named close button in the rule wizard; the alert-rules table still scrolls sideways at 390 px.

**Integration (wave C)**
* Rollup idempotency: migrate-144 gives the five rollup tables a block log (`non_replicated_deduplication_window = 1000`, verified by `ch_migrate`), and the healer inserts with `insert_deduplicate = 0`; migrate-137's comment no longer claims a deduplicated block skips the views.
* `flow_collector_stats.sequence_units` (migrate-137 `ADD COLUMN IF NOT EXISTS`, collector, health loss %) is the sequence-loss denominator; `records_decoded` remains the fallback for older rows.
* §4.3 narrowed: IN_BYTES/OUT_BYTES both present and different mean a reverse direction in NetFlow v9 only; in IPFIX IE 23/24 are post-observation counters of the same direction.
* The collector spool stays `/var/lib/zenplus-netflow/spool` (`StateDirectory=zenplus-netflow`), not under `/var/lib/zenplus`, which is root-only (0750) on OVA appliances.
* §8 `netclass` gains `Documentation / special-use` (TEST-NET-1/2/3, 2001:db8::/32, 3fff::/20, benchmarking, 0/8, 192.0.0/24, 240/4, 100::/64), in the filter, the breakdown, host profiles and the forensics picker.
* Exporter (NBAR2/App-ID) application names that denote a built-in application are counted as it (`NBAR_EQUIVALENTS`, case-insensitive); `app=` accepts the exporter's spelling.
* `/tcp-flags` reports `rst_ratio` / `rst_ratio_pct` from `overview.rst_payload` (one RST definition).
* Reverse DNS in breakdowns (`resolve=1`) follows NetFlow settings → Enrichment (switch and deadline), with a `rdns_disabled` warning.
* Mapping writes check `netflow.manage` (403) before exporter visibility (404), as §6 lists them.
* The legacy filter adapters (`scope_params`, `_scope`, `_rollup_eligible`, `_qos_rollup_eligible`, `_exporter_filter`) and the legacy re-exports from `app.api.v1.netflow` are removed; no endpoint used them.
* (wave C fix) Saved overview/section views store filter, window, page (`query.path`), ranked metric (`sort`) and the page settings in `query.params` (`by`, `side`, `resolve`, `col`, `dir`; validated on write and read), so a "top receivers" view reopens as top receivers. The save dialog lists the stored settings.
* (wave C fix) Forensics page cursors: the duplicate counter `d` must be 1..`x` (the offset) and never sets the fetch size; a continued page is `(equal-key rows LIMIT n OFFSET d) UNION ALL (rows after the key LIMIT n)` in ClickHouse, so the API receives at most `page_size + 1` rows per request.
* (wave C fix) DSCP is an observation only when ToS was exported (`fields_present` bit 1, or a legacy row): the `dscp` filter requires it, `flow_qos_1h` carries `tos_known` (1 exported, 0 not exported, 2 legacy; key column, migrate-137), and `/dscp` reports `not_exported` (+ `tos_coverage_pct`, warning `dscp_not_exported`) apart from the classes.
* (wave C fix) `fw_event=flows` = every record except NSEL created/denied events (the §3.3 event guard); rollups accept it as already true. Forensics counts the events it lists (`fw_events`, warning `firewall_events_included`); card and explain drill-downs into Forensics add `fw_event=flows`.
* (wave C fix) `/forensics/export` and `POST /forensics/export-jobs` take `snapshot` (the search's `snapshot_ms` or `snapshot_at`; future values clamp to now) so the file holds exactly the counted rows; the search returns `snapshot_ms`, which (final regression) is the newest receipt already stored for the caller's exporters, not the request time, so records still in the collector's insert queue (flushed every `NETFLOW_FLUSH_SECONDS`) cannot join a later page or the pinned export. The legacy `GET /forensics?format=csv` needs `netflow.export` and is audited (`mode: legacy`).
* (wave C fix) Forensics rows carry `icmp_type`/`icmp_code` for protocol 1/58; exports add those columns and leave `src_port`/`dst_port` empty for ICMP; the UI shows ICMP endpoints without ports.
* (wave C fix) A finding id alone never runs a Forensics search; an evidence link whose finding cannot be loaded says "Finding not found or not visible". A reopened finding keeps `first_seen` and the evidence window over every counted occurrence.
* (wave C fix) `/exporters/{ip}/check`: datagrams count as received only while the exporter's state is `receiving`; `overall` adds `late` (last receipt past the expected interval, the datagrams check warns) and `stopped` (silent: the check fails with "Not receiving … since"). Field presence is read over the exporter's own flow-time span from `flow_exporter_minutes` (no raw scan when it sent nothing), at most the newest 50 000 records, reused for 30 s; the Setup page polls every 5 s until flows arrive, then every 15 s.
* (wave C fix) Exporter-to-device suggestions follow the reverse-DNS switch: off sends no PTR query; `sources.reverse_dns_status` is `disabled`, `ok`, `no_record` or `pending`.
* (wave C fix) A downloaded TI feed's text is never echoed: its parse stats keep counts only (`samples_invalid` is empty); uploads keep their samples.
* (wave C fix) Reduced-sensitivity notes list the most heavily sampled exporters first with "and N more"; `reduced_sensitivity` lists every one and the coverage list shows them all.
* (wave C fix) Theme token `--on-primary` (white light, near-black dark) is the text colour on solid primary fills (`Button` default, selected time-range and segment buttons); primary buttons hover to `primary-hover`; inactive tabs use `text2`. Dialogs opened from code return focus to the element that opened them.
* install.sh builds and enables the collector, opens UDP 2055/6343 on an already-active ufw/firewalld, sets `NETFLOW_COLLECTOR_HEALTH_URL` and `NETFLOW_EXPORT_DIR` on the API unit and creates `/opt/zenplus/data/netflow-exports` (0750, zenplus); golden provisioning also allows UDP 2055/6343.

**Wave C polish (after the final regression)**
* (polish) Detector exceptions carry an exporter scope: `netflow_detector_exceptions.exporter_ips INET[] NULL` (`scripts/migrate-140-netflow-exception-scope.sql`; NULL = every exporter, otherwise non-empty). A tag-scoped `netflow.manage` user's exception is bound at creation to the exporters visible to it (or the subset it names in `exporters`; naming any other address or a network is 400); only an unrestricted manager creates a global one; PATCH changes reason and expiry only, so a scope never widens. Matching (`ExceptionRule.exporters`, engine, ad-hoc runs, apply-to-existing) honours it. `GET /detector-exceptions` lists a scoped caller the global exceptions and those whose scope intersects its exporters; items add `scope` (`global`/`exporters`), `exporters` (only the caller's), `exporters_outside_scope` and `can_change`, the list adds `scope_exporters`. Global exceptions' CIDRs and port are not masked: they are policy applied to the caller's own exporters (a pattern an administrator approved, not flows seen on another team's exporters), and a team must be able to tell why its findings are suppressed. PATCH/DELETE: 403 without `netflow.manage`, 404 when the exception is out of the caller's sight, 403 when it is global or also covers exporters outside the caller's scope.
* (polish) Report run share tokens: `GET /report-schedules/{id}/runs` (404 for an unknown schedule) and `GET /reports/runs` need `reports.view` and return a run's `token` only to its schedule's owner (`report_schedules.created_by`) or a `reports.manage` holder (`token: null`, `token_withheld: true` otherwise; a run whose schedule was deleted: managers only). `GET /reports/shared/{token}` stays unauthenticated (the emailed capability) and is served `Cache-Control: private, no-store`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex, nofollow`.
* (polish) Every report surface that shows alert messages withholds a NetFlow alert's message from a requester without `netflow.view` with `netflow_alert_service.redact_alert_payload` (the Alert Center's rule, `netflow_details_withheld`): `report_service._fetch_alerts(redact_netflow=…)` behind `/reports/data/technical` (noisy alerts: sample and grouping key), the `noisy_sources` and `recent_alerts` sections (`SectionCtx.withholds_netflow_alerts`: the viewer, a schedule's owner, withheld for an orphaned owner), the legacy `/reports/generate` PDF/Excel/CSV alert tables and scheduled legacy attachments (the schedule owner's permission).
* (polish) The threat-intel refresh loop's due-feeds query (`netflow_ti.DUE_FEEDS_SQL`) casts `:now` to `timestamptz`: asyncpg typed the bare parameter in `:now - make_interval(...)` as an interval, so every loop iteration failed and no URL feed ever refreshed itself.
