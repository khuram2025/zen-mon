# Agent ↔ APM Integration Spec — what the ZenPlus agent provides to Application Monitoring

*Status: Design of record · 2026-08-04 · Companion to [09-MODERNITY-ASSESSMENT-AND-BUILD-STATE.md](09-MODERNITY-ASSESSMENT-AND-BUILD-STATE.md). This is the separate agent-side input specification: everything APM needs **from the agent** to monitor applications on a server, and how the agent delivers it.*

---

## 1. Why the agent is the centerpiece

Docs 01–08 assume the customer instruments their own applications (OTel SDK env vars) or waits for a P3 eBPF agent. That is the wrong default for this product: the buyer runs Windows servers that **already have the ZenPlus agent installed**, enrolled, heartbeating, spooling, and OTA-updatable. Modern APM's decisive UX is *"install the agent → your services appear"* — Datadog, New Relic and Dynatrace all won on this, and it is precisely what our fleet machinery makes cheap for us.

Today the agent contributes **nothing** to APM (verified: zero OTLP/instrumentation code in `ZenPlus_Agent`; the server-monitoring track lists "APM/tracing" as out-of-scope). This spec assigns it eight roles and defines the exact input contract for each.

**Division of labour (unchanged from doc 05's philosophy):** the agent discovers, instruments, collects, enriches, buffers and forwards. The controller owns identity, storage, rollups, alerting, and all decisions. The agent never evaluates anything.

## 2. The eight agent roles

| # | Role | What APM gets from it | Phase |
|---|---|---|---|
| R1 | **Process & runtime discovery** | Inventory of instrumentable applications per server | A |
| R2 | **Local OTLP gateway** | gRPC/protobuf termination on-host; buffering; one egress path | A |
| R3 | **Resource enrichment** | The correlation contract (§4): every span/log/metric joined to server/agent/site | A |
| R4 | **Auto-instrumentation lifecycle** | Zero-code onboarding of .NET/IIS, Java, Node (Windows first) | B |
| R5 | **Application log shipping** | `apm_logs` with trace correlation (AM-E5's primary feed on agent hosts) | B |
| R6 | **Runtime metrics** | CLR/JVM/Node process metrics via OTLP metrics | B |
| R7 | **Deployment detection** | Change events for AM-E7 without requiring CI integration | B |
| R8 | **APM pipeline self-telemetry** | Instrumentation health visible in the UI (what's instrumented, export errors, spool depth) | A |

## 3. The input contract — exactly what the agent must send

> This section is the answer to "what input do we need from the agent side to monitor the application in the server."

### 3.1 Discovery report (R1)

New payload on the existing config/heartbeat cadence, `POST /api/v1/agents/apm/discovery` (batched, on change + every 6 h like inventory):

| Field | Type | Source (Windows) | Purpose |
|---|---|---|---|
| `process_key` | string | stable hash of (service name \| site name \| exe path) | Idempotent identity across restarts/PIDs |
| `pid`, `ppid` | int | process list | Live handle |
| `exe_path`, `cmdline` | string | Win32 API | Runtime detection + display |
| `runtime` | enum `dotnet\|dotnet_framework\|java\|node\|python\|iis\|other` | exe/module inspection (`clr.dll`/`coreclr.dll` loaded, `java.exe`, `node.exe`, `w3wp.exe`) | Chooses the instrumentation mechanic (§5) |
| `runtime_version` | string | file/version probes (`dotnet --info`, `java -version`, node binary version resource) | Compatibility gate per bundle |
| `service_name_guess` | string | IIS site+app-pool name → Windows service display name → exe basename (in that priority) | Pre-fills `service.name`; operator can override in UI |
| `windows_service` | string\|null | SCM query | Restart mechanics + stable identity |
| `iis_site`, `iis_app_pool` | string\|null | `appcmd list` / Microsoft.Web.Administration | IIS-specific instrumentation + naming |
| `listening_ports` | int[] | netstat per PID | Correlate with service checks / flows |
| `instrumentation_state` | enum `none\|pending\|active\|failed\|unsupported` | agent state store | UI status |
| `otel_detected` | bool + endpoint | env inspection (`OTEL_EXPORTER_OTLP_ENDPOINT` present) | Don't double-instrument already-instrumented apps |

Controller stores this in a new PG table `apm_agent_processes` (additive migration) and renders it as the "Discovered applications" panel per server and in the APM onboarding wizard.

### 3.2 Trace/metric/log signals (R2, R5, R6)

The agent does **not** invent a telemetry format. It forwards standard OTLP, with the resource contract of §4 guaranteed. Requirements on the forwarded stream:

- **Traces**: unmodified OTLP spans from the local SDKs, plus agent-guaranteed resource attributes (§4). Span timestamps must be left intact (the appliance already handles clock-skew per batch for host metrics; APM spans rely on the host being NTP-synced — the agent must report `clock_skew_s` in its heartbeat as it does today, and the controller flags services on skewed hosts).
- **Logs** (phase B): OTLP LogRecords tailed from operator-selected sources (file glob, Windows Event Log channel, IIS W3C logs). Mandatory fields: `body`, `severity_number`, `timestamp`, and — when the instrumented runtime injected them — `trace_id`/`span_id` for correlation. The agent must pass W3C `trace_id` through untouched, never synthesize one.
- **Runtime metrics** (phase B): OTLP metrics restricted to a pinned allowlist (CLR: GC gen counts/heap/exceptions/threadpool; JVM: heap/GC/threads/classes; Node: event-loop lag/heap; process: cpu/rss/handles) at 15 s resolution. Custom app metrics pass through subject to the cardinality budget (00 A5) — the agent enforces a per-host series cap and reports drops.

### 3.3 Instrumentation status (R8)

Every heartbeat (existing 30 s cadence) gains an `apm` block:

```json
"apm": {
  "gateway": {"listening": true, "grpc_port": 4317, "http_port": 4318},
  "instrumented": 3, "failed": 0,
  "spans_forwarded_1m": 1240, "export_errors_1m": 0,
  "spool_depth_spans": 0, "spool_bytes": 0, "dropped_spans_total": 0,
  "bundles": {"dotnet": "1.7.0", "java": "2.14.0", "node": "0.51.0"}
}
```

Zero-valued fields are mandatory (the sensor lesson: an abstaining reporter must be distinguishable from a healthy one). The controller surfaces `export_errors`/`dropped_spans` as a `apm_pipeline_degraded` warning on the server page.

### 3.4 Deployment events (R7)

On detecting a change for an instrumented process — exe mtime/hash change, IIS deploy (web.config touch), Windows service binary path change, or MSI install of a watched product — the agent POSTs `POST /api/v1/apm/deployments` `{service_name, env, version_guess, detected_by:"agent", server_id, occurred_at}`. `service.version` from the app's own resource attributes always wins over the guess; agent events fill the gap when CI integration doesn't exist. This is what makes AM-E7 land for customers who will never call our API from a pipeline.

## 4. The correlation contract (R3) — non-negotiable resource attributes

Doc 04 F21's four-layer correlation (app → host → SNMP → flow) is only possible if every APM signal can be **joined to the infrastructure tables by key, not by string luck**. The agent gateway stamps (or verifies) these resource attributes on every span/log/metric it forwards:

| Attribute | Value | Joins to |
|---|---|---|
| `zenplus.server_id` | the agent's `server_id` (UUID) | `servers`, `host_*` metrics, capture tables |
| `zenplus.agent_id` | agent UUID | `agents` |
| `host.name` | hostname (informational; the UUID is the join key) | display |
| `service.name` | SDK value; else `service_name_guess` from §3.1 | `apm_services` |
| `deployment.environment` | SDK value; else the ingest key's env | `apm_environments` |
| `service.version` | SDK value; else agent's `version_guess` | AM-E7 |
| `service.instance.id` | `<server_id>/<process_key>` | disambiguates N instances of one service |
| `zenplus.site_id` | agent's site, when set | Phase-3 site model |

Rule: agent-stamped values **never overwrite** SDK-provided ones except the `zenplus.*` keys, which are always agent-authoritative. With this contract, the service-detail "Infrastructure" tab (00's 12-tab set) becomes a straight join: service → `zenplus.server_id` → the host CPU/memory/disk charts that already exist, and onward to the switch port (SNMP) and flows the Phase-1/3 work already correlates.

## 5. Local OTLP gateway (R2) and auto-instrumentation (R4)

### 5.1 Gateway

The agent listens on **`127.0.0.1:4317` (gRPC) and `127.0.0.1:4318` (HTTP, protobuf + JSON)** — loopback only. Local SDKs need zero endpoint configuration beyond the OTel defaults, which already point at localhost:4317/4318. The gateway:

1. terminates gRPC/protobuf **on the host**, sidestepping the appliance's JSON-only limitation (09 §E-1): it re-encodes to OTLP/JSON toward the FastAPI fallback today and switches to protobuf/gRPC pass-through when the appliance collector ships — SDK compatibility stops depending on appliance protocol support;
2. batches (1 s / 2000 spans), spools to disk on egress failure (same cap/drop-oldest/counter discipline as the rest of the agent; `Retry-After` from the appliance is honoured);
3. applies §4 enrichment and phase-B edge scrubbing (`db.statement` obfuscation locally, so raw SQL never leaves the server — closes F11's live gap at the source for agent hosts);
4. authenticates to the appliance with an **agent-scoped `zpi_` key** obtained once via `POST /api/v1/agents/apm/enroll` (authenticated by the existing agent credentials). This finally gives `apm_enrollment_tokens` its missing consumer — the controller mints the key server-side and records provenance `(agent_id → key_id)`; revoking the agent revokes its key.

Multi-URL failover and TLS follow the agent's existing controller-endpoint handling (P2-T9) — no APM-specific transport code.

### 5.2 Zero-code instrumentation, Windows first

Bundles are shipped as a new artifact kind through the **existing agent package/OTA store** (sha256-verified, ring-gated like agent upgrades): `otel-bundle-dotnet`, `otel-bundle-java`, `otel-bundle-node`, unpacked under `C:\ProgramData\ZenPlus\otel\<runtime>\<version>\`.

Mechanics are **environment-only — the agent never patches binaries**:

| Runtime | Mechanism | Applied via | Restart needed |
|---|---|---|---|
| .NET (Core & Framework) | OTel .NET auto-instrumentation: `CORECLR_ENABLE_PROFILING`/`CORECLR_PROFILER{_PATH}` (+ `COR_*` for Framework), `DOTNET_STARTUP_HOOKS`, `OTEL_DOTNET_AUTO_HOME` | Windows service: SCM per-service environment. IIS: app-pool `environmentVariables` in applicationHost.config | service restart / app-pool recycle |
| Java | `-javaagent:...\opentelemetry-javaagent.jar` via `JAVA_TOOL_OPTIONS` | service environment | service restart |
| Node | `NODE_OPTIONS=--require <bundle>\bootstrap.js` | service environment | service restart |
| Python | `opentelemetry-instrument` wrapper / `sitecustomize` | deferred to phase C (env conflicts are common) | — |
| Anything else | eBPF path per AM-E11 (Linux) — out of scope here | — | — |

Lifecycle commands ride the **existing `agent_commands` queue** (new whitelisted commands, additive CHECK migration like migrate-044 did for captures): `apm_instrument {process_key, runtime, bundle_version}`, `apm_uninstrument {process_key}`, `apm_restart_target {process_key}`, `apm_set_config {sampling_rate, log_sources[]}`. Results flow back through the standard command-result path; the config-ETag mechanism carries per-host APM config (sampling floor, scrub toggles, metric allowlist).

**Safety rails (mandatory):** instrumentation is opt-in per process (operator clicks, or a policy allows auto for matching `service_name_guess` patterns); every apply records the exact env delta for rollback; after apply, the agent watches the target for crash-loop (≥2 restarts in 120 s) and **auto-rolls-back + reports `failed`** — an APM that takes down the customer's app is worse than no APM; bundles pin OTel versions and are upgraded only through rings.

## 6. Log collection sources (R5)

Operator-configurable per server (via the APM config block): file globs (with multiline rules), Windows Event Log channels, IIS W3C directories. Defaults proposed for auto-instrumented services: the runtime's console/file output if resolvable, plus IIS logs for IIS-hosted apps. The agent honours a per-host bytes/min budget with drop-oldest + counters (same honesty rules as everything else in the fleet).

## 7. Controller-side additions this spec requires

| Piece | Change | Epic home |
|---|---|---|
| `POST /api/v1/agents/apm/enroll` | mint agent-scoped `zpi_` key from agent auth | AM-E1 / E-7 |
| `POST /api/v1/agents/apm/discovery` + `apm_agent_processes` table | store discovery | E-7 |
| Heartbeat `apm` block ingestion + `apm_pipeline_degraded` warning | extend existing heartbeat handler | E-7 |
| `agent_commands` CHECK + 4 new commands | additive migration | E-7 |
| `POST /api/v1/apm/deployments` accepting `detected_by:"agent"` | shared with CI callers | E-5 |
| Onboarding wizard: server picker → discovered processes → Instrument buttons → live "waiting for first trace" | the 06 §13.1 wizard, made real | E-8 |
| `/v1/logs`, `/v1/metrics` receivers | prerequisites for R5/R6 | E-3 / E-1 |

## 8. Explicit non-goals

The agent does not: evaluate alerts or SLOs; sample by *decision* (it applies rates it is told); rewrite span content beyond §4 enrichment and configured scrubbing; instrument anything without an operator-visible record; speak to anything except its controller endpoints.

## 9. Phasing & acceptance

**Phase A — see (agent v1.5):** R1 + R2 + R3 + R8. *Accept:* on a test Windows server with one already-OTel-instrumented app pointed at localhost:4318 (protobuf), traces appear in ZenPlus with `zenplus.server_id` set and the service's Infrastructure join resolving; kill the appliance for 5 min → zero span loss (spool drains); discovery panel lists IIS sites and Windows services with correct runtime tags.

**Phase B — act (agent v1.6):** R4 (.NET + IIS first, then Java/Node) + R5 + R6 + R7. *Accept:* on a clean IIS server, operator clicks Instrument on a .NET app pool → traces within 2 min with zero app-code changes; forced bad bundle → crash-loop detected, auto-rollback, `failed` status visible; app logs show in the trace waterfall's logs panel with matching `trace_id`; a redeploy of the app produces a deployment marker on the RED charts.

**Phase C:** Python; Linux agent parity; edge tail-sampling on the gateway if the appliance collector is still pending.

Success metric (ties to 07 §7 KPIs): **time from "agent already installed" to first trace < 5 minutes with zero application code changes** — the number that makes this APM feel modern.
