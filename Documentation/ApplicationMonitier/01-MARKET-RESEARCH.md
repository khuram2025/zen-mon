# Application Monitoring — Market Research & Best Practices

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the competitive-intelligence and methodology foundation for the ZenPlus Application Monitoring (APM) module. It surveys the commercial and open-source APM landscape as of 2026 — Datadog, New Relic, Dynatrace, AppDynamics, OpenTelemetry, SigNoz, the Grafana stack (Tempo/Pyroscope/Faro), Elastic APM, Jaeger, Sentry, and Honeycomb — extracts the *must-have* feature set every serious APM now ships, codifies the underlying methodologies (golden signals, RED/USE, SLO/error-budget, head-vs-tail sampling, exemplars, trace-log correlation), and argues why a **ClickHouse + OpenTelemetry-native, SigNoz-class architecture** is the correct model for ZenPlus given its existing single-node appliance, ClickHouse `zenplus` database, Go poller, and unified alert engine. It does not re-derive the architecture (that is fixed in the blueprint and detailed in `03-ARCHITECTURE-AND-DATA-MODEL.md`); it provides the *evidence* that the blueprint's decisions are the market-correct ones, with inline source citations collected in §11.

## Related documents

- `00-INDEX.md` — navigation hub, reading order, epic list (AM-E1…AM-E12).
- `01-MARKET-RESEARCH.md` — **(this document)** competitive landscape, must-have matrix, methodologies, OTel/ClickHouse thesis.
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — what ZenPlus already has vs. the APM gaps, with the reuse map.
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — Go-collector-vs-FastAPI decision, end-to-end pipeline, full ClickHouse + Postgres DDL.
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23) with priority, acceptance criteria, API contracts.
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP protocol, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing.
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, waterfall/service-map components.
- `07-ROADMAP-AND-EPICS.md` — the four phases and twelve epics expanded.
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the OTLP-conformance/sampling/SLO-math/load test plan.

---

## 1. Executive summary of the landscape

The 2026 APM market has converged on a small number of architectural and methodological truths. Five years ago the field was a collection of proprietary-agent walled gardens; today every serious vendor either *is* OpenTelemetry-native or has bolted OTLP ingestion onto its proprietary pipeline, because customers refuse to re-instrument when they switch backends. The result is a clean separation between the **commodity layer** (instrumentation: OTel SDKs, eBPF auto-instrumentation, W3C Trace Context) and the **defensible layer** (storage economics, correlation, topology, SLOs, and root-cause analysis on top of the telemetry).

The competitive field splits into four bands:

```
                          PROPRIETARY-AGENT, SaaS, $$$$            OTel-NATIVE / OSS, self-hostable
                          ┌───────────────────────────────┐       ┌───────────────────────────────────┐
  FULL-STACK + AIOps      │ Dynatrace (Davis, Smartscape)  │       │ SigNoz (ClickHouse, OTLP-native)  │
                          │ Datadog (Watchdog, 20+ modules)│       │ Grafana LGTM+P (Tempo/Mimir/Loki/ │
                          │ AppDynamics (Business iQ)      │       │   Pyroscope/Faro)                 │
                          │ New Relic (APM 360, NRDB)      │       └───────────────────────────────────┘
                          └───────────────────────────────┘       ┌───────────────────────────────────┐
  FOCUSED / BEST-OF-BREED   Honeycomb (wide events/BubbleUp)       │ Jaeger v2 (OTel-Collector core)   │
                            Sentry (error/release health)          │ Elastic APM (Elasticsearch)       │
                                                                   └───────────────────────────────────┘
   ── INTEGRATION BACKBONE underpinning ALL of the above ─────────────────────────────────────────────
        OpenTelemetry: OTLP wire protocol (4317/4318), semantic conventions, W3C Trace Context, Collector
```

**The single most important architectural pattern** — shared by Datadog, New Relic, SigNoz, and Grafana Tempo alike — is the **three-stage decoupling of ingest, retain, and metricize**: RED metrics are computed from 100% of spans *before* any sampling, so dashboards and alerts stay accurate even when 95–99% of raw traces are dropped for cost. ZenPlus adopts this verbatim (`apm_span_metrics_5m/_1h` materialized views fed from 100% of spans; see `03-ARCHITECTURE-AND-DATA-MODEL.md`).

**The ZenPlus wedge** — derived directly from this research — is that no competitor combines an OTLP-native, single-node-affordable APM with an *on-prem network + server + netflow substrate already in the same appliance*. Datadog and New Relic correlate only their own signals at SaaS per-host prices; SigNoz and Grafana give you the OTel/ClickHouse architecture but no infrastructure layer underneath. ZenPlus fuses both: a slow `checkout` span → the host CPU saturation on its server → the SNMP interface errors on its top-of-rack switch → the netflow conversation, all in one self-hosted pane of glass.

---

## 2. Per-platform capability summaries

Each subsection below is a compact capability table. Sources are cited inline (`[n]`) and resolve in §11.

### 2.1 Datadog APM

Datadog is the reference design for the *ingestion/retention/metricization pipeline* and for cost-attribution discipline. Its defining idea is tagging every ingested span with an `ingestion_reason` (auto/rule/error/rare/manual/single_span/rum/synthetics) so cost is attributable per mechanism `[1][3]`.

| Capability | Summary |
|---|---|
| Distributed tracing | W3C tracecontext + Datadog headers; local Agent is the buffering/control point; **inferred services** synthesize nodes for uninstrumented third parties `[1][16]` |
| Ingestion control | Head-based "auto" sampling ~10 traces/s total (`DD_APM_TARGET_TPS`); per-service rules; agent-level Error (~10 tps) + Rare (~5 tps) samplers; single-span sampling; every span carries `ingestion_reason` `[1][3]` |
| Retention filters | Intelligent Retention Filter (diversity + high-latency percentiles + 1% flat) is **free + bias-aware** (excluded from billing and monitor eval); custom **tail-based** tag filters `[2]` |
| Trace metrics | RED computed from **100% of the primary operation at ingest**, accurate under sampling; span-based custom metrics turn high-cardinality tags into cheap timeseries `[4][16]` |
| Trace Explorer | **Live** (last 15 min, all ingested) vs **Indexed** (15-day retained) modes; waterfall/flame `[4]` |
| Service Catalog + Map | Ownership/on-call/runbooks; auto-derived live dependency graph with RED-on-edges `[5][6]` |
| Watchdog + RCA | Zero-config ML anomaly detection + automated RCA producing a Root Cause / Critical Failure / Impact causal map `[7]` |
| Continuous Profiler | ~60s low-overhead sampling; span→flamegraph "Profiles" tab `[8]` |
| Error Tracking | Fingerprint grouping into issues; first/last seen; affected versions; deploy correlation `[16]` |
| RUM + Session Replay | Two-stage sampling (`sessionSampleRate`/`sessionReplaySampleRate`); ~1% RUM↔backend trace stitch by `session_id` `[9]` |
| Synthetics | API (HTTP/gRPC/SSL/DNS/WS/TCP/UDP/ICMP) + browser; 25+ managed + self-hosted Private Locations; 100% sampled `[10][11]` |
| DB Monitoring | Normalized query digests; **source-side bind-parameter obfuscation**; sampled executions; multi-plan explain capture `[12][13]` |
| Correlation | `trace_id`/`span_id` injected into logs; **Unified Service Tagging** (`env`/`service`/`version`) as the universal join key `[14][15]` |
| Change tracking | Deploy markers from `version` tag; version-vs-version comparison; feeds Watchdog RCA `[15]` |
| SLOs + Monitors | Metric- or monitor-based SLOs; rolling/calendar windows; error-budget burn-rate alerting `[16]` |

