# Application Monitoring — Roadmap & Epics

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the **delivery contract** for ZenPlus Application Monitoring (APM): it expands the blueprint's twelve epics (**AM-E1 … AM-E12**) into a sequenced, four-phase build plan that takes the module from a green-field OTLP front door to a market-leading, full-stack-correlated, AI-assisted observability product. It carries the effort/impact/dependency table, per-epic detail (goal, scope, deliverables, dependencies, effort, impact, what-ships, exit criteria), a four-phase plan with outcomes (MVP OTLP ingest + tracing + service map → errors + RED analytics + alerting/SLOs → RUM + synthetics + profiling + DB/eBPF → SLO-grade AI root cause + full-stack correlation), an ASCII build-order diagram, top risks with mitigations, and KPIs. Every epic ID, route, table name, prefix, and metric key below is pinned by the authoritative blueprint and used verbatim; nothing here re-derives architecture. The estimating discipline mirrors `Documentation/enahce1/04-ROADMAP.md`: T-shirt effort for a 2–3 engineer squad spanning Go collector/poller, FastAPI, React, and PG/CH migration work — with the explicit advantage that ~60% of the platform plumbing (alert engine, notification channels, migration pipeline, agent-enrollment auth, ClickHouse singleton, Servers UI scaffolding) is **reused, not rebuilt**.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — reuse inventory and gap analysis against the existing ZenPlus appliance
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — collector-vs-FastAPI decision, end-to-end pipeline, full ClickHouse + PostgreSQL DDL, partitions/TTL/codecs
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23), MoSCoW priority, acceptance criteria, API contracts
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP paths, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing pipeline
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, components, react-query keys
- `07-ROADMAP-AND-EPICS.md` — **this document**
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the test plan (OTLP conformance, sampling correctness, RED-accuracy-under-sampling, SLO-burn math)

---

## 1. Prioritization & estimating model

- **Impact** = product viability (does anything work without it?) + competitive parity (does a Datadog/New Relic prospect notice the gap?) + differentiation (does it win a deal nobody else can?). Rated **Critical / High / Medium**.
- **Effort** (T-shirt, 2–3 engineer squad, all layers included):
  - **S** ≤ 1 week · **M** 1–2 weeks · **L** 3–4 weeks · **XL** > 1 month.
- **Sequence rule** (from the blueprint, §9): **MVP tracing + map + ingest → errors + RED + alerting/SLOs → RUM/synthetic/profiling/DB/eBPF → AI/SLO-grade RCA/advanced.** Nothing ships before its data plane; nothing markets correlation before the signals it correlates exist.
- **Reuse leverage**: every epic names the existing subsystem it forks rather than rebuilds. This is the single biggest schedule compressor and is reflected in effort ratings (e.g. AM-E6 is **L** not XL because the alert engine, `dispatch_to_channels`, quiet-hours gating, and HTML email already exist; AM-E9 is **L** not XL because the Go poller scheduler/retry/flap/maintenance backbone is reused for synthetics).

---

## 2. Effort / impact / dependency table (AM-E1 … AM-E12)

| Epic | Title | Phase | Effort | Impact | Depends on | Unlocks |
|---|---|:---:|:---:|:---:|---|---|
| **AM-E1** | OTLP ingestion + span/resource storage | P1 | **XL** | **Critical** | — (green-field) | everything; traces land in ClickHouse |
| **AM-E2** | Trace explorer + waterfall | P1 | **L** | **High** | AM-E1 | search + view any trace |
| **AM-E3** | Service registry + RED + service map | P1 | **L** | **High** | AM-E1 | service list, RED dashboards, topology, apdex |
| **AM-E4** | Error tracking / issues | P2 | **M** | **High** | AM-E1 | Sentry-style error inbox, triage |
| **AM-E5** | Logs + trace correlation | P2 | **M** | **High** | AM-E1, AM-E3 | one-click trace↔log, exemplars |
| **AM-E6** | APM alerting + SLO / error-budget | P2 | **L** | **High** | AM-E3 | latency/error/throughput/apdex/burn alerts + SLO budgets |
| **AM-E7** | Deployment / change tracking | P2 | **S** | **Medium** | AM-E3 | deploy markers + version-vs-version regression compare |
| **AM-E8** | Sampling + PII scrubbing pipeline | P2 | **M** | **High** (cost + compliance) | AM-E1 | tail/head sampling cost control + PII safety |
| **AM-E9** | Synthetic monitoring | P3 | **L** | **High** | AM-E1, AM-E6 | API + browser multi-step probes from locations |
| **AM-E10** | RUM + Core Web Vitals | P3 | **L** | **High** | AM-E1 | frontend UX + CWV, RUM→backend trace stitch |
| **AM-E11** | DB/query monitoring + eBPF agent + profiling | P3 | **XL** | **Medium–High** | AM-E1, AM-E2 | DB query view, zero-code coverage, code-level flamegraphs |
| **AM-E12** | AI anomaly + causal-lite RCA + full-stack correlation | P4 | **XL** | **High** (differentiator) | AM-E3, AM-E4, AM-E6 | auto-baselines, probable-cause incidents, span→host→SNMP→flow RCA |

**Reading the table.** AM-E1 is the long pole — it is the only **Critical** epic because no other capability exists until OTLP traces land in `apm_spans`. The three Phase-1 epics (E1/E2/E3) constitute the MVP and have no cross-dependency beyond E1. Phase 2 is parallelizable: E4, E5, E7, E8 all hang off E1/E3 and can be staffed concurrently; E6 (alerting/SLO) is the Phase-2 keystone that gates the product's "monitoring" claim. Phase 3 (E9/E10/E11) is breadth and is independently sequenceable by market demand, gated only by E1 (and E6 for synthetic-down alerting). AM-E12 sits alone in Phase 4 because it consumes the topology (E3), the error grouping (E4), and the alert engine (E6) as substrate — it is the headline differentiator but the hardest, so it is deliberately last.

