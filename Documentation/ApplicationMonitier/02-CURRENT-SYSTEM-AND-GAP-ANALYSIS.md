# Application Monitoring — Current ZenPlus System & Gap Analysis

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the honest inventory of what the ZenPlus appliance already provides that the Application Monitoring (APM) module will reuse, and the equally honest list of what it does **not** provide and must be built new. The central finding of the codebase audit is that roughly **60% of the platform plumbing an APM module needs already exists** — a multi-condition alert engine with per-rule channels and quiet hours, an agent enrollment + hashed-bearer ingest-key model, a ClickHouse time-series data platform with a tiered raw→5m→1h rollup convention and an auto-applying migration pipeline, a Go poller with a worker-pool scheduler and flap/maintenance logic, and a mature React module scaffold (the Servers module) with URL-driven lists, `?tab=` detail pages, and shared KPI primitives. What is missing is the entire application-observability substrate: there is **no OTLP receiver, no span/trace data model, no service map, no RUM, no continuous profiling, no error-issue grouping, and no SLO/error-budget engine.** This document maps each APM capability to the existing component it extends versus the net-new build, and calls out the principal risks of retrofitting trace-shaped, high-cardinality, high-volume data onto a stack designed for low-rate device/host metrics. All names, routes, table names, and epic IDs used here are the pinned values from the authoritative blueprint; see `03-ARCHITECTURE-AND-DATA-MODEL.md` for the full DDL and `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` for the collector internals.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — **this document**
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — Go-collector decision, pipeline diagram, full ClickHouse + Postgres DDL, migration numbers
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23), MoSCoW priority, acceptance criteria, API contracts
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP protocol, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing pipeline, collector internals
- `06-UI-UX-AND-DASHBOARDS.md` — every `/apm/*` page, the `?tab=` service-detail layout, components, react-query keys
- `07-ROADMAP-AND-EPICS.md` — the 4 phases and 12 epics (AM-E1..AM-E12) expanded
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the test plan

---

## 1. Method & scope

The reuse inventory below is drawn directly from a five-area audit of branch `integration/team-work`: (1) the **Service Checks** uptime/synthetic foundation, (2) the **server-monitoring agent** enrollment + telemetry ingestion transport, (3) the unified **alerting/notification/scheduling** subsystem, (4) the **data-platform conventions** (ClickHouse + Postgres + migration pipeline), and (5) the **React dashboard** module conventions. For each subsystem we record the exact file paths, the convention that constrains how APM must extend it, and the verified extension points. "Reuse" in this document means one of three concrete things:

| Reuse class | Meaning | Example |
|---|---|---|
| **Verbatim** | Call the existing code unchanged | `notifications_allowed(...)` quiet-hours gate; `get_clickhouse_client()` singleton |
| **Fork / light edit** | Copy a file, swap a constant or add a case | `agents.py` `_new_api_key` (`zpa_`→`zpi_`); `network_alert_service.py`→`apm_alert_service.py` |
| **Pattern** | Re-implement following an established shape | tiered raw→5m→1h MV rollup; `?tab=` detail page; idempotent CH migration |

Pinned conventions referenced throughout: PG metric prefix `apm_`, ingest-key prefixes `zpi_` (SDK/collector) and `zpr_` (RUM), ClickHouse DB `zenplus`, next free PG migration **`migrate-039-apm.sql`**, CH migration **`migrate-039-apm-clickhouse.sql`** (auto-applied via `clickhouse_sync.py`), epics **AM-E1 … AM-E12**.

---

## 2. Inventory of reusable subsystems

### 2.1 Service-checks / synthetic foundation

The Service Checks subsystem is ZenPlus's existing uptime/synthetic-monitoring engine and the closest structural analog to APM synthetic monitoring and SLO/uptime UI. Definitions live in Postgres; execution is owned by the Go poller; results land in a tiered ClickHouse pipeline; a 3000-line detail page already renders an SLO-grade uptime dashboard.

