# APM Modernity Assessment, Build State & Gap-Closure Plan

*Status: Assessment of record · 2026-08-04 · Supersedes all per-doc status headers in this folder. Companion: [10-AGENT-APM-INTEGRATION-SPEC.md](10-AGENT-APM-INTEGRATION-SPEC.md).*

This document answers three questions the rest of the set cannot: **is the design modern**, **what is actually built**, and **what must change to reach "fully functional, advanced APM."** It was produced by auditing all ten sibling documents against the code at v1.4.0 (branch `feat/apm-phase1`) and the live appliance.

---

## 1. Verdict: the design is modern; the build is ~25% of it; the agent story is missing

**The design (docs 01–08) holds up against the 2026 state of the art.** It is OTel-native, ClickHouse-backed, and explicitly SigNoz/Datadog-class: three-signal ingest, metrics-derived-before-sampling accuracy guarantee, head+tail sampling, RED/apdex from rollups, service map, error tracking, SLO multi-window burn rates, deployment tracking, synthetics, RUM/CWV, DB monitoring, eBPF, profiling, and AI-assisted RCA are all specified with acceptance criteria. Almost nothing in the modern-APM checklist (§3) is *undesigned*.

Three things are wrong:

1. **Doc-vs-code drift.** 23 concrete contradictions were found (catalogued in §5). The worst: `00-INDEX` declares itself the pinned truth and is wrong about storage engines, ports, tables, routes, tabs, and background loops; `08`'s prose says AM-E1–E4 "SHIPPED" while all 132 of its own checkboxes are unticked; and until v1.4.x the quickstart was **unexecutable** — nginx never proxied `/v1/`, so an SDK POSTing traces to the appliance received the SPA's `index.html` (fixed in install.sh + dashboard/nginx.conf, 2026-08-04).
2. **The built slice is thin and stops at read-only tracing.** What exists is a good vertical: JSON-only OTLP traces → `apm_spans` → RED rollups → services/map/traces/errors UI. But every *governance* and *action* layer — sampling, scrubbing, SLOs, alerting, deployments, synthetics — is dead schema or absent (§2).
3. **No agent-assisted path.** Doc 05 lists seven ingestion paths; every one requires the customer to instrument their own apps or wait for a P3 eBPF agent. The ZenPlus_Agent — already installed on the very servers whose applications need monitoring — contributes nothing and is never assigned a role. For this product, whose buyer runs Windows servers with our agent on them, **agent-assisted onboarding is the single most important modernization**, and it is the subject of the new [doc 10](10-AGENT-APM-INTEGRATION-SPEC.md).

## 2. Build state of record (2026-08-04)

This table supersedes the status headers and blockquotes in docs 00–08 and the checkbox state in 08.