**Lesson for ZenPlus:** copy the three-stage pipeline and the retain-reason tagging idiom (`attributes_string['zp.retain_reason']`). Ship an intelligent default that keeps errors + slow + a baseline sample with zero config.

### 2.2 New Relic (APM 360 / NRDB)

New Relic is the reference for the **golden-signals + entity-centric** model and for **tail-based sampling done in a cloud trace observer** (Infinite Tracing) `[17][21][22]`.

| Capability | Summary |
|---|---|
| APM 360 + golden signals | Entity (GUID)-centric summary fusing throughput / response-time percentiles / error rate / **Apdex** with infra/logs/traces/errors/deploys/SLOs/vulns on one page `[17][18][19]` |
| Apdex | `(satisfied + tolerating/2) / total`; satisfied ≤ T, tolerating ≤ 4T, frustrated > 4T; default T = 0.5s `[18]` |
| Distributed tracing | W3C Trace Context (`traceparent`/`tracestate` + legacy `newrelic`); **adaptive head-based** sampling ~10 traces/min/agent from prior-minute throughput `[20]` |
| Infinite Tracing | Agents stream 100% of spans to a cloud "trace observer" on AWS Edge; ~10s session per trace; **tail samplers**: duration-outlier 100%, error 100%, random ~1% `[21][22]` |
| Errors Inbox | Fingerprint by account+entity+class+message+stacktrace; managed normalization strips UUIDs/hex/emails; statuses unresolved/resolved/resolved-in-version/ignored; assign to any email; now embedded in Workloads `[27][28][29]` |
| NRDB + NRQL | Single columnar store, MELT (Metric/Event/Log/Span), 1B+ data points/min; SQL-like NRQL; dimensional metrics auto-roll-up `[23][24]` |
| Service Maps | From span relationships + entity relationships; golden-signal node coloring `[17]` |
| Browser RUM + Replay | PageView/AjaxRequest/JavaScriptError events; Core Web Vitals (LCP/INP/CLS); Session Replay; Gartner DEM Leader 2025 `[30][31]` |
| Logs in context | Auto-injects `entity.guid`/`trace.id`/`span.id`; on by default across 7 languages `[33]` |
| Change tracking | Deploy markers via API/CLI/CI; regression-to-build-SHA correlation `[34]` |
| Code-level metrics | CodeStream surfaces last-30-min response time/error rate inline in the IDE `[35]` |

**Lesson for ZenPlus:** standardize on the four golden signals exactly, with a per-service configurable Apdex T. Model each service as an entity with a stable identity and build the UI around an entity-summary page (the `/apm/services/:id` `?tab=overview` view).

### 2.3 Dynatrace

Dynatrace is the reference for **automation and deterministic, topology-driven RCA** `[37][38][39][40]`.

| Capability | Summary |
|---|---|
| OneAgent | One agent per host auto-injects into runtimes; zero-config code visibility for all processes/services/dependencies `[37]` |
| PurePath | Full code-level traces with method timings + DB statement context, no sampling gaps for problem transactions `[38]` |
| Smartscape | Real-time, continuously-upserted **typed entity-graph** (nodes: HOST/SERVICE/PROCESS… with `lifetime.start/end`; edges: runs_on/calls/relates_to, static vs dynamic); ~35-day retention; stored Grail-native `[38][41]` |
| Davis AI | **Deterministic causal RCA**: walks the dependency graph to find the propagating root entity, merges symptom events into one Problem with a Visual Resolution Path `[40]` |
| Dynamic baselining | Auto-learned per-metric baselines with seasonality + std-dev bands; reports deviations as ratios `[42][43]` |
| Grail + DQL | Schema-on-read lakehouse; DQL natively traverses the topology graph since topology and signals share one store `[39]` |

**Lesson for ZenPlus:** the headline differentiator (deterministic single-cause RCA) is the hardest to build. A pragmatic causal-*lite* version (graph-walk over `apm_service_graph` to surface the most-upstream impacted service and collapse symptom alerts) delivers most of the perceived value with no ML — pinned as **AM-E12** in the blueprint. Store topology as a first-class queryable structure co-located with signals (ClickHouse `apm_service_graph`).

### 2.4 AppDynamics (Cisco / Splunk)

AppDynamics is the reference for **business-context observability** and **seasonality-aware baselining** `[44][45][46]`.

| Capability | Summary |
|---|---|
| Business Transactions | Auto-classifies inbound requests into named business flows by entry point/URI; method-level Transaction Snapshots pinpoint the slow line of code `[46]` |
| Flow Maps | Dependency topology from observed traffic, colored by live performance `[44]` |
| Dynamic baselines | Rolling averages with explicit hour/day/week/month seasonality + std-dev bands; health rules reference baselines (e.g. "> 3σ above baseline") not fixed numbers `[45]` |
| Suspected causes | RCA surfaces candidate causes the operator confirms/negates `[44]` |
| Business iQ | Extracts business KPIs from transaction payloads; ranks incidents by $/min impact on critical paths `[44]` |

**Lesson for ZenPlus:** auto-baselining with seasonality is the highest-ROI AI feature and is achievable open-source — pinned as `apm_anomaly_loop` (**AM-E12**). Business-impact tagging on `apm_services`/spans is a lightweight COULD-have (F23).

### 2.5 OpenTelemetry (the integration backbone)

OpenTelemetry is not a competitor; it is the standard every competitor speaks and the backbone ZenPlus integrates on. See §5 for the full thesis `[47][53][54][55]`.