---

## 3. Per-epic detail

> Effort S(≤1wk) · M(1–2wk) · L(3–4wk) · XL(>1mo). All ClickHouse DDL, PG DDL, route paths, and metric keys are pinned in `03-ARCHITECTURE-AND-DATA-MODEL.md` and used verbatim. "Reuse" names the existing subsystem forked rather than rebuilt.

### Phase 1 — Tracing foundation & ingestion (MVP)

---

#### AM-E1 — OTLP ingestion + span/resource storage  ·  Effort XL  ·  Impact Critical

**Goal.** Stand up the OTLP front door so any OTel SDK/Collector can point at ZenPlus on the standard ports with zero re-instrumentation, and have 100% of spans land durably in ClickHouse with backpressure and authenticated ingest.

**Scope.**
- **Dedicated Go collector** (`poller/cmd/otelcollector` sibling binary) listening on **OTLP/gRPC :4317** and **OTLP/HTTP :4318**, built on the upstream `go.opentelemetry.io/collector` libraries: receivers `otlp/grpc`+`otlp/http`, processors `memory_limiter → resource/attributes → batch`, ClickHouse exporter (native protocol, buffered batch insert). Reuses the poller's build/release/OTA packaging and worker-pool patterns.
- **FastAPI OTLP/HTTP fallback** (`apm_ingest.py`, mounted at root prefix `""` to honor OTLP fixed paths): `POST /v1/traces`, `POST /v1/metrics`, `POST /v1/logs` decoding OTLP protobuf (`application/x-protobuf`) + JSON → row dicts → a **buffered async batch writer** (bounded queue, flush by size/interval) wrapping the `get_clickhouse_client()` singleton — replacing the audit's per-request synchronous `client.insert`.
- **Ingest-key auth.** Fork `agents.py` helpers `_sha256`, `_new_api_key` (prefix `zpa_`→`zpi_`), `_strip_bearer`, `_authenticate` (constant-time `hmac.compare_digest` of `sha256(bearer)` vs `apm_ingest_keys.key_hash`). One-time hashed enrollment via `apm_enrollment_tokens` (mirrors `agent_enrollment_tokens`). Go collector validates the same `zpi_` bearer against `apm_ingest_keys` via read-through cache.
- **Storage.** `migrate-039-apm-clickhouse.sql` creating `apm_spans` (daily-partitioned, `ZSTD`/`Delta` codecs, bloom skip indexes on `trace_id`/attr maps/`http_route`, minmax on `duration_nano`, 7d raw TTL, `ttl_only_drop_parts=1`) and `apm_traces_resource` (ReplacingMergeTree fingerprint side-table). `migrate-039-apm.sql` creating `apm_environments`, `apm_services`, `apm_ingest_keys`, `apm_enrollment_tokens` (full CHECK list, GIN indexes, header per `migrate-038`).
- **Control plane**: `GET/POST/DELETE /api/v1/apm/ingest-keys`, the `/enroll` token-consumption path, and `/apm/settings` ingest-key UI.
- **No-data sweeper** `apm_nodata_sweeper` registered in `main.py` `@app.on_event("startup")` (mirrors `health_sweeper_loop`), rolling `apm_services.health` to `no_data` and back; **dedupe-guarded** (runs in both uvicorn workers).

**Deliverables.** Go collector binary + service unit; FastAPI fallback receiver + buffered writer; `zpi_` auth + enrollment; both migrations (PG lockfile updated via `build-release.py lint-migrations --update-lock`); ingest-key CRUD + settings UI; nodata sweeper; OTLP partial-success + `Retry-After`/`RetryInfo` backpressure semantics.

**Dependencies.** None (green-field). Establishes the new `CODEC(Delta, ZSTD)` platform convention (documented in 03 + 05).

**What ships.** A customer sets `OTEL_EXPORTER_OTLP_ENDPOINT` + a `zpi_` bearer and sees spans in ClickHouse within seconds; ingest survives a traffic spike without OOM via `memory_limiter`/bounded queue.

**Exit criteria.**
- OTLP conformance: gRPC 4317 + HTTP 4318 (protobuf **and** JSON) accept traces; partial-success returns 200 with `partial_success` populated; retryable failures return 429/503 + `Retry-After`.
- Sustained low-millions of spans/day on a single node with daily-partition drop-parts reclaiming space; dashboards never scan raw spans (verified once E3 lands).
- `zpi_` bearer rejected with 401 on bad key; enrollment token is one-time (max_uses honored, `consumed_ip` recorded).
- Collector and FastAPI fallback write **identical** `apm_spans` row shapes (query layer agnostic to path).
- `apm_nodata_sweeper` flips a silent service to `no_data` and recovers it; no double-fire across workers.

---

#### AM-E2 — Trace explorer + waterfall  ·  Effort L  ·  Impact High

**Goal.** Make any trace searchable and viewable as a waterfall/flame, in both **live** (recent, unsampled) and **indexed** (retained) modes — the core debugging surface.

**Scope.**
- `GET /api/v1/apm/traces` (trace search; live vs indexed mode toggle; filter by service/operation/status/duration/attributes hitting promoted columns + bloom indexes) and `GET /api/v1/apm/traces/{trace_id}` (full span tree via `idx_trace_id` bloom lookup).
- Frontend `/apm/traces` (`TraceExplorerPage.tsx`, URL-driven filters/chips per the ServersPage convention) and `/apm/traces/:traceId` (`TraceWaterfallPage.tsx`).
- **New** custom horizontal-bar timeline / Gantt waterfall component under `components/apm/` (no existing primitive); span detail drawer (attributes, events, links, status).
- `TimeRangePicker`/`useTimeRange` reused verbatim; react-query keys `['apm','traces',…]`.