| Epic | Spec'd | Actually built | Gap that blocks "advanced" |
|---|---|---|---|
| AM-E1 OTLP ingest | gRPC :4317 + HTTP :4318, protobuf+JSON, traces+metrics+logs, Go collector + FastAPI fallback | FastAPI fallback only: `POST /v1/traces`, **JSON only** (protobuf → 415), app port only. No `/v1/metrics`, no `/v1/logs`, no collector, nothing on 4317/4318 | Most OTel SDKs default to `grpc` or `http/protobuf`; JSON-only makes onboarding a foot-gun. Metrics/logs signals absent entirely |
| AM-E2 Trace explorer | Explorer + waterfall + flame | Explorer + waterfall shipped; **no flame view**; perf targets unmeasured | Acceptable for now |
| AM-E3 Services/RED/map | Rollup-only reads; service map from `servicegraph` connector or MV | Services/RED shipped from `apm_span_metrics_5m` ✅; **service map self-joins raw `apm_spans`** (`apm_services.py:305-318`); `apm_service_graph` created, never written/read; `_1h` rollup and `apm_traces_resource` written-never-read | Map violates the doc-set's own non-negotiable "read only rollups" rule; breaks silently the day sampling ships; first OOM candidate at scale |
| AM-E4 Errors | Fingerprint in collector + issues | Shipped (fingerprint in FastAPI path); triage works | — |
| AM-E5 Logs | `/v1/logs` → `apm_logs`, trace↔log pivot | **Nothing** | No log correlation = not a modern APM |
| AM-E6 Alerting + SLO | 9 metric keys in 4 places, evaluator loop, SLO burn loop | **SHIPPED (E-2, 2026-08-05).** Keys in all 4 places (CHECK, `alert_rules.py` regex, evaluator, `AlertRuleFormDialog.tsx` APM scope); `apm_alert_evaluator_loop` (6 RED keys off `apm_span_metrics_5m`, `target`=service) + `apm_slo_burn_loop` (SRE-Workbook 14.4×/1h+5m, 6×/6h+30m page, 1×/3d+6h ticket, both-windows gate); `apm_slos` live via `/api/v1/apm/slos` CRUD + `/{id}/budget`; `/apm/slos` UI. Latency SLIs estimate bad-fraction from the merged t-digest (~0.1% resolution); availability/error-rate SLIs are exact. Still open: `apm_nodata_sweeper`, `apm_synthetic_down`/`apm_anomaly` sources (AM-E9/E12), cooldown/max_repeat enforcement | — |
| AM-E7 Deployments | Markers, version compare | `apm_deployments` dead schema; every span's `deployment_id` hardcoded `_ZERO_UUID` (`apm_ingest.py:221`) | "What changed?" is unanswerable |
| AM-E8 Sampling + scrubbing | Head+tail, `zp.retain_reason`, default-ON scrubbing | **Nothing.** `db.statement` stored verbatim with a comment deferring to a nonexistent collector (`apm_ingest.py:210`); `apm_sampling_rules`/`apm_scrubbing_rules` dead schema; `sampling_target_tps` read and ignored | Cost governance and PII compliance both open |
| AM-E9 Synthetics | Poller-executed, `apm_synthetic_monitors` | Dead schema | Sensors/poller infra exists and is reusable (see Phase-3 quorum work) |
| AM-E10 RUM | `zpr_` beacon, CWV | Nothing (key *kind* `rum` can be minted; no beacon, no storage, no UI) | — (P3, correctly) |
| AM-E11 DB/eBPF/profiling | — | Nothing | — (P3, correctly) |
| AM-E12 AI/RCA | — | Nothing | — (P4, correctly) |
| Enrollment | Token → key redemption | Tokens can be minted/revoked, **never redeemed** — no `/enroll` consumer exists | Closed by doc 10: the agent becomes the consumer |
| UI | 14 routes, 12 service tabs, 5-tab settings + wizard | 9 routes, 3 tabs, single-card settings; no `types/apm.ts`; bare `<select>` instead of the shared `TimeRangePicker` | — |
| Loops | 4 (`alert`, `slo_burn`, `nodata`, registry/anomaly) | 1 (`apm_service_registry_loop`) | — |

Also of record: `GET /v1/apm/ingest-stats` is **unauthenticated** (queue depth + counters leak; fold into the P0-T11 auth sweep), and six of eleven PG config tables shipped as dead schema in a forward-only migration — future epics must **use** them as-is or migrate additively, never edit `migrate-039`.

## 3. The modern-APM checklist (2026) vs this design

| Capability | In the docs? | Built? | Notes |
|---|---|---|---|
| OTel-native, three signals, protobuf+gRPC | ✅ (00/03/05) | ❌ traces-JSON only | E-1 below |
| Zero-code / agent-assisted onboarding | ⚠️ only as P3 eBPF + a Java agent | ❌ | **Biggest design gap → doc 10** |
| Metrics-before-sampling accuracy | ✅ (04 §load-bearing) | ✅ for RED (100% metricized) | Keep |
| Tail sampling with retain reasons | ✅ (05 §5.3) | ❌ | E-4 |
| Logs↔traces correlation | ✅ (AM-E5) | ❌ | E-3 |
| SLOs + multi-window burn alerts | ✅ (AM-E6) | ❌ | E-2 |
| Error tracking + release regression | ✅ (AM-E4/E7) | ⚠️ errors yes, release attribution no | E-5 |
| Service map at scale | ✅ (servicegraph) | ⚠️ self-join fallback | E-6 (= existing P1-T19) |
| Deployment markers / change intelligence | ✅ (AM-E7) | ❌ | E-5 |
| DB/query monitoring | ✅ (AM-E11) | ❌ | P3 unchanged |
| Synthetics + RUM/CWV | ✅ (AM-E9/E10) | ❌ | P3 unchanged; synthetics should ride the Phase-3 sensor/quorum fabric rather than a parallel path |
| Continuous profiling (OTel profiles signal) | ✅ (AM-E11, `/v1development/profiles`) | ❌ | P3 unchanged |
| Infra↔APM correlation (host/network/flow) | ✅ (F21, unique four-layer story) | ❌ | Needs the resource contract in doc 10 §4 to be joinable at all |
| Cardinality/cost governance | ✅ (00 A5, 03 §5.3) | ❌ | Rides E-4 |
| AI anomaly + RCA | ✅ (AM-E12) | ❌ | P4 unchanged |
| **GenAI/LLM call observability** | ❌ marked WON'T | ❌ | The one place the docs are behind the market: 2026 buyers ask for LLM span semconv (`gen_ai.*`: model, token counts, cost). Upgrade from WON'T to a COULD in AM-E12 scope — it is only a semconv promotion + one dashboard, not a new pipeline |
| Session replay | ❌ WON'T | ❌ | Correct call; keep WON'T |

## 4. Gap-closure plan (deltas, in order)