| Element | Summary |
|---|---|
| OTLP protocol | gRPC :4317 (unary `Export*ServiceRequest`) + HTTP :4318 (protobuf/JSON), one Protobuf schema; fixed paths `/v1/traces`, `/v1/metrics`, `/v1/logs`, `/v1development/profiles` `[47]` |
| Success/backpressure | Full success = HTTP 200 (no `partial_success`); partial success = 200 + `partial_success` (do **not** retry); retryable (429/502/503/504, gRPC Unavailable/Aborted) vs non-retryable (400); explicit `Retry-After`/`RetryInfo` backpressure `[47]` |
| Context propagation | W3C Trace Context `traceparent` (`version-traceid-spanid-flags`) + `tracestate`; baggage for cross-service indexing (never sensitive data) `[54]` |
| Semantic conventions | `service.name` (mandatory resource attr), HTTP/DB/messaging/RPC attribute bundles → map to promoted ClickHouse columns `[53]` |
| Collector | receivers → processors → exporters + connectors; `memory_limiter`, `batch`, `tailsamplingprocessor`, ClickHouse exporter, spanmetrics/servicegraph connectors `[52][55]` |
| eBPF (OBI/Beyla) | Zero-code, out-of-process RED+spans at protocol level for Go/Java/.NET/Node/Python/Ruby/Rust; donated to OTel; exports OTLP `[51]` |
| Profiles signal | pprof-compatible OTel Profiling Data Model (alpha/v2), dictionary-compressed, carries active trace_id `[50]` |

### 2.6 SigNoz (the literal architectural blueprint)

SigNoz is the closest architectural twin to what ZenPlus is building: OTLP-native, ClickHouse-backed traces/metrics/logs/exceptions with a query-service + React UI `[48]`.

| Element | Summary |
|---|---|
| Span table | `distributed_signoz_index_v3`: `trace_id FixedString(32)`, attributes split by type into `attributes_string/number/bool` maps + `resource` JSON; pre-extracted hot columns (`http_url`, `db_operation`…); promoted indexed attrs (`attribute_string_http$$route`) `[48]` |
| Query optimizations | `ts_bucket_start` (30-min floor) in **every** query for partition pruning; `resource_fingerprint` + a tiny `traces_v3_resource(labels, fingerprint)` side-table resolved via CTE then `GLOBAL IN` to avoid full scans `[48]` |
| RED generation | `signozspanmetrics/delta` collector processor emits `signoz_calls_total` + `signoz_latency` histogram dimensioned by service/operation/kind/status; p99 via `histogramQuantile` at query time `[48][49]` |
| Exceptions | `signoz_error_index_v2(errorID, groupID, traceID, spanID, exceptionType, exceptionMessage, exceptionStacktrace, exceptionEscaped, resourceTagsMap)` — `groupID` = Sentry-style grouping `[48]` |

**Lesson for ZenPlus:** copy this schema almost verbatim. The blueprint's `apm_spans` (typed attribute maps + promoted `http_*`/`db_*`/`rpc_*` columns + `ts_bucket` + `resource_fingerprint`), `apm_traces_resource`, `apm_span_metrics_5m`, and `apm_exceptions` are direct translations of the SigNoz idioms into ZenPlus naming.

### 2.7 Grafana stack (Tempo / Pyroscope / Faro)

| Component | Summary |
|---|---|
| Tempo 3.0 | Object-storage trace DB; **TraceQL** + TraceQL-metrics (ad-hoc metrics from traces, no TSDB); out-of-band **metrics-generator** (span-metrics + service-graph processors with edge-expiration); dropped RF3 for ~1x storage `[52][56]` |
| Span/service-graph metrics | `traces_spanmetrics_calls_total` + `traces_spanmetrics_latency`; `traces_service_graph_request_total`; auto-attaches **exemplars** (a sample `trace_id` per metric point) `[52]` |
| Pyroscope | Continuous-profiling backend; trace-to-profiles links a span to its flamegraph `[50]` |
| Faro | Frontend RUM SDK emitting web-vitals/errors/session telemetry as OTLP, tied to backend traces `[50]` |

**Lesson for ZenPlus:** exemplars are the connective tissue (metric spike → representative trace → span → exception). ZenPlus stores the `trace_id` on the high-value retained spans and links from RED charts via the `apm_exceptions.group_id`/`trace_id` path.

### 2.8 Elastic APM, Jaeger, Sentry, Honeycomb

| Platform | Summary |
|---|---|
| Elastic APM | Events = transactions/spans/errors/metrics, where a **transaction is a top-level span**; stored in Elasticsearch; converging onto OTel `[57]` |
| Jaeger v2 | Rebuilt on the **OTel Collector framework** core; OTLP-native tracing backend `[58]` |
| Sentry | Best-in-class error model: fingerprint into **issues** (primarily from stack trace, AI-assisted grouping never splits fingerprinted groups); **breadcrumbs** (timeline before the error); **release health** (crash-free sessions/users, per-release regression) `[59]` |
| Honeycomb | **Wide events** — one very wide high-cardinality structured event per request; **BubbleUp** auto-surfaces which attribute values correlate with a selected anomaly `[60]` |

**Lessons for ZenPlus:** Sentry's `group_id`-keyed issue model with triage state maps directly to `apm_exceptions` (CH) + `apm_error_issues` (PG). Honeycomb's BubbleUp-style exploration over wide rows is natural over ClickHouse `Map` columns — a future high-cardinality explorer over `apm_spans.attributes_string`.

---

## 3. Consolidated must-have feature matrix

Rows = capabilities every best-in-class APM ships (or that the methodology literature mandates). Columns = vendors + the **ZenPlus target** (with the feature ID `Fn` and epic `AM-En` from the blueprint). Legend: ● full · ◐ partial/add-on · ○ absent/not-applicable · ◐ⁿ qualified (see note). DD=Datadog, NR=New Relic, DYN=Dynatrace, AppD=AppDynamics, SIG=SigNoz, GRAF=Grafana, SEN=Sentry, HC=Honeycomb, OTel=the standard itself.

