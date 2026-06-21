# Application Monitoring — Feature Specification

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the binding per-feature contract for the ZenPlus Application Monitoring (APM) module. It specifies **every feature** (F1–F23) at advanced engineering depth: each feature's MoSCoW priority, purpose, observable behavior, the data it needs (which `apm_*` ClickHouse/Postgres tables, which API routes, which background loops), its UX surface (which `/apm/*` page or `?tab=` panel), the competitor capability it achieves parity with, and the existing ZenPlus subsystem it reuses. It closes with acceptance criteria per feature, a head-to-head feature matrix versus Datadog / New Relic / Dynatrace / SigNoz, and a consolidated reuse-mapping table. Every name, route, table, metric key, and epic ID below is taken verbatim from the authoritative blueprint — this document does not invent or contradict any pinned decision. Where a feature needs a behavior the blueprint did not pin (e.g. an exact assertion grammar or an HTTP query parameter name), it is proposed here as a v1 default and flagged as such. For the full DDL see `03-ARCHITECTURE-AND-DATA-MODEL.md`; for collector internals and the OTLP wire protocol see `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md`; for page-level UX see `06-UI-UX-AND-DASHBOARDS.md`.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — what ZenPlus already has vs. the APM gaps, with the reuse map
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — Go-collector decision, pipeline diagram, full ClickHouse + Postgres DDL, migration numbers
- `04-FEATURE-SPECIFICATION.md` — **this document**
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP protocol, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing pipeline, collector internals
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, components, react-query keys
- `07-ROADMAP-AND-EPICS.md` — the 4 phases and 12 epics (AM-E1..AM-E12) expanded
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the test plan

---

## 1. How to read this specification

### 1.1 MoSCoW priority and phase mapping

| Priority | Meaning | Phase | Epics |
|---|---|---|---|
| **MUST** | v1 cannot ship without it; the product is not "APM" otherwise | Phase 1–2 | AM-E1 … AM-E8 |
| **SHOULD** | High value, ships in the first follow-on wave; product is competitive without it but not complete | Phase 3 | AM-E9 … AM-E11 |
| **COULD** | Differentiating or premium; ships when the substrate is proven | Phase 4 | AM-E12 |
| **WON'T (v1)** | Explicitly out of scope for v1, named so nobody builds it early | — | (Session Replay, GenAI summarization) |

The feature IDs **F1–F23** are the blueprint's catalog (§5 of the blueprint). They are stable identifiers; cite them verbatim. Each feature maps to one or more epics **AM-E1 … AM-E12** (blueprint §9), expanded in `07-ROADMAP-AND-EPICS.md`.

### 1.2 The load-bearing principle every feature obeys

Three storage stages are decoupled (blueprint §6.1) and **every analytic feature reads the right stage**:

```
  100% of spans ─┬─► METRICIZE (always-on)  ─► apm_span_metrics_5m/_1h, apm_service_graph
                 │     RED + apdex + edges, computed BEFORE sampling
                 │     ── dashboards, SLOs, alerts read ONLY these ──
                 │
                 ├─► HEAD SAMPLE (bounded)   ─► probabilistic floor per apm_environments.sampling_target_tps
                 │
                 └─► TAIL SAMPLE (value-aware)─► apm_spans (raw, 7d TTL)
                       keep 100% errors + slow + baseline%, tag zp.retain_reason
                       ── trace waterfall, span search read this ──
```

**Rule for authors and implementers:** a feature that needs *accuracy* (service health, error rate, apdex, SLO budget, latency alert) reads the rollups. A feature that needs an *individual example* (open this trace, show this exception, replay this session) reads the raw tables. No feature alerts off sampled raw spans. This is the Datadog "metricize before you sample" lesson and it is non-negotiable.

### 1.3 Conventions used in each feature block

Every feature is specified with the same eight fields:

- **Priority / Epic** — MoSCoW + AM-E*.
- **Purpose** — the user problem.
- **Behavior** — what the system does, in order; the algorithm where one exists.
- **Data needed** — CH tables, PG tables, MVs, loops.
- **API surface** — exact routes from blueprint §2.3 (control plane under `/api/v1/apm`, data plane at root).
- **UX surface** — exact route / `?tab=` key from blueprint §2.2.
- **Competitor parity** — the closest competitor capability (the "Match" column).
- **Reuse** — the existing ZenPlus subsystem leveraged.
- **Acceptance criteria** — testable exit conditions (cross-referenced by `08-TASK-LIST-AND-TEST-PLAN.md`).

---

## 2. MUST features (Phase 1–2)

### F1 — OTLP ingestion

**Priority / Epic:** MUST · AM-E1.

**Purpose.** Be a drop-in OpenTelemetry backend so a customer points an existing OTel SDK or Collector at ZenPlus with **zero re-instrumentation**. This is the wedge: the defensible value is correlation/topology/RED/SLO/RCA on top of ClickHouse, not a proprietary agent.

