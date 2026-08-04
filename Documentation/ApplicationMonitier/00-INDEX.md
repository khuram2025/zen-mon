# ZenPlus Application Monitoring (APM) — Design Set Index & Pinned-Names Registry

*Status: Design proposal · 2026-06-21 · reconciled 2026-08-04 · Part of the ZenPlus Application Monitoring design set.*

This is the **navigation hub and single source of pinned truth** for the ZenPlus Application Monitoring (APM) module design. Every sibling document defers to this file for the *authoritative* names, routes, table names, ports, metric keys, epic IDs, and the migration-file allocation. If a value here disagrees with a sibling, **this file wins** — it is the reconciliation point.

> **Build state (2026-08-04):** this file pins the *target* design. What is actually implemented, what is dead schema, and the gap-closure order are recorded in [`09-MODERNITY-ASSESSMENT-AND-BUILD-STATE.md`](09-MODERNITY-ASSESSMENT-AND-BUILD-STATE.md) — **for implementation status, 09 wins over every status header and checkbox in this set.** The agent-assisted onboarding path (the module's most important post-design addition) is specified in [`10-AGENT-APM-INTEGRATION-SPEC.md`](10-AGENT-APM-INTEGRATION-SPEC.md).

## Thesis (one paragraph)

ZenPlus APM is an **OpenTelemetry-native, ClickHouse-backed Application Performance Monitoring module** (architecturally a SigNoz-class system) **fused into the existing ZenPlus appliance** rather than bolted on as a separate product. It accepts OTLP traces, metrics, logs, and (later) profiles on the standard ports, stores them in the same ClickHouse `zenplus` database that already holds ping/SNMP/host/netflow time-series, registers application **services** as first-class monitored entities alongside devices and servers, and **reuses ZenPlus's existing alert engine, notification channels, gateways, quiet-hours scheduling, migration pipeline, agent-enrollment auth, and Servers UI scaffolding** for application-level RED metrics, SLOs, errors, and incidents. The wedge: **one on-prem appliance correlating four layers** — application (new) → server/host (agent) → network device (SNMP) → flow (NetFlow) — at single-node ClickHouse economics, with no Datadog/New-Relic per-host SaaS bill and no second observability stack to run.

---

## Reading order

| # | Document | What it contains |
|---|----------|------------------|
| **00** | `00-INDEX.md` (this file) | Navigation hub · pinned-names registry · definitive migration table · service-map ruling · v1.1 addenda |
| **01** | [`01-MARKET-RESEARCH.md`](01-MARKET-RESEARCH.md) | Intensive market research: Datadog, New Relic, Dynatrace, AppDynamics, SigNoz, Grafana, Elastic, Jaeger, Sentry, Honeycomb, OpenTelemetry. Must-have feature matrix, methodologies (golden signals, RED/USE, Apdex, SLO multi-burn, head/tail sampling, exemplars), and why ClickHouse + OTel is the right model. |
| **02** | [`02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md`](02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md) | What ZenPlus has today (verified file paths), the reuse map (APM capability → existing component), the honest gap list, and retrofit risks. |
| **03** | [`03-ARCHITECTURE-AND-DATA-MODEL.md`](03-ARCHITECTURE-AND-DATA-MODEL.md) | The technical core: end-to-end architecture, the collector decision, full ClickHouse DDL + Postgres config DDL, sampling/scale/retention/cost, ingest auth, and the migration plan. |
| **04** | [`04-FEATURE-SPECIFICATION.md`](04-FEATURE-SPECIFICATION.md) | The per-feature binding contract F1–F23 (priority, behavior, data, API, UX, parity, reuse, acceptance) + competitor matrix. |
| **05** | [`05-INSTRUMENTATION-AGENTS-AND-INGESTION.md`](05-INSTRUMENTATION-AGENTS-AND-INGESTION.md) | How data gets in: OTLP transports/signals, semantic-convention→column mapping, the ZenPlus OTel distro, zero-code/eBPF, RUM SDK, synthetic execution, ingest-key auth, onboarding UX. |
| **06** | [`06-UI-UX-AND-DASHBOARDS.md`](06-UI-UX-AND-DASHBOARDS.md) | The frontend contract: routes, sidebar, `?tab=` set, page-by-page ASCII wireframes, shared primitives, query-key registry, correlation pivots, build order. |
| **07** | [`07-ROADMAP-AND-EPICS.md`](07-ROADMAP-AND-EPICS.md) | The delivery contract: effort/impact/dependency table, per-epic detail, 4-phase plan, build-order diagram, risks, KPIs. |
| **08** | [`08-TASK-LIST-AND-TEST-PLAN.md`](08-TASK-LIST-AND-TEST-PLAN.md) | Sprint-ready, per-epic layered task lists (Collector/API/Migration/UI/Alerting/Docs) with acceptance criteria + per-epic test plans (unit/integration/load/e2e) + module DoD. |
| **09** | [`09-MODERNITY-ASSESSMENT-AND-BUILD-STATE.md`](09-MODERNITY-ASSESSMENT-AND-BUILD-STATE.md) | **Status of record**: modernity verdict vs the 2026 APM bar, epic-by-epic built-vs-spec'd table, doc-vs-code contradiction catalog, and the prioritized gap-closure plan (E-0…E-8). |
| **10** | [`10-AGENT-APM-INTEGRATION-SPEC.md`](10-AGENT-APM-INTEGRATION-SPEC.md) | The agent-side input specification: the eight roles of the ZenPlus agent in APM, the exact per-signal input contract, the resource/correlation contract, zero-code instrumentation mechanics (Windows first), and phasing. |

**Where to start building:** Phase 1 = **AM-E1 → AM-E2 → AM-E3** (OTLP ingest + span storage → trace explorer/waterfall → service registry + RED + service map). That trio is the MVP and the only hard dependency is AM-E1. Read `03` then `08 §AM-E1`.

---

## Pinned-names registry (authoritative — cite verbatim)

### Ingestion

| Item | Value |
|---|---|
| OTLP gRPC port | **`4317`** (`/opt/zenplus` collector binary) |
| OTLP HTTP/protobuf port | **`4318`** (also FastAPI fallback for OTLP/HTTP) |
| OTLP standard paths | `/v1/traces`, `/v1/metrics`, `/v1/logs` (+ `/v1/profiles` later) |
| SDK ingest-key prefix | **`zpi_`** (kind = `sdk`, env-scoped) |
| RUM ingest-key prefix | **`zpr_`** (kind = `rum`, env-scoped, origin allowlist) |
| Auth | hashed ingest key (mirrors the `agents` `zpa_` enrollment/auth model) |
| ClickHouse database | **`zenplus`** (shared with all existing telemetry) |

### ClickHouse tables (raw + rollups)

| Table | Engine | Partition | ORDER BY | TTL |
|---|---|---|---|---|
| `apm_spans` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, name, ts_bucket, trace_id)` | raw 7d |
| `apm_span_metrics_5m` | AggregatingMergeTree *(shipped as such in migrate-039; originally pinned SummingMergeTree — see 08 §AM-E3 note)* | `toYYYYMM(timestamp)` | `(service_name, operation, span_kind, env, status_code, timestamp)` | 90d |
| `apm_span_metrics_1h` | AggregatingMergeTree *(same)* | `toYYYYMM(timestamp)` | `(service_name, operation, span_kind, env, status_code, timestamp)` | 395d |
| `apm_traces_resource` | ReplacingMergeTree | `toYYYYMM(seen_at)` | `(fingerprint)` | 7d |
| `apm_service_graph` | SummingMergeTree | `toYYYYMM(timestamp)` | `(client_service, server_service, env, timestamp)` | 90d |
| `apm_exceptions` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, group_id, ts_bucket, timestamp)` | raw 30d |
| `apm_rum_events` | MergeTree | `toYYYYMMDD(timestamp)` | `(application_id, view_name, ts_bucket, session_id)` | 14d |
| `apm_rum_vitals_5m` | SummingMergeTree | `toYYYYMM(timestamp)` | `(application_id, view_name, timestamp)` | 90d |
| `apm_profiles` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, profile_type, ts_bucket, timestamp)` | 14d |
| `apm_logs` | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, ts_bucket, timestamp)` | 14d |
| `apm_synthetic_results` | MergeTree | `toYYYYMM(timestamp)` | `(monitor_id, location, timestamp)` | 90d |
| `apm_synthetic_results_5m` | SummingMergeTree | `toYYYYMM(timestamp)` | `(monitor_id, location, timestamp)` | 395d |
| `apm_metrics` *(v1.1 — see addenda A1)* | MergeTree | `toYYYYMMDD(timestamp)` | `(service_name, metric_name, ts_bucket, timestamp)` | 30d |