| File path | What it is | How APM reuses it |
|---|---|---|
| `/opt/zenplus/server/app/models/service_check.py` | PG schema for check defs, groups, templates, maintenance windows | Reuse `groups`/`templates`/`maintenance` verbatim for synthetic monitors; store synthetic multi-step scripts/assertions/probe-locations in the existing JSONB `config` column — no schema churn. `apm_synthetic_monitors` (PG, `migrate-039-apm.sql`) reuses the same `level`/`config`/`tags`/`interval`/`retry`/`parent_check_id` shape |
| `/opt/zenplus/poller/internal/pinger/engine.go` | Per-check `due(now,last,interval)` scheduler, 50-worker pool (`checker.CheckBatch`), `DownCount`/`RetryCount` flap confirmation, parent-down suppression, maintenance muting | **The scheduling/retry/flap/maintenance backbone APM synthetics reuse directly** (AM-E9). Add a synthetic dispatch path; reuse `effectiveInterval`/`due`, retry thresholds, and `LoadActiveMaintenanceCheckIDs` muting unchanged |
| `/opt/zenplus/poller/internal/checker/` | Single-shot http/tcp/tls/icmp/dns probes returning `ServiceCheckResult` (`checker.go` dispatch switch) | `http.go` seeds the synthetic HTTP step. **Extension point:** add a `synthetic`/`browser` case to the `CheckOne` switch; extend `ServiceCheckResult` (`types.go`) with per-step timings/assertions/screenshots |
| `/opt/zenplus/scripts/migrate-006-services-v2-clickhouse.sql` | `service_metrics` (30d) → `service_metrics_5m` via MV (90d) → `service_status_log` (1y) | The exact raw→5m→status-log tier + TTL strategy that `apm_synthetic_results`/`apm_synthetic_results_5m` follow |
| `/opt/zenplus/server/app/services/service_check_service.py` | `get_service_sla` (uptime %, MTTR, MTBF, incidents, p95, streak), `get_hourly_uptime` (heatmap), `apply_template` | The SLO math (uptime %, MTTR/MTBF from status-log, p95, streak, error rate) is exactly what APM SLO/uptime needs — generalize to take a table/monitor-type param. `apply_template` provisions monitors across targets |
| `/opt/zenplus/server/app/api/v1/service_checks.py` | CRUD + `/summary`, `/uptime-stats`, `/{id}/metrics`, `/{id}/sla`, `/{id}/hourly-uptime`, `/{id}/status-history`, `POST /{id}/test` | Router layout, `require_operator_user`/`get_current_user` auth, status-pattern matcher copied for `/api/v1/apm/synthetics`. **Note:** `POST /{id}/test` re-implements probes in Python — APM `POST /apm/synthetics/{id}/run` must route to the poller (never a third implementation) |
| `/opt/zenplus/dashboard/src/pages/ServiceCheckDetail.tsx` | Hero status card, SLA KPI tiles, performance chart, incidents strip, 30-day uptime calendar heatmap, status-history table | A near-complete uptime/SLO UI shell. Reuse KPI-tile + UptimeCalendar + IncidentsStrip + PerformanceChart for `SyntheticDetailPage.tsx`/`SloDetailPage.tsx`; add a step-waterfall for multi-step checks |

**Reuse summary:** APM synthetic monitoring (F14 / AM-E9) does **not** re-implement scheduling, retries, flap confirmation, or maintenance muting — it adds one dispatch case to the Go poller and one result-table tier to ClickHouse. The SLA/uptime math and the detail-page UI are generalized rather than rewritten.

### 2.2 Agent enrollment + ingestion transport

The server-monitoring pipeline provides the enrollment, hashed-bearer auth, and ClickHouse batch-write primitives APM ingestion forks. APM keys reuse the same model with new prefixes (`zpi_`/`zpr_`).

| File path | What it is | How APM reuses it |
|---|---|---|
| `/opt/zenplus/server/app/api/v1/agents.py` | Enrollment, bearer auth, JSON-batch ingest, ETag config, long-poll command channel | Fork into `apm_ingest.py`. Reuse `_sha256`, `_new_api_key` (swap `zpa_`→`zpi_`/`zpr_`), `_strip_bearer`, `_client_ip`, and the `/enroll` one-time-token consumption verbatim |
| `/opt/zenplus/server/app/api/v1/agents.py:93` (`_authenticate`) | Constant-time `hmac.compare_digest` of `sha256(bearer)` vs `agents.api_key_hash`, status gating | The APM ingest-key auth primitive. The Go collector validates the same `zpi_` bearer against `apm_ingest_keys.key_hash` (read-through cache). RUM (`zpr_`) needs a distinct public, origin-scoped variant (no `X-Agent-Id` header) |
| `/opt/zenplus/server/app/services/host_metric_service.py:498` (`ingest_host_metric_batch`) | Groups a typed batch by `kind`, fans out to per-kind ClickHouse inserters; `_ts/_f/_i` coercion helpers | Mirror as `ingest_apm_batch` with kind→table inserters (`spans`/`rum_events`/`profiles`/`apm_logs`). Reuse the columnar `client.insert(table, rows, column_names=[...])` and the defensive coercion helpers |
| `/opt/zenplus/server/app/core/database.py:38` (`get_clickhouse_client`) | Process-wide `clickhouse_connect` singleton (avoids FD leak) | Reuse directly for span/RUM/profile inserts. **For trace volume, wrap in a buffered async batch writer** (queue + flush by size/interval) instead of per-request inserts |
| `/opt/zenplus/scripts/migrate-030-server-monitoring.sql:120` | `agent_enrollment_tokens` (`token_hash`, `max_uses`, `expires_at`, `revoked_at`, `consumed_ip`) | `apm_enrollment_tokens` copies this column-for-column; `apm_ingest_keys` copies the `api_key_hash`/`api_key_prefix` pattern from `agents` |
| `/opt/zenplus/server/app/schemas/agent.py:299` | `AgentResultsBatch`/`MetricSample` envelope + response (`accepted`/`rejected`/`duplicates`/`backpressure`/`errors`) | Model the FastAPI OTLP/HTTP fallback response on this; **wire the today-unused `backpressure` field** for load shedding. OTLP itself uses protobuf/JSON shapes translated to row dicts |
| `/opt/zenplus/server/app/services/server_health_service.py:266` (`health_sweeper_loop`) | Background loop rolling agents stale→offline, raising/resolving alerts on missing heartbeats | Pattern for `apm_nodata_sweeper` (service reporting-stopped alerts), registered in `main.py` alongside the existing loops |

