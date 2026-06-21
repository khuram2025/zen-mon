# Application Monitoring — Architecture & Data Model

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This is the technical core of the ZenPlus Application Monitoring (APM) design set: the end-to-end ingestion-and-storage architecture, the collector build decision, and the complete ClickHouse + PostgreSQL data model with concrete DDL. APM is an **OpenTelemetry-native, ClickHouse-backed** module fused into the existing ZenPlus appliance. It accepts OTLP traces, metrics, and logs on the standard ports (gRPC `:4317`, HTTP `:4318`), processes them through a dedicated Go collector (tail sampling, span→RED metrics, service-graph derivation, PII scrubbing) with a FastAPI OTLP/HTTP fallback, and stores them in the same `zenplus` ClickHouse database that already holds ping/SNMP/host/netflow time-series — co-locating the application layer with the network and server layers so full-stack RCA happens in one store. The load-bearing economic principle is **three-stage decoupling**: RED metrics are computed from 100% of spans at ingest (always accurate), raw traces are tail-sampled to keep only errors + slow + a baseline (cheap), and dashboards read pre-aggregated rollups (never raw spans). Every name, route, table, column, partition key, TTL, and migration number below is pinned by the authoritative blueprint and used here verbatim.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — reuse inventory and gap analysis against the existing ZenPlus appliance
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — **this document**
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23), MoSCoW priority, acceptance criteria, API contracts
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP protocol, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing pipeline, collector internals
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, components, react-query keys
- `07-ROADMAP-AND-EPICS.md` — the 4 phases and 12 epics (AM-E1..AM-E12) expanded
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the test plan

---

## 1. Architecture overview

### 1.1 The three planes

ZenPlus APM separates cleanly into three planes, each with a distinct scaling and failure profile:

| Plane | What it does | Where it runs | Scaling concern |
|---|---|---|---|
| **Data plane (ingest)** | Receive OTLP, scrub PII, tail-sample, derive RED + graph, batch-insert to ClickHouse | Dedicated **Go collector** (`:4317`/`:4318`) + FastAPI fallback (`apm_ingest.py`) | Throughput, backpressure, trace-ID-aware buffering |
| **Storage plane** | Hold raw spans/logs/RUM/profiles + pre-aggregated rollups + config | ClickHouse `zenplus` DB (time-series) + PostgreSQL `zenplus` DB (config/state) | Compression, partition pruning, TTL, cardinality |
| **Control/query plane** | Serve `/api/v1/apm/*`, run evaluator/SLO loops, render UI | FastAPI router `apm.py` + asyncio background loops + React `pages/apm/*` | Query latency (must hit rollups, never raw) |

The architectural insight ZenPlus inherits from SigNoz and the ClickHouse-OTel idioms is that **dashboards and alerts must never scan raw spans**. Raw spans exist only for trace-explorer drill-down (looked up by `trace_id`) and for the materialized views that feed the rollups. All aggregate UI surfaces — service RED charts, SLO budgets, the service map, apdex — read pre-aggregated `*_5m`/`*_1h` SummingMergeTree tables. This is what makes a single-node appliance affordable at real trace volume.

### 1.2 End-to-end data flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  INSTRUMENTED WORKLOADS                                                         │
│  • OTel SDK (Java/.NET/Node/Python/Go/Ruby/PHP)   • ZenPlus OTel distro        │
│  • eBPF zero-code agent (Beyla/OBI-style)         • Browser/Mobile RUM SDK     │
│  • DB-monitoring agent (query digests + plans)    • Synthetic = ZenPlus poller │
└───────────────┬──────────────────────────────────────────────┬────────────────┘
                │ OTLP/gRPC :4317   OTLP/HTTP :4318             │ HTTPS beacon
                │ (W3C traceparent, zpi_ bearer)               │ (zpr_ key, CORS)
                ▼                                              ▼
┌──────────────────────────────────────────────┐   ┌───────────────────────────┐
│  ZENPLUS OTEL COLLECTOR  (Go, sibling binary) │   │ FastAPI apm_ingest.py     │
│  receivers: otlp/grpc, otlp/http              │   │  (OTLP/HTTP fallback +     │
│  processors (ordered):                        │   │   RUM beacon endpoint)     │
│   memory_limiter → resource/attributes        │   │  reuses _authenticate-style│
│   → PII scrubbing → tail_sampling             │   │  hashed bearer + buffered  │
│     (keep errors + slow + p% baseline)        │   │  client.insert             │
│   → batch                                     │   └─────────────┬─────────────┘
│  connectors: spanmetrics → metrics pipeline   │                 │
│              servicegraph → graph pipeline    │                 │
│  exporters: clickhouse (buffered batch insert)│                 │
│  auth: zpi_ hashed-bearer (mirrors zpa_)      │                 │
└───────────────┬──────────────────────────────┘                 │
                │ batched columnar INSERT (native/HTTP)            │
                ▼                                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  CLICKHOUSE  (zenplus DB, single-node-friendly)                                │
│  apm_spans (7d, daily, ZSTD)        apm_span_metrics_5m/_1h (SummingMV)        │
│  apm_traces_resource (fingerprint)  apm_service_graph (SummingMV)              │
│  apm_exceptions (30d)               apm_rum_events/_vitals_5m                  │
│  apm_logs (14d)  apm_profiles(14d)  apm_synthetic_results/_5m                  │
│  ── co-located with existing ──> ping_metrics, host_*_metrics, snmp_*, flow_*  │
└───────────────┬───────────────────────────────────────────────┬───────────────┘
                │  parameterized %(name)s reads (singleton client)│
                ▼                                                 ▼