Materialized views: `apm_span_metrics_5m_mv`, `apm_span_metrics_1h_mv`, `apm_service_graph_mv`, `apm_rum_vitals_5m_mv`, `apm_synthetic_results_5m_mv`.

### PostgreSQL config tables (all in `migrate-039-apm.sql`)

`apm_environments` · `apm_services` · `apm_ingest_keys` · `apm_enrollment_tokens` · `apm_slos` · `apm_synthetic_monitors` · `apm_sampling_rules` · `apm_scrubbing_rules` · `apm_deployments` · `apm_dashboards` · `apm_error_issues`. High-volume telemetry never lands in Postgres — only definitions/config/registry/SLO/triage state.

### Frontend routes (`dashboard/src/pages/apm/`)

`/apm` · `/apm/services` · `/apm/services/:id` · `/apm/traces` · `/apm/traces/:traceId` · `/apm/service-map` · `/apm/errors` · `/apm/errors/:id` · `/apm/rum` · `/apm/synthetics` · `/apm/synthetics/:id` · `/apm/slos` · `/apm/slos/:id` · `/apm/settings`

**Service-detail `?tab=` set:** `overview` · `performance` · `traces` · `errors` · `logs` · `dependencies` · `slos` · `deployments` · `database` · `profiling` · `infrastructure` · `settings`