**Reuse summary:** the enrollment + hashed-bearer + columnar-insert model transfers directly. The audit is explicit, however, that the current ingest path is **synchronous, per-request, single-HTTP-client, with no buffering/backpressure and a no-op dedup** — which is why the blueprint puts the OTLP data plane in a dedicated Go collector and keeps the FastAPI fallback only for low-volume/RUM. See §4 (risks) and `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md`.

### 2.3 ClickHouse data platform + migration conventions

The data platform splits storage by access pattern (ClickHouse `zenplus` for time-series, Postgres `zenplus` for config/state) and enforces a strict house style APM must follow exactly — with one deliberate new convention (codecs).

| File path | What it is | How APM reuses it |
|---|---|---|
| `/opt/zenplus/scripts/init-clickhouse.sql` (ping_metrics raw/5m/1h) | Canonical 3-tier: raw MergeTree → 5m → 1h, each fed by `CREATE MATERIALIZED VIEW ... TO target` with `toStartOfFiveMinutes`/`toStartOfHour`, `sample_count` weighting, monthly partitions, TTL DELETE | The exact shape for `apm_span_metrics_5m`/`_1h` RED rollups (`apm_span_metrics_5m_mv` etc.). **Dashboards never scan raw spans** — they read these MVs |
| `/opt/zenplus/scripts/migrate-030-host-metrics-clickhouse.sql` | Multi-dimension series: `LowCardinality` dims in `ORDER BY` before `timestamp`, `Array` columns, per-tier TTLs, idempotent `CREATE IF NOT EXISTS`, auto-applied | Reference for `apm_spans`/`apm_exceptions`/`apm_rum_events` dimension layout and idempotent auto-applying CH migration |
| `/opt/zenplus/scripts/init-clickhouse.sql` (flow_records) + `migrate-20260506-netflow-clickhouse.sql` | Highest-volume raw table: `PARTITION BY toYYYYMMDD` (daily), wide `ORDER BY`, SummingMergeTree 5m rollup | **Daily-partition precedent** for `apm_spans`/`apm_rum_events`/`apm_logs`/`apm_exceptions`/`apm_profiles`; SummingMergeTree for additive `request_count`/`error_count` |
| `/opt/zenplus/updater/clickhouse_sync.py` | Auto-applies `migrate-*-clickhouse.sql` not in the CH `schema_migrations` ledger on **every** OTA update; `_LEGACY_BASELINE` holds unsafe-to-replay files | `migrate-039-apm-clickhouse.sql` auto-applies with zero release-builder action. **Every statement must be `CREATE/ALTER ... IF NOT EXISTS`; never add APM files to `_LEGACY_BASELINE`** |
| `/opt/zenplus/scripts/run-migrations.py` + `migrations.lock` + `/opt/zenplus/Documentation/18-MIGRATION-RUNNER.md` | PG runner: discovers `migrate-*.sql` (excludes any filename containing `clickhouse`), tracks checksums, lints against the lockfile | `migrate-039-apm.sql` is forward-only/immutable once released; after adding, run `python3 scripts/build-release.py lint-migrations --update-lock` and commit migration + lockfile together |
| `/opt/zenplus/scripts/migrate-038-report-schedules.sql` | Most recent PG migration (038) — idempotent header style, JSONB defaults, CHECK enums, SMALLINT range checks | Confirms **039 is the next free PG number**; copy its header/idempotency style for `migrate-039-apm.sql` |
| `/opt/zenplus/scripts/init-postgres.sql` | PG house style: `UUID PK DEFAULT gen_random_uuid()`, `TIMESTAMPTZ DEFAULT NOW()`, JSONB `'[]'`/`'{}'` + GIN, `VARCHAR(n) CHECK (... IN (...))`, partial indexes | Exact conventions for `apm_services`/`apm_slos`/`apm_ingest_keys` etc. — config/registry/SLO rows only; spans/RUM/profiles never go in Postgres |
| `/opt/zenplus/server/app/core/database.py` | `get_db` (SQLAlchemy async) + `get_clickhouse_client` (singleton) | APM ingest/query services read/write CH via the singleton, config via `get_db()` |

**New convention APM introduces (deliberately, not silently):** the codebase uses **zero** ClickHouse codecs today — a clean baseline. APM's volume justifies introducing `CODEC(Delta(8), ZSTD(1))` on nanosecond timestamps and `CODEC(ZSTD(1))` on id/blob/string columns. This is documented as a new platform convention in `03-ARCHITECTURE-AND-DATA-MODEL.md` §Codecs and `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md`.