| Capability | DD | NR | DYN | AppD | SIG | GRAF | SEN | HC | OTel | **ZenPlus target** |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|---|
| OTLP ingest (gRPC 4317 / HTTP 4318) | ◐ | ◐ | ◐ | ◐ | ● | ● | ◐ | ● | ● | **● F1 / AM-E1** (Go collector + FastAPI fallback) |
| Distributed tracing + waterfall | ● | ● | ● | ● | ● | ● | ◐ | ● | ● | **● F2 / AM-E2** (`apm_spans`, live+indexed) |
| W3C Trace Context propagation | ● | ● | ● | ● | ● | ● | ● | ● | ● | **● F1** (standard, no proprietary headers) |
| RED metrics from 100% pre-sampling | ● | ● | ● | ● | ● | ● | ○ | ◐ | ◐ | **● F3 / AM-E3** (`apm_span_metrics_5m/_1h` MVs) |
| Apdex / satisfaction score | ● | ● | ● | ● | ◐ | ○ | ○ | ○ | ○ | **● F3** (per-service T, bucket counts) |
| Service map / topology | ● | ● | ●ᵍ | ● | ● | ● | ○ | ○ | ◐ | **● F4 / AM-E4** (`apm_service_graph`) |
| Head sampling | ● | ● | ● | ● | ● | ● | ● | ◐ | ● | **● F10 / AM-E8** |
| Tail sampling (keep errors+slow) | ● | ●ᵉ | ● | ◐ | ● | ● | ○ | ◐ | ● | **● F10 / AM-E8** (`tailsamplingprocessor`) |
| Error grouping into issues + triage | ● | ● | ● | ◐ | ● | ○ | ● | ◐ | ○ | **● F5 / AM-E4** (`apm_exceptions`+`apm_error_issues`) |
| Trace↔log↔metric correlation | ● | ● | ● | ● | ● | ● | ◐ | ● | ● | **● F6 / AM-E5** (`trace_id` in `apm_logs`) |
| Exemplars (metric→trace) | ● | ◐ | ● | ◐ | ● | ● | ○ | ○ | ● | **● F6** (retained `trace_id` on RED) |
| SLO / error-budget engine | ● | ● | ● | ● | ◐ | ◐ | ◐ | ○ | ○ | **● F7 / AM-E6** (`apm_slos`+`apm_slo_burn_loop`) |
| Multi-window multi-burn-rate alerting | ● | ◐ | ● | ◐ | ○ | ○ | ○ | ○ | ○ | **● F7** (14.4×/1h, 6×/6h, 1×/3d) |
| Alerting engine integration | ● | ● | ● | ● | ◐ | ◐ | ◐ | ◐ | ○ | **● F8 / AM-E6** (reuses `alert_rules`/`alerts`) |
| Deployment / change tracking | ● | ● | ● | ● | ◐ | ◐ | ● | ○ | ○ | **● F9 / AM-E7** (`apm_deployments` markers) |
| PII scrubbing in pipeline | ● | ● | ● | ● | ● | ● | ● | ● | ● | **● F11 / AM-E8** (collector processors) |
| Ingest-key auth + enrollment | ● | ● | ● | ● | ◐ | ◐ | ● | ● | ○ | **● F12 / AM-E1** (`zpi_`/`zpr_`, hashed bearer) |
| Dashboards + APM overview | ● | ● | ● | ● | ● | ● | ◐ | ● | ○ | **● F13 / AM-E3** (`/apm` overview) |
| Synthetic monitoring (API+browser, locations) | ● | ● | ● | ● | ◐ | ◐ | ○ | ○ | ○ | **● F14 / AM-E9** (reuses Go poller) |
| RUM + Core Web Vitals | ● | ● | ● | ● | ◐ | ●ᶠ | ◐ | ○ | ◐ | **● F15 / AM-E10** (`apm_rum_events`, `zpr_` beacon) |
| Session Replay | ● | ● | ● | ◐ | ○ | ○ | ● | ○ | ○ | **◐ F22** (explicitly P4/later) |
| Database / query monitoring | ● | ◐ | ● | ● | ◐ | ○ | ○ | ○ | ◐ | **● F16 / AM-E11** (`db_*` columns, digests) |
| eBPF zero-code agent | ◐ | ◐ | ●ᵒ | ○ | ◐ | ●ᵇ | ○ | ○ | ● | **● F17 / AM-E11** (OBI/Beyla-style) |
| Continuous profiling (span→flamegraph) | ● | ◐ | ● | ○ | ◐ | ●ᵖ | ○ | ○ | ◐ | **● F18 / AM-E11** (`apm_profiles`) |
| AI anomaly / auto-baselining | ● | ● | ● | ● | ◐ | ◐ | ○ | ◐ | ○ | **● F19 / AM-E12** (`apm_anomaly_loop`) |
| Causal / topology-driven RCA | ● | ◐ | ● | ● | ○ | ○ | ○ | ◐ | ○ | **● F20 / AM-E12** (graph-walk causal-lite) |
| Full-stack network+server+app correlation | ○ | ○ | ◐ | ◐ | ○ | ○ | ○ | ○ | ○ | **● F21 / AM-E12** (**unique to ZenPlus**) |
| Business-impact tagging | ◐ | ◐ | ◐ | ● | ○ | ○ | ○ | ○ | ○ | **◐ F23** (tags on `apm_services`) |
| Single self-hosted appliance, no per-host SaaS fee | ○ | ○ | ○ | ○ | ● | ● | ◐ | ○ | n/a | **● (positioning)** |

Notes: ●ᵍ Dynatrace Smartscape is graph-native; ●ᵉ New Relic Infinite Tracing is tail sampling in a cloud observer; ●ᵒ Dynatrace OneAgent is deep-injection not eBPF; ●ᵇ/●ᵖ/●ᶠ Grafana via Beyla/Pyroscope/Faro respectively. The **last row** is the strategic point: the proprietary SaaS vendors do not sell a single self-hosted appliance, and the OSS stacks (SIG/GRAF) lack the network/server/netflow substrate. ZenPlus is the only entry that fills *both* the OTLP-native-self-hosted column **and** the full-stack-infra-correlation row.

**Reading of the matrix.** Columns F1–F13 are table stakes — every Phase-1/2 epic (AM-E1…AM-E8) exists to reach parity on these, because a customer evaluating ZenPlus against SigNoz or Datadog will check them off first. F14–F18 (Phase 3) are where ZenPlus's *reuse* economics shine: synthetic monitoring (F14) rides the existing Go poller rather than a greenfield prober, and RUM (F15) reuses the agent-key auth model. F19–F23 (Phase 4) are the differentiators, anchored by F21 (full-stack correlation) which **no competitor can match cheaply** because none ship the on-prem network+server substrate.

---

## 4. Methodologies (the discipline behind the features)

Features are downstream of methodology. The following are the canonical practices the ZenPlus design adopts, with the sources that define them.

### 4.1 The four golden signals (Google SRE)