### Backend API (`/api/v1/apm/*`)

`services` · `traces` · `service-map` · `errors` · `rum` · `synthetics` · `slos` · `ingest-keys` · `enrollment-tokens` · `sampling-rules` · `scrubbing-rules` · `deployments` · `dashboards` — plus the OTLP receiver (collector binary on `:4317`/`:4318`; FastAPI `apm_ingest.py` OTLP/HTTP fallback).

### Alert metric keys (appended to the `alert_rules.metric` CHECK in `migrate-039-apm.sql`)

`apm_latency_p50` · `apm_latency_p95` · `apm_latency_p99` · `apm_error_rate` · `apm_throughput` · `apm_apdex` · `apm_slo_burn` · `apm_synthetic_down` · `apm_anomaly`
*(v1.1 additions, see addenda A3: `apm_rum_cwv`, `apm_db_query_latency`.)*

### Background loops (registered in `server/app/main.py` startup, mirroring existing evaluator loops)

1. **APM alert evaluator** — latency pXX / error-rate / throughput / apdex conditions over `apm_span_metrics_5m` → existing `alerts` + `dispatch_to_channels`.
2. **SLO error-budget burn evaluator** — multi-window multi-burn-rate over rollups → `apm_slo_burn` alerts.
3. **Synthetic scheduler** — reuses the Go poller / service-check scheduler (retry/flap/maintenance) to run API + browser monitors.
4. **Service health + anomaly-baseline refresh** — denormalizes last-seen RED + health onto `apm_services`, refreshes baselines for `apm_anomaly`.

### Epic & feature ID map

| Epic | Title | Phase / Effort | Key features |
|---|---|---|---|
| **AM-E1** | OTLP ingestion + span/resource storage | P1 · XL | F1, F12 |
| **AM-E2** | Trace explorer + waterfall | P1 · L | F2 |
| **AM-E3** | Service registry + RED + service map | P1 · L | F3, F4, F13 |
| **AM-E4** | Error tracking / issues | P2 · M | F5 |
| **AM-E5** | Logs + trace correlation | P2 · M | F6 |
| **AM-E6** | APM alerting + SLO / error-budget | P2 · L | F7, F8 |
| **AM-E7** | Deployment / change tracking | P2 · S | F9 |
| **AM-E8** | Sampling + PII scrubbing pipeline | P2 · M | F10, F11 |
| **AM-E9** | Synthetic monitoring | P3 · L | F14 |
| **AM-E10** | RUM + Core Web Vitals | P3 · L | F15, F22 |
| **AM-E11** | DB/query monitoring + eBPF agent + profiling | P3 · XL | F16, F17, F18 |
| **AM-E12** | AI anomaly + causal-lite RCA + full-stack correlation | P4 · XL | F19, F20, F21, F23 |