### 2.4 Multi-condition alert engine + channels

ZenPlus has a single unified alerting backbone shared by every domain (ping, service-check, SNMP, host, traps), built on two Postgres tables — `alert_rules` and `alerts`. APM registers new `apm_`-prefixed metric keys and **rides this engine** rather than building a parallel stack.

| File path | What it is | How APM reuses it |
|---|---|---|
| `/opt/zenplus/server/app/api/v1/alert_engine.py` (`_conditions_match`, `_eval_one`, `_cmp`, lines 314–365) | Evaluates compound `conditions[]` (AND/OR via `condition_logic`); falls back to flat metric/operator/threshold | APM latency/error_rate/apdex/throughput become new metric keys usable in `conditions[]`. Push-path metrics feed the `metric_values` dict (lines 442–446) and work with **zero engine changes** |
| `/opt/zenplus/server/app/services/network_alert_service.py` | Every 60s loads rules `WHERE metric IN(...)`, reads ClickHouse fleet aggregates, raises/resolves with dedupe via `_active_alert_id` | **Clone as `apm_alert_service.py`**: define `APM_METRICS`, read RED from `apm_span_metrics_5m` (p95 via `quantilesTDigestMerge`), reuse `_cmp`/`_raise`/`_resolve`/`_notify`. Register `apm_alert_evaluator_loop` in `main.py` startup |
| `/opt/zenplus/server/app/services/host_alert_service.py` (`dispatch_to_channels`, lines 107–149) | Single fan-out: email(HTML)/sms/webhook/slack/teams/discord/pagerduty, resolving channel type + gateway | APM evaluators call `dispatch_to_channels(db, rule.notify_channels, ctx)` directly — **no new delivery code**. Extend the PagerDuty `dedup_key` to carry the APM service/SLO id |
| `/opt/zenplus/server/app/services/email_render.py` (`build_alert_email_html`) | Builds the HTML alert email from `{severity, status, title, message, details:[(label,value)], timestamp}` | Pass APM `details[]` rows (e.g. `('Service','checkout')`, `('p95','820ms')`, `('SLO burn','14.4x')`) — already wired through the email branch |
| `/opt/zenplus/server/app/services/alert_schedule.py` (`notifications_allowed`) | Gates outbound notifications to a rule's `schedule_start/end` + `schedule_days` in appliance timezone; alert row always recorded | Call **verbatim** before APM dispatch, exactly as host/network evaluators do |
| `/opt/zenplus/server/app/api/v1/alert_rules.py` (`_CONDITION_METRICS` regex) + `host_alert_rules.py` | REST CRUD, metric-key allowlist regex, compound conditions, preview | Add `apm_*` keys to the regex, OR add an `apm_alert_rules.py` router filtering `metric LIKE 'apm\_%'` (the established new-domain pattern) |
| `/opt/zenplus/dashboard/src/components/forms/AlertRuleFormDialog.tsx` (`NETWORK_METRICS`) | Rule editor: metric registry, `conditions[]` editor, AND/OR toggle, schedule days | Add APM metric entries (value/label/units) and an `apm`/`service` scope source; conditions editor + channel routing reused unchanged |

**The new metric keys** (added to the `alert_rules.metric` CHECK in `migrate-039-apm.sql` — re-declaring the full list per the migrate-035/037 lockstep convention — plus the Pydantic regex and the TS registry):

```
apm_latency_p50, apm_latency_p95, apm_latency_p99,
apm_error_rate, apm_throughput, apm_apdex,
apm_slo_burn, apm_synthetic_down, apm_anomaly
```

**Reuse summary:** APM alerting (F8 / AM-E6) is overwhelmingly verbatim/fork reuse. Conventions to honor: metric keys are gated in **four places** (DB CHECK, two API regexes, the TS list) with no single source of truth; periodic evaluators run in **both uvicorn workers** so every raise must be dedupe-guarded; quiet hours gate notification, never the alert row.

### 2.5 Dashboard / UI conventions

The dashboard is a Vite/React SPA with a centralized route table (`App.tsx`), react-query v5 over a single axios client (`lib/api.ts`, baseURL `/api/v1`), and a static sidebar in `Layout.tsx`. The **Servers module** is the canonical reference APM clones.

