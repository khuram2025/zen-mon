# Application Monitoring — Instrumentation, Agents & Ingestion

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the customer-facing "how does data get in" half of the ZenPlus Application Monitoring (APM) design set. It specifies every supported instrumentation path — OpenTelemetry SDKs over OTLP/gRPC `:4317` and OTLP/HTTP `:4318`, the pre-configured **ZenPlus OTel distro** (one-line auto-instrumentation per language), zero-code agents (the OTel/Contrib Java agent and an eBPF/Beyla-style auto-instrumentation agent), the browser **RUM SDK**, and synthetic probes executed by the existing **Go poller** — and the ingestion machinery behind them: the dedicated **ZenPlus OTel Collector** (receivers/processors/exporters, tail-sampling, batching), the FastAPI OTLP/HTTP fallback, ingest-key issuance and bearer auth (`zpi_`/`zpr_`) reusing the existing agent-enrollment model, and the onboarding UX (install snippets + verify-data-flowing). It is OpenTelemetry-native by design: customers point existing OTel SDKs/Collectors at ZenPlus with **zero re-instrumentation**. Storage, ClickHouse DDL, partition/TTL/codec decisions, and the collector-vs-FastAPI rationale live in `03-ARCHITECTURE-AND-DATA-MODEL.md` and are referenced, not repeated, here. Every port, route, table name, prefix, and epic ID below is pinned by the authoritative blueprint and used verbatim.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — reuse inventory and gap analysis against the existing ZenPlus appliance
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — collector-vs-FastAPI decision, end-to-end pipeline, full ClickHouse + PostgreSQL DDL, partitions/TTL/codecs
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23), MoSCoW priority, acceptance criteria, API contracts
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — **this document**
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, components, react-query keys
- `07-ROADMAP-AND-EPICS.md` — the 4 phases and 12 epics (AM-E1..AM-E12) expanded
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the test plan (OTLP conformance, sampling correctness, RED-accuracy-under-sampling)

---

## 1. Ingestion surface at a glance

Every supported path terminates at one of three front doors, all of which write the **identical** `apm_*` ClickHouse row shapes (defined in `03-ARCHITECTURE-AND-DATA-MODEL.md` §3), so the query/UI layer is agnostic to how the data arrived.

| Path | Transport | Front door | Auth | Signals | Epic |
|---|---|---|---|---|---|
| OTel SDK (native) | OTLP/gRPC `:4317`, OTLP/HTTP `:4318` | Go collector (primary) / FastAPI fallback | `zpi_` bearer | traces, metrics, logs | AM-E1 |
| ZenPlus OTel distro | OTLP (as above) | Go collector | `zpi_` bearer | traces, metrics, logs (+profiles P3) | AM-E1 |
| Zero-code Java agent | OTLP (as above) | Go collector | `zpi_` bearer | traces, metrics, logs | AM-E11 |
| eBPF / Beyla-style agent | OTLP (as above) | Go collector | `zpi_` bearer | traces (RED), metrics | AM-E11 |
| Browser RUM SDK | HTTPS beacon (POST) | FastAPI `/api/v1/apm/rum/ingest` | `zpr_` public + CORS origin allowlist | RUM events, web vitals, JS errors | AM-E10 |
| Synthetic probes | internal (poller → CH) | Go poller `engine.go` cycle | n/a (server-internal) | synthetic results | AM-E9 |
| DB-monitoring agent | OTLP (as above) | Go collector | `zpi_` bearer | spans w/ `db_*` promoted cols | AM-E11 |
| **ZenPlus agent gateway** *(added 2026-08-04 — see [`10-AGENT-APM-INTEGRATION-SPEC.md`](10-AGENT-APM-INTEGRATION-SPEC.md))* | local OTLP `127.0.0.1:4317/:4318` → controller | agent forwards to FastAPI/collector | agent-scoped `zpi_` (minted via agent enrollment) | traces, logs, runtime metrics + discovery/instrumentation lifecycle | E-7 (09 §4) |

```
                       OTLP (gRPC 4317 / HTTP 4318), W3C traceparent, Bearer zpi_
   SDK / distro / Java agent / eBPF / DB agent ───────────────────────────────┐
                                                                              ▼
                                                       ┌───────────────────────────────┐
   Browser RUM SDK ── HTTPS beacon, zpr_ + CORS ──────►│ FastAPI /api/v1/apm/rum/ingest │
                                                       └──────────────┬────────────────┘
                                                                      │  (RUM only)
   Synthetic monitors ── Go poller engine.go cycle ──────────────────┐│
                                                                     ▼▼
                                                       ┌───────────────────────────────┐
                                                       │   ClickHouse zenplus DB        │
                                                       │   apm_spans / apm_* tables     │
                                                       └───────────────────────────────┘
```

The remainder of this document walks each path top-to-bottom, then the collector internals, then issuance/auth and onboarding.

---

## 2. OpenTelemetry — the primary path