The canonical user-facing metric set is **latency, traffic, errors, saturation** `[61]`. The non-negotiable rule: **latency MUST be a distribution, not a mean** — a 100ms average can hide 1% of requests at 5s `[61]`. ZenPlus therefore stores latency as a t-digest aggregate (`quantilesTDigestState(0.5,0.75,0.9,0.95,0.99)`) in `apm_span_metrics_5m`, never as an average. Successful vs failed latency are measured separately (slow errors differ from slow successes). Saturation alerts fire *before* 100% because systems degrade earlier.

### 4.2 RED and USE

**RED** (Rate, Errors, Duration) is the request/service-centric subset of the golden signals — applied per service for *symptom* detection `[64]`. **USE** (Utilization, Saturation, Errors) is the resource-centric complement — applied per resource (CPU, memory, disk, queue) for *cause* diagnosis. ZenPlus computes RED per service+operation from `apm_span_metrics_5m`; the USE side is already covered by the existing `host_*_metrics` tables, and **F21 stitches the two** (RED symptom on a service → USE saturation on its host).

```
   RED  (symptom, per service)              USE  (cause, per resource)
   ┌──────────────────────────┐            ┌──────────────────────────┐
   │ Rate     = requests/sec   │  ── F21 ──▶│ Utilization = % busy      │
   │ Errors   = error_count    │  stitch   │ Saturation  = queue/wait  │
   │ Duration = p50..p99 (hist)│  span→host │ Errors      = device errs │
   └──────────────────────────┘            └──────────────────────────┘
     apm_span_metrics_5m                      host_*_metrics / snmp_if_*
```

### 4.3 Apdex (user-satisfaction index)

Apdex compresses raw response times into a 0–1 score against a threshold T: requests ≤ T are Satisfied (1.0), > T to ≤ 4T Tolerating (0.5), > 4T Frustrated (0); `Apdex = (Satisfied + Tolerating/2) / total` `[18][63]`. ZenPlus computes Apdex from `apm_span_metrics_5m` bucket counts (`countIf(duration ≤ T)`, `countIf(duration ≤ 4T)`) with a **per-service configurable T** stored on `apm_services`, matching New Relic's model.

### 4.4 SLO / SLI / error-budget with multi-window multi-burn-rate alerting

An SLI is a ratio (good events / total); an SLO is a target over a window (99.9% / 30 days → 0.1% ≈ 43 min budget). **Burn rate** = how fast the budget is consumed relative to the SLO; burn rate 1 exhausts the budget exactly at period end, 14.4 exhausts it 14.4× faster `[62]`. The Google SRE Workbook canonical config for a 99.9% SLO pairs each *long* window (significance) with a *short* window (~1/12 length, confirms the burn is still active), and **both must breach**:

| Severity | Long window | Burn rate | Short window | Budget consumed |
|---|---|---|---|---|
| **Page** | 1 hour | 14.4× | 5 min | 2% |
| **Page** | 6 hours | 6× | 30 min | 5% |
| **Ticket** | 3 days | 1× | 6 hours | 10% |

This is **the gap the codebase audit explicitly calls out**: ZenPlus today has only instantaneous value-vs-threshold comparisons over one window — no SLO target, no rolling compliance, no burn rate, and a single shared window per condition. The blueprint closes it with the `apm_slos` table + a dedicated `apm_slo_burn_loop` background task computing multiple windows, severity routing (page → PagerDuty/SMS, ticket → email/Slack), and `GET /api/v1/apm/slos/{id}/budget` for the burn chart (F7 / AM-E6; see `03-ARCHITECTURE-AND-DATA-MODEL.md` and §7 of the blueprint).

### 4.5 Symptom-based alerting and on-call hygiene

SRE distinguishes black-box **symptom** monitoring (what is broken, user-impacting → *page*) from white-box **cause** monitoring (why → *ticket/dashboard*) `[61]`. Every page must be actionable, novel, and require intelligent human response. ZenPlus maps this onto its existing per-rule channel routing with page-vs-ticket severities, and onto the multi-burn windows above (the 1×/3d ticket vs the 14.4×/1h page).

### 4.6 Head vs tail sampling

Sampling is the central cost-vs-fidelity lever, and it is **two-tiered** `[3][55][67]`:

- **Head-based** — the keep/drop decision is made at the trace origin (hash of `trace_id`) and propagated downstream. Cheap, deterministic, but *blind to which traces matter* (it cannot know a trace will error or be slow).
- **Tail-based** — the decision is made *after the full trace completes*, in the OTel Collector `tailsamplingprocessor`, which buffers a trace for `decision_wait` (~5s) then applies ordered policies: latency (keep slow), status_code (keep errors), probabilistic (keep a baseline %), string_attribute, rate_limiting `[67]`.

The load-bearing constraint: **all spans of a trace must reach the same collector instance** for a tail decision, forcing trace-ID-aware load balancing and a stateful, long-lived collector process `[67]`. This is precisely why the blueprint puts the OTLP data plane in a **dedicated Go collector** (not stateless FastAPI workers). ZenPlus ships **both** head (cost floor, ~10 tps/service equiv per `apm_environments.sampling_target_tps`) and tail (value-aware, default for `prod`), tagging every retained span with a `zp.retain_reason` (auto/error/slow/rule/baseline) for cost attribution — the Datadog `ingestion_reason` idiom.

### 4.7 Metrics-from-traces before sampling (the accuracy guarantee)

The reason sampling does not corrupt dashboards: **RED metrics and service-graph edges are derived from 100% of spans at ingest, before any sampling drops raw traces** `[4][16][49][52]`. Datadog computes trace metrics from 100% of the primary operation; SigNoz uses `signozspanmetrics`; Tempo uses its metrics-generator; ZenPlus uses ClickHouse materialized views (`apm_span_metrics_5m_mv`) that fire on insert into `apm_spans`. Dashboards and alerts read only these rollups and **never scan raw spans**, so p99 stays accurate even when 99% of raw traces are tail-dropped.

### 4.8 Exemplars and cross-signal correlation

The pillars correlate on **shared `trace_id`/`span_id`** `[14][54]`. An **exemplar** attaches a sample `trace_id` to a histogram bucket / metric data point, enabling a one-click metric→trace jump `[52][61]`. The full pivot chain best-in-class APM enables — and ZenPlus targets — is: **metric spike → exemplar trace → correlated logs (`trace_id` join) → profile (span→flamegraph) → exception (`group_id`) → RUM session**. OTel SDKs auto-inject `TraceId`/`SpanId` into logs emitted inside an active span; ZenPlus's `apm_logs.trace_id` + `idx_log_trace` bloom index makes the trace↔log pivot a single indexed lookup.

### 4.9 Cardinality and cost control