**Behavior.**
- Accept OTLP/**gRPC** on `:4317` and OTLP/**HTTP** on `:4318` for **traces, metrics, logs** (profiles arrive in Phase 3 via F18). The gRPC path and high-volume HTTP path are served by the **dedicated Go collector**; the FastAPI router `apm_ingest.py` provides an OTLP/HTTP-only fallback for small/single-binary installs and serves the RUM beacon.
- HTTP paths are the OTLP-fixed `POST /v1/traces`, `POST /v1/metrics`, `POST /v1/logs` (and `POST /v1development/profiles` in P3), accepting `application/x-protobuf` and `application/json` (lowerCamelCase, hex `traceId`/`spanId`).
- Honor OTLP success semantics exactly: full success → HTTP 200 with `partial_success` unset; **partial success** → 200 with `partial_success{ rejected_spans, error_message }` populated (client must NOT retry the accepted portion); **retryable** failures → 429/502/503/504 + `Retry-After`; **non-retryable** → 400. gRPC mirrors this with `Unavailable`/`Aborted` (retryable) vs `InvalidArgument`/`PermissionDenied` (non-retryable) + `RetryInfo`.
- Authenticate every request with `Authorization: Bearer zpi_...` validated by constant-time `hmac.compare_digest(sha256(bearer), apm_ingest_keys.key_hash)` (forked from `agents.py` `_authenticate`). The Go collector validates the same key against the same table via a read-through cache with periodic refresh.
- Write **identical ClickHouse row shapes** from both the Go collector and the FastAPI fallback so the query layer is path-agnostic.
- Apply backpressure: the Go collector's `memory_limiter` (first processor) + `batch` shed load under pressure; the FastAPI fallback wraps `get_clickhouse_client()` in a bounded async queue with flush-by-size/interval, finally wiring the historically-unused `backpressure` response field.

**Data needed.** CH: `apm_spans`, `apm_traces_resource`, `apm_logs`, `apm_span_metrics_5m/_1h` (via MVs). PG: `apm_ingest_keys`, `apm_enrollment_tokens`, `apm_environments`. Loop: none (ingest is request-driven).

**API surface.** Data plane (root prefix): `POST /v1/traces`, `POST /v1/metrics`, `POST /v1/logs`; gRPC `:4317`. Control plane: `GET/POST/DELETE /api/v1/apm/ingest-keys`.

**UX surface.** `/apm/settings` (`ApmSettingsPage.tsx`) — ingest-key issue/rotate/revoke, copy-once secret, per-env scope, OTLP endpoint cheat-sheet.

**Competitor parity.** OTel/SigNoz collector; Datadog Agent + New Relic OTLP ingest as the buffering/control point between SDKs and backend.

**Reuse.** New Go collector + FastAPI fallback; `agents.py` `_sha256`/`_new_api_key`/`_strip_bearer`/`_authenticate` (prefix `zpa_`→`zpi_`); `get_clickhouse_client()` singleton; `agent_enrollment_tokens` token model.

**Acceptance criteria.**
- An unmodified upstream OTel SDK (Java + Python) exporting OTLP/gRPC and OTLP/HTTP lands spans in `apm_spans` with correct `trace_id`/`span_id`/`parent_span_id`/`service_name`.
- A malformed/oversized batch returns `partial_success` with a non-retryable rejected count, not a 5xx; a downstream-CH outage returns 503 + `Retry-After` and the SDK retries.
- A request with a revoked or unknown `zpi_` key returns 401; constant-time comparison verified (no early-exit timing leak).
- Both collector and FastAPI fallback produce byte-identical row shapes for the same OTLP payload (conformance test in `08`).

---

### F2 — Distributed tracing + waterfall / flame

**Priority / Epic:** MUST · AM-E2.

**Purpose.** Let an engineer search for and open any individual request, see the full span tree, and find the slowest hop and the failing span.

**Behavior.**
- **Trace search** (`/apm/traces`) supports two modes, mirroring Datadog Live vs Indexed:
  - **Live mode** — recent spans regardless of retention decision (the rolling head of `apm_spans`), so a slow trace is findable in the seconds before tail sampling may drop it.
  - **Indexed mode** — spans retained in `apm_spans` (after tail sampling), filterable by `service_name`, `name` (operation), `env`, `status_code`, `http_route`, `http_status_code`, `db_system`, duration range, and arbitrary `attributes_string[k]=v`.
- Filters hit indexed/promoted columns (`http_*`, `db_*`, `rpc_*`) and bloom skip indexes (`idx_span_attr_keys`, `idx_span_attr_vals`, `idx_http_route`, `idx_duration`), never a full attribute-map scan. Resource-attribute filters resolve fingerprints in `apm_traces_resource` via a CTE, then `... WHERE resource_fingerprint GLOBAL IN (cte)`.
- **Trace open** (`/apm/traces/:traceId`) loads the full span set via `WHERE trace_id = ...` on the `idx_trace_id` bloom (sub-second, no scan), reconstructs the parent→child tree from `span_id`/`parent_span_id`, and renders:
  - a **waterfall** (horizontal Gantt bars, x = time, depth = nesting, width = duration, color = `span_kind_str`, red marker on `has_error=1`),
  - a **flame** view (self-time roll-up by operation),
  - span detail (attributes, `events_*`, `links_*`, `status_message`, `db_statement` digest), and one-click pivots to **logs** (F6), **profiles** (F18), and the **exception** (F5) sharing this `trace_id`.
- W3C `traceparent`/`tracestate` propagation is assumed end-to-end (SDK responsibility); ZenPlus stitches by shared `trace_id`. Span links (`links_trace_id`/`links_span_id`) render async/queue causality where strict parent-child is wrong.

**Data needed.** CH: `apm_spans` (raw), `apm_traces_resource`. No PG.

**API surface.** `GET /api/v1/apm/traces` (search; `mode=live|indexed` proposed param), `GET /api/v1/apm/traces/{trace_id}` (full span tree).

**UX surface.** `/apm/traces` (`TraceExplorerPage.tsx`), `/apm/traces/:traceId` (`TraceWaterfallPage.tsx`), and the service-detail `?tab=traces` panel. The waterfall and flame are **new** components under `dashboard/src/components/apm/` (no existing primitive).

**Competitor parity.** Datadog Trace Explorer (Live/Indexed + waterfall/flame); New Relic distributed tracing + transaction traces.

**Reuse.** `apm_spans` schema; `get_clickhouse_client()` parameterized `%(name)s` reads; `TimeRangePicker`/`useTimeRange`; the Servers list filter/URL-param skeleton for the explorer table.

**Acceptance criteria.**
- Opening a known `trace_id` returns the complete span tree in < 1 s on a single-node appliance at target volume; bars nest correctly and the critical path is highlightable.
- Live mode surfaces a trace within seconds of ingest even when the tail sampler would drop it; indexed mode returns only retained traces.
- An attribute filter `attributes_string['user.tier']='premium'` returns matches via the bloom index without a full-table scan (verified via `EXPLAIN`/query log).

---

### F3 — Service registry + RED analytics + apdex

**Priority / Epic:** MUST · AM-E3.

**Purpose.** Turn raw spans into the golden-signal service-health view (rate, errors, duration percentiles) plus an Apdex satisfaction score, so a service's health is legible at a glance and accurate even when raw traces are sampled away.

**Behavior.**
- A **service** becomes a first-class monitored entity in PG `apm_services` (keyed `UNIQUE(name, env_id)`, `name == service.name`), alongside devices and servers. The registry carries `language`, `team`, `owner`, `repo_url`, `tags`, and **denormalized last-seen RED + health** (`last_rps`, `last_error_rate`, `last_p95_ms`, `last_apdex`, `health ∈ {healthy, degraded, critical, no_data}`) maintained by the evaluator loop so the list page renders without touching ClickHouse on every paint.
- **RED rollups** are computed from **100% of spans at insert** by `apm_span_metrics_5m_mv` / `_1h_mv` into SummingMergeTree tables keyed `(service_name, operation, span_kind, env, status_code, timestamp)`:
  - **Rate** = `request_count` / window.
  - **Errors** = `error_count / request_count` (where `error_count = countIf(has_error=1)`).
  - **Duration** = `quantilesTDigestMerge(0.5,0.75,0.9,0.95,0.99)(duration_state)` (ms), plus `duration_min/max/sum`.
- **Apdex** per service with a per-service threshold T (default T = 500 ms, configurable): `apdex = (satisfied + tolerating/2) / total` where `satisfied = countIf(duration ≤ T)`, `tolerating = countIf(T < duration ≤ 4T)`, `frustrated = countIf(duration > 4T)`. Buckets computed from the rollup, not raw.
- **Operations/endpoints** breakdown: top operations per service by RED, from the same rollup grouped by `operation`.
- `health` derivation (proposed v1 thresholds, configurable): `critical` if error_rate ≥ 5% or p95 ≥ 2× the service's 7-day baseline; `degraded` if error_rate ≥ 1% or apdex < 0.85; `no_data` if no spans in the last evaluation window; else `healthy`.

**Data needed.** CH: `apm_span_metrics_5m`, `apm_span_metrics_1h` (+ MVs). PG: `apm_services`, `apm_environments`. Loop: the `apm_alert_evaluator_loop` (or a dedicated `apm_health_refresh` step) refreshes the denormalized RED/health columns each cycle.

**API surface.** `GET /api/v1/apm/services` (list + RED summary + facets, mirrors `/servers/facets`), `GET /api/v1/apm/services/{id}`, `GET /api/v1/apm/services/{id}/red` (time-series), `GET /api/v1/apm/services/{id}/operations`, `GET /api/v1/apm/services/{id}/apdex`.

**UX surface.** `/apm/services` (`ServicesPage.tsx` — chips for health, filters by env/language/team/tag, sortable columns Service/p95/error-rate/throughput/apdex/last-deploy), `/apm/services/:id` (`ServiceDetailPage.tsx`) tabs `?tab=overview` and `?tab=performance`, plus the `/apm` fleet overview.

**Competitor parity.** New Relic golden signals + APM 360 entity summary; Datadog trace metrics + Service Catalog.

**Reuse.** `apm_span_metrics_*` MVs; ping/host tiered raw→5m→1h rollup idiom; Servers list/detail UI pattern (`ServersPage.tsx`→`ServicesPage.tsx`, `ServerDetailPage.tsx`→`ServiceDetailPage.tsx`); `KpiTile`/`PanelHeader`/`MetricChartCard` from `servers/shared.tsx`.

**Acceptance criteria.**
- p95 read from `apm_span_metrics_5m` matches the p95 over raw `apm_spans` within tdigest tolerance **even after raw spans are tail-sampled** (RED-accuracy-under-sampling test, `08`).
- Apdex for a service equals the hand-computed `(sat + tol/2)/total` for a seeded fixture.
- Service list renders RED/health for ≥ 200 services without per-row ClickHouse queries (served from denormalized PG columns).

---

### F4 — Service map / topology

**Priority / Epic:** MUST · AM-E3.

**Purpose.** Auto-derive the dependency graph (who calls whom) with RED on every edge, so bottlenecks, fan-out, and error propagation are visible without anyone drawing a diagram.

**Behavior.**
- **Edges** come primarily from the Go collector's **servicegraph connector**, which pairs `CLIENT` and `SERVER` spans sharing a `trace_id` and emits per-pair `request_count`, `error_count`, `duration_sum_ms`, `sample_count` into `apm_service_graph` (SummingMergeTree, `(client_service, server_service, env, timestamp)`). Fallback when the connector is unavailable: a ClickHouse MV pairing `span_kind_str='CLIENT'` spans with their child `SERVER` spans by `trace_id`/`parent_span_id`.
- **Nodes** are the distinct services from `apm_services`, colored by `health`; edges are colored/weighted by their RED (request rate as width, error rate as color). "Inferred" nodes for uninstrumented dependencies (external HTTP host, DB, queue) are synthesized from `CLIENT`-span targets that have no matching `SERVER` service (Datadog inferred-services idiom).
- Clicking an edge drills into the RED time-series for that dependency and a filtered trace list; clicking a node opens the service detail.

**Data needed.** CH: `apm_service_graph` (+ MV/connector), `apm_spans` (drill-down). PG: `apm_services` (node set).

**API surface.** `GET /api/v1/apm/service-map` (nodes + edges with RED).

**UX surface.** `/apm/service-map` (`ServiceMapPage.tsx`) and service-detail `?tab=dependencies`. Rendered with an **echarts `graph` layout** (new component under `components/apm/`); no reusable graph primitive exists today (the Manual Maps canvas is bespoke).

**Competitor parity.** Datadog Service Map; Dynatrace Smartscape / AppDynamics Flow Maps; New Relic Service Maps.

**Reuse.** `apm_service_graph` rollup; echarts-for-react already in the dashboard; `apm_services` registry for nodes.

**Acceptance criteria.**
- For a seeded 3-tier app (`web → checkout → payments`, `checkout → db`), the map shows the correct directed edges with non-zero RED and an inferred `db` node.
- Edge RED matches `apm_service_graph` aggregates for the selected window.
- Map renders ≤ 1 s for ≤ 100 nodes / ≤ 500 edges.

---

### F5 — Error tracking / issues

**Priority / Epic:** MUST · AM-E4.

**Purpose.** Collapse the flood of individual exceptions into a small set of deduplicated **issues** with first/last-seen, counts, affected versions, trace linkage, and a triage workflow — a Sentry/New-Relic-Errors-Inbox-grade surface.

**Behavior.**
- Each exception span event is stored in CH `apm_exceptions` with a **`group_id` fingerprint** computed by the collector: a stable hash of `exception_type` + normalized stack frames (strip line numbers' noise: UUIDs, hex addresses, ephemeral ports, memory addresses, autogenerated lambda names), so the same logical error collapses to one group across hosts and versions.
- **Triage state** (the only mutable part) lives in PG `apm_error_issues` keyed by `group_id`: `status ∈ {unresolved, resolved, resolved_in_version, ignored}`, `assignee`, and a `first_seen`/`last_seen` mirror. Occurrence counts, trend sparklines, affected `service_version` list, and per-version error rate come from CH at read time.
- The **inbox** (`/apm/errors`) lists issues with count, trend (rate vs count), first/last seen, affected services/versions, and status; filterable by service/env/status/assignee. The **issue detail** (`/apm/errors/:id`) shows the representative stack trace, the occurrence timeline, the linked `trace_id` (one-click to F2 waterfall), correlated logs (F6), and **deployment markers** (F9) so a spike maps to the version that introduced it (release health).
- Triage actions `PATCH /api/v1/apm/errors/{group_id}` set status/assignee. `resolved_in_version` auto-reopens (regression) if the `group_id` recurs in a later `service_version`.

**Data needed.** CH: `apm_exceptions` (30d, ordered `(service_name, group_id, ts_bucket, timestamp)`, bloom `idx_exc_group`/`idx_exc_trace`). PG: `apm_error_issues`. Optional alert: a `new error issue` / `issue regression` event can ride F8.

**API surface.** `GET /api/v1/apm/errors` (grouped), `GET /api/v1/apm/errors/{group_id}` (detail + occurrences), `PATCH /api/v1/apm/errors/{group_id}` (status/assignee).

**UX surface.** `/apm/errors` (`ErrorsInboxPage.tsx`), `/apm/errors/:id` (`ErrorIssueDetailPage.tsx`), service-detail `?tab=errors`.

**Competitor parity.** New Relic Errors Inbox / Sentry issue grouping (fingerprint, normalization, triage statuses, assignment, release health).

**Reuse.** `apm_exceptions` (CH) + `apm_error_issues` (PG); the Alerts UI/list conventions; the Servers list filter+table skeleton for the inbox.

**Acceptance criteria.**
- Two exceptions of the same type with different UUIDs/line-noise in the stack collapse to one `group_id`; two genuinely different exceptions do not.
- A `resolved_in_version` issue auto-reopens when it recurs in a newer `service_version`.
- Issue detail links to a real `trace_id` and the correlated logs for that trace.

---

### F6 — Trace ↔ log ↔ metric correlation

**Priority / Epic:** MUST · AM-E5.

**Purpose.** One-click pivoting across pillars: from a trace to its exact logs, from a log to its trace, and from a metric spike to a representative trace — the thing that makes the module feel cohesive rather than four silos.

**Behavior.**
- OTLP **logs** land in CH `apm_logs` carrying `trace_id`/`span_id` (injected by the SDK in an active span). The **trace↔log pivot** is `WHERE trace_id = ...` on `idx_log_trace`; the **log→trace** pivot opens F2 for that `trace_id`. Full-text log search uses the `tokenbf_v1` index on `body`.
- **Metric → trace exemplars:** RED rollups carry (or can resolve) a representative `trace_id` per high-latency/error bucket so clicking a latency spike on a chart jumps to a real slow/error trace (Datadog/Tempo exemplar idiom). v1 implementation: on chart-point click, the UI issues an indexed trace query scoped to the bucket's `service_name`+`operation`+time window+`has_error`/duration band and returns the top exemplar.
- The shared join key throughout is `trace_id`/`span_id` plus the resource triple `service_name`/`env`/`service_version` (the ZenPlus equivalent of Unified Service Tagging), so a single env/service filter works across spans, logs, exceptions, and rollups.

**Data needed.** CH: `apm_logs` (14d), `apm_spans`, `apm_exceptions`, rollups. No new PG.

**API surface.** `GET /api/v1/apm/rum/...` shares the pattern; logs are queried through the trace/service detail endpoints (proposed `GET /api/v1/apm/services/{id}/logs?trace_id=` and a logs query under the traces router). Exemplar resolution rides `GET /api/v1/apm/traces`.

**UX surface.** Service-detail `?tab=logs`; the **Logs** panel on the trace waterfall (F2); exemplar pivot from any RED chart (F3) and SLO burn chart (F7).

**Competitor parity.** Datadog/New Relic logs-in-context (trace_id injection); OTel/SigNoz exemplar linking.

**Reuse.** `apm_logs`; shared `trace_id`; the host metric query-service granularity-selection pattern for the logs reader.

**Acceptance criteria.**
- From a trace waterfall, the Logs panel shows exactly the log lines with that `trace_id`; from a log line, the trace opens.
- A latency-spike click on a service RED chart opens a trace in the same bucket whose latency is in the spiking band.

---

### F7 — SLO/SLI + error-budget burn alerting

**Priority / Epic:** MUST · AM-E6.

**Purpose.** Replace raw threshold alerting with user-impact-aware reliability targets: define SLIs/SLOs, compute error budgets, and **page on burn rate** using multi-window multi-burn-rate (the Google SRE Workbook standard). The audit confirms ZenPlus has **no SLO/error-budget/burn primitives today** — this feature builds them.

**Behavior.**
- **SLO objects** in PG `apm_slos`: `sli_type ∈ {availability, latency, error_rate, custom}`, `target` (e.g. 99.9), `window_days ∈ {7,30,90}`, `latency_threshold_ms` (for latency SLIs), scope = service or `operation`, `burn_alert_enabled`, `notify_channels`.
- **`apm_slo_burn_loop`** (new background task, registered in `main.py` startup alongside the host/network loops, dedupe-guarded across both uvicorn workers) computes, per SLO and per window, from `apm_span_metrics_5m`:
  - SLI: availability/error → `1 − error_count/request_count`; latency → `countIf(duration ≤ latency_threshold_ms)/request_count`.
  - error budget remaining = `(SLI − target)/(1 − target)`.
  - **burn rate** = budget consumed per unit time relative to the SLO (burn rate 1 exhausts the budget exactly at window end; 14.4 exhausts it 14.4× faster).
- **Multi-window multi-burn-rate** (canonical 99.9% config): **Page** at 14.4× over 1h (short 5m, both must breach, ~2% budget); **Page** at 6× over 6h (short 30m, ~5%); **Ticket** at 1× over 3d (short 6h, ~10%). The short window cuts reset time so the alert clears ~5 min after recovery. Severity maps to channel routing — page → PagerDuty/SMS, ticket → email/Slack.
- The loop either emits an `apm_slo_burn` value the alert engine thresholds (F8) or raises directly via `dispatch_to_channels`; burn alerts honor quiet hours (`notifications_allowed`) and enforce `cooldown`/`max_repeat` (defined-but-unenforced today).
- `GET /api/v1/apm/slos/{id}/budget` returns per-window burn for the detail chart (budget-remaining gauge + burn-rate-by-window strip).

**Data needed.** CH: `apm_span_metrics_5m` (SLI source). PG: `apm_slos`. Loop: `apm_slo_burn_loop`. Reuse: `alert_rules`/`alerts`, `dispatch_to_channels`, `notifications_allowed`.

**API surface.** `GET/POST/PUT/DELETE /api/v1/apm/slos`, `GET /api/v1/apm/slos/{id}/budget`.

**UX surface.** `/apm/slos` (`SlosPage.tsx` — list + error-budget status), `/apm/slos/:id` (`SloDetailPage.tsx` — burn chart, budget gauge, window table), service-detail `?tab=slos`.

**Competitor parity.** Datadog/New Relic SLOs + error budgets; Google SRE Workbook multi-window multi-burn-rate.

**Reuse.** `apm_slos` (new) + the existing alert engine, `dispatch_to_channels`, quiet-hours; the service-checks SLA math (`get_service_sla`) as a reference for window aggregation.

**Acceptance criteria.**
- For a seeded error-rate series, the computed burn rate and budget-remaining match a hand-derived reference (SLO-burn-math test, `08`).
- A fast-burn breach (14.4×/1h with 5m short window) pages within one evaluation cycle and **clears ~5 min after recovery**, not at window-end.
- Burn alerts are suppressed during quiet hours but the alert row is still recorded; `cooldown`/`max_repeat` prevent re-notify storms.

---

### F8 — APM alerting integration

**Priority / Epic:** MUST · AM-E6.

**Purpose.** Make every APM signal alertable through the **existing unified alert engine** — no parallel alert stack — by registering `apm_`-prefixed metric keys that ride `alert_rules`/`alerts`, multi-condition AND/OR, per-rule channels, quiet hours, and the Alerts UI.

**Behavior.**
- New metric keys (added to the `alert_rules` `metric` CHECK in `migrate-039-apm.sql` re-declaring the full whitelist, to the Pydantic `_CONDITION_METRICS` regex, and to the TS metric registry in `AlertRuleFormDialog.tsx`): `apm_latency_p50`, `apm_latency_p95`, `apm_latency_p99`, `apm_error_rate`, `apm_throughput`, `apm_apdex`, `apm_slo_burn`, `apm_synthetic_down`, `apm_anomaly`.
- **Pull-path** metrics (latency pXX, error_rate, throughput, apdex): a new evaluator `apm_alert_service.py` (cloned from `network_alert_service.py`) runs every 60s, selects `enabled` rules `WHERE metric IN (apm_*)`, reads values from `apm_span_metrics_5m` (`quantilesTDigestMerge` for p95, `error_count/request_count`, `request_count`/window, apdex buckets), and reuses `_cmp`/`_active_alert_id`/`_raise`/`_resolve`/`_notify`. Registered as `apm_alert_evaluator_loop` in `main.py` startup, **dedupe-guarded** (runs in both workers).
- **Push-path** metrics: `apm_synthetic_down` rides the poller's existing status path — the synthetic checker emits a status transition to `/api/v1/alert-engine/evaluate-service` (the service-check path), so no new evaluator is needed. Push-path latency/error values feed `alert_engine.py`'s `metric_values` dict and are consumed by `_conditions_match` with **zero engine changes**.
- Multi-condition rules work unchanged: e.g. `apm_latency_p95 > 800 AND apm_error_rate > 0.02` combined by `condition_logic`.
- `ctx` to `dispatch_to_channels` carries APM `details[]` rows (`('Service','checkout')`, `('p95','820ms')`, `('SLO burn','14.4x')`), already wired through `build_alert_email_html`. The **PagerDuty `dedup_key`** is extended to carry the APM service/SLO id so APM incidents dedupe correctly.

**Data needed.** CH: `apm_span_metrics_5m`. PG: `alert_rules`/`alerts` (reused, no parallel tables). Loop: `apm_alert_evaluator_loop` + `apm_nodata_sweeper` (no-data/reporting-stopped, modeled on `health_sweeper_loop`).

**API surface.** Existing `/api/v1/alert-rules` (extended metric allowlist) or an `apm_alert_rules.py` router with `metric LIKE 'apm\_%'` (the established per-domain pattern); `/api/v1/alert-engine/evaluate-service` for synthetic push.

**UX surface.** Existing Alerts UI (rules, channels, gateways) + the APM metric entries in `AlertRuleFormDialog.tsx`; APM alerts surface in the shared alert center automatically once written to `alerts`.

**Competitor parity.** All vendors' monitor/alert engines on golden-signal metrics.

**Reuse.** `alert_rules`/`alerts`, `conditions[]` AND/OR, `dispatch_to_channels`, `notifications_allowed`, `build_alert_email_html`, quiet-hours; `network_alert_service.py` as the evaluator template.

**Acceptance criteria.**
- A rule `apm_latency_p95 > 800ms` raises exactly one alert across both uvicorn workers (cross-worker dedupe test, `08`) and resolves when p95 recovers.
- A compound rule (p95 AND error_rate) fires only when both conditions breach.
- `apm_synthetic_down` raises via the existing service-status push path with no new evaluator.
- APM alert email renders the APM `details[]` rows; PagerDuty dedups on the APM entity id.

---

### F9 — Deployment / change tracking

**Priority / Epic:** MUST · AM-E7.

**Purpose.** Treat deploys as first-class change events overlaid on every chart, with version-vs-version comparison, so a golden-signal regression maps to the build that caused it (the cheap precursor to full RCA).

**Behavior.**
- Deploy markers in PG `apm_deployments` (`service`, `version`, `sha`, `ts`, optional env), created via `POST /api/v1/apm/deployments` from CI/CD (GitHub Actions/Jenkins) or the UI. Each retained span carries `deployment_id` (UUID, zeros if none) so spans attribute to a deploy.
- Markers render as `ReferenceLine`s on RED/latency/error charts (recharts, the existing convention). The error inbox (F5) and SLO charts (F7) annotate the same markers.
- **Version-vs-version compare:** select two `service_version`s and the system renders side-by-side RED (error rate / p95 / throughput) from `apm_span_metrics_5m` filtered by `env`/`service_version`, flagging a bad deploy when the newer version's error rate or p95 regresses beyond a threshold.

**Data needed.** CH: `apm_span_metrics_5m` (by `service_version`), `apm_spans.deployment_id`. PG: `apm_deployments`.

**API surface.** `GET/POST /api/v1/apm/deployments`.

**UX surface.** Service-detail `?tab=deployments` (marker list + version compare); markers overlaid on `?tab=performance` and SLO/error charts.

**Competitor parity.** Datadog/New Relic change tracking + deployment markers + version comparison.

**Reuse.** `apm_deployments` (new); recharts `ReferenceLine` markers (existing chart convention); `apm_span_metrics_5m` by version.

**Acceptance criteria.**
- A `POST /deployments` marker appears on the service's performance chart at the correct timestamp.
- Comparing v1.2.0 vs v1.3.0 shows correct per-version RED deltas and flags a seeded regression.

---

### F10 — Sampling pipeline (head + tail)

**Priority / Epic:** MUST · AM-E8.

**Purpose.** Bound trace storage cost while keeping every high-value trace (errors, slow, business-critical), and never let sampling distort dashboards/alerts.

**Behavior.**
- **Metricize first (100%):** RED rollups and service-graph edges are computed before any sampling, so accuracy is sampling-independent (the §1.2 rule).
- **Head sampling (bounded floor):** probabilistic sampling targets ~`apm_environments.sampling_target_tps` traces/sec/service-equivalent for raw retention (default 10), cheap, keeps a representative baseline.
- **Tail sampling (value-aware, default for `prod`):** the Go collector `tailsamplingprocessor` buffers a trace for `decision_wait` ~5s (requires all spans of a trace on one collector instance — drives trace-ID-aware routing) and applies ordered policies: **keep 100% errors** (`status_code=ERROR`/`has_error`), **keep slow** (latency outliers beyond a pXX threshold), **keep a 1–5% probabilistic baseline**, plus string/tag policies for business-critical traces. Everything else is dropped from `apm_spans`.
- Every retained span is tagged with a retention reason in `attributes_string['zp.retain_reason'] ∈ {auto, error, slow, rule, baseline}` for cost attribution (Datadog `ingestion_reason` idiom).
- Config lives in PG `apm_sampling_rules`; `apm_environments.sampling_target_tps` sets the per-env head floor.

**Data needed.** CH: `apm_spans` (`zp.retain_reason` tag). PG: `apm_sampling_rules`, `apm_environments`. Collector: `tailsamplingprocessor` + `probabilistic_sampler`.

**API surface.** `GET/PUT /api/v1/apm/sampling-rules`.

**UX surface.** `/apm/settings` — sampling rules editor (per-env target TPS, tail policies, baseline %), retain-reason breakdown for cost visibility.

**Competitor parity.** Datadog ingestion control + retention filters; OTel `tailsamplingprocessor`; New Relic Infinite Tracing tail sampling.

**Reuse.** Go collector `tailsamplingprocessor` (upstream); `apm_sampling_rules` (new) + `apm_environments`.

**Acceptance criteria.**
- With tail sampling at a 1% baseline, **100% of error and slow traces** are retained while routine traffic is dropped (sampling-correctness test, `08`).
- RED p95/error_rate from rollups stay accurate (within tdigest tolerance) despite aggressive raw-span dropping.
- Every retained span carries a valid `zp.retain_reason`; the settings page shows the retain-reason mix.

---

### F11 — PII scrubbing in the pipeline

**Priority / Epic:** MUST · AM-E8.

**Purpose.** Redact sensitive data **in the collector before storage** so secrets/PII never reach ClickHouse — essential given ZenPlus already handles SSH/SNMP/Windows credentials.

**Behavior.**
- Scrubbing runs in the collector between receive and export (attribute drop/hash, allow-list + value masking, span/log drop filters, OTTL-style regex partial masking). Config in PG `apm_scrubbing_rules`.
- **Default rules ship ON:** scrub `Authorization`, `Cookie`, `password`, `token`; obfuscate bind parameters in `db_statement` **source-side** (query digests only — literals never leave the app host, Datadog DBM idiom); regex-mask emails and card numbers. RUM masks form input by default.
- Placed before routing/export so all downstream steps see clean data.

**Data needed.** PG: `apm_scrubbing_rules`. Collector: attribute/redaction/filter/transform-OTTL processors.

**API surface.** `GET/PUT /api/v1/apm/scrubbing-rules`.

**UX surface.** `/apm/settings` — scrubbing-rules editor (attribute allow/deny, regex masks, defaults toggle).

**Competitor parity.** OTel collector PII processors; Datadog DBM source-side obfuscation.

**Reuse.** Go collector processors; `apm_scrubbing_rules` (new).

**Acceptance criteria.**
- A span carrying `http.request.header.authorization` and a SQL statement with literals is stored with the header dropped and the statement reduced to a digest.
- A custom regex rule (e.g. mask SSNs) masks matching values in attributes/log bodies before insert.
- Disabling defaults is an explicit, audited action.

---

### F12 — Ingest-key auth + enrollment

**Priority / Epic:** MUST · AM-E1.

**Purpose.** Secure, rotate, and scope the keys that authorize ingestion — SDK/collector keys (secret) and RUM keys (public, origin-scoped) — mirroring the proven agent enrollment model.

**Behavior.**
- **SDK/collector keys** (`zpi_` + `token_urlsafe(32)`): returned once, stored only as `sha256` hash + 16-char prefix in `apm_ingest_keys`; runtime auth via constant-time `hmac.compare_digest`. Enrollment via one-time hashed `apm_enrollment_tokens` (`max_uses`, `expires_at`, `revoked_at`, `consumed_ip`). The Go collector validates the same key (read-through cache).
- **RUM keys** (`zpr_`): public, origin-scoped — no secret in client JS. Auth = a distinct public key + CORS `origin_allowlist` (`apm_ingest_keys.origin_allowlist`) + per-origin rate-limiting on `/api/v1/apm/rum/ingest`.
- Keys are env-scoped (`env_id`); `kind ∈ {sdk, rum}`; rotate/revoke supported.

**Data needed.** PG: `apm_ingest_keys`, `apm_enrollment_tokens`, `apm_environments`.

**API surface.** `GET/POST/DELETE /api/v1/apm/ingest-keys`.

**UX surface.** `/apm/settings` — key management (issue/rotate/revoke, copy-once, env scope, RUM origin allowlist).

**Competitor parity.** Datadog/New Relic API keys; RUM public client tokens with origin scoping.

**Reuse.** `agents.py` `_authenticate`/`_new_api_key`/`/enroll`; `agent_enrollment_tokens` token model; `apm_ingest_keys`/`apm_enrollment_tokens` (new).

**Acceptance criteria.**
- A `zpi_` key authenticates collector/SDK ingest; a revoked key returns 401 immediately.
- A `zpr_` key only accepts beacons from an allow-listed `Origin` with correct CORS preflight; other origins are rejected and rate-limited.
- The plaintext key is shown exactly once; only the hash+prefix persist.

---

### F13 — Dashboards + APM overview

**Priority / Epic:** MUST · AM-E3 (overview) / cross-cutting.

**Purpose.** A fleet APM landing page and saved custom dashboards so operators and leadership see the application layer in one pane.

**Behavior.**
- `/apm` overview: fleet RED summary, worst-N services by error rate / p95 / burn, recent deploys, open error issues, synthetic/SLO status tiles — all from rollups (never raw spans).
- Saved dashboards in PG `apm_dashboards` (widget definitions over RED/SLO/RUM/synthetic series), built from recharts (trend panels) and echarts (dense series), scoped by env/time via `TimeRangePicker`.

**Data needed.** CH: rollups (`apm_span_metrics_*`, `apm_rum_vitals_5m`, `apm_synthetic_results_5m`, `apm_service_graph`). PG: `apm_dashboards`.

**API surface.** Read endpoints across `/api/v1/apm/*`; `apm_dashboards` CRUD (proposed under the `apm.py` router).

**UX surface.** `/apm` (`ApmOverviewPage.tsx`); dashboards embedded across pages.

**Competitor parity.** All vendors' APM overview + custom dashboards.

**Reuse.** recharts/echarts; `apm_dashboards` (new); `TimeRangePicker`; Servers dashboard landing pattern.

**Acceptance criteria.**
- The overview renders fleet RED + worst-N + open issues without scanning raw spans.
- A saved dashboard persists widget defs and re-renders with the selected time range/env.

---

## 3. SHOULD features (Phase 3)

### F14 — Synthetic monitoring (API + multi-step browser, probe locations)

**Priority / Epic:** SHOULD · AM-E9.

**Purpose.** Proactive scripted probes (API, single browser, multi-step browser flows) from multiple probe locations on a schedule, catching availability/perf/functional regressions before real users — and measuring uptime where there is no organic traffic.

**Behavior.**
- **Run path reuses the Go poller** (no third Python probe implementation): extend `checker.CheckOne` with a `synthetic`/`browser` case and flow through `engine.go`'s `runServiceCheckCycle` (per-monitor due-scheduling, 50-worker pool, `DownCount`/`RetryCount` flap confirmation, parent suppression, maintenance muting). Monitor defs in PG `apm_synthetic_monitors` (also reusing `service_checks` group/template/maintenance infra); multi-step scripts, assertions, and probe-location lists live in the JSONB `config` (no schema churn), per the service-checks convention.
- **Monitor types:** `api` (HTTP/gRPC/SSL/DNS/TCP single + multistep), `browser` (headless real-browser journey), `multistep` (ordered steps with per-step assertions). Assertions (proposed v1 grammar): status code, latency threshold, header match, JSON-path equality/regex, body substring/regex.
- **Probe locations:** each result records `location` and `poller_id`; multiple pollers act as geographic probes; the UI compares by location.
- **Results** in CH `apm_synthetic_results` (per-result and per-step via `step_index`/`step_name`, `is_up`, `response_ms`, `status_code`, `assertion_failed`, `error_message`, `backend_trace_id` to stitch the synthetic request to its backend trace), rolled up to `apm_synthetic_results_5m` (uptime_pct, avg/p95 response per monitor+location).
- **Run-now** via `POST /api/v1/apm/synthetics/{id}/run` routes to the poller (never re-implements probes). **`synthetic_down`** rides the existing service-status push to `/api/v1/alert-engine/evaluate-service` (F8) — alerting/recovery/schedule for free.

**Data needed.** CH: `apm_synthetic_results`, `apm_synthetic_results_5m`. PG: `apm_synthetic_monitors` (+ reused `service_check_groups`/`_templates`/`_maintenance`). Poller: extended `CheckOne` + cycle.

**API surface.** `GET/POST/PUT/DELETE /api/v1/apm/synthetics`, `POST /api/v1/apm/synthetics/{id}/run`.

**UX surface.** `/apm/synthetics` (`SyntheticsPage.tsx`), `/apm/synthetics/:id` (`SyntheticDetailPage.tsx`) — reuse the service-checks KPI tiles / uptime calendar / incidents strip + a **new step-waterfall** for multi-step.

**Competitor parity.** Datadog/New Relic Synthetics (API + scripted browser, managed + private locations).

**Reuse.** Go poller `engine.go` scheduler/retry/flap/maintenance + `checker/` (`http.go` is the seed for an HTTP step); `service_checks` group/template/maintenance infra; service-checks SLA/uptime UI components; the service-status alert push.

**Acceptance criteria.**
- A 3-step browser flow (login → search → checkout) runs from two locations, records per-step timings/assertions, and flips to `down` only after `RetryCount` confirmation.
- A failing assertion raises `synthetic_down` via the existing service-status path and recovers automatically.
- `backend_trace_id` stitches a synthetic API request to its backend trace (F2).
- Run-now executes on the poller, not in Python.

---

### F15 — RUM + Core Web Vitals + session context

**Priority / Epic:** SHOULD · AM-E10.

**Purpose.** Measure real end-user experience in the browser — Core Web Vitals, JS errors, route changes, sessions — and stitch frontend sessions to backend traces.

**Behavior.**
- A browser RUM SDK posts events to the **public origin-scoped beacon** `POST /api/v1/apm/rum/ingest` (auth = `zpr_` key + CORS allowlist + rate-limit, F12). Events land in CH `apm_rum_events` (`view`/`action`/`error`/`resource`/`long_task`).
- **Core Web Vitals** captured on view events: LCP, INP, CLS, FCP, TTFB, load — rolled up to `apm_rum_vitals_5m` at **p75** (the CWV standard) per `application_id`+`view_name`.
- **Session context:** `session_id`/`view_id`/`view_name` (route), `user_id`, `country`, `browser`, `device_type`; JS errors (`error_message`) feed the same fingerprinting surface as F5 where applicable.
- **RUM → backend trace stitch:** the SDK injects trace headers on XHR/fetch so `backend_trace_id` links a frontend view to its backend trace (one-click to F2).
- Form input masked by default (PII, F11).

**Data needed.** CH: `apm_rum_events` (14d), `apm_rum_vitals_5m` (+ MV). PG: `apm_ingest_keys` (`zpr_`).

**API surface.** `POST /api/v1/apm/rum/ingest` (public beacon); `GET /api/v1/apm/rum/...` (views/sessions/web-vitals).

**UX surface.** `/apm/rum` (`RumPage.tsx`) — CWV p75 by view, sessions, JS errors, geo/browser/device breakdown.

**Competitor parity.** Datadog/New Relic RUM (CWV LCP/INP/CLS, JS errors, session → backend trace).

**Reuse.** `apm_rum_events`/`_vitals_5m` (new); the public `zpr_` beacon (forked auth); ping/host rollup MV idiom.

**Acceptance criteria.**
- CWV p75 (LCP/INP/CLS) per view matches the hand-computed p75 over `apm_rum_events`.
- A page view with an XHR to an instrumented backend resolves `backend_trace_id` and opens that trace.
- The beacon accepts only allow-listed origins and rate-limits per origin.

---

### F16 — Database / query monitoring

**Priority / Epic:** SHOULD · AM-E11.

**Purpose.** Performance visibility below the application: normalized query digests, sampled executions, and (where available) plan capture — with source-side obfuscation so literals never leave the host.

**Behavior.**
- DB spans already promote `db_system`, `db_operation`, `db_statement` (normalized digest) columns on `apm_spans`; the DB-monitoring view aggregates these into per-digest RED (count, error rate, p95 latency, rows where available) per service and database.
- A **DB agent** (Phase 3) on/near the DB host collects normalized query digests (literals/binds → `?`), samples individual executions (not every query), captures explain plans where the engine supports it, and **obfuscates bind parameters at the source** (Datadog DBM idiom). Digests correlate back to the `apm_spans` that issued the query via `trace_id`/`db_statement`.

**Data needed.** CH: `apm_spans` (`db_*` columns). New tables for sampled executions/plans authored in `03`/`05` (DB-agent phase). PG: none new in v1 of this feature beyond config.

**API surface.** Queried through service detail (`?tab=database`); a digest list/detail under the `apm.py` router (proposed).

**UX surface.** Service-detail `?tab=database` — top query digests by latency/throughput/errors, per-digest trend, sampled executions/plans.

**Competitor parity.** Datadog Database Monitoring (normalized queries, explain plans, source-side obfuscation).

**Reuse.** `apm_spans` `db_*` promoted columns; DB agent into the same OTLP/ingest path; the service-checks/query-service granularity patterns for digest time-series.

**Acceptance criteria.**
- Identical statements with different literals aggregate to one digest; the digest shows count/error/p95.
- `db_statement` is a digest with no literals (obfuscation verified, F11).
- A digest links to a representative `trace_id`.

---

### F17 — eBPF zero-code agent

**Priority / Epic:** SHOULD · AM-E11.

**Purpose.** Get RED metrics + spans for HTTP/gRPC/SQL/Mongo services with **no SDK and no code changes**, at the protocol level — aligning with ZenPlus's appliance/agent fleet model.

**Behavior.**
- A Beyla/OBI-style eBPF agent captures protocol-level RED + spans out-of-process for Go/Java/.NET/Node/Python/Ruby/Rust and exports **OTLP into the same collector** (F1). No instrumentation library; coverage becomes a deployment decision.
- Spans land in `apm_spans` identically to SDK spans, so all downstream features (F2–F10) work unchanged.

**Data needed.** CH: `apm_spans`, rollups (shared). Agent packaged through the poller/agent fleet toolchain.

**API surface.** Ingests via OTLP (F1); enrolled via F12.

**UX surface.** Surfaces in all service/trace/map views; agent fleet status in `/apm/settings` (proposed).

**Competitor parity.** OTel OBI/Beyla; Dynatrace OneAgent zero-config.

**Reuse.** OTLP export into the collector; the poller/agent fleet packaging and OTA/update path.

**Acceptance criteria.**
- An uninstrumented HTTP service yields RED + spans visible in F3/F2 with no code change.
- eBPF spans are indistinguishable downstream from SDK spans (same schema, same rollups).

---

### F18 — Continuous profiling

**Priority / Epic:** SHOULD · AM-E11.

**Purpose.** Code-level CPU/alloc/lock attribution with span→flamegraph linking — bridge "which service" (traces) to "which function".

**Behavior.**
- Low-overhead sampling profiles (cpu/alloc/lock/wall) arrive via the OTLP profiles endpoint `POST /v1development/profiles` and land in CH `apm_profiles` (pprof blob stored inline `String CODEC(ZSTD(3))`), carrying `trace_id`/`span_id` so a slow span links to the flamegraph of the code path.
- Aggregated flame graphs compare CPU/alloc by `service_version` or operation.

**Data needed.** CH: `apm_profiles` (14d, bloom `idx_prof_trace`).

**API surface.** Data plane `POST /v1development/profiles`; read via service detail.

**UX surface.** Service-detail `?tab=profiling` — flamegraph, by-version compare; **span → profile** pivot from F2.

**Competitor parity.** Datadog Continuous Profiler / Grafana Pyroscope; OTel profiles signal.

**Reuse.** `apm_profiles` (new); OTLP profiles endpoint; the established "everything inline in ClickHouse" convention (object-storage pointer is a documented future risk, see `03`).

**Acceptance criteria.**
- A profile links from a span via shared `trace_id` and renders a flamegraph.
- Two `service_version`s' CPU profiles are comparable side by side.

---

## 4. COULD features (Phase 4)

### F19 — AI anomaly detection / auto-baselining

**Priority / Epic:** COULD · AM-E12.

**Purpose.** Replace hand-set thresholds with learned per-metric baselines (seasonality + std-dev bands) so deviations alert without tuning.

**Behavior.**
- A new `apm_anomaly_loop` computes per-service/per-metric rolling baselines with hour-of-day/day-of-week seasonality and std-dev bands (AppDynamics-style) from `apm_span_metrics_*` history, and emits a derived `apm_anomaly` boolean/score the alert engine (F8) thresholds — so anomaly alerting plugs into the existing engine with no new dispatch code.

**Data needed.** CH: `apm_span_metrics_*` history; derived `apm_anomaly` metric. Loop: `apm_anomaly_loop`. Reuse: alert engine.

**API surface.** Surfaced via alert rules (`apm_anomaly` metric) and service detail.

**UX surface.** Baseline bands on RED charts; anomaly badges on `/apm` overview and service detail.

**Competitor parity.** Datadog Watchdog / AppDynamics dynamic baselines / New Relic Lookout.

**Reuse.** New loop; alert engine threshold on a derived metric.

**Acceptance criteria.**
- A seasonal series with a daily cycle produces a baseline band that adapts; a genuine spike outside the band raises `apm_anomaly` while normal diurnal variation does not.

---

### F20 — Causal-lite RCA

**Priority / Epic:** COULD · AM-E12.

**Purpose.** When an alert storm fires, point at the probable root cause and collapse symptom alerts into one incident — without a full ML causal engine.

**Behavior.**
- A deterministic **graph-walk** over `apm_service_graph`: among co-firing alerts, the **most-upstream impacted service** (the one whose downstream dependents are all degraded but whose own upstreams are healthy) is surfaced as the probable cause; the rest are collapsed into one incident in `alerts` (correlation), reusing the topology-suppression idea already sketched in `alert_engine`.

**Data needed.** CH: `apm_service_graph`. PG: `alerts` (correlation/incident grouping).

**API surface.** Incident view under `/api/v1/apm/*` (proposed); rides the alerts API.

**UX surface.** Incident/probable-cause panel on `/apm` overview and the Alerts UI.

**Competitor parity.** Dynatrace Davis AI / Datadog Watchdog RCA (symptom-to-cause, single trackable problem).

**Reuse.** `apm_service_graph`; `alerts` correlation; existing topology-suppression scaffolding in `alert_engine`.

**Acceptance criteria.**
- For a seeded cascade (db slow → checkout slow → web slow), the probable cause is the db/most-upstream node and the three alerts collapse into one incident.

---

### F21 — Full-stack correlation panels

**Priority / Epic:** COULD · AM-E12.

**Purpose.** The ZenPlus-unique differentiator: pivot from a slow span to the host metrics of its server, to the SNMP interface counters of its top-of-rack switch, to the netflow conversation — full-stack RCA no SaaS competitor can do cheaply because they lack the on-prem network+server substrate.

**Behavior.**
- A correlation panel joins a span/service to the existing platform tables: `host_*_metrics` (the server running it), `snmp_*`/`if_*` (its switch interface), `flow_records` (the conversation) — keyed by the resource attributes mapping a service to a `server_id`/host and onward through existing topology.

**Data needed.** CH: `apm_spans`, `host_*_metrics`, `snmp_*`, `flow_records` (all existing).

**API surface.** Correlation reads under `/api/v1/apm/*` joining existing host/network query services.

**UX surface.** Service-detail `?tab=infrastructure` — span → host → switch interface → flow.

**Competitor parity.** Unique to ZenPlus (no SaaS APM owns the on-prem network+server layers).

**Reuse.** Existing `host_*`, `snmp_*`, `flow_records` tables and their query services.

**Acceptance criteria.**
- From a slow span, the infrastructure tab shows the running server's CPU/mem at that time and the relevant switch interface counters.

---

### F22 — Session Replay

**Priority / Epic:** COULD (explicitly P4/later) · AM-E12 adjacent — **WON'T for v1 core**.

**Purpose.** DOM-playback of real user sessions for frontend debugging.

**Behavior.** Record DOM mutations/input client-side (privacy-masked) and reconstruct playback server-side, linked to RUM (F15) and backend traces. **No reuse exists; explicitly deferred** — named here so it is not built early.

**Competitor parity.** Datadog/New Relic Session Replay.

**Acceptance criteria.** Out of scope for v1; tracked as a future epic.

---

### F23 — Business-impact tagging

**Priority / Epic:** COULD · AM-E12.

**Purpose.** Mark endpoints/services business-critical (orders, logins, checkout) so incidents rank by impact, not just latency.

**Behavior.** Tags on `apm_services`/spans flag business-critical paths; incidents and SLO breaches on tagged paths rank higher; a lightweight Business-iQ-style ranking needs no new infrastructure.

**Data needed.** PG: `apm_services.tags`; span attributes.

**Competitor parity.** AppDynamics Business iQ.

**Reuse.** `tags` on `apm_services`/spans; the alerts ranking surface.

**Acceptance criteria.** A business-critical-tagged service's incident ranks above an equally-degraded non-critical one.

---

## 5. Cross-feature behaviors

### 5.1 The unified resource tag (ZenPlus's Unified Service Tagging)

Every signal carries the triple `service_name` / `env` / `service_version` (from OTel resource attributes `service.name`, `deployment.environment`, `service.version`). This triple plus `trace_id`/`span_id` is the join key that makes F6 (trace↔log↔metric), F5 (errors by version), F9 (deploy compare), and F15 (RUM↔trace) work. **No feature may introduce a second, conflicting service identity.**

### 5.2 No-data and reporting-stopped

`apm_nodata_sweeper` (modeled on `health_sweeper_loop`) rolls a service to `health='no_data'` and can raise a "service stopped reporting" alert when spans cease — reusing the alert engine (F8). This guards the "missing table / no spans" window the host-metric module was built to tolerate.

### 5.3 Multi-environment scoping (the v1 isolation primitive)

`env` (from `deployment.environment`) scopes ingest keys, SLOs, dashboards, and UI filters via `apm_environments`. Full RBAC/multi-tenant is an E5-platform deliverable; the `env` + `team`/`owner` + `tags` columns are the seams a future tenant layer scopes on without schema churn. Until generalized silences land, APM alerts use **quiet-hours gating only** (not silences, which are server-scoped today).

---

## 6. Feature matrix vs competitors

Legend: ● full parity · ◐ partial / phased · ○ not in v1 · ★ ZenPlus-unique.

| Capability (feature) | ZenPlus APM | Datadog | New Relic | Dynatrace | SigNoz |
|---|---|---|---|---|---|
| OTLP-native ingest (gRPC 4317 / HTTP 4318) — **F1** | ● | ◐ (agent-first) | ● | ◐ | ● |
| Distributed tracing + waterfall/flame — **F2** | ● | ● | ● | ● | ● |
| Live vs indexed trace search — **F2** | ● | ● | ◐ | ◐ | ◐ |
| RED metrics from 100% (pre-sampling) — **F3** | ● | ● | ● | ● | ● |
| Apdex (per-service threshold) — **F3** | ● | ◐ | ● | ◐ | ◐ |
| Service map / topology with RED edges — **F4** | ● | ● | ● | ● (Smartscape) | ● |
| Error issues (fingerprint + triage + release health) — **F5** | ● | ● | ● | ◐ | ◐ |
| Trace↔log↔metric correlation + exemplars — **F6** | ● | ● | ● | ● | ● |
| SLO + error budget — **F7** | ● | ● | ● | ◐ | ◐ |
| Multi-window multi-burn-rate alerting — **F7** | ● | ◐ | ◐ | ◐ | ○ |
| Unified alert engine reuse (per-rule channels, quiet hours) — **F8** | ● | ● | ● | ● | ◐ |
| Deployment/change tracking + version compare — **F9** | ● | ● | ● | ● | ◐ |
| Head + tail sampling with retain-reason — **F10** | ● | ● | ● (Infinite Tracing) | ◐ | ◐ |
| In-pipeline PII scrubbing (OTTL) — **F11** | ● | ● | ◐ | ◐ | ◐ |
| Ingest-key auth + RUM origin-scoped keys — **F12** | ● | ● | ● | ● | ◐ |
| Custom dashboards + APM overview — **F13** | ● | ● | ● | ● | ● |
| Synthetic (API + multi-step browser + locations) — **F14** | ◐ (P3) | ● | ● | ● | ○ |
| RUM + Core Web Vitals (p75) + session→trace — **F15** | ◐ (P3) | ● | ● | ● | ◐ |
| Database/query monitoring (digests, plans, obfuscation) — **F16** | ◐ (P3) | ● | ◐ | ● | ○ |
| eBPF zero-code agent — **F17** | ◐ (P3) | ◐ | ◐ | ● (OneAgent) | ◐ |
| Continuous profiling + span→flamegraph — **F18** | ◐ (P3) | ● | ◐ | ● | ◐ |
| AI anomaly / auto-baselining — **F19** | ◐ (P4) | ● (Watchdog) | ● (Lookout) | ● (Davis) | ○ |
| Causal-lite RCA (graph-walk, incident collapse) — **F20** | ◐ (P4) | ● | ◐ | ● (Davis) | ○ |
| **Full-stack span→host→SNMP→flow correlation — F21** | ★ | ○ | ○ | ○ | ○ |
| Session Replay — **F22** | ○ (later) | ● | ● | ◐ | ○ |
| Business-impact tagging — **F23** | ◐ (P4) | ◐ | ◐ | ◐ (Business iQ via AppD) | ○ |
| Self-hosted single-node, no per-host SaaS pricing | ★ | ○ | ○ | ○ | ● |

**Reading of the matrix.** By end of Phase 2, ZenPlus APM reaches full parity on the entire MUST set (tracing, service map, RED+apdex, errors, correlation, SLO+multi-burn, alerting reuse, deploy tracking, sampling, scrubbing, dashboards). Phases 3–4 close the SHOULD/COULD gaps (synthetic, RUM, DBM, eBPF, profiling, anomaly, RCA). The two cells no competitor can fill — **F21 full-stack network+server+app correlation** and **self-hosted single-node economics** — are the wedge.

---

## 7. Reuse mapping (consolidated)

This table is the binding reuse contract: for each feature, the existing ZenPlus subsystem it leverages and the reuse class (Verbatim / Fork / Pattern / New). Cite these when implementing; see `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` for file paths.

| Feature | Existing subsystem reused | Reuse class | New build |
|---|---|---|---|
| F1 OTLP ingest | `agents.py` auth helpers; `get_clickhouse_client()`; `agent_enrollment_tokens` | Fork + Verbatim | Go collector; `apm_ingest.py` fallback; `apm_spans`/`apm_logs` tables |
| F2 Tracing + waterfall | `get_clickhouse_client()` reads; `TimeRangePicker`; Servers list skeleton | Verbatim + Pattern | `apm_spans` schema; waterfall/flame components |
| F3 RED + apdex | ping/host raw→5m→1h MV idiom; Servers list/detail UI; `KpiTile` | Pattern | `apm_span_metrics_*` MVs; `apm_services` registry |
| F4 Service map | echarts-for-react; `apm_services` nodes | Pattern | `apm_service_graph` + connector/MV; echarts graph component |
| F5 Errors/issues | Alerts UI conventions; Servers list filter skeleton | Pattern | `apm_exceptions` (CH) + `apm_error_issues` (PG); collector fingerprinting |
| F6 Correlation | host metric query-service granularity; shared `trace_id` | Pattern | `apm_logs`; exemplar resolution |
| F7 SLO/burn | alert engine; `dispatch_to_channels`; `notifications_allowed`; service-checks SLA math | Verbatim + Pattern | `apm_slos`; `apm_slo_burn_loop` |
| F8 Alerting | `alert_rules`/`alerts`; `conditions[]` AND/OR; `dispatch_to_channels`; `build_alert_email_html`; `network_alert_service.py` template | Verbatim + Fork | `apm_*` metric keys; `apm_alert_service.py` + loop; `apm_nodata_sweeper` |
| F9 Deploy tracking | recharts `ReferenceLine`; `apm_span_metrics_5m` by version | Pattern | `apm_deployments`; version-compare view |
| F10 Sampling | Go collector `tailsamplingprocessor`/`probabilistic_sampler` | Verbatim (upstream) | `apm_sampling_rules`; retain-reason tagging |
| F11 PII scrubbing | Go collector attribute/redaction/filter/OTTL processors | Verbatim (upstream) | `apm_scrubbing_rules`; default rules |
| F12 Ingest keys | `agents.py` `_authenticate`/`_new_api_key`/`/enroll`; `agent_enrollment_tokens` | Fork | `apm_ingest_keys`/`apm_enrollment_tokens`; RUM public-key path |
| F13 Dashboards | recharts/echarts; `TimeRangePicker`; Servers dashboard landing | Pattern | `apm_dashboards`; `ApmOverviewPage` |
| F14 Synthetic | poller `engine.go` cycle + `checker/`; `service_checks` group/template/maintenance; SLA/uptime UI; service-status alert push | Fork + Verbatim | synthetic/browser checker; `apm_synthetic_monitors`/`_results`/`_5m`; step-waterfall |
| F15 RUM | public `zpr_` beacon (forked auth); rollup MV idiom | Fork + Pattern | `apm_rum_events`/`_vitals_5m`; RUM SDK |
| F16 DBM | `apm_spans` `db_*` columns; query-service granularity | Pattern | DB agent; digest/plan storage |
| F17 eBPF | OTLP export into collector; poller/agent fleet packaging | Verbatim (upstream) + Pattern | eBPF agent binary |
| F18 Profiling | "everything inline in CH" convention; OTLP profiles endpoint | Pattern | `apm_profiles`; flamegraph component |
| F19 Anomaly | alert engine threshold on derived metric | Verbatim | `apm_anomaly_loop`; baseline math |
| F20 RCA | `apm_service_graph`; `alerts` correlation; `alert_engine` topology-suppression | Pattern | graph-walk RCA; incident collapse |
| F21 Full-stack | `host_*`/`snmp_*`/`flow_records` + query services | Verbatim | correlation panel join |
| F22 Replay | — | New (deferred) | (out of v1) |
| F23 Business tagging | `apm_services.tags`; alerts ranking | Pattern | impact ranking |

**Reuse headline:** of the 23 features, **0 require a parallel alert stack, a second ClickHouse client, a third synthetic probe implementation, or a new migration pipeline** — every one rides an existing subsystem (verbatim or lightly forked) or an established pattern, which is why ~60% of the platform plumbing already exists and APM ships fast.

---

## 8. Acceptance-criteria roll-up (for `08-TASK-LIST-AND-TEST-PLAN.md`)

The test plan in `08` must cover at minimum, per the per-feature criteria above:

1. **OTLP conformance** (F1): upstream SDK gRPC+HTTP land correctly; partial-success/retryable/Retry-After semantics; collector vs FastAPI byte-identical rows.
2. **Trace lookup performance** (F2): trace-id bloom sub-second; attribute filters avoid full scans; Live vs Indexed correctness.
3. **RED-accuracy-under-sampling** (F3, F10): rollup p95/error_rate match raw within tdigest tolerance after aggressive tail sampling.
4. **Apdex math** (F3) and **SLO-burn math** (F7) against hand-derived fixtures; fast-burn clears ~5 min after recovery.
5. **Error fingerprinting** (F5): line-noise collapses; regressions reopen.
6. **Cross-worker alert dedupe** (F8): exactly one alert across both uvicorn workers.
7. **Sampling correctness** (F10): 100% of error/slow traces retained; retain-reason tagged.
8. **PII scrubbing** (F11): secrets/literals never reach ClickHouse.
9. **Synthetic** (F14): multi-step flow, flap confirmation, `synthetic_down` push, `backend_trace_id` stitch, run-now-on-poller.
10. **RUM** (F15): CWV p75 correctness; session→trace stitch; origin-scoped beacon.
11. **Load/cost benchmark**: single-node sustains low-millions of spans/day raw at 7d TTL with sub-second service-scoped queries (blueprint §6.4).

---

*End of Feature Specification. Every feature ID (F1–F23), route, table, metric key, and epic ID above is the pinned value from the authoritative blueprint; raise any deviation against that blueprint, not locally.*