Feature catalog (F1–F23): F1 OTLP ingestion · F2 Distributed tracing + waterfall/flame · F3 Service registry + RED + apdex · F4 Service map/topology · F5 Error tracking/issues · F6 Trace↔log↔metric correlation · F7 SLO/SLI + error-budget burn · F8 APM alerting integration · F9 Deployment/change tracking · F10 Sampling pipeline (head+tail) · F11 PII scrubbing · F12 Ingest-key auth + enrollment · F13 Dashboards + APM overview · F14 Synthetic monitoring · F15 RUM + Core Web Vitals · F16 Database/query monitoring · F17 eBPF zero-code agent · F18 Continuous profiling · F19 AI anomaly/auto-baselining · F20 Causal-lite RCA · F21 Full-stack correlation panels · F22 Session Replay · F23 Business-impact tagging.

### Service-map renderer ruling (resolves the 03/06 library question)

**The service map is rendered with the echarts `graph` series** (`force`/`circular` layout), reusing the existing `echarts` + `echarts-for-react` dependency. **cytoscape is NOT used** — it is not a project dependency (`dashboard/package.json` ships `echarts ^6`, `echarts-for-react ^3`, `recharts ^2`; the `02-TECH-STACK.md` mention of cytoscape is aspirational and was never installed). All documents use "echarts `graph`" for the service map.

---

## Definitive migration-file allocation (resolves the 03 §7.2 ↔ 08 §1.1 conflict)

Postgres and ClickHouse advance in **separate monotonic sequences, both starting at 039** (next free after `migrate-038-report-schedules.sql`; existing scripts top out at 038). Any filename containing `clickhouse` is excluded from the Postgres runner and auto-applies via `updater/clickhouse_sync.py` on every OTA update; **every CH statement is `CREATE … IF NOT EXISTS` and is never added to `_LEGACY_BASELINE`**. After each PG migration: `python3 scripts/build-release.py lint-migrations --update-lock`, then commit the migration + `scripts/migrations.lock` together.

| File | Store | Phase / epic | Contents |
|---|---|---|---|
| `migrate-039-apm.sql` | PG | P1 (E1/E3/E6 baseline) | All 11 config tables (`apm_environments`, `apm_services`, `apm_ingest_keys`, `apm_enrollment_tokens`, `apm_slos`, `apm_synthetic_monitors`, `apm_sampling_rules`, `apm_scrubbing_rules`, `apm_deployments`, `apm_dashboards`, `apm_error_issues`) + the **full re-declared** `alert_rules.metric` CHECK with the 9 `apm_*` keys appended |
| `migrate-039-apm-clickhouse.sql` | CH | P1 (E1/E3/E4) | `apm_spans`, `apm_traces_resource`, `apm_span_metrics_5m`+`_1h` (+MVs), `apm_service_graph` (+MV), **`apm_exceptions`** |
| `migrate-040-apm-logs-clickhouse.sql` | CH | P2 (E5) | `apm_logs` |
| `migrate-041-apm-synthetics-clickhouse.sql` | CH | P3 (E9) | `apm_synthetic_results`, `apm_synthetic_results_5m` (+MV) |
| `migrate-042-apm-rum-clickhouse.sql` | CH | P3 (E10) | `apm_rum_events`, `apm_rum_vitals_5m` (+MV) |
| `migrate-043-apm-profiles-clickhouse.sql` | CH | P3 (E11) | `apm_profiles` |

> **`apm_exceptions` ships in `039`, not with logs (`040`):** exceptions are span events on the same OTLP trace path as spans, so error tracking (AM-E4) writes them through the same ingest writer the moment tracing exists — they do not wait for the logs pipeline (AM-E5). The v1.1 `apm_metrics` table (addenda A1) lands as `migrate-044-apm-metrics-clickhouse.sql`.

`03 §7.2` and `08 §1.1` now both carry this exact allocation.

---

## Design addenda & v1.1 refinements (resolved open items)

The independent design review surfaced seven advanced-completeness gaps. Each is resolved here as an explicit decision so the design is closed, not silently incomplete. These extend — never contradict — the sibling docs.