Uncontrolled label/attribute cardinality is the dominant cause of observability cost blowups `[68]`. Controls: attribute allow-lists + cardinality caps, tiered retention + downsampling, tail/dynamic sampling, and — critically — **deriving metrics from traces before sampling** so high-cardinality span detail collapses into bounded timeseries. ZenPlus splits attributes into typed maps + **promoted hot columns only** (`http_*`/`db_*`/`rpc_*`), keeps `operation`/`http_route` as `LowCardinality`, normalizes unbounded text (`db_statement` → query digests), and offers span-based custom metrics (P4) instead of indexing everything.

### 4.10 PII scrubbing in the pipeline

Sensitive data must be redacted **before it leaves the customer environment**, in the collector between receive and export `[65][66]`. The OTel processor toolkit: `attributes` (drop/hash), `redaction` (allow-list + mask values), `filter` (drop whole spans/logs), and `transform`/OTTL (regex *partial* masking — the most powerful) `[65][66]`. ZenPlus runs scrubbing in the Go collector with `apm_scrubbing_rules` config and ships **default rules on** (scrub `Authorization`/`Cookie`/`password`/`token`, obfuscate DB bind parameters source-side, mask emails/cards), which matters because the platform already handles SSH/SNMP/Windows credentials (F11 / AM-E8).

---

## 5. OpenTelemetry as the integration backbone

OpenTelemetry is the reason ZenPlus can be "drop-in compatible" rather than another walled garden. The strategic argument:

1. **Instrumentation is commodity; correlation is the moat.** OTel SDKs and eBPF auto-instrumentation (OBI/Beyla) produce spans, RED metrics, and DB-statement context across every major language for free `[51][53]`. Building proprietary deep agents from scratch is a multi-year effort that customers don't want anyway (it's lock-in). ZenPlus's defensible layer is *correlation, topology, RED, SLOs, and full-stack RCA on top of ClickHouse* — exactly the SigNoz thesis, fused with infra monitoring.

2. **OTLP is the universal wire.** Speaking OTLP/gRPC (4317) + OTLP/HTTP (4318) with correct partial-success and `Retry-After` backpressure semantics `[47]` means a customer points their *existing* OTel SDKs/Collectors at ZenPlus with zero re-instrumentation. Every competitor — Datadog, New Relic, Dynatrace, Jaeger v2, Elastic — has converged on accepting OTLP `[16][24][57][58]`; not speaking it natively would be disqualifying in 2026.

3. **Semantic conventions make the schema portable.** Standardizing on `service.name`, `http.request.method`, `db.system`, etc. `[53]` lets ZenPlus promote exactly those keys into indexed ClickHouse columns so UI filters hit indexes, not map scans — the SigNoz column-promotion pattern `[48]`.

4. **W3C Trace Context is the correlation thread.** `traceparent`/`tracestate` propagation `[54]` is what lets ZenPlus assemble cross-service traces and auto-derive the service map for free — and what lets RUM/synthetic requests stitch to backend traces (`apm_rum_events.backend_trace_id`, `apm_synthetic_results.backend_trace_id`).

5. **The Collector is the control plane.** receivers → processors → exporters + connectors give `memory_limiter` (load shedding), `batch` (write-amplification control), `tailsamplingprocessor`, the ClickHouse exporter, and spanmetrics/servicegraph connectors essentially for free `[52][55]`. This is why the blueprint's data plane is a Go collector built on `go.opentelemetry.io/collector` libraries, reusing ZenPlus's existing Go build/release/OTA toolchain.

```
  OTel SDKs / OBI-eBPF / Faro RUM ── OTLP (4317/4318, traceparent, zpi_ bearer) ──▶
     ZenPlus Go Collector  [ memory_limiter → scrub → tail_sampling → batch ]
        ├─ spanmetrics connector  ─▶ RED metrics pipeline
        ├─ servicegraph connector ─▶ edges pipeline
        └─ clickhouse exporter    ─▶ apm_spans / apm_* (zenplus DB)
```

---

## 6. Why ClickHouse + OTel-native (SigNoz-class) is right for ZenPlus

The research yields an unambiguous architectural recommendation, and it aligns exactly with what the ZenPlus appliance already is.

**6.1 ClickHouse is proven for OTel telemetry.** The canonical ClickHouse OTel trace schema and SigNoz's evolved `signoz_index_v3` both demonstrate ~9–10× compression with `LowCardinality` + typed attribute maps + ZSTD codecs `[48][63ch]`. ZenPlus *already runs ClickHouse* as the `zenplus` database holding ping/SNMP/host/netflow time-series — APM is co-located, not a second store. There is no operational cost to adding a ClickHouse-backed APM because the appliance is already a ClickHouse appliance.

**6.2 The single-node economics work** *because* of the SigNoz idioms. A single-node ZenPlus appliance comfortably sustains low-millions of spans/day at 7-day raw TTL when it copies the SigNoz/ClickHouse-OTel storage patterns:

- **Tail sampling drops the boring 95–99%** of raw spans while RED stays 100%-accurate (§4.6–4.7).
- **Daily-partitioned (`toYYYYMMDD`) raw spans with `ttl_only_drop_parts=1`** reclaim space by atomic part-drop — the netflow precedent already in the codebase `[63ch]`.
- **`ts_bucket` (30-min floor) as a leading ORDER BY dimension + `resource_fingerprint` CTE pruning + bloom skip indexes** on `trace_id`/attribute maps keep service-scoped and trace-id queries sub-second without full scans — the exact SigNoz pruning strategy `[48]`.
- **Dashboards never scan raw spans** — they read pre-aggregated `apm_span_metrics_5m/_1h` rollups (§4.7).
- A **new codec convention** (`CODEC(Delta, ZSTD(1))` on ns timestamps, `CODEC(ZSTD(1))` on id/blob/string columns) is introduced deliberately, justified by APM's volume — the audit confirms the codebase has zero existing codec usage, so this is a clean, documented addition.

**6.3 SigNoz proves the full stack is buildable on this base.** SigNoz = OTel collector + ClickHouse + query-service + React UI, delivering traces, RED, service maps, trace explorer, logs, and exceptions `[48]`. ZenPlus already has the React dashboard (the Servers module is the scaffolding template), the Go poller/agent fleet (the collector packaging path), the ClickHouse singleton + columnar `client.insert`, the migration auto-apply pipeline (`clickhouse_sync.py`), and the unified alert engine. The audit estimates ~60% of the platform plumbing already exists; ClickHouse + OTel-native is the architecture that lets ZenPlus *reuse* all of it.