| File path | What it is | How APM reuses it |
|---|---|---|
| `/opt/zenplus/dashboard/src/App.tsx` | Centralized `<Routes>` under the Protected `<Layout>` Outlet; flat `path="section/sub"` + `:id` detail | Register every `/apm/*` route here (`/apm`, `/apm/services/:id`, `/apm/traces/:traceId`, `/apm/service-map`, `/apm/errors/:id`, `/apm/rum`, `/apm/synthetics/:id`, `/apm/slos/:id`, `/apm/settings`) |
| `/opt/zenplus/dashboard/src/components/Layout.tsx` | `sections: NavSection[]` sidebar + `routeLabels` + `routeSections` breadcrumb maps | Insert the `APM` `NavSection` **between `Servers` and `MAP`**; add `routeLabels`/`routeSections` for **every** `/apm/*` path or breadcrumbs break. Icons: Services→`Boxes`, Traces→`GitBranch`, Service Map→`Network`, Errors→`Bug`, RUM→`MonitorSmartphone`, Synthetics→`Radar`, SLOs→`Target`, Settings→`SlidersHorizontal`; `/apm`→`Layers` (`end:true`) |
| `/opt/zenplus/dashboard/src/pages/servers/ServerDetailPage.tsx` | 12-tab detail driven by `?tab=` `useSearchParams` + `const TABS=[{key,label,icon}]`; per-tab `useQuery`; `PanelHeader`/`MetricChartCard` helpers; recharts AreaCharts | Clone as `ServiceDetailPage.tsx`. Use the **hand-rolled `border-b-2` tab bar, not `ui/Tabs`**. APM tabs: `overview`, `performance`, `traces`, `dependencies`, `errors`, `database`, `profiling`, `slos`, `deployments`, `logs`, `infrastructure`, `settings` |
| `/opt/zenplus/dashboard/src/pages/servers/ServersPage.tsx` | List: URL-driven filters via `useSearchParams`, STATUS_CHIPS with facet counts, table/cards toggle, sortable headers, pagination, bulk actions | Clone as `ServicesPage.tsx` (health chips healthy/degraded/critical; env/language/team/tag filters; sort by p95/error rate/throughput/apdex). Same skeleton for `ErrorsInboxPage.tsx`/`SyntheticsPage.tsx` |
| `/opt/zenplus/dashboard/src/components/servers/shared.tsx` | Module primitives: status badges, `OsIcon`, `UsageBar`, `TagList`, `KpiTile` | Create `components/apm/shared.tsx`: `ServiceHealthBadge`, `LanguageIcon`, `LatencySparkline`/`ErrorRateBar`, `SpanKindBadge`; reuse `KpiTile`/`TagList` |
| `/opt/zenplus/dashboard/src/lib/api.ts` | Shared axios instance (baseURL `/api/v1`, bearer interceptor, 401→`zp-auth-expired`) | Call `/api/v1/apm/*` via the same `api` import — do **not** create a second axios instance |
| `/opt/zenplus/dashboard/src/components/TimeRangePicker.tsx` | URL-driven `?range=1h|24h|7d|1M|custom` → `{hours,fromISO,toISO,label}` | Reuse verbatim at the top of every APM metrics view |

**Net-new UI with no reuse** (build under `components/apm/`): the **trace waterfall/flame** (custom SVG/flex horizontal bars — no primitive exists) and the **service-map graph** (echarts `graph` layout or custom SVG; `ManualMapsPage` is a bespoke topology canvas, not reusable). A `dashboard/src/types/apm.ts` must be created (`types/servers.ts` is the template). Do **not** edit the stale `components/layout/Sidebar.tsx` — `Layout.tsx` holds the active sidebar.

---

## 3. Gap analysis — what's missing

The platform has **no application-observability primitives whatsoever**. The following are greenfield and must be designed and built. Each row names the blueprint artifact that closes it.

| Gap (absent today) | Evidence from audit | Closed by |
|---|---|---|
| **No distributed tracing / span data model** | No trace/span/parent_id schema in PG or CH anywhere; no waterfall UI primitive | `apm_spans` + `apm_traces_resource` CH tables; `TraceExplorerPage`/`TraceWaterfallPage` (F2 / AM-E2) |
| **No OTLP receiver** | "No OTLP receiver at all (no opentelemetry/OTLP/gRPC anywhere in `server/app`)"; transport is agent-pull JSON batches | Go collector on 4317/4318 + FastAPI OTLP/HTTP fallback (F1 / AM-E1); see `05-…` |
| **No service map / topology** | No node-link graph component; `ManualMapsPage` is a bespoke canvas, not reusable; no dependency-edge store | `apm_service_graph` (SummingMergeTree, servicegraph connector) + `ServiceMapPage` (F4 / AM-E3) |
| **No RUM** | No browser/mobile telemetry, no Core Web Vitals, no session model; no public origin-scoped ingest key | `apm_rum_events`/`apm_rum_vitals_5m` + public `zpr_` beacon `/api/v1/apm/rum/ingest` (F15 / AM-E10) |
| **No continuous profiling** | No pprof storage, no span→flamegraph link; Playwright exists only as an MCP tool | `apm_profiles` + OTLP profiles endpoint `/v1development/profiles` (F18 / AM-E11) |
| **No error tracking / issue grouping** | No exception fingerprinting/grouping, no triage workflow | `apm_exceptions` (CH, `group_id` fingerprint) + `apm_error_issues` (PG triage) + `ErrorsInboxPage` (F5 / AM-E4) |
| **No SLO / error-budget engine** | "No SLO/error-budget primitives exist anywhere"; engine does only instantaneous value-vs-threshold over one window | `apm_slos` (PG) + `apm_slo_burn_loop` (multi-window multi-burn) (F7 / AM-E6); see §3.1 |
| **No multi-step / browser synthetics** | Every checker is single-shot; no ordered steps, no headless browser, no per-step waterfall, no probe locations | Extend poller `CheckOne` + `apm_synthetic_results` (F14 / AM-E9) |
| **No anomaly / auto-baselining** | All rules are static thresholds; no seasonality/z-score/forecast | `apm_anomaly_loop` → derived `apm_anomaly` metric (F19 / AM-E12) |
| **No DB/query monitoring** | No query digests, plan capture, or source-side obfuscation | `db_*` promoted columns on `apm_spans` + DB agent (F16 / AM-E11) |
| **No trace↔log↔metric correlation / exemplars** | Logs not stamped with trace_id; no metric→trace pivot | `apm_logs` (shared `trace_id` + bloom index) + exemplars (F6 / AM-E5) |
| **No generalized silences** | `alert_silences.server_id NOT NULL`; snooze rejects non-server alerts | Generalized polymorphic-scope silences = E5 deliverable; until then APM uses quiet-hours gating (§3.2) |
| **No sampling / cardinality enforcement at ingest** | `cardinality_limits` is advisory only; no head/tail sampling | Go collector `tailsamplingprocessor` + `apm_sampling_rules` (F10 / AM-E8); see §4 |
| **No real idempotency/dedup** | "`duplicates=0` … cheap and good enough for MVP"; no batch ledger | trace_id/span_id keys; decision documented in `05-…` and `08-…` test plan |