**Deliverables.** Two query endpoints; explorer + waterfall pages; waterfall component; span detail drawer; resource-fingerprint CTE pattern wired for resource-attribute filters.

**Dependencies.** AM-E1 (spans must exist).

**What ships.** Search "service=checkout status=ERROR p95>800ms last 15m", click a row, see the full span tree with the slow child highlighted.

**Exit criteria.**
- Trace-id lookup is sub-second via bloom index even at 7d retention.
- Live mode returns spans ingested in the last ~15 min regardless of retention; indexed mode returns retained spans.
- Waterfall renders correct parent/child nesting, durations, error markers, and async span links.
- Service/attribute filters hit indexed/promoted columns (no full scans), validated by `EXPLAIN`.

---

#### AM-E3 — Service registry + RED + service map  ·  Effort L  ·  Impact High

**Goal.** Register services as first-class monitored entities (alongside devices/servers), compute always-on RED metrics + apdex from 100% of spans, and auto-derive the dependency topology — the "service overview" product surface.

**Scope.**
- **Rollups computed at ingest** (never from raw spans): `apm_span_metrics_5m`/`_1h` SummingMergeTree + `_5m_mv`/`_1h_mv` materialized views (request/error counts, `quantilesTDigestState` for p50/p75/p90/p95/p99, apdex buckets). p95 at read = `quantilesTDigestMerge(0.95)(duration_state)`.
- `apm_service_graph` SummingMergeTree + `_mv` / collector `servicegraph` connector pairing CLIENT/SERVER spans into edges.
- `apm_services` PG registry with denormalized last-seen RED + `health` (`healthy/degraded/critical/no_data`), populated from rollups; `apm_environments` env scoping.
- API: `GET /apm/services` (+ facets like `/servers/facets`), `/services/{id}`, `/services/{id}/red`, `/services/{id}/operations`, `/services/{id}/apdex`, `/service-map` (nodes + edges).
- Frontend: `/apm` (`ApmOverviewPage.tsx` fleet dashboard), `/apm/services` (`ServicesPage.tsx`, cloned from ServersPage — health chips, env/language/team/tag filters, sortable RED columns), `/apm/services/:id` (`ServiceDetailPage.tsx`, hand-rolled `?tab=` bar) with **overview** + **performance** tabs, `/apm/service-map` (`ServiceMapPage.tsx`, new echarts `graph` with RED-on-edges). `components/apm/shared.tsx` (ServiceHealthBadge, LanguageIcon, latency/error bars, KpiTile).

**Deliverables.** RED + service-graph MVs; `apm_services`/`apm_environments` tables; six query endpoints; overview/services/service-detail/service-map pages; `apm/shared.tsx`; sidebar `APM` NavSection + `routeLabels`/`routeSections` entries for all `/apm/*`.

**Dependencies.** AM-E1.

**What ships.** A services list with live RED + apdex + health, a service detail page with latency/throughput/error-rate charts and top operations, and an auto-built dependency map colored by health.

**Exit criteria.**
- **RED accuracy under sampling**: rollups computed from 100% of spans at insert — RED/apdex match ground truth even after E8 tail-sampling drops raw traces.
- Service list/detail dashboards read **only** rollups (zero raw-span scans).
- Service map auto-derives nodes from `apm_services` and edges from `apm_service_graph` with per-edge rate/error/latency.
- Apdex computed per-service from threshold buckets (`≤ T`, `≤ 4T`); breadcrumbs render for every `/apm/*` route.

---

### Phase 2 — Errors, correlation, alerting, SLOs

---

#### AM-E4 — Error tracking / issues  ·  Effort M  ·  Impact High

**Goal.** Collapse the flood of raw exceptions into a deduplicated, triageable **issues inbox** (Sentry/New-Relic-Errors-Inbox class), grouped by fingerprint, with first/last seen, counts, affected versions, and assignee workflow.

**Scope.**
- `apm_exceptions` (CH, 30d — errors are high-value) with `group_id` = fingerprint of `exception_type` + normalized stack (UUIDs/hex/line-noise stripped), computed in the collector.
- `apm_error_issues` (PG) holding **triage state** (`unresolved`/`resolved`/`resolved_in_version`/`ignored`, assignee, first/last-seen mirror) keyed by `group_id`; **grouping key lives in CH, triage state in PG**.
- API: `GET /apm/errors` (grouped), `/apm/errors/{group_id}` (detail + occurrences + trend), `PATCH /apm/errors/{group_id}` (status/assignee).
- Frontend `/apm/errors` (`ErrorsInboxPage.tsx`, same filter+table skeleton as ServersPage), `/apm/errors/:id` (`ErrorIssueDetailPage.tsx` — stack trace, occurrence trend, linked traces, affected versions). Service-detail **errors** tab.

**Deliverables.** `apm_exceptions` + `apm_error_issues`; collector fingerprinting; three endpoints; inbox + issue-detail pages; triage/assignee mutations.

**Dependencies.** AM-E1.

**What ships.** An errors inbox where similar exceptions collapse to one issue with a count and trend; click → stack trace + the exact traces that threw it; assign + resolve-in-version.

**Exit criteria.**
- Fingerprint normalization collapses identical errors differing only by UUID/hex/line numbers; distinct error classes never merge.
- Occurrence counts/trends from CH; triage state persists in PG and survives raw-exception TTL.
- Each issue links to representative `trace_id`s (via `apm_spans`/`apm_exceptions` shared ids).