**6.4 Co-location enables the wedge (F21).** Because traces, host metrics, SNMP interface counters, and netflow records all live in the *same* ClickHouse `zenplus` database, a single query can join a slow span to the saturated host running it, to the erroring switch interface, to the netflow conversation. This full-stack RCA is **the one row in §3's matrix that no SaaS or OSS competitor can match cheaply** — it requires exactly the on-prem network+server substrate ClickHouse co-location gives ZenPlus.

**Conclusion:** the SigNoz-class architecture is not merely *a* viable option — it is the *only* option that simultaneously (a) is OTLP-native for drop-in compatibility, (b) reuses ZenPlus's existing ClickHouse/Go/React/alert infrastructure, (c) holds single-node cost down via tail-sampling + daily-partition + rollup-only dashboards, and (d) unlocks the differentiated full-stack correlation wedge. The blueprint's architecture decisions (`03-ARCHITECTURE-AND-DATA-MODEL.md`) are the market-correct ones.

---

## 7. Synthesis: what this research mandates for ZenPlus

1. **Be OTLP-native** (4317/4318, W3C Trace Context, semantic conventions) — non-negotiable in 2026 (§5).
2. **Decouple ingest / retain / metricize**; compute RED from 100% of spans pre-sampling so dashboards and SLO/burn alerts stay accurate (§4.6–4.7).
3. **Adopt golden signals + RED + Apdex with per-service T**, latency always as histograms/percentiles (§4.1–4.3).
4. **Ship an explicit SLO/error-budget engine with multi-window multi-burn-rate alerting** — the audit's headline gap (§4.4).
5. **Copy the SigNoz ClickHouse schema** (typed attribute maps, promoted hot columns, `ts_bucket`, `resource_fingerprint`, RED MVs, `group_id` exceptions) for single-node affordability (§6).
6. **Tail sampling in a stateful Go collector**, head sampling as cost floor, `zp.retain_reason` tagging for cost attribution (§4.6).
7. **PII scrubbing in-pipeline with default rules on** — essential given ZenPlus handles credentials (§4.10).
8. **Reuse, don't rebuild**: ride the existing `alert_rules`/`alerts` engine, `dispatch_to_channels`, quiet hours, Go poller (synthetics), and agent-key auth (`zpi_`/`zpr_`) (§3 matrix, blueprint §3.3).
9. **Lead the differentiation with full-stack correlation (F21)** and causal-lite RCA (F20) + auto-baselining (F19) — the things no cheap competitor has (§3, §2.3–2.4).

These mandates are realized feature-by-feature in `04-FEATURE-SPECIFICATION.md` (F1–F23), sequenced in `07-ROADMAP-AND-EPICS.md` (AM-E1…AM-E12), and grounded in the data model of `03-ARCHITECTURE-AND-DATA-MODEL.md`.

---

## 8. Sources

Datadog
- [1] Datadog — Ingestion Mechanisms (head-based sampling, error/rare/single-span, `ingestion_reason`): https://docs.datadoghq.com/tracing/trace_pipeline/ingestion_mechanisms/
- [2] Datadog — Trace Retention (Intelligent Retention Filter, tail-based custom filters): https://docs.datadoghq.com/tracing/trace_pipeline/trace_retention/
- [3] Datadog — Ingestion volume control with APM Distributed Tracing: https://docs.datadoghq.com/tracing/guide/trace_ingestion_volume_control/
- [4] Datadog — Trace Explorer (Live vs indexed search, visualizations): https://docs.datadoghq.com/tracing/trace_explorer/
- [5] Datadog — Software/Service Catalog: https://docs.datadoghq.com/tracing/service_catalog/
- [6] Datadog — Service Map: https://docs.datadoghq.com/tracing/services/services_map/
- [7] Datadog — Watchdog RCA (Root Cause / Critical Failure / Impact): https://docs.datadoghq.com/watchdog/rca/
- [8] Datadog — Connect Traces and Profiles (Continuous Profiler): https://docs.datadoghq.com/profiler/connect_traces_and_profiles/
- [9] Datadog — RUM Session Replay sampling: https://docs.datadoghq.com/real_user_monitoring/guide/sampling-browser-plans/
- [10] Datadog — Synthetic Testing and Monitoring: https://docs.datadoghq.com/synthetics/
- [11] Datadog — Synthetics Private Locations: https://docs.datadoghq.com/synthetics/platform/private_locations/
- [12] Datadog — Database Monitoring (normalized queries, explain plans, sampling): https://docs.datadoghq.com/database_monitoring/
- [13] Datadog — Database Monitoring Data Collected: https://docs.datadoghq.com/database_monitoring/data_collected/
- [14] Datadog — Correlate Logs and Traces (`trace_id` injection): https://docs.datadoghq.com/tracing/other_telemetry/connect_logs_and_traces/
- [15] Datadog — Unified Service Tagging (env/service/version): https://docs.datadoghq.com/getting_started/tagging/unified_service_tagging/
- [16] Datadog — Application Performance Monitoring (APM) product: https://www.datadoghq.com/product/apm/

New Relic
- [17] New Relic — Introducing New Relic APM 360: https://newrelic.com/blog/nerdlog/apm-360
- [18] New Relic — Apdex: Measure user satisfaction: https://docs.newrelic.com/docs/apm/new-relic-apm/apdex/apdex-measure-user-satisfaction/
- [19] New Relic — Why You Need to Monitor the Four Golden Signals: https://newrelic.com/blog/apm/monitoring-golden-signals
- [20] New Relic — Technical distributed tracing details: https://docs.newrelic.com/docs/distributed-tracing/concepts/how-new-relic-distributed-tracing-works/
- [21] New Relic — Introduction to Infinite Tracing: https://docs.newrelic.com/docs/distributed-tracing/infinite-tracing/introduction-infinite-tracing/
- [22] New Relic — Set up the trace observer: https://docs.newrelic.com/docs/distributed-tracing/infinite-tracing/set-trace-observer/
- [23] New Relic — Data types: metrics, events, logs, traces (MELT): https://docs.newrelic.com/docs/data-apis/understand-data/new-relic-data-types/
- [24] New Relic — Get started with NRQL: https://docs.newrelic.com/docs/nrql/get-started/introduction-nrql-new-relics-query-language/
- [27] New Relic — Errors inbox in APM: track and triage: https://docs.newrelic.com/docs/apm/errors-inbox/errors-inbox-ui/
- [28] New Relic — Error tracking / Errors Inbox: https://docs.newrelic.com/docs/errors-inbox/errors-inbox/
- [29] New Relic — Errors Inbox now integrated within Workloads (2025): https://docs.newrelic.com/whats-new/2025/08/whats-new-08-06-workloads-errors-inbox/
- [30] New Relic — Browser Monitoring: https://newrelic.com/platform/browser-monitoring
- [31] New Relic — Session Replay: https://newrelic.com/platform/session-replay
- [33] New Relic — APM logs in context: https://docs.newrelic.com/docs/logs/logs-context/get-started-logs-context/
- [34] New Relic — Introduction to change tracking: https://docs.newrelic.com/docs/change-tracking/overview/
- [35] New Relic — Code-level metrics (CodeStream): https://docs.newrelic.com/docs/codestream/observability/code-level-metrics/