### 3.1 The SLO gap in detail

The audit is unambiguous: the existing engine does **instantaneous value-vs-threshold comparisons over a single fixed window** (`min_duration` vs `DEFAULT_WINDOW_S`). Multi-window multi-burn-rate alerting **cannot be expressed** by a single `{metric,operator,threshold}` or even the AND/OR `conditions[]` array, because every condition shares one evaluation window. APM therefore introduces a dedicated SLO model:

- **`apm_slos`** (PG): `sli_type` (availability/latency/error_rate/custom), `target`, `window_days` (7/30/90), `latency_threshold_ms`, scope (service/operation), burn-alert config.
- **`apm_slo_burn_loop`** (new background task) computes per SLO, per window: `SLI = good/total` from `apm_span_metrics_5m`; `error_budget_remaining = (SLI − target)/(1 − target)`; **burn rate** = budget consumed per unit time. It implements the Google SRE Workbook canonical 99.9% config — **Page** at 14.4x/1h (short 5m), **Page** at 6x/6h (short 30m), **Ticket** at 1x/3d (short 6h), both windows must breach — and maps severity to channel routing (page→PagerDuty/SMS, ticket→email/Slack).

Additionally, `max_repeat` and `cooldown` exist on `alert_rules` today but are **defined-but-unenforced** in every evaluator; the new APM evaluators enforce them for re-notify/hysteresis (AM-E6).

### 3.2 The silence gap in detail

`alert_silences` is **server-scoped only** (`server_id NOT NULL` FK; the snooze API rejects non-server alerts with "Only server alerts can be snoozed"). Service/SLO-scoped APM alerts therefore cannot be silenced in v1. The generalized polymorphic-scope/dedupe silence model is an **E5 deliverable**; until it lands, APM alerts honor **quiet-hours gating only** via `notifications_allowed(...)`. The blueprint pins `env`/`team`/`owner`/`tags` columns on `apm_services` now so the future RBAC/multi-tenant layer can scope silences, quotas, and dashboards without schema churn.

---

## 4. Reuse map

APM capability → existing ZenPlus component to extend → net-new build. This is the load-bearing table of the document.

