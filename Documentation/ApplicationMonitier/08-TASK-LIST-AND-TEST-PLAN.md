# Application Monitoring — Task List & Test Plan

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the **sprint-ready execution contract** for ZenPlus Application Monitoring (APM). It decomposes the blueprint's twelve epics (**AM-E1 … AM-E12**) into granular, buildable tasks grouped by layer — **Collector (Go)**, **FastAPI API**, **ClickHouse/PostgreSQL migrations**, **React UI**, **Alert-engine integration**, and **Docs** — each with a one-line acceptance criterion, followed by a per-epic **test plan** (unit / integration / load / e2e) and a **definition of done**. It pins the exact `migrate-0NN` numbers (PG and CH each start at **039** and advance monotonically in their own sequence), the real route paths, table names, metric keys, and file paths from the authoritative blueprint, and the concrete reuse seams in the existing codebase. The test plan is deliberately exhaustive on the load-bearing correctness concerns that distinguish a credible APM from a toy: **trace-ingestion load**, **sampling correctness**, **span→metrics (RED) accuracy under sampling**, **service-map edge correctness**, **SLO burn-rate alert firing math**, **alert dedupe across both uvicorn workers**, and **RUM/synthetic happy-paths**. Style mirrors `Documentation/features/server-monitoring-03-task-list-and-test-plan.md`; every name is used verbatim from the blueprint and nothing re-derives architecture.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — reuse inventory and gap analysis against the existing ZenPlus appliance
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — collector-vs-FastAPI decision, end-to-end pipeline, full ClickHouse + PostgreSQL DDL, partitions/TTL/codecs
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23), MoSCoW priority, acceptance criteria, API contracts
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP paths, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing pipeline
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, components, react-query keys
- `07-ROADMAP-AND-EPICS.md` — phase/epic expansion with goal/scope/effort/impact/dependencies/exit-criteria
- `08-TASK-LIST-AND-TEST-PLAN.md` — **this document**

---

## 1. How to read this document

- **Layer tags.** Each epic's task list is grouped by build layer so a squad can parallelize: `[Collector]` (Go binary `poller/cmd/otelcollector`), `[API]` (FastAPI routers/services), `[Migration]` (PG `migrate-039-apm.sql…` and CH `migrate-039-apm-clickhouse.sql…`), `[UI]` (`dashboard/src/pages/apm/*`), `[Alerting]` (reuse of `alert_rules`/`alerts` + new evaluator loops), `[Docs]`.
- **Acceptance criterion.** Every checkbox carries an italic *AC:* — the binary, testable statement that closes the task.
- **Migration numbering.** Postgres and ClickHouse migrations are **separate monotonic sequences both starting at 039**. PG: `migrate-039-apm.sql`, `migrate-040-apm-…sql`, … CH: `migrate-039-apm-clickhouse.sql`, `migrate-040-apm-logs-clickhouse.sql`, … (full allocation in §1.1). Any file whose name contains `clickhouse` is excluded from the PG runner and auto-applies via `updater/clickhouse_sync.py`; **every CH statement is `CREATE … IF NOT EXISTS` / `ALTER … IF NOT EXISTS`** and **never** added to `_LEGACY_BASELINE`. After each PG migration: `python3 scripts/build-release.py lint-migrations --update-lock` and commit the migration + `scripts/migrations.lock` together.
- **Dedupe discipline.** Background loops run in **both uvicorn workers**; every `[Alerting]` raise task carries a dedupe-guard AC. This is a recurring, non-negotiable correctness requirement.
- **Test taxonomy.** `unit` (pure function, no I/O), `integration` (FastAPI + PG + CH + collector wired, real inserts/queries), `load` (throughput/latency/backpressure under synthetic OTLP traffic), `e2e` (browser-driven through the React UI via Playwright). Each epic lists all four where applicable.

### 1.1 Migration-number allocation (pinned)

| Migration file | Epic | Contents |
|---|---|---|
| `migrate-039-apm.sql` (PG) | AM-E1, E3, E6 baseline | `apm_environments`, `apm_services`, `apm_ingest_keys`, `apm_enrollment_tokens`, `apm_slos`, `apm_sampling_rules`, `apm_scrubbing_rules`, `apm_deployments`, `apm_dashboards`, `apm_error_issues`, `apm_synthetic_monitors`; **full re-declared** `alert_rules_metric_check` CHECK with `apm_*` keys appended |
| `migrate-039-apm-clickhouse.sql` (CH) | AM-E1, E3, E4 | `apm_spans`, `apm_traces_resource`, `apm_span_metrics_5m`(+`_1h`)+MVs, `apm_service_graph`(+MV), `apm_exceptions` |
| `migrate-040-apm-logs-clickhouse.sql` (CH) | AM-E5 | `apm_logs` |
| `migrate-041-apm-synthetics-clickhouse.sql` (CH) | AM-E9 | `apm_synthetic_results`, `apm_synthetic_results_5m`+MV |
| `migrate-042-apm-rum-clickhouse.sql` (CH) | AM-E10 | `apm_rum_events`, `apm_rum_vitals_5m`+MV |
| `migrate-043-apm-profiles-clickhouse.sql` (CH) | AM-E11 | `apm_profiles` |

> PG only needs **one** migration (`039`) for v1 — all config tables are idempotent `CREATE TABLE IF NOT EXISTS` and can ship together; later PG schema changes (e.g. E12 generalized silences) take the next free PG number forward-only.

---

## AM-E1 — OTLP ingestion + storage (Phase 1, XL)

**Goal:** stand up the OTLP front door (Go collector on 4317/4318 + FastAPI OTLP/HTTP fallback), the `apm_spans`/`apm_traces_resource` ClickHouse tables, the buffered batch writer, and `zpi_` ingest-key auth — so traces land in ClickHouse via OTLP.