Dynatrace & AppDynamics
- [37] Dynatrace — What is Dynatrace: https://docs.dynatrace.com/docs/discover-dynatrace/what-is-dynatrace
- [38] Dynatrace — The new Smartscape: real-time dependency graph: https://www.dynatrace.com/news/blog/new-smartscape-make-better-decisions-with-real-time-dependency-graph-of-digital-systems/
- [39] Dynatrace — Smartscape on Grail: https://docs.dynatrace.com/docs/discover-dynatrace/platform/grail/smartscape-on-grail
- [40] Dynatrace — Davis AI (Semantic Dictionary): https://docs.dynatrace.com/docs/semantic-dictionary/model/davis
- [41] Dynatrace — Smartscape on Grail (node/edge model): https://docs.dynatrace.com/docs/discover-dynatrace/platform/grail/smartscape-on-grail
- [42] Splunk AppDynamics — Features: https://www.splunk.com/en_us/products/splunk-appdynamics-features.html
- [43] Cisco AppDynamics — Concepts: https://docs.appdynamics.com/appd/24.x/24.10/en/cisco-appdynamics-essentials/cisco-appdynamics-concepts
- [44] Splunk AppDynamics — Features (Flow Maps, RCA, Business iQ): https://www.splunk.com/en_us/products/splunk-appdynamics-features.html
- [45] Cisco AppDynamics — Dynamic Baselines: https://docs.appdynamics.com/appd/21.x/21.9/en/application-monitoring/business-transactions/business-transaction-performance/dynamic-baselines
- [46] Cisco AppDynamics — Concepts (Business Transactions): https://docs.appdynamics.com/appd/24.x/24.10/en/cisco-appdynamics-essentials/cisco-appdynamics-concepts

OpenTelemetry & OSS / ClickHouse-native stacks
- [47] OpenTelemetry — OTLP Specification 1.10.0: https://opentelemetry.io/docs/specs/otlp/
- [48] SigNoz — Traces Schema / Writing ClickHouse Traces Queries: https://signoz.io/docs/userguide/writing-clickhouse-traces-query/
- [49] SigNoz — Metrics ClickHouse Query Guide (`signoz_calls_total`/`signoz_latency`): https://signoz.io/docs/userguide/write-a-metrics-clickhouse-query/
- [50] Grafana Tempo — Metrics from traces (overview): https://grafana.com/docs/tempo/latest/metrics-from-traces/
- [51] OpenTelemetry — eBPF Instrumentation (OBI / Beyla): https://opentelemetry.io/docs/zero-code/obi/
- [52] Grafana Tempo — Metrics-generator (span metrics, service graphs, exemplars): https://grafana.com/docs/tempo/latest/metrics-from-traces/metrics-generator/
- [53] OpenTelemetry — Semantic conventions (via Collector Configuration): https://opentelemetry.io/docs/collector/configuration/
- [54] OpenTelemetry — Context Propagation (Context, W3C Trace Context, baggage): https://opentelemetry.io/docs/concepts/context-propagation/
- [55] OpenTelemetry — Collector Configuration (receivers/processors/exporters/connectors): https://opentelemetry.io/docs/collector/configuration/
- [56] Grafana — Tempo 3.0 release (TraceQL metrics GA): https://grafana.com/blog/tempo-3-0-release-all-the-latest-features/
- [57] Elastic — APM data model (transactions/spans/errors/metrics): https://www.elastic.co/guide/en/observability/current/apm-data-model.html
- [58] CNCF — Jaeger v2 released: OpenTelemetry in the core: https://www.cncf.io/blog/2024/11/12/jaeger-v2-released-opentelemetry-in-the-core/
- [59] Sentry — Grouping / fingerprinting: https://develop.sentry.dev/backend/application-domains/grouping/
- [60] Honeycomb — Identify Outliers with BubbleUp: https://www.honeycomb.io/platform/bubbleup
- [63ch] ClickHouse — Building an Observability Solution with ClickHouse, Part 2: Traces (trace schema DDL): https://clickhouse.com/blog/storing-traces-and-spans-open-telemetry-in-clickhouse

Methodology (vendor-neutral)
- [61] Google SRE Book — Monitoring Distributed Systems (golden signals; symptoms vs causes; latency distributions): https://sre.google/sre-book/monitoring-distributed-systems/
- [62] Google SRE Workbook — Alerting on SLOs (burn rate; multi-window multi-burn-rate; 14.4×/6×/1× windows): https://sre.google/workbook/alerting-on-slos/
- [63] Coralogix — Apdex Score: calculation and thresholds: https://coralogix.com/guides/real-user-monitoring/apdex-score/
- [64] SRE School — What is the RED method (Rate, Errors, Duration): https://sreschool.com/blog/red-method/
- [65] OpenTelemetry — Handling Sensitive Data (PII scrubbing in the Collector): https://opentelemetry.io/docs/security/handling-sensitive-data/
- [66] Dash0 — Scrubbing Sensitive Data with OpenTelemetry (attribute/redaction/filter/OTTL): https://www.dash0.com/guides/scrubbing-sensitive-data-with-opentelemetry
- [67] OpenTelemetry Collector Contrib — Tail Sampling Processor (policies; same-collector constraint): https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/README.md
- [68] Augment Code — Application Performance Monitoring: The 2026 Guide (six signals; OTel-native; cost/cardinality; AIOps boundaries): https://www.augmentcode.com/guides/application-performance-monitoring
- [69] SolarWinds — What Is Real User Monitoring (RUM vs synthetic): https://www.solarwinds.com/resources/it-glossary/real-user-monitoring

Pricing context
- [70] CloudZero — New Relic Pricing Explained: A 2025 Guide: https://www.cloudzero.com/blog/new-relic-pricing/
- [71] New Relic — Transparent Pricing: https://newrelic.com/pricing

---

*End of 01-MARKET-RESEARCH.md — the competitive and methodological foundation for the ZenPlus APM design set. Architecture and DDL: `03-ARCHITECTURE-AND-DATA-MODEL.md`. Feature specs: `04-FEATURE-SPECIFICATION.md`. Roadmap: `07-ROADMAP-AND-EPICS.md`.*