| APM capability | Existing component to extend (reuse) | Net-new build |
|---|---|---|
| **OTLP ingestion** (F1/AM-E1) | `agents.py` auth helpers (`zpi_`); `get_clickhouse_client` singleton; `AgentResultsBatch` response shape | Go OTLP collector (4317/4318); FastAPI `apm_ingest.py` OTLP/HTTP fallback; buffered async batch writer; `apm_ingest_keys`/`apm_enrollment_tokens` |
| **Distributed tracing + waterfall** (F2/AM-E2) | CH MergeTree idiom; daily-partition (flow_records) precedent | `apm_spans` + `apm_traces_resource` tables; trace-id bloom index; `TraceExplorerPage`/`TraceWaterfallPage` waterfall component |
| **Service registry + RED + apdex** (F3/AM-E3) | ping/host `*_5m_mv TO *_5m` rollup pattern; Servers list/detail UI; `apm_services` (PG, Servers-model style) | `apm_span_metrics_5m`/`_1h` MVs (tdigest state); apdex bucket query; `ServicesPage`/`ServiceDetailPage` |
| **Service map / topology** (F4/AM-E3) | SummingMergeTree (flow_traffic_5m) idiom | `apm_service_graph` + servicegraph connector/MV; echarts `graph` `ServiceMapPage` |
| **Error tracking / issues** (F5/AM-E4) | Alerts UI; PG house style | `apm_exceptions` (CH, `group_id` fingerprint); `apm_error_issues` (PG triage); `ErrorsInboxPage` |
| **Trace↔log↔metric correlation** (F6/AM-E5) | host metric tables; shared `trace_id` convention | `apm_logs` (CH) + bloom/tokenbf index; exemplar wiring |
| **SLO + error-budget burn** (F7/AM-E6) | `alert_rules`/`alerts`; `dispatch_to_channels`; `notifications_allowed`; `network_alert_service.py` evaluator shape | `apm_slos` (PG); `apm_slo_burn_loop` (multi-window); `SlosPage`/`SloDetailPage` burn chart |
| **APM alerting** (F8/AM-E6) | full alert engine: `conditions[]` AND/OR, channels, quiet hours, `email_render` | `apm_*` metric keys (CHECK+regex+UI); `apm_alert_service.py` + `apm_alert_evaluator_loop` |
| **Deployment / change tracking** (F9/AM-E7) | recharts `ReferenceLine` markers | `apm_deployments` (PG); `deployment_id` on spans; version-vs-version compare |
| **Sampling pipeline** (F10/AM-E8) | — (no precedent) | Go collector `tailsamplingprocessor` (head+tail); `apm_sampling_rules`; `zp.retain_reason` tagging |
| **PII scrubbing** (F11/AM-E8) | platform credential-handling discipline (SSH/SNMP/Windows) | Go collector attribute/redaction/filter/OTTL processors; `apm_scrubbing_rules` + default rules |
| **Ingest-key auth + enrollment** (F12/AM-E1) | `agents.py` `_authenticate`/`_new_api_key`/`/enroll`; `agent_enrollment_tokens` schema | `apm_ingest_keys`/`apm_enrollment_tokens`; public `zpr_` origin-scoped RUM key path |
| **Dashboards + overview** (F13) | recharts/echarts; `TimeRangePicker`; `KpiTile` | `apm_dashboards` (PG); `ApmOverviewPage` |
| **Synthetic monitoring** (F14/AM-E9) | poller `engine.go` scheduler/retry/flap/maintenance + `checker/`; `service_checks` infra; ServiceCheckDetail UI | synthetic/browser `CheckOne` case; `apm_synthetic_monitors`/`apm_synthetic_results`; step-waterfall |
| **RUM + Core Web Vitals** (F15/AM-E10) | — (no RUM precedent) | RUM SDK; public `zpr_` beacon `/apm/rum/ingest` (CORS); `apm_rum_events`/`_vitals_5m`; `RumPage` |
| **DB/query monitoring** (F16/AM-E11) | `apm_spans` `db_*` promoted columns | DB agent (digests/plans, source-side obfuscation); database tab |
| **eBPF zero-code agent** (F17/AM-E11) | poller/agent fleet packaging; OTLP into same collector | Beyla/OBI-style eBPF agent |
| **Continuous profiling** (F18/AM-E11) | — | `apm_profiles`; OTLP profiles endpoint; span→flamegraph link |
| **AI anomaly / auto-baselining** (F19/AM-E12) | alert-engine threshold on derived metric | `apm_anomaly_loop` (seasonality + std-dev) → `apm_anomaly` |
| **Causal-lite RCA** (F20/AM-E12) | `_find_suppressing_dependency` topology-suppression idea; `alerts` correlation | graph-walk over `apm_service_graph`; symptom→single-incident collapse |
| **Full-stack correlation panels** (F21/AM-E12) | existing `host_*_metrics`, `snmp_*`, `flow_records` tables — **unique to ZenPlus** | span→host→SNMP→flow correlation panels |
| **Session Replay** (F22) | — | DOM playback (explicitly P4/later, no reuse) |
| **Business-impact tagging** (F23) | `tags` on `apm_services`/spans | endpoint criticality + incident ranking |

---

## 5. Risks of retrofitting

APM is the **highest-volume, highest-cardinality data on the platform** and is being retrofitted onto a stack tuned for low-rate device/host metrics. The principal risks, each tied to a mitigation the blueprint pins.

### 5.1 Trace volume vs the current JSON ingestion path

The current ingestion path (`agents.py` → `host_metric_service.ingest_host_metric_batch` → per-request `client.insert`) is, per the audit, **synchronous, per-request, single-HTTP-client, with no buffering/backpressure beyond an unused `backpressure` field, and a no-op dedup**. It also mixes a CH insert with multiple Postgres upserts and health/baseline recompute **in one request handler** — far too heavy for per-span/per-trace rates. Pointing OTLP SDKs/Collectors at this path would saturate it.

```
TODAY (host metrics, ~per-minute batches)          APM (spans, low-millions/day)
SDK ──POST JSON──▶ FastAPI handler                 SDK/Collector ──OTLP──▶ Go collector
                  │ client.insert (sync)                              │ memory_limiter
                  │ + N Postgres upserts                              │ → tail_sampling
                  │ + health/baseline recompute                      │ → batch
                  ▼ (one request thread)                             ▼ buffered native INSERT
              ClickHouse                                         ClickHouse (lean append-only)
```