---

#### AM-E5 — Logs + trace correlation  ·  Effort M  ·  Impact High

**Goal.** One-click pivot between a trace and its exact logs, and metric→trace exemplars — the "metrics-to-detect, trace-to-localize, logs-to-explain" loop.

**Scope.**
- `apm_logs` (CH, 14d) with `trace_id`/`span_id`, `severity`/`severity_num`, body `tokenbf_v1` index, `idx_log_trace` bloom; OTLP logs ingest (`POST /v1/logs` via collector + FastAPI fallback).
- Trace↔log pivot (`WHERE trace_id = …` on `idx_log_trace`); exemplar linking from RED charts → representative trace.
- API: `GET /apm/...` log views scoped by trace/service/severity. Frontend: service-detail **logs** tab + a "logs for this trace" panel on the waterfall.

**Deliverables.** `apm_logs` table + MV-free raw ingest; logs query endpoints; logs tab + trace-logs panel; exemplar wiring metric→trace.

**Dependencies.** AM-E1, AM-E3.

**What ships.** From a slow span, jump to its logs; from a latency spike on a RED chart, jump to a representative trace.

**Exit criteria.**
- `trace_id` injected logs join to spans with sub-second `idx_log_trace` lookup.
- Exemplar from a 5m rollup data point resolves to a real trace in that bucket.
- Body full-text search uses the token bloom index (no full scan within service scope).

---

#### AM-E6 — APM alerting + SLO / error-budget  ·  Effort L  ·  Impact High

**Goal.** Plug APM into the **existing unified alert engine** (no parallel stack) for latency/error/throughput/apdex alerts, and add the SLO/error-budget primitive the audit says is entirely missing — including multi-window multi-burn-rate paging.

**Scope.**
- **New metric keys** `apm_latency_p50/p95/p99`, `apm_error_rate`, `apm_throughput`, `apm_apdex`, `apm_slo_burn`, `apm_synthetic_down`, `apm_anomaly` added (in lockstep) to: the full `alert_rules_metric_check` CHECK in `migrate-039-apm.sql`, the `_CONDITION_METRICS` regex (new `apm_alert_rules.py`), and the TS `NETWORK_METRICS`-style registry in `AlertRuleFormDialog.tsx`.
- **`apm_alert_service.py`** (cloned from `network_alert_service.py`): `APM_METRICS = {…}`; every 60s read RED from `apm_span_metrics_5m` (p95 via `quantilesTDigestMerge`, error_rate = error_count/request_count, throughput = request_count/window, apdex from buckets); reuse `_cmp`/`_active_alert_id`/`_raise`/`_resolve`/`_notify`; **enforce `cooldown`/`max_repeat`** (defined-but-unenforced today) for re-notify/hysteresis. Register `apm_alert_evaluator_loop` in `main.py` startup; **dedupe-guarded**.
- **SLO engine.** `apm_slos` (PG: `sli_type` availability/latency/error_rate/custom, `target`, `window_days` 7/30/90, `latency_threshold_ms`, scope service/operation, burn-alert config). **`apm_slo_burn_loop`** computes SLI = good/total per window, error budget remaining = `(SLI − target)/(1 − target)`, and **multi-window multi-burn** (Google SRE Workbook 99.9% canonical): Page 14.4x/1h (short 5m), Page 6x/6h (short 30m), Ticket 1x/3d (short 6h); severity→channel routing (page→PagerDuty/SMS, ticket→email/Slack).
- API: `GET/POST/PUT/DELETE /apm/slos`, `GET /apm/slos/{id}/budget` (per-window burn). Frontend `/apm/slos` + `/apm/slos/:id` (burn chart); service-detail **slos** tab.
- Reuse `dispatch_to_channels`, `notifications_allowed` (quiet hours), `email_render.build_alert_email_html` (APM `details[]` rows). Extend PagerDuty `dedup_key` to carry APM service/SLO id. `synthetic_down` rides the push path (poller → `/api/v1/alert-engine/evaluate-service`), needing no new evaluator.

**Deliverables.** Metric-key registration (4 places, in lockstep); `apm_alert_service.py` + loop; `apm_slos` + `apm_slo_burn_loop`; SLO CRUD + budget endpoints; SLO list/detail + burn UI; cooldown/max_repeat enforcement.

**Dependencies.** AM-E3 (RED rollups are the alert source).

**What ships.** Latency/error/throughput/apdex alert rules in the existing Alerts UI routed to existing channels with quiet hours; SLOs with error budgets and burn-rate paging.

**Exit criteria.**
- **SLO-burn math** verified against the SRE Workbook worked examples; both long+short windows must breach to fire.
- APM alerts dedupe correctly across both uvicorn workers (no double-page).
- `cooldown`/`max_repeat` honored on re-raise; recovery resolves the alert row.
- Burn alerts honor quiet hours; PagerDuty incidents dedupe on service/SLO id.
- Push-path latency/error metrics feed `alert_engine.py` `metric_values` and evaluate via `_conditions_match` with **zero engine changes**.

---

#### AM-E7 — Deployment / change tracking  ·  Effort S  ·  Impact Medium

**Goal.** Treat deploys as first-class change events, overlay markers on every chart, and compare version-vs-version — the cheapest high-leverage MTTR win (attribute a regression to a build).

**Scope.**
- `apm_deployments` (PG: service, version, sha, ts); spans carry `deployment_id`.
- API: `GET/POST /apm/deployments`. Frontend: recharts `ReferenceLine` markers on service-detail charts; service-detail **deployments** tab with version-vs-version RED compare.