┌──────────────────────────────────────────────┐   ┌───────────────────────────┐
│  POSTGRES (zenplus DB)                         │   │  BACKGROUND LOOPS (FastAPI │
│  apm_services, apm_environments,               │   │  lifespan, asyncio tasks)  │
│  apm_ingest_keys, apm_enrollment_tokens,       │   │  • apm_alert_evaluator_loop│
│  apm_slos, apm_synthetic_monitors,             │   │  • apm_slo_burn_loop       │
│  apm_sampling_rules, apm_scrubbing_rules,      │   │  • apm_nodata_sweeper       │
│  apm_deployments, apm_dashboards,              │   │  • apm_anomaly_loop (P4)    │
│  apm_error_issues   + reuse alert_rules/alerts │   │  (mirror host/network loops)│
└───────────────┬──────────────────────────────┘   └─────────────┬─────────────┘
                │                                                  │
                ▼                                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  FastAPI API  /api/v1/apm/*   (router apm.py; get_db + get_clickhouse_client)  │
│  reuses: alert_engine dispatch_to_channels, email_render, alert_schedule       │
└───────────────────────────────────┬────────────────────────────────────────────┘
                                     │ axios (lib/api.ts, baseURL /api/v1, bearer)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  REACT DASHBOARD  pages/apm/*  (mirrors pages/servers/* scaffolding)           │
│  Services list · Service detail (?tab=) · Trace explorer/waterfall · Service   │
│  Map · Errors inbox · RUM · Synthetics · SLOs · Settings                        │
│  react-query keys ['apm',...] · TimeRangePicker · components/apm/shared.tsx     │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 Identical row shapes from both ingest paths

The Go collector (primary) and the FastAPI `apm_ingest.py` fallback (secondary) **write identical ClickHouse row shapes** — the `apm_*` tables in §3. The query/UI layer is therefore agnostic to which path ingested a given span. The Go collector uses the upstream ClickHouse exporter (native protocol, batched); the FastAPI fallback wraps the `get_clickhouse_client()` singleton in a buffered async batch writer (bounded queue, flush by size/interval) instead of per-request inserts. This is the single most important rule of the ingest design: **one canonical row schema, two writers.**

---

## 2. The collector decision: dedicated Go collector + FastAPI fallback

**Decision:** the OTLP front door is a **dedicated Go collector binary** in the poller toolchain (`poller/cmd/otelcollector` or a sibling binary) listening on `:4317` (gRPC) and `:4318` (HTTP), with FastAPI providing a **minimal OTLP/HTTP-only fallback receiver** (`apm_ingest.py`) for small/single-binary/dev installs and for the RUM beacon.

### 2.1 Why not "just FastAPI"

The codebase audit is explicit that the current FastAPI/`clickhouse_connect` ingest path (the host-agent telemetry pipeline) is **synchronous, per-request, single HTTP client, with no buffering/backpressure**, and that idempotency is a stated no-op (`duplicates=0`). The host-agent handler also mixes a ClickHouse insert with multiple Postgres upserts and a health recompute in one request — far too heavy for per-span volume. APM ingest must instead be a lean append-only ClickHouse write with async post-processing. The decisive factors:

| Factor | FastAPI today | Requirement | Verdict |
|---|---|---|---|
| OTLP/gRPC (`:4317`) | Painful — no first-class gRPC | Mandatory for drop-in OTel compatibility | **Go wins** |
| Tail sampling | Needs all spans of a trace on one instance (stateful buffering); FastAPI runs 2 stateless uvicorn workers | `decision_wait` buffering + trace-ID-aware routing | **Go wins** |
| Backpressure / load shedding | Only an unused `backpressure` response field | `memory_limiter` + bounded queue, OOM-safe on a single appliance | **Go wins** |
| Span→RED + service-graph | Hand-rolled | `spanmetrics` + `servicegraph` connectors exist upstream | **Go wins** |
| Buffered batch insert | Per-request `client.insert` | Async batched columnar insert | **Go wins** |
| Packaging / OTA | — | ZenPlus already ships Go poller binaries through the same build/release/OTA toolchain | **Go reuses it** |

The Go collector reuses the upstream `go.opentelemetry.io/collector` libraries, so receivers, processors (`memory_limiter`, `batch`, `tailsamplingprocessor`), connectors (`spanmetrics`, `servicegraph`), and the ClickHouse exporter come essentially for free. The Go poller's worker-pool/scheduler patterns transfer directly.

### 2.2 Why keep a FastAPI fallback

The FastAPI `apm_ingest.py` receiver decodes OTLP protobuf/JSON → row dicts → buffered `client.insert`, serving:

1. **Tiny/single-binary/dev installs** that should not be forced to run the Go collector.
2. **The RUM beacon** (`POST /api/v1/apm/rum/ingest`), which is HTTP-only, origin-scoped, and CORS-bound anyway — gRPC is irrelevant here.
3. **Graceful degradation** — if the Go collector is down, low-volume OTLP/HTTP still lands.

Both paths authenticate the `zpi_` hashed bearer against the same `apm_ingest_keys` table (the Go collector via a read-through cache with periodic refresh), and both emit the same rows. See `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` for collector-config internals.

---

## 3. ClickHouse data model

> **Conventions followed.** `DateTime64(9,'UTC')` for span-shaped tables (OTel nanoseconds); `DateTime64(3,'UTC')` for synthetic + all rollups (millis, matches existing tables). `PARTITION BY toYYYYMMDD` (daily) for high-volume raw tables (spans/RUM/logs/exceptions/profiles — the netflow `flow_records` precedent), `PARTITION BY toYYYYMM` (monthly) for rollups. `ORDER BY (entity[,dims], ts_bucket, timestamp)` with a `ts_bucket` first/middle dim for partition-prune-friendly scans plus bloom-filter skip indexes for high-cardinality `trace_id` lookup. `LowCardinality(String)` for bounded dims; typed attribute maps (string/number/bool) + a resource JSON column (the SigNoz pattern). `SummingMergeTree` rollups fed by `... TO ... AS SELECT` materialized views. Per-table TTL with `ttl_only_drop_parts = 1` so a whole day's partition is dropped atomically. **Every statement `CREATE ... IF NOT EXISTS`** — these ship in `migrate-039-apm-clickhouse.sql` (and split files), auto-applied by `clickhouse_sync.py` on every OTA update, and must be idempotent and **never** added to `_LEGACY_BASELINE`.

### 3.1 New codec convention (deliberate)

ClickHouse codecs are unused anywhere in the current ZenPlus schema (a clean baseline). APM's volume justifies **introducing a codec convention deliberately** (not silently):

| Column class | Codec | Why |
|---|---|---|
| Nanosecond timestamps (`timestamp`) | `CODEC(Delta(8), ZSTD(1))` | Monotonic-ish ns values compress massively under delta+entropy coding |
| IDs / blobs / large strings (`trace_id`, `db_statement`, `resource`, `exception_stack`, `body`, pprof `payload`) | `CODEC(ZSTD(1))` (pprof `ZSTD(3)`) | High-entropy or large text; ZSTD gives the ~9–10× compression OTel/SigNoz schemas report |

`LowCardinality(String)` dimensions carry their own dictionary encoding and do not need an explicit codec.

### 3.2 The trace-id-lookup vs service/time-scan tension

`trace_id`/`span_id` cardinality is far higher than any existing entity (`device_id`/`server_id`) and is a poor leading `ORDER BY` key. Two query classes must both be fast:

- **Trace lookup** (`GET /apm/traces/{trace_id}` → waterfall): `WHERE trace_id = ...`. Served by a `bloom_filter(0.001)` skip index `idx_trace_id`, not by the primary key.
- **Service/time scans** (RED, errors, search by service+operation+time): `WHERE service_name = ... AND name = ... AND timestamp BETWEEN ...`. Served by the primary key via leading `(service_name, name, ts_bucket, ...)`.

`ts_bucket UInt32` = `toUInt32(toStartOfInterval(timestamp, 1800s))` (30-minute floor) sits in the `ORDER BY` so time-windowed queries prune the index without depending on `timestamp` precision, exactly mirroring how ZenPlus already prunes its rollups. Resource-attribute filters resolve against a tiny side table (`apm_traces_resource`) via CTE + `GLOBAL IN`, avoiding full scans on the big table.

### 3.3 `apm_spans` — the core table

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_spans
(
    timestamp        DateTime64(9,'UTC') CODEC(Delta(8), ZSTD(1)),
    trace_id         FixedString(32)     CODEC(ZSTD(1)),
    span_id          String              CODEC(ZSTD(1)),
    parent_span_id   String              CODEC(ZSTD(1)),
    name             LowCardinality(String),          -- operation / span name
    span_kind        Int8,                            -- 0..5 OTel kind
    span_kind_str    LowCardinality(String),
    service_name     LowCardinality(String),
    env              LowCardinality(String),          -- resource: deployment.environment
    service_version  LowCardinality(String),          -- resource: service.version
    duration_nano    UInt64               CODEC(ZSTD(1)),
    status_code      LowCardinality(String),          -- UNSET/OK/ERROR
    status_message   String               CODEC(ZSTD(1)),
    has_error        UInt8,
    -- promoted semantic-convention hot columns (filter-fast):
    http_method      LowCardinality(String),
    http_route       LowCardinality(String),
    http_status_code UInt16,
    db_system        LowCardinality(String),
    db_operation     LowCardinality(String),
    db_statement     String               CODEC(ZSTD(1)),   -- normalized digest
    rpc_method       LowCardinality(String),
    -- typed attribute bags:
    attributes_string Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    attributes_number Map(LowCardinality(String), Float64),
    attributes_bool   Map(LowCardinality(String), UInt8),
    resource          String CODEC(ZSTD(1)),          -- resource attrs as JSON
    resource_fingerprint String CODEC(ZSTD(1)),
    events_ts        Array(DateTime64(9,'UTC')),
    events_name      Array(LowCardinality(String)),
    events_attrs     Array(String) CODEC(ZSTD(1)),
    links_trace_id   Array(FixedString(32)),
    links_span_id    Array(String),
    ts_bucket        UInt32,                          -- toUInt32(toStartOfInterval(ts,1800s))
    deployment_id    UUID,                            -- FK to apm_deployments (zeros if none)
    INDEX idx_trace_id       trace_id            TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_span_attr_keys mapKeys(attributes_string)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_vals mapValues(attributes_string) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_duration       duration_nano       TYPE minmax GRANULARITY 1,
    INDEX idx_http_route     http_route          TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, name, ts_bucket, trace_id)
TTL toDateTime(timestamp) + toIntervalDay(7)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
```

**Design notes.** Attributes are split into three typed maps (string/number/bool) plus a `resource` JSON column — never one stringly map — so the UI filters on indexed promoted columns (`http_*`, `db_*`, `rpc_*`) and only falls back to the maps (via `idx_span_attr_keys`/`idx_span_attr_vals`) for arbitrary attributes. `db_statement` stores a **normalized digest** (literals/bind-params stripped), never raw SQL with PII. Events and links are stored as parallel arrays (the OTel/ClickHouse-exporter idiom).

**Resource side table** (avoids full scans on resource-attribute filters):

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_traces_resource
(
    fingerprint  String,
    labels       String,            -- resource attrs JSON
    seen_at      DateTime64(3,'UTC'),
    ts_bucket    UInt32
)
ENGINE = ReplacingMergeTree(seen_at)
PARTITION BY toYYYYMM(seen_at)
ORDER BY (fingerprint)
TTL toDateTime(seen_at) + toIntervalDay(7);
```

Query pattern: resolve matching fingerprints in this tiny table via a CTE, then filter the big table with `... WHERE resource_fingerprint GLOBAL IN (cte)`.

### 3.4 Span → RED metric rollups (5m + 1h)

Dashboards, SLOs, and alerts read **only** these rollups. The MV computes them from **100% of spans at insert, before any tail sampling drops raw spans** — so RED and apdex stay accurate even when raw traces are sampled away. This is the linchpin of the cost model.

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_span_metrics_5m
(
    timestamp     DateTime64(3,'UTC'),
    service_name  LowCardinality(String),
    operation     LowCardinality(String),
    span_kind     LowCardinality(String),
    env           LowCardinality(String),
    status_code   LowCardinality(String),
    request_count UInt64,
    error_count   UInt64,
    -- histogram state for percentiles at query time:
    duration_state AggregateFunction(quantilesTDigest(0.5,0.75,0.9,0.95,0.99), Float64),
    duration_sum  Float64,
    duration_min  Float64,
    duration_max  Float64,
    sample_count  UInt32
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (service_name, operation, span_kind, env, status_code, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);

CREATE MATERIALIZED VIEW IF NOT EXISTS zenplus.apm_span_metrics_5m_mv
TO zenplus.apm_span_metrics_5m AS
SELECT
    toStartOfFiveMinutes(timestamp)                       AS timestamp,
    service_name, name AS operation, span_kind_str AS span_kind,
    env, status_code,
    count()                                               AS request_count,
    countIf(has_error = 1)                                AS error_count,
    quantilesTDigestState(0.5,0.75,0.9,0.95,0.99)(duration_nano/1e6) AS duration_state,
    sum(duration_nano)/1e6                                AS duration_sum,
    min(duration_nano)/1e6                                AS duration_min,
    max(duration_nano)/1e6                                AS duration_max,
    count()                                               AS sample_count
FROM zenplus.apm_spans
GROUP BY service_name, operation, span_kind, env, status_code, timestamp;
```

`apm_span_metrics_1h` + `apm_span_metrics_1h_mv` follow identically with `toStartOfHour(timestamp)` and `TTL ... + toIntervalDay(395)`. Read-time math:

| RED signal | Query expression |
|---|---|
| Throughput (rate) | `sum(request_count) / window_seconds` |
| Error rate | `sum(error_count) / sum(request_count)` |
| Latency p95 | `quantilesTDigestMerge(0.95)(duration_state)` |
| Apdex (T threshold) | `(satisfied + tolerating/2) / total`, where buckets come from per-T `countIf(duration ≤ T)` / `countIf(duration ≤ 4T)` distributions stored alongside (computed from `request_count` + threshold buckets) |

Storing latency as tdigest **aggregate state** (not a precomputed percentile) means percentiles merge correctly across rollup parts and across 5m→1h, and arbitrary quantiles are queryable without re-scanning raw spans.

### 3.5 `apm_service_graph` — dependency edges

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_service_graph
(
    timestamp        DateTime64(3,'UTC'),
    client_service   LowCardinality(String),
    server_service   LowCardinality(String),
    env              LowCardinality(String),
    request_count    UInt64,
    error_count      UInt64,
    duration_sum_ms  Float64,
    sample_count     UInt32
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (client_service, server_service, env, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);
```

Primary derivation path: the Go collector's `servicegraph` connector pairs `CLIENT`/`SERVER` spans sharing a trace and emits edges with RED. Fallback path: a ClickHouse MV pairing `span_kind_str='CLIENT'` spans with their child `SERVER` spans by `trace_id`/`parent_span_id`. Graph nodes = distinct services from PG `apm_services`; edges + edge-RED come from this table. The service map (`GET /apm/service-map`) reads nodes + edges from here, never from `apm_spans`.

### 3.6 `apm_exceptions` — Sentry-style error grouping

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_exceptions
(
    timestamp        DateTime64(9,'UTC') CODEC(Delta(8), ZSTD(1)),
    error_id         UUID,
    group_id         FixedString(16),                 -- fingerprint of type+normalized-stack
    trace_id         FixedString(32),
    span_id          String,
    service_name     LowCardinality(String),
    env              LowCardinality(String),
    service_version  LowCardinality(String),
    exception_type   LowCardinality(String),
    exception_message String CODEC(ZSTD(1)),
    exception_stack  String CODEC(ZSTD(1)),
    exception_escaped UInt8,
    http_route       LowCardinality(String),
    resource_tags    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ts_bucket        UInt32,
    INDEX idx_exc_group group_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_exc_trace trace_id TYPE bloom_filter(0.001) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, group_id, ts_bucket, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(30);
```

**Split of concerns:** `group_id` (a 16-byte fingerprint of `exception_type` + normalized stack frames — stripping UUIDs/hex/line-noise so similar errors collapse) is computed by the collector and stored in ClickHouse; **occurrence counts/trends/first-last-seen come from CH**, while **triage state** (status `unresolved`/`resolved`/`resolved_in_version`/`ignored`, assignee) lives in PG `apm_error_issues` keyed by the same `group_id`. Errors retain 30 days (longer than spans' 7) because they are high-value. The Errors Inbox (`GET /apm/errors`) joins CH counts to PG triage rows on `group_id`.

### 3.7 `apm_rum_events` + vitals rollup

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_rum_events
(
    timestamp       DateTime64(3,'UTC'),
    application_id  LowCardinality(String),
    session_id      String CODEC(ZSTD(1)),
    view_id         String CODEC(ZSTD(1)),
    view_name       LowCardinality(String),          -- route
    event_type      LowCardinality(String),          -- view/action/error/resource/long_task
    -- Core Web Vitals (only on view events):
    lcp_ms          Float32,
    inp_ms          Float32,
    cls             Float32,
    fcp_ms          Float32,
    ttfb_ms         Float32,
    load_ms         Float32,
    error_message   String CODEC(ZSTD(1)),
    user_id         String CODEC(ZSTD(1)),
    country         LowCardinality(String),
    browser         LowCardinality(String),
    device_type     LowCardinality(String),
    backend_trace_id FixedString(32),                -- stitch to apm_spans
    attributes      Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ts_bucket       UInt32,
    INDEX idx_rum_session session_id TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_rum_trace   backend_trace_id TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (application_id, view_name, ts_bucket, session_id)
TTL toDateTime(timestamp) + toIntervalDay(14);
```

`apm_rum_vitals_5m` + `apm_rum_vitals_5m_mv` roll up per `application_id`+`view_name`: **p75** LCP/INP/CLS (the Core Web Vitals standard percentile) via tdigest state, plus `sample_count`, `TTL 90d`. `backend_trace_id` stitches a browser view directly to the backend `apm_spans` trace (RUM→backend correlation).

### 3.8 `apm_profiles` — continuous profiling (Phase 3)

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_profiles
(
    timestamp     DateTime64(9,'UTC') CODEC(Delta(8), ZSTD(1)),
    service_name  LowCardinality(String),
    env           LowCardinality(String),
    service_version LowCardinality(String),
    profile_type  LowCardinality(String),            -- cpu/alloc/lock/wall
    trace_id      FixedString(32),                   -- link span→flamegraph
    span_id       String,
    duration_ns   UInt64,
    sample_count  UInt32,
    payload       String CODEC(ZSTD(3)),             -- pprof blob (dictionary-compressed wire)
    ts_bucket     UInt32,
    INDEX idx_prof_trace trace_id TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, profile_type, ts_bucket, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(14);
```

**Decision:** store the pprof blob **inline** in ClickHouse `String CODEC(ZSTD(3))`, consistent with the platform's "everything inline" convention. `trace_id`/`span_id` enable trace→flamegraph linking (jump from a slow span to the exact code path). **Risk (noted):** if blob sizes prove problematic at scale, revisit an object-storage-pointer model (a CH row referencing an external blob); this is the one place the inline convention is most likely to be challenged.

### 3.9 `apm_logs` — trace-correlated logs

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_logs
(
    timestamp     DateTime64(9,'UTC') CODEC(Delta(8), ZSTD(1)),
    service_name  LowCardinality(String),
    env           LowCardinality(String),
    severity      LowCardinality(String),            -- TRACE..FATAL
    severity_num  UInt8,
    body          String CODEC(ZSTD(1)),
    trace_id      FixedString(32),
    span_id       String,
    attributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    resource      String CODEC(ZSTD(1)),
    ts_bucket     UInt32,
    INDEX idx_log_trace trace_id TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_log_body  body     TYPE tokenbf_v1(8192,3,0) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (service_name, ts_bucket, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(14);
```

Trace↔log pivot is one-click: `WHERE trace_id = ...` hits `idx_log_trace`. Free-text log search uses the `tokenbf_v1` token bloom-filter on `body`. Logs are correlated to spans by the shared `trace_id`/`span_id` injected by the SDK/agent at emit time.

### 3.10 `apm_synthetic_results` (Phase 3, reuses poller infra)

```sql
CREATE TABLE IF NOT EXISTS zenplus.apm_synthetic_results
(
    timestamp     DateTime64(3,'UTC'),
    monitor_id    UUID,
    monitor_type  LowCardinality(String),            -- api/browser/multistep
    location      LowCardinality(String),            -- probe location id
    is_up         UInt8,
    response_ms   UInt32,
    status_code   UInt16,
    step_index    UInt16,                             -- 0 = whole; >0 per step
    step_name     LowCardinality(String),
    assertion_failed UInt8,
    error_message String CODEC(ZSTD(1)),
    backend_trace_id FixedString(32),                -- synthetic request → backend trace
    poller_id     LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (monitor_id, location, timestamp)
TTL toDateTime(timestamp) + toIntervalDay(90);
```

`apm_synthetic_results_5m` + `apm_synthetic_results_5m_mv` roll up per `monitor_id`+`location`: `uptime_pct`, avg/p95 `response_ms`, `sample_count`, `TTL 395d`. Synthetics reuse the existing Go poller (`engine.go` scheduler/retry/flap/maintenance + `checker/` extended with a synthetic/browser case) and the `service_checks` infrastructure — they do **not** re-implement probes in Python. `step_index > 0` rows carry per-step latency for multi-step waterfalls; `backend_trace_id` links a synthetic request to the backend trace it generated.

### 3.11 ClickHouse table summary

| Table | Engine | Partition | ORDER BY | TTL |
|---|---|---|---|---|
| `apm_spans` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, name, ts_bucket, trace_id)` | raw 7d |
| `apm_span_metrics_5m` | SummingMergeTree | `toYYYYMM(timestamp)` | `(service_name, operation, span_kind, env, status_code, timestamp)` | 90d |
| `apm_span_metrics_1h` | SummingMergeTree | `toYYYYMM(timestamp)` | `(service_name, operation, span_kind, env, status_code, timestamp)` | 395d |
| `apm_traces_resource` | ReplacingMergeTree | `toYYYYMM(seen_at)` | `(fingerprint)` | 7d |
| `apm_service_graph` | SummingMergeTree | `toYYYYMM(timestamp)` | `(client_service, server_service, env, timestamp)` | 90d |
| `apm_exceptions` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, group_id, ts_bucket, timestamp)` | raw 30d |
| `apm_rum_events` | MergeTree | `toYYYYMMDD(timestamp)` | `(application_id, view_name, ts_bucket, session_id)` | 14d |
| `apm_rum_vitals_5m` | SummingMergeTree | `toYYYYMM(timestamp)` | `(application_id, view_name, timestamp)` | 90d |
| `apm_profiles` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, profile_type, ts_bucket, timestamp)` | 14d |
| `apm_logs` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, ts_bucket, timestamp)` | 14d |
| `apm_synthetic_results` | MergeTree | `toYYYYMM(timestamp)` | `(monitor_id, location, timestamp)` | 90d |
| `apm_synthetic_results_5m` | SummingMergeTree | `toYYYYMM(timestamp)` | `(monitor_id, location, timestamp)` | 395d |

Materialized views: `apm_span_metrics_5m_mv`, `apm_span_metrics_1h_mv`, `apm_service_graph_mv`, `apm_rum_vitals_5m_mv`, `apm_synthetic_results_5m_mv`.

---

## 4. PostgreSQL configuration model

> **House style** (per `migrate-038`): `UUID PRIMARY KEY DEFAULT gen_random_uuid()` (pgcrypto), `TIMESTAMPTZ DEFAULT NOW()`, `JSONB DEFAULT '[]'::jsonb`/`'{}'::jsonb`, `VARCHAR(n) CHECK (col IN (...))` enums, GIN indexes on queried JSONB, explicit `idx_*` indexes (partial where appropriate). All `CREATE TABLE IF NOT EXISTS`, all idempotent. High-volume telemetry never lands in Postgres — only definitions/config/registry/SLO/triage state. These ship in **`migrate-039-apm.sql`**.

### 4.1 Config tables

| Table | Purpose |
|---|---|
| `apm_environments` | env registry (`prod`/`staging`/`dev`), per-env retention/sampling overrides |
| `apm_services` | service registry: name, env_id, language, team, owner, repo, tags, denormalized last-seen RED + health |
| `apm_ingest_keys` | ingest key registry (`key_hash`, `key_prefix`, `kind` ∈ `sdk`/`rum`, env scope, RUM origin allowlist) |
| `apm_enrollment_tokens` | one-time enrollment tokens (mirrors `agent_enrollment_tokens`) |
| `apm_slos` | SLO defs: sli_type, target, window, scope (service/operation), burn-alert config |
| `apm_synthetic_monitors` | synthetic monitor defs (also reuses `service_checks` infra) |
| `apm_sampling_rules` | head/tail sampling policy config |
| `apm_scrubbing_rules` | PII redaction rules (attribute allow/deny, regex/OTTL-style) |
| `apm_deployments` | deployment/change markers (service, version, sha, ts) |
| `apm_dashboards` | saved APM dashboards |
| `apm_error_issues` | error-issue triage state (status, assignee, first/last-seen mirror) — grouping key in CH, triage state in PG |

### 4.2 Representative DDL

```sql
CREATE TABLE IF NOT EXISTS apm_environments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(64) NOT NULL UNIQUE,                 -- prod/staging/dev
    retention_days_raw SMALLINT NOT NULL DEFAULT 7 CHECK (retention_days_raw BETWEEN 1 AND 30),
    sampling_target_tps SMALLINT NOT NULL DEFAULT 10,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS apm_services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,                        -- == OTel service.name
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    language VARCHAR(32),
    team VARCHAR(128),
    owner VARCHAR(128),
    repo_url TEXT,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    health VARCHAR(16) NOT NULL DEFAULT 'no_data'
           CHECK (health IN ('healthy','degraded','critical','no_data')),
    last_seen_at TIMESTAMPTZ,
    last_rps DOUBLE PRECISION, last_error_rate DOUBLE PRECISION,
    last_p95_ms DOUBLE PRECISION, last_apdex DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (name, env_id)
);
CREATE INDEX IF NOT EXISTS idx_apm_services_health ON apm_services(health);
CREATE INDEX IF NOT EXISTS idx_apm_services_tags ON apm_services USING GIN (tags);

CREATE TABLE IF NOT EXISTS apm_ingest_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'sdk' CHECK (kind IN ('sdk','rum')),
    key_hash VARCHAR(128) NOT NULL UNIQUE,
    key_prefix VARCHAR(16) NOT NULL,                  -- zpi_ / zpr_
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    origin_allowlist JSONB NOT NULL DEFAULT '[]'::jsonb,  -- RUM CORS origins
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    rotated_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_apm_ingest_keys_hash ON apm_ingest_keys(key_hash) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS apm_enrollment_tokens (    -- mirrors agent_enrollment_tokens
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash VARCHAR(128) NOT NULL UNIQUE,
    token_prefix VARCHAR(16) NOT NULL,
    kind VARCHAR(16) NOT NULL DEFAULT 'sdk' CHECK (kind IN ('sdk','rum')),
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ, consumed_ip INET,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS apm_slos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    operation VARCHAR(255),                            -- NULL = whole service
    sli_type VARCHAR(32) NOT NULL CHECK (sli_type IN ('availability','latency','error_rate','custom')),
    latency_threshold_ms INTEGER,                      -- for latency SLI
    target DOUBLE PRECISION NOT NULL,                  -- e.g. 99.9
    window_days SMALLINT NOT NULL DEFAULT 30 CHECK (window_days IN (7,30,90)),
    burn_alert_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notify_channels JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS apm_deployments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    version VARCHAR(128) NOT NULL,
    git_sha VARCHAR(64),
    env_id UUID REFERENCES apm_environments(id) ON DELETE SET NULL,
    deployed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_apm_deployments_service ON apm_deployments(service_id, deployed_at);

CREATE TABLE IF NOT EXISTS apm_error_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id VARCHAR(32) NOT NULL,                     -- == CH apm_exceptions.group_id (hex)
    service_id UUID REFERENCES apm_services(id) ON DELETE CASCADE,
    status VARCHAR(24) NOT NULL DEFAULT 'unresolved'
           CHECK (status IN ('unresolved','resolved','resolved_in_version','ignored')),
    resolved_in_version VARCHAR(128),
    assignee VARCHAR(128),
    first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (group_id, service_id)
);
CREATE INDEX IF NOT EXISTS idx_apm_error_issues_status ON apm_error_issues(status);
```

`apm_synthetic_monitors`, `apm_sampling_rules`, `apm_scrubbing_rules`, and `apm_dashboards` follow the same house style (full DDL in `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` for the ingest/sampling/scrubbing tables, and `06-UI-UX-AND-DASHBOARDS.md` for dashboards). `apm_synthetic_monitors` additionally reuses the `service_checks` JSONB `config` idiom to store multi-step scripts/assertions/locations without schema churn.

### 4.3 Reuse of the existing alert tables

APM does **not** create parallel alert tables. It reuses `alert_rules`/`alerts` and registers new `apm_`-prefixed metric keys via the `alert_rules.metric` CHECK constraint (see §6 and `04-FEATURE-SPECIFICATION.md` / `07-ROADMAP-AND-EPICS.md`).

---

## 5. Sampling, scale, retention & cost control

### 5.1 Three-stage decoupling (the cost principle)

| Stage | Sampling | What it produces | Read by |
|---|---|---|---|
| **1. Metricize** (always-on, 100%) | none | `apm_span_metrics_5m/_1h`, `apm_service_graph` (computed at insert, before sampling) | dashboards, SLOs, alerts |
| **2. Ingest control** (head, bounded) | probabilistic, ~`sampling_target_tps`/service/env | a representative raw-span baseline | trace explorer (baseline) |
| **3. Retain/index** (tail, value-aware) | `tailsamplingprocessor` (buffer `decision_wait` ~5s) | errors + slow + business-critical + small baseline kept in `apm_spans` | trace explorer (high-value) |

Because RED/apdex/graph are computed from 100% of spans **before** sampling drops raw traces, dashboards and alerts stay correct even when 95–99% of raw traces are dropped. This is the SigNoz/Datadog "metrics-from-traces-before-sampling" idiom.

### 5.2 Head vs tail sampling

- **Head** (probabilistic, decided at trace origin, propagated in `traceparent`): cheap, blind to which traces matter; sets the cost floor. Default head target is `apm_environments.sampling_target_tps` (default 10 tps/service-equivalent).
- **Tail** (`tailsamplingprocessor`, decided after the full trace is buffered): ordered policies — **keep 100% errors** (`status_code=ERROR` / `has_error`), **keep slow** (latency outliers above a pXX threshold), **keep ~1–5% probabilistic baseline**, plus string-attribute policies for business-critical traces. **Tail is the default for `prod`.** Tail requires all spans of a trace on one collector instance — the reason ingest is a stateful Go collector, not a stateless FastAPI worker.

Every retained span carries an ingestion/retention reason in `attributes_string['zp.retain_reason']` (`auto`/`error`/`slow`/`rule`/`baseline`) for cost attribution — the Datadog `ingestion_reason` idiom, enabling per-mechanism volume accounting.

### 5.3 Cardinality control

- Attributes split into typed maps + **promoted hot columns only** (`http_*`, `db_*`, `rpc_*`); UI filters hit indexed columns, not arbitrary high-cardinality maps.
- `operation`/`http_route`/all dimensions are `LowCardinality` (bounded); allow-list + cardinality caps on promoted dimensions.
- Unbounded text (`db_statement`, `exception_message`, log `body`) is normalized (query digests) or kept out of `ORDER BY` and indexed sparingly (`tokenbf_v1` on `body` only).
- Span→RED rollups collapse high-cardinality span detail into bounded timeseries; business KPIs are promoted as span-based custom metrics (Phase 4) rather than indexing everything.

### 5.4 Retention tiers

| Data | Raw TTL | Rollup TTL |
|---|---|---|
| Spans | 7d (daily partitions, ZSTD) | metrics 5m=90d, 1h=395d |
| Exceptions | 30d (high-value) | — |
| RUM events | 14d | vitals 5m=90d |
| Logs | 14d | — |
| Profiles | 14d | — |
| Synthetic | 90d raw | 5m=395d |
| Service graph | — | 90d |

`ttl_only_drop_parts = 1` + daily partitions means TTL reclaims space by dropping whole-day parts atomically — cheap and free of per-row mutation overhead.

### 5.5 Scale / volume math (single-node affordability)

Worked example for a mid-size appliance:

- 1,000 req/s ingress fan-out × ~8 spans/request ≈ **8,000 spans/s** ≈ **~690M spans/day** raw arriving at the collector.
- Tail sampling keeps errors + slow + ~3% baseline. With ~2% error+slow + 3% baseline ≈ **5% retained** → ~**35M spans/day** persisted to `apm_spans`.
- At ZSTD ~9–10× compression and an estimated ~250 raw bytes/span post-compression, 35M spans/day ≈ **~8–9 GB/day** raw, held 7 days ≈ **~60 GB** steady-state for the raw span store — comfortably single-node.
- RED rollups: dimensioned by (service × operation × span_kind × env × status) at 5-minute granularity, the `apm_span_metrics_5m` cardinality is **bounded by service/operation count**, not by span count — typically a few thousand rows per 5-minute bucket regardless of the 8,000 spans/s. This is why dashboards stay sub-second.
- `ts_bucket` (30-min) as an `ORDER BY` dim + `resource_fingerprint` CTE pruning + bloom skip indexes keep service-scoped scans and `trace_id` lookups **sub-second** without full scans.
- The Go collector's `memory_limiter` (first processor) + `batch` provide backpressure/load-shedding so spikes do not OOM the appliance; the FastAPI fallback uses a bounded queue and finally wires the previously-unused `backpressure` response field.

Single-node ClickHouse comfortably handles **low-millions of retained spans/day** because (a) tail sampling drops the boring 95–99%, (b) daily drop-parts reclaim space cheaply, and (c) dashboards never touch raw spans.

---

## 6. Ingest auth (reusing the agent ingest-key model)

APM forks the proven `agents.py` auth primitives rather than inventing new ones.

| Concern | Mechanism | Source reused |
|---|---|---|
| **SDK/collector keys** | `zpi_` + `token_urlsafe(32)`; returned once; stored as `sha256` hash + 16-char prefix in `apm_ingest_keys.key_hash`. Runtime: `Authorization: Bearer zpi_...` validated by constant-time `hmac.compare_digest(sha256(bearer), key_hash)` | fork of `agents.py` `_sha256`, `_new_api_key` (prefix `zpa_`→`zpi_`), `_strip_bearer`, `_authenticate` |
| **Enrollment** | one-time hashed `apm_enrollment_tokens` (`max_uses`, `expires_at`, `revoked_at`, `consumed_ip`) | mirror of `agent_enrollment_tokens` + `/enroll` consumption logic |
| **Go collector auth** | validates the same `zpi_` bearer against the same `apm_ingest_keys` table via a read-through cache with periodic refresh | shared table, no second key store |
| **RUM keys** | `zpr_` **public, origin-scoped**: no secret in client JS; CORS origin allowlist (`apm_ingest_keys.origin_allowlist`) + per-origin rate-limiting on `POST /api/v1/apm/rum/ingest` | distinct public-key path alongside `_authenticate` |
| **Control-plane RBAC** | `require_operator_user` (writes) / `get_current_user` (reads) on all `/api/v1/apm/*` | same as `service_checks` |

PII scrubbing runs **in the collector between receive and export** (attribute drop/hash, allow-list + value masking, span/log drop filters, OTTL-style regex partial-masking; config in `apm_scrubbing_rules`) so sensitive data never reaches ClickHouse. Default rules ship on (scrub `Authorization`/`Cookie`/`password`/`token`, obfuscate `db_statement` bind-params at source, regex emails/cards; RUM masks form input). See `08-SECURITY` content in `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md`.

---

## 7. Migration file plan

Both Postgres and ClickHouse sequences start at **039** (next free after `migrate-038-report-schedules.sql`) but advance in their own monotonic sequences.

### 7.1 PostgreSQL

| File | Contents |
|---|---|
| **`migrate-039-apm.sql`** | All §4 config tables (`apm_environments`, `apm_services`, `apm_ingest_keys`, `apm_enrollment_tokens`, `apm_slos`, `apm_synthetic_monitors`, `apm_sampling_rules`, `apm_scrubbing_rules`, `apm_deployments`, `apm_dashboards`, `apm_error_issues`) + the `apm_*` metric-key additions to the `alert_rules.metric` CHECK constraint (re-declared **in full** per the migrate-035/037 lockstep convention — keep all existing `ping_status`…`host_process_down` keys and append `apm_latency_p50/p95/p99`, `apm_error_rate`, `apm_throughput`, `apm_apdex`, `apm_slo_burn`, `apm_synthetic_down`, `apm_anomaly`). |

Workflow after adding: `python3 scripts/build-release.py lint-migrations --update-lock`, then commit `migrate-039-apm.sql` **and** the updated `scripts/migrations.lock` together. Immutable once released; any later schema change is a new forward-only `migrate-0NN`.

### 7.2 ClickHouse

Filenames contain the literal string `clickhouse` → excluded from the Postgres runner; auto-applied by `clickhouse_sync.py` on every OTA update. **Every statement idempotent (`CREATE ... IF NOT EXISTS`); never added to `_LEGACY_BASELINE`.** Split by phase so each ships with its epic:

| File | Phase / epic | Contents |
|---|---|---|
| **`migrate-039-apm-clickhouse.sql`** | P1 (AM-E1/E3/E4) | `apm_spans`, `apm_traces_resource`, `apm_span_metrics_5m`+`_1h` (+MVs), `apm_service_graph` (+MV), `apm_exceptions` |
| **`migrate-040-apm-logs-clickhouse.sql`** | P2 (AM-E5) | `apm_logs` |
| **`migrate-041-apm-synthetics-clickhouse.sql`** | P3 (AM-E9) | `apm_synthetic_results`, `apm_synthetic_results_5m` (+MV) |
| **`migrate-042-apm-rum-clickhouse.sql`** | P3 (AM-E10) | `apm_rum_events`, `apm_rum_vitals_5m` (+MV) |
| **`migrate-043-apm-profiles-clickhouse.sql`** | P3 (AM-E11) | `apm_profiles` |

> **Why `apm_exceptions` ships in `039` (not with logs):** exceptions are span events emitted on the same OTLP trace path as spans, so error tracking (AM-E4) lands them via the same ingest writer the moment tracing exists — they do not wait for the logs pipeline (AM-E5). This allocation is the canonical one; `08-TASK-LIST-AND-TEST-PLAN.md` §1.1 and `00-INDEX.md` carry the identical table.

> **Idempotency caveat.** `clickhouse_sync.py` is best-effort and never raises; a failed CH migration silently retries on the next update. Ingest code must therefore tolerate a brief missing-table window (the original host-metrics "Table does not exist" failure mode this auto-apply pipeline was built to fix) — the FastAPI fallback and Go exporter both treat a missing target table as a soft drop-and-log, not a crash.

### 7.3 Escaping note

Any literal `%` inside a ClickHouse expression or LIKE pattern that passes through the updater's Python-style formatting must be escaped as `%%` (a single `%` is read as a format placeholder). Relevant for any `LIKE`/`formatDateTime`-style literals embedded via the migration path.

---

## 8. Open risks & decisions captured

| # | Risk / decision | Resolution in this design |
|---|---|---|
| R1 | pprof blobs inline in CH may bloat `apm_profiles` | Ship inline (`ZSTD(3)`) per platform convention; revisit object-storage pointer only if blob size proves problematic. |
| R2 | `trace_id` is a poor leading sort key | `ORDER BY (service_name, name, ts_bucket, trace_id)` + `bloom_filter(0.001)` `idx_trace_id` serves both scan classes. |
| R3 | Tail sampling needs single-instance trace assembly | Stateful Go collector with trace-ID-aware buffering; FastAPI fallback is HTTP-only/low-volume and does not tail-sample. |
| R4 | RED accuracy under heavy sampling | RED/graph computed from 100% of spans at insert via MVs, before sampling — accurate regardless of raw retention. |
| R5 | New codec convention introduced silently | Documented deliberately here (§3.1) and in `05`; `Delta+ZSTD` on ns timestamps, `ZSTD` on ids/blobs/strings. |
| R6 | CH migration auto-apply is best-effort | Ingest tolerates a missing-table window (soft drop-and-log); migrations are idempotent and retry next update. |
| R7 | Dedup / at-least-once SDK retries | Trace explorer tolerates duplicate spans (waterfall dedupes on `span_id`); rollups are additive over `SummingMergeTree` so exact-once is not required for RED. |

---

*End of Architecture & Data Model. Cross-reference rule: cite pinned route/table/column names from this document verbatim; link sibling docs by filename (e.g. "see `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` §collector").*