**Mitigations (pinned):** the OTLP data plane is a **dedicated Go collector** (4317/4318) using the upstream ClickHouse exporter with `memory_limiter` (first processor, load-shedding) + `batch`; FastAPI keeps only a **lightweight OTLP/HTTP fallback** for low-volume/single-binary installs and the RUM beacon, and that fallback wraps the singleton in a **buffered async batch writer** (queue + flush by size/interval) rather than per-request inserts. APM ingest is a **lean append-only CH write** with async post-processing — never the host-metric handler's heavy mixed path. The three-stage decoupling (metricize 100% at ingest → head sampling → tail sampling) means dashboards/alerts read pre-aggregated RED rollups and stay accurate even when raw traces are dropped. See `03-…` §3 and `05-…`.

### 5.2 Cardinality

`trace_id`/`span_id` cardinality is **orders of magnitude higher** than any existing entity (`device_id`/`server_id`), and there is **no precedent in the repo** for keying on it. A naive `ORDER BY (..., trace_id)` or indexing arbitrary high-cardinality attribute maps would destroy compression and query performance. The existing `cardinality_limits` config is **advisory only** and enforces nothing at ingest.

**Mitigations (pinned):** `apm_spans` orders by `(service_name, name, ts_bucket, trace_id)` with `ts_bucket` (30-min `UInt32` floor) as the 3rd dim for partition pruning, and a **`bloom_filter(0.001)` skip index** on `trace_id` for point lookups — never a leading key. Attributes split into **typed maps** (`attributes_string`/`number`/`bool`) plus **promoted hot columns only** (`http_*`/`db_*`/`rpc_*`) so UI filters hit indexed columns, not unbounded maps. `operation`/`http_route` are `LowCardinality`; unbounded text (`db_statement`, `exception_message`, log `body`) is normalized (query digests) or kept out of `ORDER BY`. A resource-fingerprint side table (`apm_traces_resource`, ReplacingMergeTree) + CTE/`GLOBAL IN` avoids full scans on resource filters. Allow-lists + cardinality caps apply on promoted dimensions; span→RED rollups collapse high-cardinality detail into bounded timeseries.

### 5.3 Retention

Every existing table is TTL-bounded (raw 14–60d, rollups 90–365d), but trace-shaped data needs **shorter raw TTLs with atomic reclamation** to stay single-node-affordable, and there is no codec/retention-tier guidance for it. A failed CH migration is **best-effort and silently retried next update** by `clickhouse_sync.py` — ingestion code must tolerate a missing-table window (the original `host_*` "Table does not exist" failure mode the auto-apply pipeline was built to fix).

**Mitigations (pinned):** daily partitions (`toYYYYMMDD`) + `ttl_only_drop_parts = 1` make TTL a cheap atomic partition drop. Retention tiers: spans 7d raw / metrics 5m 90d / 1h 395d; exceptions 30d (high-value); RUM 14d / vitals 90d; logs 14d; profiles 14d; synthetic 90d raw / 5m 395d; service graph 90d. The new `CODEC(Delta(8), ZSTD(1))`/`CODEC(ZSTD(1))` convention (a deliberate first for the platform) plus `LowCardinality` + typed maps yield ~9–10x compression, so a single node sustains low-millions of spans/day. Ingestion code must guard against the missing-table window during the auto-apply cycle.

### 5.4 Cross-cutting retrofit risks

- **Dual-worker dedup.** Periodic evaluators run in **both uvicorn workers**; every APM raise (and every `apm_slo_burn_loop` decision) must be dedupe-guarded exactly like `_active_alert_id`/`create_server_alert`, or alerts double-fire. (Verified in `08-…` test plan: alert dedupe across workers.)
- **Four-place metric registry.** Adding an `apm_` metric touches the DB CHECK, two API regexes, and the TS `NETWORK_METRICS` list — there is **no single source of truth**, so drift is a standing risk; the migration must re-declare the **full** CHECK list per the migrate-035/037 lockstep convention.
- **`%%` escaping.** Literal `%` in any ClickHouse expression or LIKE pattern that passes through the Python-formatted updater/migration path must be escaped as `%%`.
- **Migration immutability.** `migrate-039-apm.sql` is forward-only once released; schema changes are new numbered migrations, and the lockfile (`scripts/migrations.lock`) must be committed with it.
- **Inline-blob convention vs profiles.** The platform stores everything inline in ClickHouse; pprof blobs (`apm_profiles.payload String CODEC(ZSTD(3))`) follow this, with object-storage pointers flagged as a revisit-if-problematic risk in `03-…`.

---

*End of document. Detail for every artifact named here lives in the sibling docs listed under "Related documents"; any deviation from the pinned names/decisions must be raised against the blueprint, not decided locally.*