**Deliverables.** `apm_deployments` table; deploy CRUD; chart markers; version-compare view.

**Dependencies.** AM-E3.

**What ships.** A deploy from CI posts a marker; the performance chart annotates it; a regression after v2.3.1 is visible as a before/after RED delta.

**Exit criteria.** Markers render at correct timestamps; version-vs-version compare shows RED deltas; markers feed AM-E12 RCA later.

---

#### AM-E8 — Sampling + PII scrubbing pipeline  ·  Effort M  ·  Impact High (cost + compliance)

**Goal.** Bring raw-span volume (and therefore single-node cost) under control with value-aware tail sampling, and guarantee sensitive data never reaches ClickHouse via in-pipeline scrubbing.

**Scope.**
- **Sampling.** Go collector `tailsamplingprocessor` (buffers a trace `decision_wait` ~5s): keep 100% errors, keep slow (latency outliers), keep small probabilistic baseline (~1–5%), plus string-attribute/tag policies. Head probabilistic floor targeting ~10 tps/service equiv (`apm_environments.sampling_target_tps`). Tail is the default for `prod`. Retain reason tagged in `attributes_string['zp.retain_reason']` (`auto`/`error`/`slow`/`rule`/`baseline`) for cost attribution. Config in `apm_sampling_rules` (PG); API `GET/PUT /apm/sampling-rules`.
- **Scrubbing.** Collector processors between receive and export: attribute drop/hash, allow-list + value masking, span/log drop filters, OTTL-style regex partial-masking. **Default rules ship on** (scrub `Authorization`/`Cookie`/`password`/`token`, `db_statement` bind params via source-side digesting, emails/cards via regex; RUM masks form input). Config in `apm_scrubbing_rules` (PG); API `GET/PUT /apm/scrubbing-rules`.
- Frontend `/apm/settings` (`ApmSettingsPage.tsx`) sections for sampling rules, scrubbing rules, retention.

**Deliverables.** Tail+head sampling with retain-reason tagging; scrubbing processors + default rule set; `apm_sampling_rules`/`apm_scrubbing_rules` tables; settings UI.

**Dependencies.** AM-E1 (the collector pipeline it extends).

**What ships.** `prod` keeps every error + slow trace + a thin baseline and drops the boring 95–99%; `Authorization` headers and bind params never land in ClickHouse.

**Exit criteria.**
- **RED-accuracy-under-sampling** preserved (rollups computed pre-sampling in E3) — verified by comparing rollup RED to ground truth with tail sampling active.
- Every retained span carries a `zp.retain_reason`; cost attributable per reason.
- Default scrubbing rules verified to redact secrets before any CH write; partial-mask (not all-or-nothing) confirmed via OTTL.
- `decision_wait` buffering does not OOM under spike (memory_limiter holds).

---

### Phase 3 — RUM, synthetic, profiling, DB, eBPF

---

#### AM-E9 — Synthetic monitoring  ·  Effort L  ·  Impact High

**Goal.** Proactive API + browser (multi-step) probes from selectable locations, reusing the Go poller scheduler/retry/flap/maintenance backbone — **not** a third probe implementation.

**Scope.**
- Extend `poller/internal/checker.CheckOne` dispatch + `engine.go` `runServiceCheckCycle` with a synthetic/browser case (reuse `effectiveInterval`/`due`, `DownCount`/`RetryCount` flap confirmation, parent suppression, `LoadActiveMaintenanceCheckIDs` muting unchanged). Multi-step scripts/assertions/locations stored in JSONB `config` (no schema churn).
- `apm_synthetic_monitors` (PG, reuses `service_checks` group/template/maintenance infra); `apm_synthetic_results` (CH, raw 90d, per-step `step_index`/`step_name`, `backend_trace_id` to stitch synthetic→backend trace) + `apm_synthetic_results_5m` (+MV, 395d).
- API: `GET/POST/PUT/DELETE /apm/synthetics`, `POST /apm/synthetics/{id}/run` (routes to poller, never re-implements probes). Frontend `/apm/synthetics` + `/apm/synthetics/:id` (reuse ServiceCheckDetail KPI tiles + uptime calendar + incidents strip; add step-waterfall).
- `synthetic_down` push: poller emits a status transition to `/api/v1/alert-engine/evaluate-service` (reusing the service-check path), so it needs no new evaluator.

**Deliverables.** Poller synthetic/browser checker + cycle integration; `apm_synthetic_monitors` + `apm_synthetic_results(_5m)`; monitor CRUD + run-now; synthetics list/detail UI with step waterfall; `synthetic_down` alerting.

**Dependencies.** AM-E1, AM-E6 (`synthetic_down` metric + alert routing).

**What ships.** A multi-step checkout journey probed from N locations on a schedule, with per-step latency, assertions, uptime SLA, and a synthetic→backend trace link; failures page via the existing engine.

**Exit criteria.**
- Run-now routes through the poller (no Python re-implementation drift); maintenance windows still write metrics but mute status/alerts.
- Per-step timings + assertions stored and charted as a step waterfall.
- `backend_trace_id` stitches a synthetic request to its backend trace.
- `synthetic_down` raises/resolves via the existing service-status path; SLA math reuses `get_service_sla`.

---

#### AM-E10 — RUM + Core Web Vitals  ·  Effort L  ·  Impact High

**Goal.** Real-user monitoring of browser sessions — Core Web Vitals (p75), JS errors, route/resource timing — stitched to backend traces, via a public origin-scoped beacon.