> **Implementation status — 2026-06-21 (FastAPI fallback path SHIPPED & verified end-to-end).**
> **Done:** all ClickHouse tables + RED rollups (`migrate-039-apm-clickhouse.sql`, applied + idempotency-verified; rollups use `AggregatingMergeTree`/`SimpleAggregateFunction`+tdigest — a deliberate fix over the doc's `SummingMergeTree` sketch, which would have summed `duration_min/max`); all PG config tables (`migrate-039-apm.sql`) + the `apm_*` alert-metric CHECK; `migrations.lock` updated + both ledgers recorded; FastAPI OTLP/HTTP **JSON** receiver at root `/v1/traces` (decode → buffered async batch writer → ClickHouse) with `zpi_` bearer auth (30 s read-through cache), partial-success + `Retry-After` backpressure; ingest-key + enrollment-token control plane (`/api/v1/apm/*`, create-once/list-masked/revoke); `ApmSettingsPage` + `/apm/settings` route + sidebar "APM" section + "APM › Settings" breadcrumb; nginx `/v1/` proxy so SDKs reach the appliance root; quickstart doc ([quickstart-otlp-ingest.md](quickstart-otlp-ingest.md)). Verified via curl (incl. promoted columns, MV rollup p95, partial-success, 401, 415, revoke) and Playwright (create → copy-once → masked list).
> **Deferred to the Go-collector sub-task:** the high-throughput `poller/cmd/otelcollector` binary (OTLP/gRPC `:4317`, OTLP/protobuf `:4318`, tail-sampling, spanmetrics/servicegraph connectors). The FastAPI fallback returns `415` for protobuf with a pointer to it. Until the collector ships, ingest is OTLP/HTTP+JSON.

### Tasks — Collector (Go)
- [ ] Scaffold `poller/cmd/otelcollector` sibling binary reusing the poller's build/release/OTA packaging. *AC: `make build` produces `zenplus-otelcollector` and it registers in the release manifest alongside the poller.*
- [ ] Configure `otlp` receiver (gRPC `:4317`, HTTP `:4318`) accepting protobuf + JSON. *AC: a `curl` OTLP/HTTP `POST /v1/traces` and a gRPC `Export` both return 200/OK partial-success-unset for a valid payload.*
- [ ] Processor chain ordered `memory_limiter → resource/attributes → (scrubbing stub) → batch`. *AC: `memory_limiter` is first; config dump shows the exact order.*
- [ ] ClickHouse exporter (native protocol) writing the `apm_spans` row shape (typed maps, promoted columns, `ts_bucket`, `resource_fingerprint`). *AC: exported rows match the §4.1 column list 1:1, verified by `DESCRIBE apm_spans` vs insert column set.*
- [ ] `zpi_` bearer auth extension: validate `Authorization: Bearer zpi_…` against `apm_ingest_keys.key_hash` with a read-through cache + periodic refresh. *AC: a revoked key is rejected within the cache TTL (≤30s); a valid key is accepted.*
- [ ] Compute `resource_fingerprint` (stable hash of resource attrs) and emit companion `apm_traces_resource` rows. *AC: identical resource attr sets produce identical fingerprints across restarts.*
- [ ] Promote semantic-convention hot columns (`http_method/route/status_code`, `db_system/operation/statement-digest`, `rpc_method`, `service_version`, `env`) from OTel attributes. *AC: a span with `http.route=/checkout` lands with `http_route='/checkout'` in the column, not only the map.*
- [ ] Backpressure: under `memory_limiter` pressure return OTLP retryable (gRPC `Unavailable` / HTTP 503 + `Retry-After`). *AC: a forced-pressure run returns 503 with `Retry-After` and never OOMs.*

### Tasks — FastAPI API (fallback receiver)
- [ ] Create `server/app/api/v1/apm_ingest.py` mounted at **root** prefix `""` (not `/api/v1`). *AC: `POST /v1/traces` resolves to this router; `POST /api/v1/v1/traces` 404s.*
- [ ] OTLP/HTTP protobuf + JSON decode → row dicts → buffered async batch writer (queue + flush by size/interval) over the `get_clickhouse_client()` singleton. *AC: 1k spans posted in one request are inserted in ≤2 flushes, not 1k per-request inserts.*
- [ ] Fork `agents.py` auth helpers `_sha256`/`_new_api_key`(prefix `zpi_`)/`_strip_bearer`/`_authenticate` (constant-time `hmac.compare_digest`). *AC: a tampered bearer fails with 401 in constant time (no early-return on length mismatch).*
- [ ] Partial-success + `Retry-After` semantics matching OTLP spec. *AC: a batch with N rejected spans returns 200 with `partial_success{rejected_spans=N, error_message}` and the client does not retry.*
- [ ] Wire the previously-unused `backpressure` response field to a bounded queue high-water mark. *AC: queue at high-water returns 503 + `Retry-After`; queue drains and 200s resume.*

### Tasks — Migrations
- [ ] `migrate-039-apm-clickhouse.sql`: `apm_spans` (MergeTree, `PARTITION BY toYYYYMMDD(timestamp)`, `ORDER BY (service_name, name, ts_bucket, trace_id)`, TTL 7d, `ttl_only_drop_parts=1`, `CODEC(Delta(8),ZSTD(1))` ts + `CODEC(ZSTD(1))` ids/blobs, bloom indexes on `trace_id`/attr keys+values/`http_route`, minmax on `duration_nano`). *AC: table created idempotently; re-running the migration is a no-op.*
- [ ] `apm_traces_resource` (ReplacingMergeTree(seen_at), `ORDER BY (fingerprint)`, 7d TTL). *AC: created; replays clean.*
- [ ] `migrate-039-apm.sql`: `apm_environments`, `apm_services`, `apm_ingest_keys`, `apm_enrollment_tokens` (house style: UUID PK, TIMESTAMPTZ, JSONB, CHECK enums, `idx_*`/GIN). *AC: `run-migrations.py` applies it once; `schema_migrations` records the checksum.*
- [ ] Run `lint-migrations --update-lock`; commit migration + lockfile together. *AC: CI migration-lint is green.*
- [ ] Confirm CH file is **not** in `_LEGACY_BASELINE` and is picked up by `clickhouse_sync.py` glob. *AC: a fresh appliance update applies the CH file exactly once.*

### Tasks — FastAPI API (control plane, ingest keys)
- [ ] `server/app/api/v1/apm.py` router `prefix="/apm"` mounted under `/api/v1` in `main.py`. *AC: `GET /api/v1/apm/ingest-keys` resolves with `get_current_user`.*
- [ ] `GET/POST/DELETE /api/v1/apm/ingest-keys` (returns plaintext key once; stores `key_hash`+`key_prefix`+`kind`). *AC: the plaintext `zpi_…` is returned only on POST and never retrievable again.*
- [ ] Enrollment-token endpoints mirroring `agent_enrollment_tokens` (`max_uses`, `expires_at`, `revoked_at`, `consumed_ip`). *AC: a one-time token is consumed exactly once; a second use 409s.*

### Tasks — UI
- [ ] `dashboard/src/pages/apm/ApmSettingsPage.tsx` Ingest-Keys panel (create/list/revoke, copy-once modal). *AC: creating a key shows a one-time copy dialog; the key is masked thereafter.*
- [ ] Register `/apm/settings` route + sidebar `Settings` (`SlidersHorizontal`) + `routeLabels`/`routeSections`. *AC: breadcrumb renders "APM / Settings".*

### Tasks — Docs
- [ ] Quickstart: point an OTel SDK/Collector at `:4318`/`:4317` with a `zpi_` key. *AC: a copy-paste env-var snippet ingests a trace end-to-end on a clean appliance.*

### Test plan — AM-E1
| Type | Test | Pass criterion |
|---|---|---|
| unit | OTLP protobuf + JSON decode → row dict (Go + Python) | golden OTLP fixtures decode to byte-identical row shapes on both paths |
| unit | `resource_fingerprint` stability | same attrs → same hash; reordered attrs → same hash; one differing attr → different hash |
| unit | `zpi_` auth constant-time compare | mutation tests confirm no early-return; revoked/expired/disabled keys rejected |
| unit | promoted-column extraction | each semantic-convention key maps to its column; missing attr → empty/default, not error |
| integration | OTLP/HTTP + gRPC ingest → `apm_spans` | 200/OK; rows queryable by `trace_id` via bloom index; `apm_traces_resource` populated |
| integration | partial-success | malformed span in a batch → `rejected_spans=1`, good spans persisted |
| integration | Go-collector vs FastAPI fallback row-shape parity | identical trace ingested via both paths yields byte-identical `apm_spans` rows (UI-agnostic) |
| **load** | **trace-ingestion load — sustained** | drive synthetic OTLP at **20k spans/s for 30 min** on a single-node appliance: p99 ingest ack < 250ms, **zero dropped accepted spans**, CH insert lag < 5s, memory steady under `memory_limiter` |
| **load** | **trace-ingestion load — spike + backpressure** | 5× spike for 60s → collector/fallback returns 503+`Retry-After`, no OOM, recovers to 200 within 30s of spike end; accepted-span count never decreases |
| load | batch-writer efficiency (fallback) | ≥500 spans/insert average batch size at 5k spans/s; insert QPS to CH stays bounded |
| e2e | create ingest key → SDK emits trace → trace visible | a Playwright-driven key creation + scripted OTLP emit results in a queryable trace within 10s |

**Definition of done — AM-E1:** OTLP/gRPC `:4317` and OTLP/HTTP `:4318` accept traces with `zpi_` auth on both the Go collector and the FastAPI fallback writing **byte-identical** `apm_spans`/`apm_traces_resource` rows; the load suite passes (20k spans/s sustained, spike backpressure, zero accepted-span loss); migrations apply idempotently and lockfile is committed; ingest-key lifecycle (create-once/list/revoke/enroll) works from the UI.

---

## AM-E2 — Trace explorer + waterfall (Phase 1, L)

**Goal:** search traces (live + indexed modes) and view any trace as a waterfall/flame.

> **Implementation status — 2026-06-21 (SHIPPED & verified).**
> **Done:** `apm_traces.py` — `GET /api/v1/apm/traces` (two-stage: match trace_ids by filters → summarise the *whole* trace; filters service/operation/errors-only/min-duration/http-route/env; `live` = last 15m, `indexed` = range window) and `GET /api/v1/apm/traces/{trace_id}` (bloom-`idx_trace_id` lookup → server-assembled parent/child tree with depth, start-offset, events). `TraceExplorerPage` (`/apm/traces`, URL-driven filters + live auto-refresh + result table) and `TraceWaterfallPage` (`/apm/traces/:traceId`, custom horizontal-bar timeline with depth indent, error/DB coloring, span-detail panel); routes + APM "Traces" nav + breadcrumbs + react-query keys `['apm','traces',filters]`/`['apm','trace',id]`. Verified via curl (nested 4-span/2-service trace: correct tree, durations, error/DB flags, whole-trace summary under any service filter) and Playwright (explorer → row → waterfall → error-span detail).
> **Note:** sub-second indexed-search and 50M-span load targets in the test plan below are unmeasured (no large dataset on the dev box); the query is partition-pruned by window + `ORDER BY (service_name,name,ts_bucket,…)` and trace lookup uses the bloom index.

### Tasks — FastAPI API
- [ ] `GET /api/v1/apm/traces` with live mode (recent rolling window, attribute filters) vs indexed mode (retained, `ts_bucket`-pruned + `resource_fingerprint` CTE + `GLOBAL IN`). *AC: a service+time+`http.route` filter returns sub-second on a 7-day table without full scan (verified via `EXPLAIN`/`system.query_log`).*
- [ ] `GET /api/v1/apm/traces/{trace_id}` returning the full ordered span tree (parent/child, events, links). *AC: a trace with 200 spans returns a correctly-nested tree with root first.*
- [ ] Trace-id lookup hits `idx_trace_id` bloom, not a scan. *AC: query reads ≤2 granules per matching part.*

### Tasks — UI
- [ ] `TraceExplorerPage.tsx` (`/apm/traces`): URL-driven filters (service/operation/status/duration/attr), live/indexed toggle, result table, `TimeRangePicker`. *AC: all filters round-trip through `useSearchParams`; bookmark restores state.*
- [ ] `TraceWaterfallPage.tsx` (`/apm/traces/:traceId`): new custom horizontal-bar timeline component under `components/apm/` (span bars by start/duration, depth indent, error coloring, span detail drawer, events markers). *AC: a 200-span trace renders a correct Gantt with collapsible subtrees and an error span flagged red.*
- [ ] Register both routes + `Traces` sidebar item (`GitBranch`) + breadcrumb maps. *AC: breadcrumbs render "APM / Traces" and "APM / Traces / {id}".*
- [ ] react-query keys `['apm','traces',filters]` / `['apm','trace',traceId]`. *AC: `qc.invalidateQueries({queryKey:['apm']})` refreshes both.*

### Tasks — Docs
- [ ] Trace explorer guide (live vs indexed, attribute filter syntax). *AC: documents the `resource_fingerprint` CTE behavior for resource filters.*

### Test plan — AM-E2
| Type | Test | Pass criterion |
|---|---|---|
| unit | span-tree assembly | spans in arbitrary insert order assemble to the correct parent/child tree; orphan spans attach to a synthetic root |
| unit | live-vs-indexed query builder | live builds an unpruned recent-window query; indexed builds a `ts_bucket` + fingerprint-CTE query |
| integration | trace-id lookup | `GET /traces/{id}` returns all spans of a known trace; bloom index used (granule count asserted) |
| integration | attribute filter correctness | `http.route` + `status_code=ERROR` filter returns exactly the seeded matching traces |
| load | explorer query latency | indexed search over 50M-span / 7-day table p95 < 1.5s with service+time+attr filter |
| e2e | search → open waterfall | Playwright: filter to an error trace, open it, confirm the error span is highlighted and DB child span timing is shown |

**Definition of done — AM-E2:** trace search works in both live and indexed modes with sub-second indexed queries; the waterfall renders correctly-nested span trees with events/links/errors; routes, breadcrumbs, and react-query keys are wired.

---

## AM-E3 — Service registry + RED + service map (Phase 1, L)

**Goal:** `apm_services` registry, `apm_span_metrics_5m/_1h` RED rollups computed from **100% of spans pre-sampling**, the `apm_service_graph` dependency graph, and the Services list / detail (overview+performance) / Service Map UI; apdex.

> **Implementation status — 2026-06-21 (SHIPPED & verified).**
> **Done:** rollups gained **apdex buckets** (`satisfied_count`/`tolerating_count` at the default T=500ms, computed in the MV from 100% of spans — folded into `migrate-039` since it is pre-release; lock + ledger re-synced). `apm_services.py`: `GET /apm/services` (RED on entry spans SERVER/CONSUMER: rps/error-rate/p50/p95/p99/apdex/health + env & health facets), `/services/{name}`, `/services/{name}/red` (timeseries), `/services/{name}/operations`, `/service-map` (nodes + edges derived from `apm_spans` parent/child where service differs). Background `apm_service_registry_loop` upserts `apm_services` + denormalised health (verified auto-registering). UI: `ApmOverviewPage` (fleet KPIs + worst-services), `ServicesPage` (KPI strip + RED table + filters), `ServiceDetailPage` (KPI strip + RED area-charts + top operations, overview/performance tabs), `ServiceMapPage` (echarts `graph`, health-colored nodes, error-colored edges, click→detail). Nav: APM section now Overview/Services/Service Map/Traces/Settings + breadcrumbs.
> **Fixed a real platform bug:** the shared `clickhouse_connect` client raised *"concurrent queries within the same session"* under the dashboard's parallel CH reads. Added a **thread-local CH client** (`get_ch_client`) used by all APM modules (reads + ingest insert resolved inside the worker thread); verified 0 failures across 36 concurrent calls (was ~50% 500s).
> **Deferred:** service-graph edges are query-time-derived (self-join on `apm_spans`) as the fallback; the Go collector's `servicegraph`/`spanmetrics` connectors remain the scale path. Per-service apdex T uses the global default (per-service `apdex_threshold_ms` column exists for later).

### Tasks — Migrations
- [ ] `apm_span_metrics_5m` + `_5m_mv` (SummingMergeTree; `quantilesTDigestState` for p50/75/90/95/99; `request_count`/`error_count`/`duration_sum/min/max`; TTL 90d) and `apm_span_metrics_1h` + `_1h_mv` (TTL 395d), both in `migrate-039-apm-clickhouse.sql`. *AC: inserting spans populates the 5m/1h rollups via MV with no query-time aggregation of raw spans; `quantilesTDigestMerge(0.95)` returns p95.*
- [ ] `apm_service_graph` + `apm_service_graph_mv` (SummingMergeTree, `(client_service, server_service, env, timestamp)`, TTL 90d). *AC: paired client/server spans produce edge rows with summed request/error/duration.*

### Tasks — Collector (Go)
- [ ] Wire `spanmetrics` connector → metrics pipeline producing the same RED dimensions (`service_name, operation, span_kind, status_code, env`). *AC: collector-produced RED matches the CH-MV RED for the same input within rounding.*
- [ ] Wire `servicegraph` connector emitting edges. *AC: a 3-hop trace yields 2 edges with correct client/server orientation.*
- [ ] **RED + graph are computed before tail sampling drops raw spans.** *AC: with raw-span retention at 1%, `request_count` still reflects 100% of spans (see E8 cross-test).*

### Tasks — FastAPI API
- [ ] `GET /api/v1/apm/services` (list + RED summary + facets, mirroring `/servers/facets`). *AC: returns health/rps/error-rate/p95/apdex per service + facet counts for env/language/team.*
- [ ] `GET /api/v1/apm/services/{id}`, `/red` (time-series), `/operations` (top by RED), `/apdex` (score + buckets, per-service threshold T). *AC: apdex = (satisfied + tolerating/2)/total with satisfied ≤ T, tolerating ≤ 4T from `countIf` buckets.*
- [ ] `GET /api/v1/apm/service-map` (nodes from `apm_services`, edges + RED from `apm_service_graph`). *AC: returns a node/edge graph with per-edge rate/errors/latency.*
- [ ] Service-registry upsert: services auto-register on first span (denormalized last-seen RED/health into `apm_services`). *AC: a never-seen `service.name` appears in `apm_services` within one rollup cycle with `health` set.*

### Tasks — UI
- [ ] `ServicesPage.tsx` cloned from `ServersPage.tsx`: health chips (healthy/degraded/critical/no_data), filters (env/language/team/tag), sortable columns (Service, p95, error rate, throughput, apdex, last deploy), row→`/apm/services/:id`. *AC: filters/sort/pagination URL-driven; chip counts match facets.*
- [ ] `ServiceDetailPage.tsx` cloned from `ServerDetailPage.tsx`, hand-rolled `?tab=` bar; ship `overview` + `performance` tabs (RED AreaCharts via recharts, KPI strip: RPS/p50/p95/p99/error%/apdex). *AC: `?tab=performance` deep-links; charts use `rgb(var(--token))` gradients.*
- [ ] `ServiceMapPage.tsx`: new echarts `graph` (or SVG) under `components/apm/` with RED-on-edges, node health coloring, click→service detail. *AC: a 5-service topology renders with colored edges and drill-through.*
- [ ] `dashboard/src/components/apm/shared.tsx`: `ServiceHealthBadge`, `LanguageIcon`, `LatencySparkline`/`ErrorRateBar`, `SpanKindBadge`; reuse `KpiTile`/`TagList`. *AC: badges use Badge variants success/warning/danger/outline.*
- [ ] `ApmOverviewPage.tsx` (`/apm`, `Layers`, `end:true`) fleet APM dashboard. *AC: section landing shows fleet RED + worst services.*
- [ ] Register routes + sidebar (`Services`→`Boxes`, `Service Map`→`Network`) + `dashboard/src/types/apm.ts`. *AC: breadcrumbs render for all three paths.*

### Tasks — Docs
- [ ] RED/apdex semantics + "dashboards never scan raw spans" rationale. *AC: documents the three-stage metricize/ingest/retain decoupling.*

### Test plan — AM-E3
| Type | Test | Pass criterion |
|---|---|---|
| unit | apdex math | (satisfied+tolerating/2)/total computed correctly across boundary cases (exactly T, exactly 4T) |
| unit | error-rate / throughput derivation | error_rate = error_count/request_count; throughput = request_count/window_seconds |
| unit | p95 from tdigest merge | `quantilesTDigestMerge(0.95)` within ±1% of exact p95 on a known distribution |
| integration | **span→metrics accuracy** | insert N spans with known per-operation rate/errors/latency; 5m rollup `request_count`/`error_count` exact, p95 within ±2% |
| integration | **service-map correctness** | seed a known call graph (A→B→C, A→D); `/service-map` returns exactly edges A-B,B-C,A-D with correct direction and summed RED |
| integration | service auto-registration | first span for a new `service.name` registers it with health + last-seen RED within one cycle |
| load | RED query latency | `/services` fleet RED + `/services/{id}/red` over 90d of 5m rollups p95 < 800ms |
| e2e | services list → detail → map | Playwright: filter to degraded service, open performance tab, jump to service map, confirm its edges highlight |

**Definition of done — AM-E3:** services auto-register; RED + apdex + service-map are computed from 100% of spans via MV/connector and served sub-second from rollups (never raw spans); Services list, Service detail (overview+performance), and Service Map render with correct RED; `apm/shared.tsx` + `types/apm.ts` exist.

---

## AM-E4 — Error tracking / issues (Phase 2, M)

**Goal:** `apm_exceptions` (CH) + `apm_error_issues` (PG triage state); fingerprint grouping in the collector; Errors inbox + issue detail with triage + assignee.

### Tasks — Collector (Go)
- [ ] Exception extraction from span events + `group_id` fingerprint = hash(`exception_type` + normalized stack; strip UUIDs/hex/line-noise). *AC: two occurrences of the same error in different requests share `group_id`; an unrelated error differs.*
- [ ] Emit `apm_exceptions` rows (type/message/stack/escaped, `trace_id`/`span_id`, `http_route`, `resource_tags`). *AC: every error span yields one exception row linked to its trace.*

### Tasks — Migrations
- [ ] `apm_exceptions` in `migrate-039-apm-clickhouse.sql` (MergeTree, `PARTITION BY toYYYYMMDD`, `ORDER BY (service_name, group_id, ts_bucket, timestamp)`, TTL 30d, bloom on `group_id`/`trace_id`). *AC: created idempotently.*
- [ ] `apm_error_issues` in `migrate-039-apm.sql` (status `unresolved`/`resolved`/`resolved_in_version`/`ignored`, assignee, first/last-seen mirror, keyed by `group_id`). *AC: triage state persists in PG; counts/trends read from CH.*

### Tasks — FastAPI API
- [ ] `GET /api/v1/apm/errors` (grouped issues: count, first/last seen, affected versions, user impact) joining CH occurrence data with PG triage. *AC: list reflects CH counts and PG status together.*
- [ ] `GET /api/v1/apm/errors/{group_id}` (occurrences, stack, trend, linked traces/logs). *AC: occurrence detail links to a representative `trace_id`.*
- [ ] `PATCH /api/v1/apm/errors/{group_id}` (status + assignee; assignee may be a user or email). *AC: status transitions persist and surface in the list.*

### Tasks — UI
- [ ] `ErrorsInboxPage.tsx` (filter+table skeleton from `ServicesPage`): chips by status, filters by service/env/version. *AC: triage status filter works URL-driven.*
- [ ] `ErrorIssueDetailPage.tsx` (occurrences, stack, trend chart, trace links, status/assignee controls). *AC: changing status/assignee mutates and invalidates `['apm','errors']`.*
- [ ] Add `errors` tab to `ServiceDetailPage`. *AC: per-service error issues render in-tab.*
- [ ] Register routes + `Errors` sidebar (`Bug`) + breadcrumbs. *AC: breadcrumbs render.*

### Test plan — AM-E4
| Type | Test | Pass criterion |
|---|---|---|
| unit | **fingerprint grouping** | same type+normalized-stack → same `group_id`; UUID/hex/line-number variance collapses; different type → different group |
| unit | stack normalization | UUIDs, hex addresses, line numbers, and ephemeral ids stripped deterministically |
| integration | issue list join | CH occurrence counts + PG triage status merge into one issue row |
| integration | triage transitions | PATCH unresolved→resolved→ignored persists and reflects in list filters |
| integration | trace linkage | issue detail's representative `trace_id` resolves to a real trace in `apm_spans` |
| e2e | error inbox triage | Playwright: open a new issue, assign it, resolve-in-version, confirm it leaves the unresolved chip |

**Definition of done — AM-E4:** error spans are fingerprinted into stable issues, occurrences/trends come from CH, triage/assignee state lives in PG, and the Errors inbox + issue detail + per-service errors tab work with one-click trace linkage.

---

## AM-E5 — Logs + trace correlation (Phase 2, M)

**Goal:** `apm_logs`, OTLP logs ingest, trace↔log pivot, logs tab, metric→trace exemplars.

### Tasks — Collector (Go) / API
- [ ] OTLP logs receiver path → `apm_logs` (severity, body, `trace_id`/`span_id`, attrs, resource). *AC: `POST /v1/logs` (and gRPC) persists log records with trace context.*
- [ ] FastAPI fallback `POST /v1/logs` decode → buffered writer. *AC: parity with Go path row shape.*

### Tasks — Migrations
- [ ] `migrate-040-apm-logs-clickhouse.sql`: `apm_logs` (MergeTree, `PARTITION BY toYYYYMMDD`, `ORDER BY (service_name, ts_bucket, timestamp)`, TTL 14d, bloom on `trace_id`, `tokenbf_v1` on `body`). *AC: created idempotently; `trace_id` lookup uses bloom.*

### Tasks — FastAPI API
- [ ] `GET /api/v1/apm/...` logs query (by service/severity/time/full-text and **by `trace_id`**). *AC: `?trace_id=` returns exactly that trace's logs via `idx_log_trace`.*
- [ ] Exemplar wiring: metric data-point → representative `trace_id`. *AC: a latency-spike point exposes a clickable exemplar trace id.*

### Tasks — UI
- [ ] `logs` tab on `ServiceDetailPage` + one-click "view logs" from a span in the waterfall and "view trace" from a log line. *AC: span→logs and log→trace pivots both round-trip.*
- [ ] Metric-chart exemplar dots link to a trace. *AC: clicking an exemplar opens the waterfall.*

### Test plan — AM-E5
| Type | Test | Pass criterion |
|---|---|---|
| unit | OTLP logs decode | severity/body/trace context mapped to columns |
| integration | trace↔log pivot | logs for a `trace_id` returned via bloom; span→log and log→trace IDs match |
| integration | full-text body search | `tokenbf_v1` returns expected matches for a token query |
| load | logs ingest | 10k logs/s for 10 min: no drop, insert lag < 5s |
| e2e | span→logs→trace round-trip | Playwright: open trace, jump to a span's logs, jump back to the trace from a log line |

**Definition of done — AM-E5:** logs ingest via OTLP, store with trace context, and pivot one-click both ways; metric exemplars link to representative traces.

---

## AM-E6 — APM alerting + SLO/error-budget (Phase 2, L)

**Goal:** register `apm_*` metric keys into the unified alert engine; `apm_alert_service.py` + `apm_alert_evaluator_loop`; `apm_slos` (PG) + `apm_slo_burn_loop` (multi-window multi-burn); SLOs UI; enforce `cooldown`/`max_repeat`.

### Tasks — Migrations / registry
- [ ] In `migrate-039-apm.sql`, **re-declare the full** `alert_rules_metric_check` CHECK (all existing keys) **plus** `apm_latency_p50, apm_latency_p95, apm_latency_p99, apm_error_rate, apm_throughput, apm_apdex, apm_slo_burn, apm_synthetic_down, apm_anomaly`. *AC: inserting an `apm_latency_p95` rule passes the CHECK; an unknown `apm_foo` is rejected.*
- [ ] `apm_slos` PG table (sli_type, target, window_days∈{7,30,90}, latency_threshold_ms, scope service/operation, burn_alert_enabled, notify_channels). *AC: created idempotently.*

### Tasks — Alerting integration
- [ ] Add `apm_*` keys to the Pydantic `_CONDITION_METRICS` regex (new `apm_alert_rules.py` modeled on `host_alert_rules.py`, `metric LIKE 'apm\_%'`). *AC: API accepts/validates `apm_*` rules; rejects others in the APM router.*
- [ ] `apm_alert_service.py` cloned from `network_alert_service.py`: `APM_METRICS` set; every 60s `SELECT … WHERE enabled AND metric IN (apm_*)`; read RED from `apm_span_metrics_5m` (`p95` via `quantilesTDigestMerge`, `error_rate=error_count/request_count`, `throughput=request_count/window`), apdex from buckets; reuse `_cmp`/`_active_alert_id`/`_raise`/`_resolve`/`_notify`. *AC: a breaching p95 rule raises exactly one alert; clearing resolves it.*
- [ ] Register `apm_alert_evaluator_loop`, `apm_slo_burn_loop`, `apm_nodata_sweeper` in `main.py` `@app.on_event("startup")` with matching shutdown cancels. *AC: loops start/stop with the app; logs confirm one active instance per worker.*
- [ ] **Dedupe-guard every raise** (runs in both uvicorn workers) via `_active_alert_id`/dedupe key. *AC: with 2 workers, a single breach produces exactly one `alerts` row.*
- [ ] `synthetic_down` rides the push path: poller synthetic checker → `/api/v1/alert-engine/evaluate-service` (no new evaluator). *AC: a synthetic down transition raises via the existing service-status path.*
- [ ] `apm_slo_burn_loop`: per SLO + per window compute SLI (availability/error: `1 - error_count/request_count`; latency: `countIf(duration ≤ threshold)/request_count`), error-budget remaining `(SLI-target)/(1-target)`, burn rate; **multi-window multi-burn** — Page 14.4×/1h (short 5m), Page 6×/6h (short 30m), Ticket 1×/3d (short 6h), both windows must breach. *AC: synthetic budget-burn fixtures fire Page/Ticket at the exact canonical thresholds and not below.*
- [ ] Emit `apm_slo_burn` value (engine-thresholded) or raise directly via `dispatch_to_channels`; honor `notifications_allowed` (quiet hours). *AC: a burn alert suppressed during quiet hours records the alert row but sends no notification.*
- [ ] Enforce `cooldown`/`max_repeat` (re-notify/hysteresis) in the new evaluators. *AC: within cooldown, no re-notify; after `max_repeat`, no further notifies until resolve.*
- [ ] Extend PagerDuty `dedup_key` to carry APM service/SLO id. *AC: two SLO alerts on different SLOs do not collapse into one PD incident.*
- [ ] `ctx.details[]` rows for APM (e.g. `('Service','checkout')`,`('p95','820ms')`,`('SLO burn','14.4x')`) through `email_render.build_alert_email_html`. *AC: the HTML email shows the APM fact rows.*

### Tasks — FastAPI API
- [ ] `GET/POST/PUT/DELETE /api/v1/apm/slos`, `GET /api/v1/apm/slos/{id}/budget` (per-window burn for the detail chart). *AC: budget endpoint returns each window's burn rate + remaining budget.*

### Tasks — UI
- [ ] `SlosPage.tsx` (list + error-budget status) + `SloDetailPage.tsx` (burn chart, multi-window). *AC: budget bar + burn-rate windows render.*
- [ ] `slos` tab on `ServiceDetailPage`. *AC: per-service SLOs in-tab.*
- [ ] Add `apm_*` entries to the `NETWORK_METRICS`-style registry + an `apm`/`service` scope source in `AlertRuleFormDialog.tsx`. *AC: APM metrics selectable with labels/units in the rule editor.*
- [ ] Register `/apm/slos`(+`:id`) routes + `SLOs` sidebar (`Target`) + breadcrumbs. *AC: breadcrumbs render.*

### Test plan — AM-E6
| Type | Test | Pass criterion |
|---|---|---|
| unit | **SLI / error-budget / burn-rate math** | availability/latency SLI, budget remaining `(SLI-target)/(1-target)`, and burn rate computed exactly on fixtures |
| unit | **multi-window multi-burn thresholds** | 14.4×/1h+5m → Page; 6×/6h+30m → Page; 1×/3d+6h → Ticket; **both** windows required; just-below thresholds do **not** fire |
| unit | metric derivation in evaluator | p95/error_rate/throughput/apdex from `apm_span_metrics_5m` match E3 derivations |
| unit | cooldown/max_repeat | re-notify suppressed within cooldown; capped at max_repeat |
| integration | **alert dedupe across workers** | simulate 2 workers evaluating the same breach → exactly one `alerts` row, one notification |
| integration | **SLO burn-rate alert firing** | drive error_count to consume budget at 14.4× → Page fires within one cycle and routes to PD/SMS; recovery resolves and clears within ~5m via short window |
| integration | quiet-hours gating | breach during quiet window records alert, sends no notification; outside window notifies |
| integration | synthetic_down push path | poller down transition raises via `/evaluate-service` with no new evaluator |
| integration | PD dedup_key isolation | two SLO alerts → two PD incidents |
| e2e | create SLO → breach → page → budget chart | Playwright: define a 99.9% latency SLO, drive a breach, see the alert in the Alerts UI and the burn chart on SLO detail |

**Definition of done — AM-E6:** `apm_*` keys validate through CHECK + Pydantic + UI registry; the APM evaluator and SLO burn loop run dedupe-guarded in both workers; multi-window multi-burn fires Page/Ticket at canonical thresholds and routes via `dispatch_to_channels` honoring quiet hours, cooldown, and max_repeat; SLOs list/detail and the rule editor expose APM metrics.

---

## AM-E7 — Deployment / change tracking (Phase 2, S)

**Goal:** `apm_deployments`, deploy markers on charts, version-vs-version compare.

### Tasks — Migration / API
- [ ] `apm_deployments` PG table (service, version, sha, ts) in `migrate-039-apm.sql`. *AC: created idempotently.*
- [ ] `GET/POST /api/v1/apm/deployments`. *AC: posting a deploy marker returns it on subsequent GETs scoped to service/time.*
- [ ] Stamp `deployment_id` on spans where resolvable (collector tags by `service.version`+window). *AC: spans after a deploy carry its `deployment_id`.*

### Tasks — UI
- [ ] recharts `ReferenceLine` deploy markers on RED charts + a `deployments` tab with version-vs-version RED compare. *AC: markers render at deploy timestamps; compare shows old vs new error/latency/throughput.*

### Test plan — AM-E7
| Type | Test | Pass criterion |
|---|---|---|
| unit | version-compare aggregation | RED grouped by `service_version` over two windows computed correctly |
| integration | deploy marker round-trip | POST then GET returns the marker; spans post-deploy carry `deployment_id` |
| e2e | deploy → marker → compare | Playwright: record a deploy, see the marker, open compare and confirm a regressed version is flagged |

**Definition of done — AM-E7:** deploys are recorded, rendered as chart markers, and comparable version-vs-version on RED.

---

## AM-E8 — Sampling + PII scrubbing pipeline (Phase 2, M)

**Goal:** head+tail sampling with retain-reason tagging (`apm_sampling_rules`); PII scrubbing processors with shipped default rules (`apm_scrubbing_rules`); settings UI.

### Tasks — Collector (Go)
- [ ] Head probabilistic sampler targeting `apm_environments.sampling_target_tps`. *AC: configured TPS is held within ±10% under steady load.*
- [ ] `tailsamplingprocessor` (`decision_wait` ~5s) policies ordered: keep 100% errors, keep slow (latency outliers), keep ~1–5% baseline, string-attribute/tag policies; **prod default = tail**. *AC: all error + all slow traces retained; boring traffic dropped to baseline.*
- [ ] Tag every retained span `attributes_string['zp.retain_reason']` ∈ {auto,error,slow,rule,baseline}. *AC: every retained span carries exactly one retain reason.*
- [ ] **RED/graph computed before sampling** (assert ordering: spanmetrics/servicegraph connectors precede tail-sampling drop). *AC: rollup counts equal 100% of input regardless of retained-span count.*
- [ ] Scrubbing processors between receive and export: attribute drop/hash, allow-list + value masking, span/log drop filters, OTTL-style regex partial-masking; **ship default rules on** (scrub `Authorization`/`Cookie`/`password`/`token`, `db_statement` bind params → digest, email/card regex; RUM masks form input). *AC: a span carrying `Authorization` is exported with that attribute removed/hashed; PII never reaches CH.*

### Tasks — Migration / API / UI
- [ ] `apm_sampling_rules` + `apm_scrubbing_rules` PG tables in `migrate-039-apm.sql`. *AC: created idempotently with default scrubbing rows seeded.*
- [ ] `GET/PUT /api/v1/apm/sampling-rules`, `GET/PUT /api/v1/apm/scrubbing-rules`. *AC: edits propagate to the collector (read-through/refresh) within one refresh cycle.*
- [ ] Settings UI panels for sampling + scrubbing on `ApmSettingsPage`. *AC: rule edits persist and reflect in the collector.*

### Test plan — AM-E8
| Type | Test | Pass criterion |
|---|---|---|
| unit | head-sampler rate | hash-based keep ratio matches configured probability within ±2% over 1M trace ids |
| unit | tail policy ordering | error/slow/baseline policies evaluated in order; a trace matching multiple keeps once with highest-priority reason |
| unit | OTTL partial-mask regex | email/card patterns masked partially (not all-or-nothing); allow-list passes intended attrs |
| integration | **sampling correctness** | seed 100k traces (5% error, 1% slow): **100% errors + 100% slow retained**, remainder ≈ configured baseline ±2%; every retained span has a valid `zp.retain_reason` |
| integration | **span→metrics accuracy under sampling** | with raw retention at 1%, `apm_span_metrics_5m.request_count`/`error_count` still equal 100% of input (pre-sampling metricization proven) |
| integration | scrubbing effectiveness | seed spans with `Authorization`/`password`/email/`db_statement` literals → CH rows contain none of them; only digests survive |
| load | tail-sampling memory | 20k spans/s with 5s decision_wait holds bounded buffer memory; no OOM; trace-complete assembly correct |
| e2e | edit sampling rule → effect | Playwright: lower baseline %, confirm retained-span volume drops while RED counts hold steady |

**Definition of done — AM-E8:** head+tail sampling drops boring traffic while retaining 100% of errors/slow with retain-reason tags; **RED accuracy is provably independent of sampling**; default PII scrubbing ships on and is verified to keep secrets/PII out of ClickHouse; rules are editable from settings.

---

## AM-E9 — Synthetic monitoring (Phase 3, L)

**Goal:** extend the Go poller (`checker.CheckOne` + `engine.go` cycle) with a synthetic/browser case; `apm_synthetic_monitors` (PG, reusing `service_checks` infra) + `apm_synthetic_results` (CH); synthetics UI; run-now via the poller (never Python); `synthetic_down` → evaluate-service push.

### Tasks — Collector/Poller (Go)
- [ ] Add `synthetic`/`browser` dispatch case to `poller/internal/checker/checker.go` `CheckOne`; multi-step + assertions read from JSONB `config`. *AC: a 3-step API monitor executes steps in order with per-step timings + assertions.*
- [ ] Reuse `engine.go` `runServiceCheckCycle` scheduling/retry/flap/maintenance unchanged for synthetics. *AC: synthetic monitors honor interval/retry/flap and maintenance muting like service checks.*
- [ ] Emit per-step rows to `apm_synthetic_results` (`step_index`/`step_name`, `assertion_failed`, `backend_trace_id`). *AC: each step writes a row; whole-monitor row at `step_index=0`.*
- [ ] `synthetic_down` status transition → `/api/v1/alert-engine/evaluate-service`. *AC: a failing monitor raises via the existing service-status path.*

### Tasks — Migration / API
- [ ] `apm_synthetic_monitors` PG (reuses level/config/tags/interval/retry/parent_check_id columns) in `migrate-039-apm.sql`. *AC: created idempotently.*
- [ ] `migrate-041-apm-synthetics-clickhouse.sql`: `apm_synthetic_results` (MergeTree, `ORDER BY (monitor_id, location, timestamp)`, TTL 90d) + `apm_synthetic_results_5m` + MV (uptime_pct/avg/p95, TTL 395d). *AC: created; rollup via MV.*
- [ ] `GET/POST/PUT/DELETE /api/v1/apm/synthetics`, `POST /api/v1/apm/synthetics/{id}/run` (routes to poller, **never re-implements probes**). *AC: run-now triggers a poller execution, not a Python re-probe.*

### Tasks — UI
- [ ] `SyntheticsPage.tsx` (list, filter+table skeleton) + `SyntheticDetailPage.tsx` (uptime calendar, per-step waterfall, location compare). *AC: per-step timings + uptime heatmap render; locations comparable.*
- [ ] Register routes + `Synthetics` sidebar (`Radar`) + breadcrumbs. *AC: breadcrumbs render.*

### Test plan — AM-E9
| Type | Test | Pass criterion |
|---|---|---|
| unit | multi-step executor + assertions | steps run in order; status/latency/JSON-path/header assertions evaluated; failure short-circuits/records correctly |
| unit | flap confirmation reuse | DownCount vs RetryCount transitions identical to service-checks behavior |
| integration | result write + rollup | per-step + whole-monitor rows persist; 5m rollup uptime_pct correct |
| integration | run-now via poller | `/run` produces a poller-sourced result (single implementation, no Python probe) |
| integration | synthetic_down alert | failing monitor raises via evaluate-service and routes to channels |
| e2e | create monitor → run → step waterfall | Playwright: create a 3-step API monitor, run-now, view per-step waterfall and assertion results |

**Definition of done — AM-E9:** synthetic/browser monitors run through the existing poller scheduler/retry/flap/maintenance with multi-step assertions and per-step ClickHouse results, run-now routes to the poller (single probe implementation), and `synthetic_down` alerts via the existing service-status path; synthetics UI with per-step waterfall + uptime works.

---

## AM-E10 — RUM + Core Web Vitals (Phase 3, L)

**Goal:** RUM SDK + public `zpr_` beacon (`/api/v1/apm/rum/ingest`, CORS); `apm_rum_events`/`_vitals_5m`; RUM page; RUM→backend trace stitch.

### Tasks — API
- [ ] `POST /api/v1/apm/rum/ingest` public, origin-scoped (`zpr_` key + `origin_allowlist` CORS + per-origin rate-limit). *AC: a disallowed origin is rejected; allowed origin accepted; no secret key in client JS.*
- [ ] `GET /api/v1/apm/rum/...` views/sessions/web-vitals. *AC: p75 LCP/INP/CLS per view returned.*

### Tasks — Migration
- [ ] `migrate-042-apm-rum-clickhouse.sql`: `apm_rum_events` (MergeTree, `PARTITION BY toYYYYMMDD`, `ORDER BY (application_id, view_name, ts_bucket, session_id)`, TTL 14d, bloom on `session_id`/`backend_trace_id`) + `apm_rum_vitals_5m` + MV (p75 CWV, TTL 90d). *AC: created; CWV rolled up at p75.*

### Tasks — UI / SDK
- [ ] Browser RUM SDK emitting view/action/error/resource/long_task events + CWV + optional backend traceparent injection. *AC: a page load emits an LCP/INP/CLS view event; XHR carries traceparent when enabled.*
- [ ] `RumPage.tsx` (CWV p75, sessions, JS errors) + RUM→trace pivot. *AC: a RUM session with `backend_trace_id` links to its backend trace.*
- [ ] Register `/apm/rum` route + `RUM` sidebar (`MonitorSmartphone`) + breadcrumbs. *AC: breadcrumbs render.*

### Test plan — AM-E10
| Type | Test | Pass criterion |
|---|---|---|
| unit | CWV p75 rollup | p75 LCP/INP/CLS computed correctly (CWV standard) from tdigest state |
| unit | origin/CORS gating | allowlisted origins pass; others 403; rate-limit trips per origin |
| integration | beacon → events → vitals | beacon POST persists events; vitals_5m populated; `backend_trace_id` present when injected |
| integration | RUM→backend trace stitch | a session's `backend_trace_id` resolves to a real `apm_spans` trace |
| load | RUM beacon | 5k beacons/s for 10 min: no drop, per-origin rate-limit enforced, insert lag < 5s |
| e2e | RUM happy-path | Playwright: load an instrumented page, confirm a CWV view event appears in `/apm/rum` and links to its backend trace |

**Definition of done — AM-E10:** the public origin-scoped `zpr_` beacon ingests RUM events without a secret in client JS, CWV roll up at p75, and RUM sessions stitch to backend traces; the RUM page renders.

---

## AM-E11 — DB/query monitoring + eBPF agent + profiling (Phase 3, XL)

**Goal:** DB agent (digests/plans, source-side obfuscation) into `db_*` columns; eBPF zero-code agent (OTLP); `apm_profiles` + OTLP profiles + span→flamegraph; profiling/database tabs.

### Tasks — Collector/Agents (Go)
- [ ] DB-monitoring agent: normalized query digests, sampled executions, **source-side bind-parameter obfuscation**, plan capture → `apm_spans.db_*` columns. *AC: literals never leave the host; `db_statement` is a digest only.*
- [ ] eBPF zero-code agent (Beyla/OBI-style) emitting RED + spans over OTLP into the same collector. *AC: an uninstrumented HTTP service produces RED + spans with no SDK.*
- [ ] OTLP profiles receiver (`/v1development/profiles`) → `apm_profiles` (pprof inline, `CODEC(ZSTD(3))`), `trace_id`/`span_id` link. *AC: a profile links to its span; span→flamegraph resolves.*

### Tasks — Migration / API / UI
- [ ] `migrate-043-apm-profiles-clickhouse.sql`: `apm_profiles` (MergeTree, `PARTITION BY toYYYYMMDD`, `ORDER BY (service_name, profile_type, ts_bucket, timestamp)`, TTL 14d, bloom on `trace_id`). *AC: created idempotently.*
- [ ] `POST /v1development/profiles` ingest + profiling/database query endpoints. *AC: profiles queryable by service/type/trace.*
- [ ] `database` tab (query digests, slow queries, plans) + `profiling` tab (flamegraph, span→profile link) on `ServiceDetailPage`. *AC: clicking a slow span opens its flamegraph.*

### Test plan — AM-E11
| Type | Test | Pass criterion |
|---|---|---|
| unit | query normalization/obfuscation | literals/binds replaced with `?`; identical statements share a digest; no PII in digest |
| unit | pprof decode + span link | profile payload decodes; `trace_id`/`span_id` link resolves |
| integration | DB metrics into spans | `db_system`/`db_operation`/digest promoted columns populated; rows-returned/locks surfaced |
| integration | eBPF coverage | uninstrumented service yields RED + spans via OTLP with correct service_name |
| integration | span→flamegraph | a slow span resolves to its profile flamegraph |
| load | profile ingest size | inline ZSTD(3) pprof blobs stay within budgeted size; insert lag bounded (risk-gated per 03) |
| e2e | slow span → flamegraph | Playwright: open a slow trace, jump to its profile, see the hot function |

**Definition of done — AM-E11:** DB query monitoring populates `db_*` with source-obfuscated digests + plans; the eBPF agent gives zero-code RED+span coverage over OTLP; continuous profiles store inline and link span→flamegraph; database + profiling tabs work.

---

## AM-E12 — AI anomaly + causal-lite RCA + full-stack correlation (Phase 4, XL)

**Goal:** `apm_anomaly_loop` (seasonality + std-dev baselines → `apm_anomaly` metric); graph-walk causal-lite RCA (most-upstream impacted service; collapse symptom alerts into one incident); full-stack correlation panels (span→`host_*`→`snmp_*`→`flow_records`); generalized silences.

### Tasks — Alerting / API
- [ ] `apm_anomaly_loop`: per-metric rolling baseline with hour-of-day/day-of-week seasonality + std-dev bands → derived `apm_anomaly` boolean/score the engine thresholds. *AC: a metric outside the learned band emits an `apm_anomaly` breach; in-band does not.*
- [ ] Causal-lite RCA: on correlated alerts, walk `apm_service_graph` to the most-upstream impacted service and collapse symptom alerts into one incident. *AC: a fan-out failure produces one incident rooted at the upstream cause, not N pages.*
- [ ] **Generalized silences** (E5/audit gap): polymorphic scope/dedupe-keyed silence so service/SLO-scoped APM alerts become silenceable. *AC: an SLO alert can be silenced by scope; the silence suppresses notifications but records alerts.*

### Tasks — UI
- [ ] Full-stack correlation panel on `infrastructure` tab: span → its server's `host_*_metrics` → its switch's `snmp_if_*` → `flow_records`. *AC: a slow span surfaces correlated host CPU, interface errors, and the netflow conversation in one panel.*

### Test plan — AM-E12
| Type | Test | Pass criterion |
|---|---|---|
| unit | seasonal baseline + std-dev band | baseline tracks hour/day seasonality; band width = configured σ; deviation ratio reported |
| unit | graph-walk root selection | given a seeded dependency graph + multi-alert set, the most-upstream impacted node is chosen |
| integration | incident collapse | N symptom alerts on a failing dependency collapse into one incident with impact path |
| integration | generalized silence | service/SLO-scoped silence suppresses notifications, still records the alert row |
| integration | full-stack correlation | span→host→snmp→flow join returns the correct correlated rows for a known topology |
| e2e | anomaly → incident → full-stack panel | Playwright: trigger an anomaly, see a single root-caused incident, open the full-stack panel and confirm correlated infra signals |

**Definition of done — AM-E12:** auto-baselining emits `apm_anomaly` breaches with seasonality; correlated alerts collapse into a single root-caused incident via graph-walk; APM alerts are silenceable via generalized scope; the full-stack correlation panel stitches span→host→snmp→flow.

---

## 2. Cross-cutting test suites (run on every epic)

### 2.1 Migration & idempotency
- [ ] **CH idempotency replay:** apply every `migrate-*-apm*-clickhouse.sql` twice on a fresh and a populated ClickHouse; second run is a no-op (no errors, no dup MVs). *AC: zero diffs in `system.tables`/`system.columns` after the second apply.*
- [ ] **Missing-table tolerance:** ingest code tolerates a transient missing-table window (the host_* "Table does not exist" failure mode). *AC: ingest returns retryable, does not 500-crash, recovers once the table exists.*
- [ ] **PG lockfile lint:** `build-release.py lint-migrations` green; `migrations.lock` committed with `039`. *AC: CI gate passes.*
- [ ] **`_LEGACY_BASELINE` exclusion:** no APM CH file is in `_LEGACY_BASELINE`. *AC: grep asserts absence.*
- [ ] **`%%` escaping:** any literal `%` in CH expressions/LIKE through the updater path is `%%`. *AC: updater applies without format errors.*

### 2.2 Auth & multi-tenancy
- [ ] `zpi_`/`zpr_` key auth: constant-time, revoked/expired/disabled rejection, origin-scoped RUM. *AC: see E1/E10 unit + integration.*
- [ ] RBAC: `/api/v1/apm/*` writes require `require_operator_user`, reads `get_current_user`. *AC: a reader cannot POST/PUT/DELETE.*
- [ ] Env scoping: env-scoped ingest keys + env-aware UI filters + env-aware SLOs. *AC: a prod key cannot write staging-scoped data; UI env filter partitions views.*

### 2.3 Performance / cost regression gates (CI-tracked, single-node appliance)
| Gate | Target |
|---|---|
| Sustained trace ingest | ≥ 20k spans/s, p99 ack < 250ms, zero accepted-span loss |
| Spike backpressure | 5× spike → 503+Retry-After, no OOM, recover < 30s |
| Indexed trace search (7d, 50M spans) | p95 < 1.5s |
| Fleet RED + service RED (90d 5m rollups) | p95 < 800ms |
| Service-map query | p95 < 1s |
| SLO budget endpoint (per-window) | p95 < 600ms |
| CH compression | ≥ 8× on `apm_spans` (ZSTD + LowCardinality + typed maps) |
| Raw-span TTL atomic drop | daily partition drop via `ttl_only_drop_parts=1` reclaims space without scan |

### 2.4 Alert-engine integration invariants (apply to E6, E9, E12)
- [ ] **Dual-worker dedupe:** every raise is dedupe-guarded; exactly one `alerts` row + one notification per breach across 2 uvicorn workers. *AC: load harness with 2 workers never double-raises.*
- [ ] **Always-record / conditionally-notify:** quiet hours/silences gate notification only; the `alerts` row is always written. *AC: suppressed alerts still appear in the Alerts UI.*
- [ ] **Resolve correctness:** a cleared breach resolves the alert (push recovery or evaluator clear); SLO short-window clears within ~5m. *AC: no stuck-active alerts after recovery.*
- [ ] **Channel routing:** APM alerts dispatch via `dispatch_to_channels` (email HTML/SMS/webhook/slack/teams/discord/pagerduty) with correct per-rule `notify_channels`. *AC: each configured channel receives the alert; PD dedup_key isolates APM services/SLOs.*

---

## 3. Test data, fixtures & harness

- **OTLP fixture corpus:** golden `ExportTraceServiceRequest`/`Metrics`/`Logs` protobuf+JSON payloads (valid, partial-invalid, oversized, malformed) for decode unit tests on both Go and Python paths.
- **Synthetic trace generator:** parameterized emitter (spans/s, error %, slow %, service fan-out depth, attribute cardinality) used by load + sampling-correctness + RED-accuracy suites; emits over OTLP/gRPC and OTLP/HTTP.
- **Known-graph fixture:** a fixed multi-service call graph (A→B→C, A→D) with known per-edge RED for service-map and RCA assertions.
- **Budget-burn fixtures:** time-series that consume an SLO error budget at exactly 14.4×/6×/1× for multi-window threshold assertions.
- **PII corpus:** spans/logs seeded with `Authorization`/`Cookie`/`password`/`token`/email/card/`db_statement` literals for scrubbing assertions (must not appear in CH).
- **Dual-worker harness:** runs FastAPI with 2 uvicorn workers + both evaluator loops to prove dedupe.
- **Playwright e2e:** drives `/apm/*` pages (browser-driven), asserting the happy-paths above against a seeded appliance.

---

## 4. Definition of done — module-level

The APM module v1 (Phases 1–2: AM-E1…E8) is **done** when, on a single-node appliance:

1. OTLP traces/metrics/logs ingest via gRPC `:4317` + HTTP `:4318` (Go collector) and the FastAPI fallback, authenticated by `zpi_` keys, writing byte-identical rows; the load gates in §2.3 pass.
2. RED + apdex + service-map + SLO math read **only** from rollups/graph (never raw spans) and are **provably sampling-independent** (RED accuracy under 1% raw retention).
3. Sampling (head+tail, retain-reason) and default PII scrubbing are on, with scrubbing verified to keep secrets/PII out of ClickHouse.
4. APM alerting + multi-window multi-burn SLOs fire dedupe-guarded across both workers, route via `dispatch_to_channels`, and honor quiet hours/cooldown/max_repeat.
5. The full `/apm/*` UI (overview, services list/detail with overview+performance+errors+slos tabs, trace explorer/waterfall, service map, errors inbox, SLOs, settings) renders with correct breadcrumbs and passes its Playwright happy-paths.
6. All migrations apply idempotently, the PG lockfile is committed, and no APM CH file is in `_LEGACY_BASELINE`.

Phases 3–4 (AM-E9…E12) extend with synthetics, RUM, DB/eBPF/profiling, and AI/RCA/full-stack correlation, each closed by its own per-epic DoD above.