Numbered E-1…E-8 to avoid colliding with AM-E* epic numbers. Each lands inside the existing epic it completes.

- **E-0 (done, this change): make the shipped thing reachable.** `location /v1/` added to both nginx templates; quickstart now executable on fresh installs. Auth `/v1/apm/ingest-stats` when P0-T11 lands.
- **E-1 (AM-E1 completion): protocol honesty, then breadth.** Accept `http/protobuf` on the existing FastAPI path (protobuf decode is a small dependency — the 415 is a policy choice, not a constraint), then stand up the Go collector for gRPC :4317/:4318 as spec'd. Add `/v1/metrics` (→ `apm_metrics`, new CH migration per 00 A1) and `/v1/logs` (→ `apm_logs`, AM-E5 DDL in 03 §3.9) as accept-and-store first, correlation UI after. **The agent gateway (doc 10) removes the urgency**: it terminates gRPC/protobuf on the host and forwards in whatever the appliance speaks, so SDK compatibility stops depending on the appliance's protocol surface.
- **E-2 (done, 2026-08-05): make APM able to page someone.** The 9 metric keys are wired into all four places (`alert_engine` needed no change — `_conditions_match` is metric-agnostic and APM pull metrics never ride the status path); `apm_alert_evaluator_loop` + `apm_slo_burn_loop` shipped as dedupe-guarded loops (active-alert dedupe, the same worker-safety idiom as host/network — not advisory-lock leader election, which none of the evaluators use); `apm_slos` is live behind `/api/v1/apm/slos` + the `/apm/slos` page. Verified end-to-end on the appliance: seeded 10% errors → threshold rule + all three burn tiers raised with correct severities, budget endpoint reported 100× burn. Deferred within AM-E6: `apm_nodata_sweeper`, cooldown/`max_repeat` enforcement (still unenforced engine-wide), `apm_synthetic_down`/`apm_anomaly` sources.
- **E-3 (AM-E5): logs.** `/v1/logs` endpoint, `apm_logs` table (migrate-XXX-apm-logs-clickhouse per 03 §3.9), trace_id pivot in the waterfall, logs tab. Agent (doc 10 §6) becomes the primary log shipper for agent-managed hosts.
- **E-4 (AM-E8): sampling + scrubbing.** Server-side scrubbing first (`db.statement` obfuscation on the FastAPI path — one function, closes the live PII hole regardless of collector timing), enforcing `apm_scrubbing_rules`; then head-sampling config push (SDK/agent-facing) and tail sampling wherever the collector lands (appliance or agent gateway). `zp.retain_reason` written from day one of sampling.
- **E-5 (AM-E7): deployments.** `POST /api/v1/apm/deployments` (CI-callable + agent-reportable per doc 10 §7), resolve `deployment_id` at ingest from `(service, env, service.version)`, `ReferenceLine` markers on RED charts, version-compare view. Cheap (S effort) and high leverage for "what changed."
- **E-6 (AM-E3 debt): service map off raw spans.** Populate `apm_service_graph` incrementally from the ingest batch writer; map reads the table. Already planned as P1-T19 — reaffirmed here because sampling (E-4) silently corrupts the self-join map.
- **E-7: agent integration phase A** — doc 10 §9 (discovery + gateway + enrichment). This is the "works with our agent" milestone and should run in parallel with E-2.
- **E-8: UI completion to spec** — 12 tabs, settings tabs + onboarding wizard, `types/apm.ts`, shared `TimeRangePicker`, flame view. Do incrementally as each backend delta lands; the wizard belongs with E-7 (its happy path becomes "pick a server, click instrument").

Recommended order: **E-0 ✅ → E-2 ✅ → E-7 → E-4(scrub) → E-5 → E-3 → E-6 → E-1(collector) → E-4(tail) → E-8**, then the unchanged P3/P4 epics.

## 5. Corrections applied to this doc set (2026-08-04)

1. `00-INDEX` — engine pins corrected (5m/1h rollups are **AggregatingMergeTree**), build-state authority transferred to this doc, docs 09/10 registered.
2. `02` — "No OTLP receiver at all" claim removed (receiver exists since AM-E1).
3. `05` — ingestion-surface table gains path #8: ZenPlus agent gateway (→ doc 10).
4. `08` — header note: checkbox state is stale; this doc's §2 is the status of record.
5. `quickstart` — status header corrected to "AM-E1 partial (JSON fallback)"; nginx `/v1/` prerequisite noted as fixed.
6. Repo — `install.sh` + `dashboard/nginx.conf` gained the `/v1/` proxy block (E-0).

Unfixed by choice: `08`'s 132 checkboxes (historical artifact; §2 above supersedes), `03`'s unbuilt DDL sections (still the correct forward spec), dead-schema tables (immutable migrations — future epics consume them as shipped).