**Scope.**
- RUM SDK + **public `zpr_` beacon** `POST /api/v1/apm/rum/ingest` (HTTP-only, CORS, origin allowlist in `apm_ingest_keys.origin_allowlist`, per-origin rate limit — **never** a secret key in client JS).
- `apm_rum_events` (CH, 14d, `view/action/error/resource/long_task`, CWV `lcp/inp/cls/fcp/ttfb`, `backend_trace_id` to stitch to `apm_spans`, session/geo/browser/device dims) + `apm_rum_vitals_5m` (+MV, p75 LCP/INP/CLS — CWV standard — 90d).
- API: `GET /apm/rum/...` views/sessions/web-vitals. Frontend `/apm/rum` (`RumPage.tsx`): CWV tiles, session list, RUM→trace pivot.

**Deliverables.** RUM SDK + `zpr_` public-key path; `/apm/rum/ingest` beacon (CORS, rate-limit); `apm_rum_events`/`_vitals_5m`; RUM views/sessions endpoints; RUM page; RUM→backend trace stitch.

**Dependencies.** AM-E1.

**What ships.** Frontend p75 LCP/INP/CLS per route, JS error tracking, and a click from a slow page view to the backend trace that served it.

**Exit criteria.**
- `zpr_` beacon is public + origin-scoped (CORS allowlist enforced, rate-limited); no secret in client JS.
- CWV reported at p75 per `application_id`+`view_name`; form input masked by default.
- `backend_trace_id` stitches a RUM view to its backend trace.

---

#### AM-E11 — DB/query monitoring + eBPF agent + profiling  ·  Effort XL  ·  Impact Medium–High

**Goal.** Three premium-APM depth features that share the same OTLP/storage substrate: query-level DB monitoring, zero-code eBPF coverage, and code-level continuous profiling with span→flamegraph linking.

**Scope.**
- **DB monitoring.** DB agent emits normalized query digests + sampled executions + captured plans into the `db_system`/`db_operation`/`db_statement` promoted columns on `apm_spans`; **source-side bind-parameter obfuscation** (literals never leave the app host). Service-detail **database** tab.
- **eBPF agent.** Beyla/OBI-style zero-code RED+spans (HTTP/gRPC/SQL/Mongo) exported over OTLP into the same collector — no SDK; packaged via the poller/agent fleet path.
- **Profiling.** `apm_profiles` (CH, 14d, pprof blob `String CODEC(ZSTD(3))` inline, `trace_id`/`span_id` link, `idx_prof_trace`); OTLP profiles endpoint `POST /v1development/profiles`; span→flamegraph link. Service-detail **profiling** tab.

**Deliverables.** DB agent (digests/plans/obfuscation) → `db_*` columns + database tab; eBPF agent → OTLP; `apm_profiles` + profiles ingest + profiling tab + span→flamegraph.

**Dependencies.** AM-E1, AM-E2 (waterfall is the span→profile launch point).

**What ships.** A DB query view with normalized digests + plans (no PII), zero-code coverage for services without SDKs, and a jump from a slow span to the flamegraph of the code that burned the time.

**Exit criteria.**
- DB bind params obfuscated at source; only digests reach CH.
- eBPF agent produces RED + spans for an uninstrumented Linux HTTP service over OTLP.
- Span→profile link resolves to a flamegraph for that code path; pprof blob inline storage size validated (object-storage pointer noted as a 03 risk if blobs prove large).

---

### Phase 4 — AI & advanced

---

#### AM-E12 — AI anomaly + causal-lite RCA + full-stack correlation  ·  Effort XL  ·  Impact High (differentiator)

**Goal.** The headline wedge: auto-baselining, graph-walk probable-cause incidents, and the full-stack correlation **no competitor can do on-prem** — span → the host metrics of its server → the SNMP interface of its switch → the netflow conversation.

**Scope.**
- **Anomaly.** `apm_anomaly_loop` computing seasonality (hour-of-day/day-of-week) + std-dev baselines over RED history → emits the derived `apm_anomaly` boolean/score the alert engine thresholds (no new alert stack).
- **Causal-lite RCA.** Graph-walk over `apm_service_graph`: when multiple alerts fire, correlate via topology edges and surface the **most-upstream impacted service** as probable cause; collapse symptom alerts into one incident (extends the existing `alert_engine` topology-suppression idea + `alerts` correlation).
- **Full-stack correlation panels.** Stitch a slow span → `host_*_metrics` of its server → `snmp_*` `if_*` of its top-of-rack switch → `flow_records` conversation (reuses existing ZenPlus tables). Service-detail **infrastructure** tab.
- **Generalized silences** (audit gap: `alert_silences` is server-scoped only) → polymorphic scope/dedupe key so APM service/SLO alerts become silenceable. (Session Replay explicitly deferred to later.)

**Deliverables.** `apm_anomaly_loop` + `apm_anomaly` metric; graph-walk RCA + incident collapse; full-stack correlation panels (infrastructure tab); generalized silence model.

**Dependencies.** AM-E3 (topology), AM-E4 (error grouping), AM-E6 (alert engine + silences).

**What ships.** Auto-baselined anomaly alerts with no hand-set thresholds; an incident view that names a probable root cause and collapses the alert storm; a single pane stitching app→host→switch→flow.

**Exit criteria.**
- Anomaly baselines handle daily/weekly seasonality; deviations reported as ratios vs baseline.
- Graph-walk names the most-upstream impacted service and groups symptom alerts into one incident (deterministic, no ML required).
- Full-stack panel resolves a span to its server's `host_*` metrics, the switch's `if_*`, and the `flow_records` conversation.
- APM service/SLO alerts silenceable via the generalized polymorphic scope.

---

## 4. Four-phase plan & outcomes