ZenPlus speaks OTLP natively. **There is no proprietary wire protocol for the SDK path.** A customer who already runs OpenTelemetry SDKs or an OTel Collector changes one endpoint and one header and is done. This is the whole reason to be OTLP-native: the defensible layer is correlation/topology/RED/SLO/RCA on top of ClickHouse, not lock-in at the wire (per `01-MARKET-RESEARCH.md`).

### 2.1 Transports and ports (pinned)

OTLP runs over two transports sharing one Protobuf schema:

| Transport | Port | Content type | Paths |
|---|---|---|---|
| **OTLP/gRPC** | `4317` | protobuf (unary `Export*ServiceRequest`, gzip optional) | n/a (gRPC services) |
| **OTLP/HTTP** | `4318` | `application/x-protobuf` **or** `application/json` (lowerCamelCase, hex `traceId`/`spanId`) | `/v1/traces`, `/v1/metrics`, `/v1/logs`, `/v1development/profiles` |

- **gRPC `:4317`** is served exclusively by the **Go collector** (FastAPI does not serve gRPC). This is mandatory for drop-in compatibility and is the recommended high-volume transport.
- **HTTP `:4318`** is served by the Go collector and also by the FastAPI fallback router `apm_ingest.py` at the fixed OTLP paths `POST /v1/traces|metrics|logs` (mounted at root prefix `""`, **not** under `/api/v1`, to honor OTLP's fixed path contract). Profiles `POST /v1development/profiles` is phase 3.

### 2.2 Supported signals

| Signal | OTLP request type | Status | Lands in |
|---|---|---|---|
| Traces | `ExportTraceServiceRequest` | v1 (AM-E1) | `apm_spans` (+ `apm_span_metrics_5m/_1h`, `apm_service_graph` via MV/connector) |
| Logs | `ExportLogsServiceRequest` | v1 (AM-E5) | `apm_logs` |
| Metrics | `ExportMetricsServiceRequest` | v1 (AM-E1, OTLP receiver) | host/app metrics (see note) |
| Profiles | `ExportProfilesServiceRequest` | P3 (AM-E11) | `apm_profiles` |

> **Metrics note.** RED metrics that drive APM dashboards/SLOs are **derived from 100% of spans** at ingest (the spanmetrics connector → `apm_span_metrics_5m/_1h`), *not* from the OTLP metrics signal. The OTLP `/v1/metrics` receiver is accepted for completeness (custom application/runtime metrics, exemplars) but APM never alerts off ingested-and-sampled metric data — it alerts off the always-on, pre-sampling RED rollups. This is the three-stage decoupling pinned in `03-ARCHITECTURE-AND-DATA-MODEL.md` §6.

### 2.3 OTLP response semantics (conformance contract)

The receiver MUST implement the full OTLP response model. Conformance is part of the AM-E1 test plan (`08-TASK-LIST-AND-TEST-PLAN.md`).

| Outcome | gRPC | HTTP | Client behavior |
|---|---|---|---|
| Full success | `OK`, `partial_success` unset | `200`, `partial_success` unset | — |
| Partial success | `OK` + `partial_success{rejected_*_records, error_message}` | `200` + populated `partial_success` | **MUST NOT retry** |
| Retryable failure | `UNAVAILABLE` / `ABORTED` + `RetryInfo` | `429` / `502` / `503` / `504` + `Retry-After` | exponential backoff |
| Non-retryable failure | `INVALID_ARGUMENT` / `PERMISSION_DENIED` | `400` (bad request) / `401` / `403` | drop, surface error |

- **Backpressure is explicit.** Under memory pressure or downstream (ClickHouse) lag, the front door returns `RetryInfo` (gRPC) / `Retry-After` (HTTP) so well-behaved SDKs back off rather than overwhelming the appliance. The FastAPI fallback wires the previously-unused `backpressure` response field (audit: `schemas/agent.py`) to a bounded-queue high-watermark.
- **Auth failures** surface as `PERMISSION_DENIED` / HTTP `401`/`403` (invalid or revoked `zpi_` key), **not** retryable — a bad key must not be retried forever.

### 2.4 Semantic conventions (the join keys)

Distributed traces only stitch and dashboards only work if attributes are named consistently. ZenPlus maps OTel semantic-convention keys to **promoted ClickHouse columns** so UI filters hit indexed columns, not arbitrary attribute maps.

| OTel key | ZenPlus column | Notes |
|---|---|---|
| `service.name` (resource, **required**) | `service_name LowCardinality(String)` → PG `apm_services.name` | the one mandatory resource attribute; becomes a first-class monitored entity |
| `deployment.environment` | `env LowCardinality(String)` → PG `apm_environments` | v1 isolation primitive; ingest keys are env-scoped |
| `service.version` | `service_version LowCardinality(String)` | drives deployment/version compare (AM-E7) |
| `http.request.method` | `http_method` | promoted hot column |
| `http.route` | `http_route` (+ bloom index) | promoted hot column |
| `http.response.status_code` | `http_status_code UInt16` | promoted hot column |
| `db.system` | `db_system` | promoted; DB-monitoring tab |
| `db.operation` / `db.query.text` | `db_operation` / `db_statement` (normalized digest) | literals stripped source-side |
| `rpc.method` | `rpc_method` | promoted |

Everything else lands in the typed attribute maps `attributes_string` / `attributes_number` / `attributes_bool` plus the `resource` JSON column. This is exactly the SigNoz column-promotion pattern (`03-ARCHITECTURE-AND-DATA-MODEL.md` §3.3). ZenPlus's **Unified-Service-Tagging analogue** is the triple `(service.name, deployment.environment, service.version)` — the join key across traces, logs, RUM, profiles, and the existing infra layers.

### 2.5 W3C Trace Context propagation

ZenPlus standardizes on **W3C Trace Context** end-to-end so customers' existing instrumentation interoperates with zero changes:

- **`traceparent`** header — `version-traceid-spanid-traceflags`, e.g. `00-<32 hex>-<16 hex>-01`. The receiver reads it; downstream services create child spans with the caller's span as parent.
- **`tracestate`** — vendor/continuation data, propagated unchanged.
- **`baggage`** — arbitrary cross-service key/value indexing context (never carry secrets; baggage is unencrypted and crosses trust boundaries).

The ZenPlus distro and SDKs default the global propagator to `tracecontext,baggage`. The RUM SDK injects `traceparent` on XHR/`fetch` so browser sessions stitch to backend `apm_spans` via the shared `trace_id` (see §6.4). The legacy `b3`/`b3multi` propagators are accepted on ingest for customers migrating off Zipkin/Jaeger, but `tracecontext` is the only propagator ZenPlus emits.

---

## 3. The ZenPlus OTel distro

OTLP compatibility gets a customer *able* to send data; the **distro** gets them sending data in one line. The distro is a thin, per-language wrapper that pre-configures the upstream OpenTelemetry SDK/agent with the right endpoint, auth header, propagators, semantic-convention defaults, and ZenPlus-friendly batching — so onboarding is a single env-var block or a single dependency, not a tutorial.

### 3.1 What the distro pins (every language)

| Concern | Distro default | Underlying OTel knob |
|---|---|---|
| Endpoint | appliance host `:4317` (gRPC) | `OTEL_EXPORTER_OTLP_ENDPOINT` |
| Protocol | `grpc` (SDKs), `http/protobuf` (browser/serverless) | `OTEL_EXPORTER_OTLP_PROTOCOL` |
| Auth | `Authorization=Bearer zpi_…` | `OTEL_EXPORTER_OTLP_HEADERS` |
| Service identity | `service.name`, `deployment.environment`, `service.version` required at boot (boot fails loudly if `service.name` missing) | `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` |
| Propagators | `tracecontext,baggage` | `OTEL_PROPAGATORS` |
| Sampling | parent-based always-on at the SDK (head sampling done at the collector, not the app) | `OTEL_TRACES_SAMPLER=parentbased_always_on` |
| Batching | tuned `BatchSpanProcessor` (512 batch / 5s) | `OTEL_BSP_*` |
| Logs correlation | auto-inject `trace_id`/`span_id` into the app logger | per-language logging bridge |

### 3.2 One-line onboarding per language

The distro ships as a pre-configured SDK/agent per language. The blueprint scope is **Java / .NET / Node / Python / Go** (Ruby/PHP follow the same pattern).

```bash
# Common env block emitted by the onboarding wizard (any language)
export OTEL_EXPORTER_OTLP_ENDPOINT="http://appliance.local:4317"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer zpi_xxxxxxxxxxxxxxxx"
export OTEL_SERVICE_NAME="checkout"
export OTEL_RESOURCE_ATTRIBUTES="deployment.environment=prod,service.version=2026.06.21"
```

```bash
# Java — zero-code: attach the agent jar (auto-instruments servlets, JDBC, gRPC, Kafka, …)
java -javaagent:/opt/zenplus/otel/zenplus-otel-javaagent.jar -jar app.jar
```

```bash
# .NET — auto-instrumentation
zenplus-otel-dotnet-install.sh && OTEL_DOTNET_AUTO_HOME=/opt/zenplus/otel dotnet App.dll
```

```bash
# Node — preload the auto-instrumentation, no code changes
node --require @zenplus/otel-node/register app.js
```

```bash
# Python — auto-instrumentation bootstrap
zenplus-otel-bootstrap -a install   # detects installed libs, adds instrumentations
zenplus-otel-instrument python app.py
```

```go
// Go — no runtime auto-instrumentation, so the distro is a 3-line SDK init helper
import zpotel "github.com/zenplus/otel-go"

func main() {
    shutdown := zpotel.Init(ctx) // reads OTEL_* env, sets exporter+propagators+resource
    defer shutdown(ctx)
    // ... use otel.Tracer("checkout") as normal
}
```

The onboarding wizard at `/apm/settings` (route pinned in blueprint §2.2) generates the exact snippet **with the `zpi_` key inlined** and a copy button, then watches for first data (see §8.2). Because the distro is just the upstream SDK with defaults, anything the customer can't express through it (custom samplers, manual spans, extra processors) is still available through the standard OTel API — ZenPlus never forks the SDK surface.

---

## 4. Zero-code instrumentation

Two zero-code paths require **no application code change at all** — important for the NOC/MSP customer who does not own the source of every app they must monitor.

### 4.1 Java agent (bytecode, zero-code)

The `-javaagent` jar (§3.2) is the upstream OpenTelemetry Java agent, re-shipped as `zenplus-otel-javaagent.jar` with the distro defaults baked in. It instruments 100+ libraries (Servlet, Spring, JDBC, gRPC, Kafka, JAX-RS, Hibernate, …) at class-load via bytecode injection, emitting full traces + JVM metrics + log-correlation with no recompile. This is the lowest-friction "deep" instrumentation and the model the market (New Relic/Dynatrace OneAgent) is judged against (`01-MARKET-RESEARCH.md`). Ships under **AM-E11**.

### 4.2 eBPF / Beyla-style auto-instrumentation (the differentiator)

For languages without a runtime agent — and for the case where the customer cannot touch the workload at all — ZenPlus ships an **eBPF zero-code agent** modeled on OpenTelemetry eBPF Instrumentation (OBI, the donated Grafana Beyla). It captures HTTP/gRPC/SQL/Mongo **RED metrics and spans out-of-process at the protocol level** for Go/Java/.NET/Node/Python/Ruby/Rust, with no SDK and no code change, and exports them over OTLP into the same collector.

```
┌──────────────────────────────────────────────────────────────────┐
│  Linux host / K8s node                                             │
│  ┌───────────┐   ┌───────────┐   ┌───────────┐                     │
│  │ workload  │   │ workload  │   │ workload  │  (uninstrumented)   │
│  └─────┬─────┘   └─────┬─────┘   └─────┬─────┘                     │
│        │ syscalls / uprobes / kprobes (kernel)                     │
│   ┌────▼──────────────────────────────────────┐                   │
│   │  zenplus-ebpf-agent  (eBPF probes, OBI)    │                   │
│   │  • HTTP/gRPC/SQL/Mongo RED + spans         │                   │
│   │  • reads service.name from process/k8s meta│                   │
│   └────────────────────┬───────────────────────┘                  │
└────────────────────────┼──────────────────────────────────────────┘
                         │ OTLP/gRPC :4317  (Bearer zpi_)
                         ▼     ZenPlus OTel Collector
```

**How ZenPlus ships it (reusing the appliance toolchain):**

- The eBPF agent is a **Go binary distributed through the existing poller/agent fleet packaging and OTA update path** — the same build/release/service-management pipeline that already ships the poller and (per the blueprint) the Go collector. No new distribution channel.
- It enrolls and authenticates exactly like an SDK key: `zpi_` bearer validated against `apm_ingest_keys` (§7). The agent reads `service.name` from process/cgroup/K8s metadata so spans land against the right `apm_services` entity automatically.
- It is **out-of-process and trace-context-aware** at the protocol layer, so it produces correct RED + spans without parent-child guesswork inside the app. Where it cannot see app-internal context (e.g. distributed `traceparent` across an uninstrumented hop), it emits the edge it *can* see; the service-graph connector (§5.4) still derives the topology edge.

This is a genuine wedge versus SDK-only competitors: combined with ZenPlus's existing network/host/netflow substrate, an eBPF-instrumented service's slow span correlates down to the host CPU, the switch interface, and the netflow conversation (full-stack RCA, AM-E12, F21). Ships under **AM-E11**.

---

## 5. Collector / gateway architecture

The OTLP front door is a **dedicated Go collector** binary in the poller toolchain, with FastAPI `apm_ingest.py` as a lightweight OTLP/HTTP-only fallback. The full decision and rationale are in `03-ARCHITECTURE-AND-DATA-MODEL.md` §3.1; the short version: tail sampling needs all spans of a trace on one stateful, long-lived instance; gRPC `:4317` is first-class in Go and painful in FastAPI; and the upstream `go.opentelemetry.io/collector` libraries give receivers/processors/exporters, `memory_limiter`, `batch`, `tailsamplingprocessor`, the spanmetrics/servicegraph connectors, and the ClickHouse exporter essentially for free.

### 5.1 Embed vs require an external OTel Collector — the decision

| Option | Verdict | Why |
|---|---|---|
| **Require** customers to run their own Collector | ❌ not required | Forces an extra moving part on small appliances; defeats "one appliance" positioning |
| **Embed** a Collector built from the upstream libraries as a ZenPlus sibling binary | ✅ **chosen (primary)** | Reuses upstream receivers/processors/connectors/exporter; ships via existing OTA toolchain; runs as a managed appliance service |
| FastAPI OTLP/HTTP receiver only | ✅ **fallback (secondary)** | For single-binary/dev/low-volume installs and the RUM beacon, which is HTTP-only and origin-scoped anyway |

**Customers MAY still run their own Collector in front of ZenPlus** (e.g. an existing agent/gateway tier) and simply add a ZenPlus OTLP exporter — that is the OTLP-native promise. ZenPlus does not *require* it, but interoperates with it.

### 5.2 Pipeline topology (ordered)

Pipelines are per-signal (traces / metrics / logs); **processor order matters**; connectors bridge pipelines (a connector is an exporter at the end of one pipeline and a receiver at the start of another).

```
receivers:                      processors (ordered):                         exporters:
  otlp/grpc  (:4317) ──┐        ┌─► memory_limiter   (FIRST: shed load)        ┌─► clickhouse
  otlp/http  (:4318) ──┼──traces│   → resource/attributes (enrich env/ver)     │   (buffered batch
                       │        │   → redaction/transform (PII scrub, OTTL)    │    insert → apm_spans)
                       │        │   → tail_sampling (errors+slow+baseline)     │
                       │        │   → batch          (cut write amplification) ┘
                       │        │
                       │  connectors:
                       │   spanmetrics  ──► metrics pipeline ──► clickhouse (apm_span_metrics_5m/_1h)
                       │   servicegraph ──► graph  pipeline  ──► clickhouse (apm_service_graph)
  otlp (metrics) ──────┤        (memory_limiter → batch → clickhouse)
  otlp (logs)    ──────┘        (memory_limiter → resource → redaction → batch → clickhouse → apm_logs)
```

| Stage | Component | Purpose |
|---|---|---|
| Receive | `otlp` (grpc/http) | accept OTLP on `:4317`/`:4318`; validate `zpi_` bearer (read-through cache against `apm_ingest_keys`) |
| Guard | `memory_limiter` (first) | `check_interval`/`limit_mib`/`spike_limit_mib`; sheds load → emits `Retry-After`/`RetryInfo` so the appliance never OOMs |
| Enrich | `resource` / `attributes` | normalize `deployment.environment`/`service.version`; stamp `resource_fingerprint` |
| Scrub | `redaction` + `transform` (OTTL) | drop/hash/mask PII **before** export (§7.2) — sensitive data never reaches ClickHouse |
| Metricize | `spanmetrics` connector | emit RED (calls + latency histogram) from **100% of spans** → `apm_span_metrics_5m/_1h` (before sampling) |
| Topologize | `servicegraph` connector | pair client/server spans sharing a trace → edges → `apm_service_graph` |
| Sample | `tail_sampling` | buffer trace `decision_wait≈5s`; keep errors + slow + baseline (§5.3) — drops raw spans only |
| Batch | `batch` | group rows to cut ClickHouse write amplification |
| Export | `clickhouse` | native-protocol buffered batch insert; identical row shape to the FastAPI fallback |

> **Ordering invariant.** `memory_limiter` is first (load-shedding before work). `spanmetrics`/`servicegraph` consume **before** `tail_sampling`, so RED metrics and graph edges are computed from 100% of traffic and stay accurate even when raw traces are tail-dropped. Scrubbing runs before any exporter so no downstream step ever sees sensitive values.

### 5.3 Tail sampling policies

`tail_sampling` buffers a trace for `decision_wait` (~5s for late spans), then applies **ordered** policies. Default for `prod` (the cost-floor + value combination pinned in `03-ARCHITECTURE-AND-DATA-MODEL.md` §6.1):

| # | Policy | Keeps | Retain reason tag |
|---|---|---|---|
| 1 | `status_code` = ERROR / `has_error` | **100% of errors** | `error` |
| 2 | `latency` > pXX threshold | slow outliers | `slow` |
| 3 | `string_attribute` (business-critical tags) | tagged traces (e.g. premium customer) | `rule` |
| 4 | `probabilistic` (~1–5%) | representative baseline | `baseline` |
| — | everything else | dropped from `apm_spans` | — |

Every **retained** span is tagged `attributes_string['zp.retain_reason']` (`auto`/`error`/`slow`/`rule`/`baseline`) for cost attribution — the Datadog `ingestion_reason` idiom. Head sampling (cheap probabilistic, bounded by `apm_environments.sampling_target_tps`, default ~10 tps/service) can run upstream at the SDK as a cost floor; **tail is the default and the value layer.** Policy config lives in PG `apm_sampling_rules`, surfaced at `/apm/settings` and `GET/PUT /api/v1/apm/sampling-rules`.

> **Same-collector constraint.** Tail decisions require every span of a trace on one collector instance. The single-node appliance satisfies this trivially; a future multi-collector deployment requires trace-ID-aware load balancing (noted as a scale risk in `03-ARCHITECTURE-AND-DATA-MODEL.md`).

### 5.4 Connectors: RED metrics and service graph

- **spanmetrics** → emits a calls counter + a latency histogram dimensioned by `service_name`, `operation` (span name), `span_kind`, `env`, `status_code`, materialized into `apm_span_metrics_5m`/`_1h` (SummingMergeTree). p95 at read time = `quantilesTDigestMerge(0.95)(duration_state)`. These power every RED dashboard, apdex, and the `apm_alert_service.py` evaluator + SLO burn loop **without ever scanning raw spans**.
- **servicegraph** → pairs `CLIENT`↔`SERVER` spans sharing a `trace_id` and emits edges with request/error/duration into `apm_service_graph`. Nodes come from `apm_services`; the `/apm/service-map` graph renders RED-on-edges. A ClickHouse MV fallback (pairing by `trace_id`/`parent_span_id`) exists if the connector is unavailable.

### 5.5 FastAPI fallback receiver

`apm_ingest.py` (mounted at root `""`) decodes OTLP protobuf/JSON → row dicts → a **buffered async batch writer** (bounded queue + flush by size/interval) wrapping the `get_clickhouse_client()` singleton — **not** per-request inserts (the audit-flagged failure mode of the host pipeline). It serves `POST /v1/traces|metrics|logs` for single-binary/dev/low-volume installs and is the only path for the RUM beacon (`POST /api/v1/apm/rum/ingest`, §6). It writes identical `apm_*` rows to the Go collector and reuses the same `zpi_` auth helper, so it is a true drop-in for low volume. It does **not** do tail sampling (no cross-span buffering in a stateless uvicorn worker); low-volume installs keep more raw spans, which is acceptable at low volume.

---

## 6. RUM browser SDK

Real-user monitoring is the only client-side, untrusted-origin path. It does not use OTLP/gRPC or a secret bearer; it uses a **public, origin-scoped `zpr_` key** and a CORS-guarded HTTPS beacon. Ships under **AM-E10** (F15).

### 6.1 Script snippet

The onboarding wizard emits a copy-paste snippet with the `zpr_` key and `application_id` inlined:

```html
<script src="https://appliance.local/rum/zp-rum.js"></script>
<script>
  ZenPlusRUM.init({
    applicationId: "web-storefront",
    clientToken:  "zpr_xxxxxxxxxxxxxxxx",   // public, origin-scoped
    env:          "prod",
    service:      "storefront",              // stitches to backend apm_services
    version:      "2026.06.21",
    site:         "https://appliance.local/api/v1/apm/rum/ingest",
    sessionSampleRate: 100,                  // % of sessions tracked
    trackWebVitals: true,
    tracePropagation: ["https://api.storefront.com"], // inject traceparent on these origins
    defaultPrivacyLevel: "mask-user-input"   // mask form input by default
  });
</script>
```

### 6.2 What it captures

| Category | Captured | ClickHouse landing (`apm_rum_events`) |
|---|---|---|
| Core Web Vitals | LCP, INP, CLS, FCP, TTFB, load (CWV reported at **p75** per the standard) | `lcp_ms`, `inp_ms`, `cls`, `fcp_ms`, `ttfb_ms`, `load_ms` on `event_type='view'` |
| View / route | SPA route changes, view name | `view_name`, `view_id`, `event_type='view'` |
| Actions | clicks/inputs (privacy-masked) | `event_type='action'` |
| Resources / long tasks | resource timing, long tasks | `event_type='resource'`/`long_task'` |
| JS errors | message + (source-mapped) stack | `event_type='error'`, `error_message` |
| Session context | session/view/user id, country, browser, device | `session_id`, `user_id`, `country`, `browser`, `device_type` |

CWV p75 rollups land in `apm_rum_vitals_5m` (per `application_id`+`view_name`, tdigest state, TTL 90d). Two-stage sampling mirrors the market: `sessionSampleRate` selects which sessions are tracked at all (default 100), and a future `sessionReplaySampleRate` gates Session Replay (P4, F22).

### 6.3 Source maps

JS error stacks are minified in production. The customer uploads source maps (per `application_id` + `service.version`) via `/apm/settings`; the FastAPI ingest path symbolicates `event_type='error'` stacks **server-side at read time** (keeping the raw minified stack in `error_message` and resolving on display), so the Errors inbox shows original file/line. Source maps are stored against `(application_id, version)` and never shipped to the browser.

### 6.4 RUM → backend trace stitching

When `tracePropagation` matches a request origin, the SDK injects a `traceparent` header on XHR/`fetch`. The resulting backend trace's `trace_id` is captured on the RUM event as `backend_trace_id FixedString(32)` (bloom-indexed `idx_rum_trace`), so a slow page view pivots one-click to the backend `apm_spans` waterfall — front-to-back visibility. This reuses the same W3C propagation as §2.5.

---

## 7. Synthetic probe execution model

Synthetics are the one path that does **not** ingest customer-pushed data — ZenPlus *generates* it by actively probing targets. The model **reuses the existing Go poller** rather than building a third probe implementation. Ships under **AM-E9** (F14).

### 7.1 Reuse the service-check executor

The poller (`poller/internal/pinger/engine.go`) already loads enabled checks from Postgres, schedules each via a `due(now,last,interval)` ticker, runs them through a 50-worker pool (`checker.CheckBatch`), confirms flaps via in-memory `DownCount` vs per-check `RetryCount`, suppresses parent-down children, mutes maintenance windows (still writing metrics for SLA accuracy), and writes results to ClickHouse. APM synthetics flow through this **same cycle, status-transition, and SLA-write logic** unchanged.

| Reused piece | Path | APM use |
|---|---|---|
| Scheduler + worker pool + flap/maintenance | `poller/internal/pinger/engine.go` | run synthetic monitors on the existing cycle |
| Per-type checker dispatch | `poller/internal/checker/checker.go` `CheckOne` switch | **extension point**: add `synthetic`/`browser` case |
| HTTP checker (headers, body, redirect, status/content match) | `poller/internal/checker/http.go` | seed for an HTTP synthetic step |
| Monitor config in JSONB | `service_checks.config` pattern | store multi-step scripts, assertions, probe-location list — no schema churn |
| `/test` "run now" UX | service_checks router pattern | `POST /api/v1/apm/synthetics/{id}/run` routes to the poller (never re-implements probes in Python) |

> **Pinned anti-pattern to avoid.** The legacy service-checks `/test` endpoint re-implements every probe in Python; doing the same for synthetics would create a *third* implementation and worsen drift (audit gap). APM "run now" therefore **routes to the poller**, not to a Python re-implementation.

### 7.2 Browser checks via a headless engine

Multi-step API and **browser** monitors are a new checker case (`synthetic`/`browser`) added to the `CheckOne` switch. Browser checks execute scripted user journeys (login → checkout) in a **headless engine** (Playwright/Chromium), asserting status, latency thresholds, content, and capturing per-step timings + screenshots + Core Web Vitals. The poller's `ServiceCheckResult` is extended with per-step fields; results land in `apm_synthetic_results` with `step_index`/`step_name`/`assertion_failed`, rolled up to `apm_synthetic_results_5m` (uptime%, p95, TTL 395d) — same raw→5m→TTL idiom as the existing service-metrics pipeline.

### 7.3 Synthetic → trace stitching and alerting

The synthetic request injects `traceparent`, so `apm_synthetic_results.backend_trace_id` links a failing probe to the backend trace that served it. Alerting needs **no new evaluator**: the synthetic checker emits a status transition to the existing service-check status path (`POST /api/v1/alert-engine/evaluate-service`), which feeds the `apm_synthetic_down` metric through the unified alert engine, channels, and quiet hours (`03`/`07`; `apm_alert_service.py`). Locations are recorded per result (`location`, `poller_id`) so the UI can compare by probe location.

---

## 8. Ingest-key issuance & auth

APM does **not** invent an auth system. It forks the proven agent-enrollment model (audit: `server/app/api/v1/agents.py`), swapping the key prefix and table. Two key kinds, two trust models. Ships under **AM-E1/F12**.

### 8.1 Key kinds and the issuance flow

| Kind | Prefix | Trust model | Used by | Auth |
|---|---|---|---|---|
| SDK / collector | `zpi_` | secret bearer | SDKs, distro, Java/eBPF/DB agents, the Go collector | `Authorization: Bearer zpi_…` |
| RUM | `zpr_` | **public**, origin-scoped | browser RUM SDK | public key + CORS origin allowlist + per-origin rate-limit |

**Issuance mirrors `_new_api_key`.** A key is `prefix + token_urlsafe(32)`, returned to the operator **once**, and stored only as a `sha256` hash + 16-char prefix in PG `apm_ingest_keys` (`key_hash`, `key_prefix`, `kind IN ('sdk','rum')`, `env_id`, `origin_allowlist`, `enabled`, `rotated_at`, `revoked_at`). Management API: `GET/POST/DELETE /api/v1/apm/ingest-keys`.

**Enrollment** (for agents/collectors that bootstrap) copies `agent_enrollment_tokens` verbatim into `apm_enrollment_tokens`: one-time hashed `token_hash`, `max_uses`, `expires_at`, `revoked_at`, `consumed_ip`. Consuming a token mints a `zpi_` key.

```
Operator (UI /apm/settings)                ZenPlus
   │  POST /api/v1/apm/ingest-keys {name, kind, env, origins?}
   ├───────────────────────────────────────────►│  generate zpi_/zpr_ = prefix+token_urlsafe(32)
   │                                             │  store sha256(key)+prefix in apm_ingest_keys
   │  ◄── 201 { key: "zpi_…" }  (shown ONCE)     │
   ▼
Workload / SDK / collector
   │  OTLP export, Authorization: Bearer zpi_…
   ├───────────────────────────────────────────►│  _authenticate: hmac.compare_digest(
   │                                             │     sha256(bearer), apm_ingest_keys.key_hash)
   │  ◄── 200 / 401(invalid) / 403(revoked)      │  status/env gating; read-through cache in Go collector
```

### 8.2 Auth primitive (forked, constant-time)

Runtime auth reuses the forked `_authenticate`: strip the bearer, `sha256` it, and `hmac.compare_digest` against `apm_ingest_keys.key_hash` (constant-time; no timing oracle), gated on `enabled`/`revoked_at` and env scope. **The Go collector validates the same `zpi_` key against the same table** via a read-through cache with periodic refresh, so the data plane and control plane share one key registry. Revocation (`revoked_at`) propagates on the next cache refresh.

### 8.3 RUM public-key model

The browser cannot hold a secret, so RUM uses a distinct **public** `zpr_` key validated by **CORS origin allowlist** (`apm_ingest_keys.origin_allowlist`) + per-origin rate-limiting on `POST /api/v1/apm/rum/ingest`. A `zpr_` key never authorizes the SDK/collector path; a leaked `zpr_` key only lets an allow-listed origin send RUM beacons (rate-limited), never traces. This is the only place a non-secret key is acceptable, and it is firewalled to the RUM endpoint.

### 8.4 PII scrubbing at issuance-adjacent layers

Scrubbing is a pipeline concern (collector processors, §5.2) but is referenced here because keys are env-scoped and **default scrubbing rules ship on**: redact `Authorization`/`Cookie`/`password`/`token` attributes, obfuscate `db_statement` bind parameters **source-side** (query digests only — literals never leave the app host, the Datadog DBM idiom), and regex-mask emails/cards; RUM masks form input by default (`defaultPrivacyLevel`). Config lives in PG `apm_scrubbing_rules`, surfaced at `/apm/settings` and `GET/PUT /api/v1/apm/scrubbing-rules`. ZenPlus already handles SSH/SNMP/Windows credentials, so secret-scrubbing defaults are mandatory, not optional.

---

## 9. Onboarding UX

The product goal is **time-to-first-trace under five minutes** from a fresh service. The onboarding flow lives at `/apm/settings` (and a guided first-run wizard) and has three steps.

### 9.1 Step 1 — issue a key

The operator names a key, picks `kind` (`sdk`/`rum`), an environment (`prod`/`staging`/`dev` → `apm_environments`), and (for `zpr_`) an origin allowlist. The full `zpi_`/`zpr_` key is shown **once** with a copy button (§8.1).

### 9.2 Step 2 — install snippet (language-aware)

The wizard renders the exact distro snippet for the chosen language with the key, endpoint, `service.name`, `env`, and `version` **pre-filled** (the §3.2 snippets), plus a transport toggle (gRPC `:4317` vs HTTP `:4318`) and a "running your own Collector?" tab that shows the ZenPlus OTLP **exporter** stanza instead. For RUM it renders the `<script>` snippet (§6.1); for synthetics it opens the monitor builder (steps + assertions + locations) instead of a snippet.

### 9.3 Step 3 — verify data flowing

A live "waiting for data" panel polls `GET /api/v1/apm/services` (and the per-service RED endpoint) and flips to **green** the moment the first span for that `service.name`+`env` lands in `apm_span_metrics_5m`. It surfaces:

- first span timestamp + `service_name`/`env`/`version` as received (so typos in `service.name` are caught immediately);
- a count of spans/sec and the active `zp.retain_reason` mix (so the operator sees sampling working);
- common failure hints — `401`/`403` → bad/revoked `zpi_` key; CORS error → origin not in `zpr_` allowlist; no data → wrong endpoint/port or `service.name` unset (the distro fails boot loudly if `service.name` is missing, per §3.1).

Once green, the wizard deep-links to `/apm/services/:id?tab=overview` for the new service. Because RED metrics are computed from 100% of spans at ingest, the verify panel is accurate immediately — it does not wait for sampling or rollup lag beyond the 5-minute `apm_span_metrics_5m` bucket (live counts come from the spanmetrics connector before bucketing).

---

## 10. Cross-reference summary

| This document covers | Authoritative source for the rest |
|---|---|
| OTLP protocol, ports, signals, response/backpressure semantics, semantic conventions, W3C propagation | storage/DDL → `03-ARCHITECTURE-AND-DATA-MODEL.md` §3 (ClickHouse row shapes) & §4 (Postgres config) |
| ZenPlus OTel distro (per-language one-line onboarding) | feature acceptance criteria → `04-FEATURE-SPECIFICATION.md` (F1) |
| Zero-code (Java agent, eBPF/Beyla) | collector/packaging → `03` §2; epics → `07-ROADMAP-AND-EPICS.md` (AM-E11) |
| Collector internals (receivers/processors/exporters, tail sampling, batching, embed-vs-require) | collector-vs-FastAPI decision → `03` §2; cost/scale → `03` §5 |
| RUM SDK (snippet, web vitals, sessions, source maps, trace stitch) | `apm_rum_events`/`_vitals_5m` DDL → `03` §3.7; UI → `06-UI-UX-AND-DASHBOARDS.md` (RUM) |
| Synthetic execution (poller reuse, headless browser) | `apm_synthetic_results` DDL → `03` §3.10; alerting → `07` (AM-E6) |
| Ingest-key issuance/auth (`zpi_`/`zpr_`, enrollment) | `apm_ingest_keys`/`apm_enrollment_tokens` DDL → `03` §4 (config tables, §4.2 DDL); ingest auth → `03` §6 |
| Onboarding UX | `/apm/settings` page spec → `06-UI-UX-AND-DASHBOARDS.md` |

**Cross-reference rule (per blueprint §10):** all pinned route/table/endpoint/epic names above are used verbatim; any deviation must be raised against the authoritative blueprint, not decided locally.

---
*End of document.*