- **A1 — OTLP metrics signal storage (`apm_metrics`).** v1 derives RED from spans (`apm_span_metrics_*`). Custom application/runtime metrics arriving on OTLP `/v1/metrics` (gauge/sum/histogram) and **exemplars** get a first-class home: `apm_metrics` (MergeTree, `PARTITION BY toYYYYMMDD`, `ORDER BY (service_name, metric_name, ts_bucket, timestamp)`, TTL 30d) with `metric_name LowCardinality(String)`, typed attribute maps, `value Float64`, histogram bucket columns, and **exemplar `trace_id`/`span_id`** so any metric point pivots to a trace. Accepted at ingest in AM-E1; storage + dashboards (F13) finalized alongside the span-metrics MVs. Ships in `migrate-044-apm-metrics-clickhouse.sql`.
- **A2 — Exemplar trace pinning (survives tail sampling).** Rollup points store an exemplar `trace_id`, but tail sampling (~5% kept) can evict it, breaking the spike→trace pivot. Decision: the span→metrics MV records the exemplar (trace_id, span_id, duration) for the slowest/error span per bucket, **and** the tail-sampling processor runs a **"keep exemplars" policy** — any trace selected as a rollup exemplar (error or pXX outlier) is force-retained (sampling decision = keep), guaranteeing the pivot resolves. "Live mode" covers the most-recent window before rollups settle.
- **A3 — RUM & DB alerting.** v1 ships the 9 core alert metric keys. Two more are pinned for v1.1 so Core Web Vitals regressions and slow queries are alertable: **`apm_rum_cwv`** (e.g. LCP/INP p75 over `apm_rum_vitals_5m`, added with AM-E10) and **`apm_db_query_latency`** (slow-query threshold over DB spans, added with AM-E11). Each is appended via the standard full-re-declared CHECK pattern in its epic's PG migration.
- **A4 — Capacity / disk sizing.** Operators size disk from a single budget. Worked reference (≈2k spans/s ingested, ~5% tail-retained, plus rollups): raw `apm_spans` 7d dominates (~tens of GB; see `03 §5.5`), `apm_exceptions` 30d and `apm_logs`/`apm_rum_events`/`apm_profiles` 14d are smaller, `apm_*_5m`/`_1h`/synthetic rollups (90d/395d) are tiny. A consolidated "sum across all `apm_*` TTLs at reference volume → total GB" table belongs in `03 §5.5`; budget ~80–120 GB for the reference workload on a single-node appliance, scaling roughly linearly with ingested span rate and inversely with the tail-sampling keep-rate.
- **A5 — Multi-tenant quota & cardinality budget.** v1 isolation primitive = **environment scoping** + env-scoped ingest keys (no per-tenant separation). When platform RBAC/multi-tenancy (`enahce1` E5) lands, the model is: a `tenant_id` prefixed onto the `apm_*` ORDER BY under multi-tenant mode; **per-tenant ingest quota** (spans/s + bytes/day) and **per-tenant cardinality budget** (max distinct `service.name`/`operation`) enforced at the collector with HTTP 429 backpressure and an observable drop counter. Sketched now so the storage keys don't need a breaking change later.
- **A6 — Dead-letter / poison-batch handling.** Spans that decode but fail ClickHouse insert must not vanish silently. Add a bounded **`apm_ingest_deadletter`** spool (local disk on the collector + small CH table for metadata) capturing failed-batch reason/count, plus an **`apm_ingest_drops`** counter that is itself alertable, so ingestion loss is observable rather than a soft drop-and-log. v1.1, in the AM-E8 (pipeline) workstream.
- **A7 — OTLP version-skew & schema negotiation.** Beyond response semantics, the collector pins the OTLP proto version it was built against and tolerates unknown fields (forward-compatible protobuf); a schema-version label on ingested batches lets the writer route legacy shapes. Documented as an ingest-robustness item in AM-E1/AM-E8.

---

## Cross-reference convention

All pinned route/table/endpoint/epic/feature names in every document are taken **verbatim from this registry**. ClickHouse DDL lives in `03 §3`; Postgres config DDL in `03 §4`; the collector decision in `03 §2`; sampling/scale/cost in `03 §5`; ingest auth in `03 §6`; the migration plan in `03 §7`. Any deviation must be raised against this index, not decided locally.