### Phase 1 — MVP: OTLP ingest + tracing + service map  *(AM-E1, AM-E2, AM-E3)*
**Theme:** *"Point your OTel SDK at ZenPlus and see your services."*
- Stand up the Go collector + FastAPI fallback on 4317/4318 with `zpi_` auth; spans land in `apm_spans`.
- Trace explorer + waterfall (live + indexed).
- Service registry + always-on RED/apdex rollups + auto-derived service map.
- **Outcome:** a working OTLP-native APM MVP — services list, RED dashboards, topology, and full trace search — with rollups computed from 100% of traffic so the analytics are accurate before any sampling exists. ZenPlus is now a four-layer (network/server/synthetic/**application**) appliance.

### Phase 2 — Errors + RED analytics + alerting/SLOs  *(AM-E4, AM-E5, AM-E6, AM-E7, AM-E8)*
**Theme:** *"Now it monitors — alerts, SLOs, errors, and cost control."*
- Sentry-style error issues inbox with triage/assignee.
- Trace↔log correlation + metric→trace exemplars.
- APM alerting on the **existing** engine (latency/error/throughput/apdex) + the new SLO/error-budget engine with multi-window multi-burn paging.
- Deployment markers + version-vs-version regression compare.
- Tail/head sampling (cost) + in-pipeline PII scrubbing (compliance).
- **Outcome:** the module is a complete monitoring product — accurate RED-based alerts and burn-rate SLOs routed through existing channels with quiet hours, deduplicated error triage, deploy-regression attribution, and single-node cost under control. This is the "deal-viable APM" milestone.

### Phase 3 — RUM + synthetics + profiling + DB + eBPF  *(AM-E9, AM-E10, AM-E11)*
**Theme:** *"Full-stack coverage, front-to-back, with zero-code reach."*
- Synthetic (API + browser, multi-step, locations) reusing the poller backbone; synthetic→backend trace stitch.
- Browser RUM + Core Web Vitals (p75) + RUM→backend trace stitch.
- DB query monitoring (digests/plans, source obfuscation), eBPF zero-code coverage, and continuous profiling with span→flamegraph.
- **Outcome:** front-to-back visibility (real user → synthetic probe → backend trace → DB query → code-level flamegraph) and SDK-free coverage via eBPF — the premium depth features users associate with Datadog/New Relic, delivered on the same appliance.

### Phase 4 — SLO-grade AI root cause + advanced  *(AM-E12)*
**Theme:** *"Answers, not dashboards — and the full-stack RCA only ZenPlus can do."*
- Auto-baselining/anomaly (`apm_anomaly`).
- Causal-lite graph-walk RCA with symptom-alert collapse into incidents.
- Full-stack correlation: span → `host_*` → `snmp_*`/`if_*` → `flow_records`.
- Generalized silences across service/SLO scopes.
- **Outcome:** the differentiated end-state — auto-baselined anomaly detection, probable-cause incidents instead of alert storms, and the on-prem network+server substrate stitched into application RCA that no SaaS competitor can match cheaply.

---

## 5. Dependency / build-order diagram

```
PHASE 1 (MVP)                 PHASE 2 (monitor)            PHASE 3 (full-stack)        PHASE 4 (AI/RCA)
─────────────                 ─────────────────            ────────────────────        ────────────────

                              ┌──▶ AM-E4 errors ───────────────────────────────────────────┐
                              │     (CH apm_exceptions                                       │
                              │      + PG apm_error_issues)                                  │
                              │                                                              ▼
AM-E1  OTLP ingest  ──────────┼──▶ AM-E5 logs+correlation                              ┌───────────────┐
  (Go collector + FastAPI     │     (needs E1+E3)                                       │   AM-E12      │
   fallback; apm_spans;       │                                                         │  AI anomaly + │
   zpi_ auth; PG+CH 039)      ├──▶ AM-E8 sampling+scrubbing                             │  causal RCA + │
        │                     │     (extends E1 collector)                              │  full-stack   │
        │                     │                                                         │  correlation +│
        ├──▶ AM-E2 trace ─────┼─────────────────────────────▶ AM-E11 DB+eBPF+profiling  │  gen. silences│
        │     explorer +      │                                 (needs E1+E2)           └───────────────┘
        │     waterfall       │                                                              ▲
        │                     │                                                              │
        └──▶ AM-E3 services ──┼──▶ AM-E6 alerting+SLO ───────────────────────────────────────┤
              + RED + map     │     (RED rollups source;                                      │
              (rollups,       │      apm_slos + burn loop;                                    │
               apm_service_   │      reuse alert engine) ──┐                                  │
               graph)         │                            │                                 │
                              │                            ├──▶ AM-E9 synthetics ─────────────┘
                              ├──▶ AM-E7 deploy markers     │     (poller backbone;
                              │     (chart annotations,     │      needs E1+E6 for
                              │      version compare)       │      synthetic_down)
                              │                             │
                              └─────────────────────────────┴──▶ AM-E10 RUM+CWV
                                                                  (zpr_ beacon; needs E1)

Critical path: AM-E1 ─▶ AM-E3 ─▶ AM-E6 ─▶ AM-E12   (ingest → RED → alerting/SLO → RCA)
Parallelizable once E1+E3 land: {E4, E5, E7, E8} in P2 ; {E9, E10, E11} in P3.
```

---

## 6. Top risks & mitigations

| # | Risk | Why it matters | Mitigation |
|---|---|---|---|
| R1 | **Single-node ClickHouse can't sustain trace volume / cost blows up.** | APM is the highest-volume data on the platform; naive raw retention swamps a single-node appliance. | Three-stage decoupling (03/06): metricize at 100% pre-sampling, head floor, **tail sampling default in `prod`** (keep errors+slow+baseline). Daily partitions + `ttl_only_drop_parts=1` for atomic 7d drops; `ZSTD`/`Delta` codecs (~9–10x); dashboards read **only** rollups. Ship E8 (sampling) in Phase 2, not later. |
| R2 | **FastAPI ingest can't do high-throughput / tail sampling (audit's per-request, no-buffering finding).** | Tail sampling needs all spans of a trace on one stateful instance; FastAPI runs 2 stateless workers. | **Dedicated Go collector** is the primary front door (gRPC + `tailsamplingprocessor` + `memory_limiter`); FastAPI is an OTLP/HTTP fallback for tiny installs + the RUM beacon only, fronted by a bounded async batch writer. |
| R3 | **RED/SLO inaccuracy once raw traces are sampled away.** | Alerting/SLOs off sampled data is the classic APM bug. | Rollups + service-graph computed from **100% of spans at insert, before any sampling** (E3 before E8). Explicit RED-accuracy-under-sampling test in `08-TASK-LIST-AND-TEST-PLAN.md`. |
| R4 | **Alert dedupe across two uvicorn workers (double-paging).** | Every periodic evaluator runs in both workers. | **Dedupe-guard every raise** (`_active_alert_id`/existing-open-alert check) in `apm_alert_service.py`/`apm_slo_burn_loop`/`apm_nodata_sweeper`, per the established host/network pattern; verified by the cross-worker dedupe test. |
| R5 | **SLO burn math wrong (multi-window).** | Burn-rate paging is the modern standard; getting windows wrong erodes trust. | Implement the SRE-Workbook canonical config exactly (14.4x/1h+5m, 6x/6h+30m, 1x/3d+6h, both windows must breach); unit-test against worked examples; isolate in `apm_slo_burn_loop`. |
| R6 | **Metric-key registry drift (4 places: DB CHECK, API regex, TS registry, evaluator).** | Audit flags no single source of truth; adding a key touches 4+ spots. | Add all nine `apm_*` keys in lockstep in one PR; re-declare the **full** CHECK list per the migrate-035/037 convention; CI lint checks the three lists agree. |
| R7 | **PII leakage into ClickHouse.** | ZenPlus already handles SSH/SNMP/Windows creds; spans can carry secrets/bind params. | Scrubbing runs **in the collector before export** with **default rules on** (E8 ships default-on); source-side DB bind obfuscation; RUM form-masking default; OTTL partial-mask. |
| R8 | **CH migration silently fails on update (clickhouse_sync best-effort, never raises).** | A failed APM CH migration retries next update; ingestion may hit a missing table. | Every CH statement idempotent (`CREATE … IF NOT EXISTS`), never added to `_LEGACY_BASELINE`; ingestion code tolerates a missing-table window (the failure mode host_* was built to fix). |
| R9 | **pprof blobs / wide attribute maps bloat storage (no precedent).** | Profiles inline in CH is a new pattern. | Store pprof inline `String CODEC(ZSTD(3))` per platform convention; **flag object-storage pointer as a 03 risk** to revisit only if blob sizes prove problematic; 14d TTL caps exposure. |
| R10 | **AM-E12 scope creep (full causal AI is XL and open-ended).** | Davis-class deterministic RCA + lakehouse is a multi-quarter effort. | Ship **causal-*lite*** first: deterministic **graph-walk** over `apm_service_graph` (most-upstream impacted service, collapse symptoms) — no ML needed — plus statistical seasonality baselines. Defer Session Replay. Keep humans in the loop. |
| R11 | **Generalized silences retrofit (today server-scoped only).** | APM alerts are service/SLO-scoped and can't be silenced until E12. | Until E12, APM alerts use **quiet-hours gating only**; pin `env`/`team`/`tags` columns now (E1/E3) so the polymorphic silence/RBAC seam needs no schema churn later. |

---

## 7. KPIs & success metrics

**Adoption & onboarding**
- Time-to-first-trace after pointing an OTel SDK at ZenPlus (target: < 2 min, P1).
- Services auto-registered per install; % with non-`no_data` health (P1).
- % services covered with **zero-code** (eBPF) vs SDK instrumentation (P3, differentiation signal).

**Product correctness (the things that must not be wrong)**
- RED/apdex error vs ground truth **with tail sampling active** (target: < 1% deviation — proves the metricize-before-sample design).
- p99 visibility: latency stored as tdigest histograms, never averages (binary pass/fail).
- Trace-id lookup latency (target: sub-second at 7d retention).
- Alert dedupe correctness across workers (target: 0 double-pages).

**Operational efficiency (the alerting thesis)**
- Mean alerts per incident (target ↓ via E12 symptom collapse) and false-positive rate.
- SLO burn-alert precision/recall against the SRE-Workbook windows; alert reset time after recovery (target ~5 min via the short window).
- Error-issue dedup ratio (raw exceptions ÷ distinct issues — higher is better grouping).

**Cost & scale (single-node affordability)**
- Spans/day sustained per node at 7d TTL; ClickHouse bytes/span after `ZSTD` (target ~9–10x compression).
- % raw spans dropped by tail sampling while keeping 100% errors + slow (target 95–99% drop in `prod`).
- Retain-reason cost attribution available for every kept span (binary pass/fail).

**Differentiation (the wedge)**
- # incidents where the **full-stack** panel (span→`host_*`→`snmp_*`→`flow_records`) surfaced the cause — the on-prem RCA no SaaS competitor can match.
- # probable-cause incidents auto-named by graph-walk RCA vs operator-confirmed correct.

---

*End of roadmap. Per-epic task breakdowns and the full test plan live in `08-TASK-LIST-AND-TEST-PLAN.md`; all DDL/architecture decisions referenced here are pinned in `03-ARCHITECTURE-AND-DATA-MODEL.md`.*
