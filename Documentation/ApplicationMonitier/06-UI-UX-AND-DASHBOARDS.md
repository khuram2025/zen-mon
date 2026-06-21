# Application Monitoring — UI/UX & Dashboards

*Status: Design proposal · 2026-06-21 · Part of the ZenPlus Application Monitoring design set.*

This document is the binding frontend contract for the ZenPlus Application Monitoring (APM) module. It specifies the complete `/apm/*` page family — navigation and routing, page-by-page layouts with ASCII wireframes, the `?tab=` service-detail panel set, the two net-new visual components (trace waterfall/flame and the echarts-`graph` service map), and the interaction patterns that stitch the signals together (metric spike → exemplar trace, trace → correlated logs, error → trace, RUM → backend trace). Every page is built to ZenPlus's existing React/Vite/Tailwind conventions: `@tanstack/react-query` v5 against the shared axios client (`lib/api.ts`, baseURL `/api/v1`), the hand-rolled `?tab=` border-b tab bar from `ServerDetailPage.tsx`, URL-driven filters/chips/facets from `ServersPage.tsx`, recharts `AreaChart` for module trend panels and echarts-for-react for dense series, and the per-module `shared.tsx` primitives convention (`KpiTile`, status badges, tag pills). Every route, page filename, API endpoint, table name, metric key, and epic ID below is taken verbatim from the authoritative blueprint; this document does not invent or contradict any pinned decision. Where a UX detail was not pinned (a column order, a query-param name, a panel arrangement), it is proposed here as a v1 default and flagged as such. For the API contracts see `04-FEATURE-SPECIFICATION.md`; for the tables read see `03-ARCHITECTURE-AND-DATA-MODEL.md`.

## Related documents

- `00-INDEX.md` — navigation hub, document summaries, epic list, reading order
- `01-MARKET-RESEARCH.md` — competitive landscape (Datadog/New Relic/Dynatrace/AppDynamics/SigNoz/Grafana/Sentry/Honeycomb/OTel) and the wedge
- `02-CURRENT-SYSTEM-AND-GAP-ANALYSIS.md` — what ZenPlus already has vs. the APM gaps, with the reuse map
- `03-ARCHITECTURE-AND-DATA-MODEL.md` — Go-collector decision, pipeline diagram, full ClickHouse + Postgres DDL, migration numbers
- `04-FEATURE-SPECIFICATION.md` — per-feature specs (F1–F23), API contracts, acceptance criteria
- `05-INSTRUMENTATION-AGENTS-AND-INGESTION.md` — OTLP protocol, ZenPlus OTel distro/eBPF agent, ingest-key enrollment, sampling/scrubbing pipeline, collector internals
- `06-UI-UX-AND-DASHBOARDS.md` — **this document**
- `07-ROADMAP-AND-EPICS.md` — the 4 phases and 12 epics (AM-E1..AM-E12) expanded
- `08-TASK-LIST-AND-TEST-PLAN.md` — epic→task breakdown plus the test plan

---

## 1. Design principles & the read-the-right-stage rule

Every APM page obeys five rules carried over from the blueprint and the existing dashboard conventions. They are non-negotiable because they determine both cost and visual consistency.

1. **Dashboards never scan raw spans.** Every aggregate panel (RED charts, service KPIs, apdex, SLO burn, RUM vitals) reads pre-aggregated rollups — `apm_span_metrics_5m`/`_1h`, `apm_service_graph`, `apm_rum_vitals_5m`, `apm_synthetic_results_5m`. Only the **Trace Explorer**, the **Trace Waterfall**, the **Errors occurrence list**, and the **Logs** panel read raw rows (`apm_spans`, `apm_exceptions`, `apm_logs`), and they always do so under a tight time + entity filter. The frontend enforces this by routing each query to the correct backend endpoint (§4 of `04-FEATURE-SPECIFICATION.md`), never by composing ClickHouse in the client.

2. **URL is the source of truth for view state.** Filters, sort, page, selected tab, time range, selected trace/span, and selected error group all live in `useSearchParams`, exactly as `ServersPage.tsx` and `ServerDetailPage.tsx` do today. This makes every APM view bookmarkable, shareable (the load-bearing property for incident handoff), and back-button-correct. A `setParam(key,val)` helper resets `page=1` on any filter change.

3. **One time vocabulary.** Every metrics view mounts the existing `TimeRangePicker` / `useTimeRange` (`?range=1h|24h|7d|1M|custom&from&to`) at the top-right of the page header. No APM page invents its own range control. The returned `{range:{hours,fromISO,toISO,label}}` is threaded into every `useQuery` key so a range change refetches deterministically.

4. **One design system.** All atoms come from `components/ui/` (`Badge`, `Button`, `Card`, `Table`, `Select`, `Input`, `Dialog`, `ConfirmDialog`, `Skeleton`, `Switch`, `toast`). Colors are CSS-variable tokens (`bg/surface/surface2/border/text/text2/muted/primary/success/warning/danger/info/accent`) via `bg-primary`, `text-muted`, and `rgb(var(--token))` inside chart options. No hardcoded hex outside echarts palettes that already follow the dark theme. The service-detail tab bar is the **hand-rolled `border-b-2` button row** keyed on `?tab=` — not `ui/Tabs` — to match Servers exactly.

5. **react-query keys are hierarchical under `['apm', …]`.** Every fetch keys `['apm', <area>, …params]` so a mutation can `qc.invalidateQueries({ queryKey: ['apm'] })` (or a narrower prefix) and the whole module refreshes. Live views use `refetchInterval` 15–30 s; pure-historical views use `staleTime` and no polling.

### 1.1 The signal-correlation map (what links to what)

```
                 click a latency spike on any RED chart
                                   │  exemplar trace_id on the data point
                                   ▼
   apm_span_metrics ──exemplar──► TRACE WATERFALL (/apm/traces/:traceId)
        ▲                              │                 │
        │ RED                          │ "View logs"     │ "Profiles" tab (span)
   SERVICE DETAIL ◄──drill──┐          ▼                 ▼
   (/apm/services/:id)      │   LOGS panel (?tab=logs)  FLAMEGRAPH (apm_profiles)
        │ map node          │   WHERE trace_id=…         span→pprof
        ▼                   │          ▲
   SERVICE MAP ─edge RED────┘          │ trace_id pivot
   (/apm/service-map)                  │
                              ERRORS INBOX ──group→occurrence──► WATERFALL
                              (/apm/errors)   (apm_exceptions.trace_id)
                                   ▲
              RUM error ──backend_trace_id──┘     SYNTHETIC step ──backend_trace_id──► WATERFALL
              (/apm/rum)                          (/apm/synthetics/:id)
```

Everything pivots on three shared IDs already present in the data model: `trace_id` (spans ↔ logs ↔ exceptions ↔ RUM ↔ synthetics ↔ profiles), `group_id` (exception grouping), and `service_name` (the RED/topology join key). The frontend never computes these joins; it carries the ID in the URL and asks the matching endpoint.

---

## 2. Navigation, routes & breadcrumbs

### 2.1 Sidebar section

A new `NavSection` labelled **`APM`** is inserted into the static `sections: NavSection[]` array in `dashboard/src/components/Layout.tsx`, positioned **between the `Servers` section and the `MAP` section** (blueprint §2.1). Do **not** edit the stale `components/layout/Sidebar.tsx` — `Layout.tsx` holds the live sidebar.

```ts
// dashboard/src/components/Layout.tsx — sections[] (insert between Servers and MAP)
import {
  Layers, Boxes, GitBranch, Network, Bug,
  MonitorSmartphone, Radar, Target, SlidersHorizontal,
} from 'lucide-react'

{
  label: 'APM',
  items: [
    { to: '/apm',              label: 'Overview',    icon: Layers,            end: true },
    { to: '/apm/services',     label: 'Services',    icon: Boxes },
    { to: '/apm/traces',       label: 'Traces',      icon: GitBranch },
    { to: '/apm/service-map',  label: 'Service Map', icon: Network },
    { to: '/apm/errors',       label: 'Errors',      icon: Bug },
    { to: '/apm/rum',          label: 'RUM',         icon: MonitorSmartphone },
    { to: '/apm/synthetics',   label: 'Synthetics',  icon: Radar },
    { to: '/apm/slos',         label: 'SLOs',        icon: Target },
    { to: '/apm/settings',     label: 'Settings',    icon: SlidersHorizontal },
  ],
}
```

The section landing route `/apm` uses `Layers` with `end: true` so it only highlights on the exact path (the same convention the Servers dashboard landing uses). Active-state, pin/hover collapse, and the primary accent bar are handled by `Layout.tsx` automatically once the items are registered.

### 2.2 Routes (register in `dashboard/src/App.tsx`)

All page components live under `dashboard/src/pages/apm/`, are named exports, and register as flat children of the Protected `<Layout>` `<Outlet>`, mirroring the Servers route block. Heavy pages (Trace Waterfall, Service Map) use `lazy()` + `<Suspense>` per the Reports precedent because they pull in the waterfall renderer and the large echarts `graph`/force-layout view (echarts is already a dependency, but the service-map view is code-split to keep it off the main bundle).

| Route | Page component (`pages/apm/`) | Purpose |
|---|---|---|
| `/apm` | `ApmOverviewPage.tsx` | Section landing / fleet APM dashboard |
| `/apm/services` | `ServicesPage.tsx` | Services list (RED KPI strip + table, filters, chips) |
| `/apm/services/:id` | `ServiceDetailPage.tsx` | Service detail; tabs via `?tab=` |
| `/apm/traces` | `TraceExplorerPage.tsx` | Trace/span explorer (live + indexed) |
| `/apm/traces/:traceId` | `TraceWaterfallPage.tsx` | Single-trace waterfall / flame |
| `/apm/service-map` | `ServiceMapPage.tsx` | Dependency graph (RED-on-edges, echarts `graph`) |
| `/apm/errors` | `ErrorsInboxPage.tsx` | Error issues inbox |
| `/apm/errors/:id` | `ErrorIssueDetailPage.tsx` | Single issue (group) detail |
| `/apm/rum` | `RumPage.tsx` | Real-user monitoring (Core Web Vitals, sessions) |
| `/apm/synthetics` | `SyntheticsPage.tsx` | Synthetic monitors list |
| `/apm/synthetics/:id` | `SyntheticDetailPage.tsx` | Synthetic monitor detail |
| `/apm/slos` | `SlosPage.tsx` | SLO list + error-budget status |
| `/apm/slos/:id` | `SloDetailPage.tsx` | SLO detail + burn chart |
| `/apm/settings` | `ApmSettingsPage.tsx` | Ingest keys, sampling rules, PII scrubbing, retention |

```tsx
// dashboard/src/App.tsx (inside the Protected <Layout> route)
<Route path="apm"                    element={<ApmOverviewPage />} />
<Route path="apm/services"           element={<ServicesPage />} />
<Route path="apm/services/:id"       element={<ServiceDetailPage />} />
<Route path="apm/traces"             element={<TraceExplorerPage />} />
<Route path="apm/traces/:traceId"    element={<Suspense fallback={<PageSkeleton/>}><TraceWaterfallPage /></Suspense>} />
<Route path="apm/service-map"        element={<Suspense fallback={<PageSkeleton/>}><ServiceMapPage /></Suspense>} />
<Route path="apm/errors"             element={<ErrorsInboxPage />} />
<Route path="apm/errors/:id"         element={<ErrorIssueDetailPage />} />
<Route path="apm/rum"                element={<RumPage />} />
<Route path="apm/synthetics"         element={<SyntheticsPage />} />
<Route path="apm/synthetics/:id"     element={<SyntheticDetailPage />} />
<Route path="apm/slos"               element={<SlosPage />} />
<Route path="apm/slos/:id"           element={<SloDetailPage />} />
<Route path="apm/settings"           element={<ApmSettingsPage />} />
```

### 2.3 Breadcrumbs (mandatory)

Every `/apm/*` path **must** be added to both `routeLabels` and `routeSections` in `Layout.tsx`, or breadcrumbs render blank (this is the single most common breakage when adding a module). The detail routes resolve their dynamic segment label from the loaded entity (service name, trace id short form, error type, monitor name, SLO name) supplied by the page via the existing breadcrumb override mechanism.

```ts
// routeLabels (Layout.tsx)
'/apm':              'APM',
'/apm/services':     'Services',
'/apm/traces':       'Traces',
'/apm/service-map':  'Service Map',
'/apm/errors':       'Errors',
'/apm/rum':          'RUM',
'/apm/synthetics':   'Synthetics',
'/apm/slos':         'SLOs',
'/apm/settings':     'Settings',

// routeSections (so breadcrumbs show "APM / Services / checkout")
'/apm':              'APM', '/apm/services': 'APM', '/apm/traces': 'APM',
'/apm/service-map':  'APM', '/apm/errors': 'APM', '/apm/rum': 'APM',
'/apm/synthetics':   'APM', '/apm/slos': 'APM', '/apm/settings': 'APM',
```

### 2.4 Service-detail `?tab=` keys

The service-detail tab bar uses these keys verbatim (blueprint §2.2), driven by `?tab=` on `/apm/services/:id`:

`overview` · `performance` · `traces` · `dependencies` · `errors` · `database` · `profiling` · `slos` · `deployments` · `logs` · `infrastructure` · `settings`

These mirror the `ServerDetailPage.tsx` 12-tab pattern (a `const TABS = [{ key, label, icon }]` array, `tab = params.get('tab') || 'overview'`, hand-rolled `border-b-2` buttons, per-tab child components each owning their own `useQuery`). Tabs gated by phase (`profiling`, `database`) render an inline "Not yet ingesting" empty state until their epic ships (AM-E11), never a broken panel.

---

## 3. Shared frontend scaffolding

### 3.1 `components/apm/shared.tsx`

Following the "every module gets a `shared.tsx`" convention (`components/servers/shared.tsx`), create `components/apm/shared.tsx` with these primitives. `KpiTile` and `TagList`/`TagPill` are imported from `servers/shared` directly (do not duplicate).

| Export | Mirrors | Purpose |
|---|---|---|
| `ServiceHealthBadge` | `ServerStatusBadge` | `healthy`→success, `degraded`→warning, `critical`→danger, `no_data`→outline; dot + label; `title` carries reasons |
| `SERVICE_HEALTH_META` | `SERVER_STATUS_META` | the `Record<health, {label,variant,dot}>` map |
| `LanguageIcon` | `OsIcon` | lucide/brand glyph per `apm_services.language` (java/node/python/go/dotnet/ruby/php/rust) |
| `SpanKindBadge` | — | `SERVER`/`CLIENT`/`PRODUCER`/`CONSUMER`/`INTERNAL` chip, color-coded |
| `StatusCodeBadge` | — | `OK`→success, `ERROR`→danger, `UNSET`→muted (span status) |
| `LatencySparkline` | `UsageBar` | tiny recharts sparkline for table cells (p95 trend) |
| `ErrorRateBar` | `UsageBar` | 0–100% bar tinted by threshold (green<1%, amber<5%, red≥5%) |
| `ApdexBadge` | — | apdex 0–1 with band color (≥0.94 success, ≥0.85 warning, else danger) |
| `SeverityDot` | (alerts) | log/exception severity color dot |
| `RetainReasonChip` | — | `auto`/`error`/`slow`/`rule`/`baseline` chip (from `attributes_string['zp.retain_reason']`) |

### 3.2 `types/apm.ts`

Create `dashboard/src/types/apm.ts` (template: `types/servers.ts`) with the wire types the API returns: `Service`, `ServiceRed`, `Operation`, `Span`, `Trace`, `TraceNode` (waterfall tree), `ServiceMapNode`, `ServiceMapEdge`, `ErrorGroup`, `ErrorOccurrence`, `Slo`, `SloBudget`, `SyntheticMonitor`, `SyntheticResult`, `RumView`, `RumSession`, `WebVitals`, `Deployment`, `IngestKey`, `SamplingRule`, `ScrubbingRule`, `Health` (`'healthy'|'degraded'|'critical'|'no_data'`), `SpanKind`, `StatusCode`.

### 3.3 `hooks/useApm.ts` (optional thin hooks) and query-key registry

Per the Servers convention either inline `useQuery` in pages or centralize in `hooks/useApm.ts`. We standardize the **query-key registry** so invalidation is predictable:

```ts
// dashboard/src/hooks/useApm.ts
export const apmKeys = {
  all:        ['apm'] as const,
  overview:   (r) => ['apm', 'overview', r] as const,
  services:   (f) => ['apm', 'services', f] as const,
  service:    (id) => ['apm', 'service', id] as const,
  red:        (id, r, op) => ['apm', 'service', id, 'red', r, op] as const,
  operations: (id, r) => ['apm', 'service', id, 'operations', r] as const,
  apdex:      (id, r) => ['apm', 'service', id, 'apdex', r] as const,
  traces:     (f) => ['apm', 'traces', f] as const,
  trace:      (tid) => ['apm', 'trace', tid] as const,
  serviceMap: (r, env) => ['apm', 'service-map', r, env] as const,
  errors:     (f) => ['apm', 'errors', f] as const,
  error:      (gid) => ['apm', 'error', gid] as const,
  rum:        (f) => ['apm', 'rum', f] as const,
  synthetics: (f) => ['apm', 'synthetics', f] as const,
  synthetic:  (id) => ['apm', 'synthetic', id] as const,
  slos:       (f) => ['apm', 'slos', f] as const,
  sloBudget:  (id) => ['apm', 'slo', id, 'budget'] as const,
  ingestKeys: () => ['apm', 'ingest-keys'] as const,
}
```

All HTTP goes through the single `api` axios instance (`lib/api.ts`); never a second client. Example: `api.get('/apm/services', { params })`, `api.get(\`/apm/traces/${traceId}\`)`.

### 3.4 Chart conventions (which library, when)

| Panel kind | Library | Pattern |
|---|---|---|
| Service-detail RED trend (latency/throughput/error-rate area charts) | **recharts** | clone `MetricChartCard` from `ServerDetailPage.tsx`: `AreaChart` + `linearGradient` using `rgb(var(--primary)/--info/--success/--danger)`, shared `ttStyle()` tooltip, `ReferenceLine` for thresholds/SLO targets |
| Dense series (overview fleet trace-rate, RUM distribution, latency heatmap) | **echarts-for-react** | `TimeSeriesChart` or a new echarts option with `dataZoom`, `lttb` sampling, dark palette |
| Latency distribution / percentile bands | recharts (`ComposedChart`) | p50/p90/p95/p99 stacked area from tdigest quantiles |
| Apdex / error-budget gauge | recharts `RadialBar` or custom SVG arc | single-value, band-colored |
| **Trace waterfall** | **custom** (§7) | horizontal flex/SVG Gantt bars — no existing primitive |
| **Service map** | **echarts `graph`** (§8) | echarts `graph` series with `force`/`circular` layout — reuses the existing `echarts`/`echarts-for-react` dependency (no new package) |
| **Flamegraph** (profiling tab) | custom SVG | recursive rect layout over `apm_profiles` pprof tree |
| Deployment markers | recharts `ReferenceLine` | vertical lines on RED charts keyed to `apm_deployments.ts` |

Deployment markers are a cross-cutting overlay: any RED `MetricChartCard` accepts a `markers: Deployment[]` prop and renders a labelled `ReferenceLine` per deploy (version tooltip), exactly the New Relic / Datadog change-tracking idiom (`01-MARKET-RESEARCH.md`).

---

## 4. Page: APM Overview (`/apm` → `ApmOverviewPage.tsx`)

The section landing / fleet dashboard. Answers "is the application layer healthy right now, and where do I go." Reads only rollups. KPI strip uses the shared `KpiTile` grid (`grid-cols-2 md:grid-cols-3 xl:grid-cols-6`).

Data: `GET /api/v1/apm/services` (summary + facets) for the fleet roll-up and worst-offenders; `GET /api/v1/apm/services/{id}/red` aggregated fleet-wide for the trend strip; `GET /api/v1/apm/slos` for the budget summary; the shared `/alerts` list filtered to `apm_*` for the incident strip.

```
┌─ APM / Overview ───────────────────────────────── [env ▾ prod]  [TimeRangePicker 24h ▾] ┐
│                                                                                          │
│  ┌ Services ┐ ┌ Throughput ┐ ┌ Error rate ┐ ┌ p95 latency ┐ ┌ Apdex ┐ ┌ Open APM    ┐  │
│  │   42     │ │ 18.4k rpm  │ │   0.62%    │ │   214 ms    │ │ 0.96  │ │ incidents 3 │  │
│  │ 3 critical│ │  ▲ 4% d/d  │ │  ▼ vs base │ │  ▲ 8% d/d   │ │ good  │ │ 1 SLO burn  │  │
│  └──────────┘ └────────────┘ └────────────┘ └─────────────┘ └───────┘ └─────────────┘  │
│                                                                                          │
│  ┌ Fleet throughput & errors (stacked) ──────────┐ ┌ Fleet p50/p95/p99 latency ───────┐ │
│  │ ▟▟▟▟▟▟▟▟▟▟ requests  ░░ errors (echarts)       │ │ ╱╲╱╲ percentile bands (recharts) │ │
│  └────────────────────────────────────────────────┘ └──────────────────────────────────┘ │
│                                                                                          │
│  ┌ Services needing attention (worst by error-budget burn / error rate) ──────────────┐  │
│  │ ● checkout      critical   2.1% err   p95 980ms   burn 14.4x   ▸                    │  │
│  │ ● payments-api  degraded   0.9% err   p95 420ms   burn  2.0x   ▸                    │  │
│  │ ● cart          degraded   0.4% err   p95 310ms   budget 38%   ▸                    │  │
│  └─────────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                          │
│  ┌ Open APM incidents (shared alerts, metric LIKE 'apm_%') ─┐ ┌ SLO budget summary ───┐ │
│  │ ⚠ checkout p95 > 800ms     12m   page                    │ │ 9/11 healthy          │ │
│  │ ⚠ payments error_rate>1%    4m   page                    │ │ 1 burning · 1 at-risk  │ │
│  └──────────────────────────────────────────────────────────┘ └────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Behaviors: the env selector writes `?env=`; the worst-offenders rows link to `/apm/services/:id` (deep-linking to `?tab=overview`); incident rows link to the existing Alerts detail; SLO summary links to `/apm/slos`. Refetch 30 s.

---

## 5. Page: Services list (`/apm/services` → `ServicesPage.tsx`)

The RED service table. This is a direct clone of `ServersPage.tsx`: a STATUS_CHIPS row with facet counts, a Filters `Card`, a sortable table with a card-view toggle, URL-driven state, row → detail. The "RED KPI strip" sits above the chips and summarizes the **filtered** set.

Data: `GET /api/v1/apm/services` returns `{ items: Service[], total, facets }`. `Service` carries the denormalized RED fields from `apm_services` (`last_rps`, `last_error_rate`, `last_p95_ms`, `last_apdex`, `health`, `last_seen_at`, `team`, `language`, `env`, `tags`). Facets (health / env / language / team / tag counts) drive the chips and selects, mirroring `/servers/facets`.

URL params: `?health=&env=&language=&team=&tag=&search=&sort=&order=&view=&page=`. `sort ∈ {name, rps, error_rate, p95, apdex, last_deploy, last_seen}`.

```
┌─ APM / Services ─────────────────────────────────────── [TimeRangePicker 24h ▾] ┐
│  ┌ Services ┐ ┌ Throughput ┐ ┌ Avg err ┐ ┌ Worst p95 ┐ ┌ Avg apdex ┐ ┌ No-data ┐  │
│  │   42     │ │  18.4k rpm │ │ 0.62%   │ │  980 ms   │ │   0.94    │ │    2    │  │
│  └──────────┘ └────────────┘ └─────────┘ └───────────┘ └───────────┘ └─────────┘  │
│                                                                                  │
│  [All 42] [● Healthy 28] [● Degraded 11] [● Critical 3] [○ No-data 2]            │
│                                                                                  │
│  ┌ Filters ──────────────────────────────────────────────────────────────────┐  │
│  │ 🔎 [search service…]  Env[prod ▾]  Lang[any ▾]  Team[any ▾]  Tag[any ▾]     │  │
│  │                                                              [Clear all]     │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                                              [▦ table] [▤ cards]  │
│  ┌──────────────────────────────────────────────────────────────────────────────┐│
│  │ Service ▲    Health    Throughput  Error%   p50 / p95 / p99   Apdex  LastDeploy││
│  ├──────────────────────────────────────────────────────────────────────────────┤│
│  │ ☕ checkout   ●critical  4.2k rpm   2.10%▮▮  42/980/1.8k ms   0.71   v812 2h  ▸││
│  │ ⬢ cart        ●degraded  3.1k rpm   0.40%▮   18/310/640 ms    0.93   v77  1d  ▸││
│  │ ⬡ payments    ●degraded  2.0k rpm   0.90%▮   30/420/910 ms    0.90   v210 3h  ▸││
│  │ ◆ search      ●healthy   5.6k rpm   0.05%    12/140/260 ms    0.98   v44  6h  ▸││
│  │ ○ batch-worker ○no-data    —          —          —             —     —       ▸││
│  └──────────────────────────────────────────────────────────────────────────────┘│
│                                              ◀ 1 2 3 ▶   showing 1–25 of 42       │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Column cells: `Health` → `ServiceHealthBadge`; `Error%` → `ErrorRateBar`; the p50/p95/p99 cell shows three values with the worst tinted; `Apdex` → `ApdexBadge`; `LastDeploy` → relative time + version chip linking to `?tab=deployments`. Sortable headers toggle `sort`/`order` in the URL. Row click → `/apm/services/:id`. The language glyph uses `LanguageIcon`. Card view reuses the same data in a grid of `Card`s for narrow screens.

---

## 6. Page: Service detail (`/apm/services/:id` → `ServiceDetailPage.tsx`)

The entity-centric hub, structurally identical to `ServerDetailPage.tsx`: a header (back link, service name, `ServiceHealthBadge`, env/language/team chips, deploy version), a RED `KpiTile` strip, then the hand-rolled `border-b-2` tab bar keyed on `?tab=`. Each tab is its own component owning its own `useQuery`s, all keyed under `apmKeys`. The `TimeRangePicker` sits in the header and scopes every metric tab.

```
┌─ ‹ Services / checkout ──────────────────────────────── [TimeRangePicker 24h ▾] ┐
│  ☕ checkout   ●critical   env:prod  lang:java  team:payments   deploy v812      │
│  ┌ Throughput ┐ ┌ Error rate ┐ ┌ p50 ┐ ┌ p95 ┐ ┌ p99 ┐ ┌ Apdex ┐                 │
│  │ 4.2k rpm   │ │  2.10%     │ │42ms │ │980ms│ │1.8s │ │ 0.71  │                 │
│  └────────────┘ └────────────┘ └─────┘ └─────┘ └─────┘ └───────┘                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │Overview│Performance│Traces│Dependencies│Errors│Database│Profiling│SLOs│…    │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│  …per-tab content…                                                               │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Tab-by-tab specification:

| `?tab=` | Reads | Content |
|---|---|---|
| `overview` | `/services/{id}`, `/red`, `/operations`, `/apdex` | RED summary cards + the three RED area charts (rate / errors / duration percentiles) with deploy markers, a top-operations mini-table, an apdex gauge, and a "recent error groups" strip; health-reason callout |
| `performance` | `/red?operation=`, `/operations` | Latency percentile bands (p50/p90/p95/p99 stacked area), throughput, error-rate, **per-operation breakdown table** (operation, span_kind, rps, err%, p95) with row → filter-into-traces; latency heatmap (echarts) |
| `traces` | `/traces?service=` | Embedded Trace Explorer scoped to this service (the §9 table + filters), row → `/apm/traces/:traceId` |
| `dependencies` | `/service-map?focus=` | A focused service-map slice (upstream callers / downstream callees) as a small echarts `graph` + an edge table (peer, direction, rps, err%, p95) |
| `errors` | `/errors?service=` | The Errors Inbox table scoped to this service (the §10 layout), grouped issues with sparkline + status |
| `database` | `/traces?service=&db_system=*` (P3, AM-E11) | Top normalized queries (`db_statement` digests) by total time / call count / p95, plan capture, source-side-obfuscated statement text; empty-state until DB agent ingests |
| `profiling` | `/profiles?service=` (P3, AM-E11) | CPU/alloc flamegraph (custom SVG) with a function table; span→profile link from the waterfall lands here; empty-state pre-ingest |
| `slos` | `/slos?service=`, `/slos/{id}/budget` | SLOs scoped to this service: target, current SLI, budget-remaining gauge, multi-window burn chip strip; link to `/apm/slos/:id` |
| `deployments` | `/deployments?service=` | Deployment timeline (version, sha, ts, author) + version-vs-version RED compare (pick two versions → side-by-side rate/err/p95 deltas) |
| `logs` | `/apm/...logs?service=` | Service-scoped log stream (severity filter, full-text), each line pivots to its trace; reads `apm_logs` |
| `infrastructure` | shared `host_*`/`snmp_*`/`flow_records` (P4, AM-E12) | Full-stack correlation: the hosts/servers running this service (`host_*_metrics`), their switch interfaces (`snmp_if_*`), netflow conversations — the ZenPlus-unique panel |
| `settings` | `/services/{id}` (PATCH) | Apdex threshold T, team/owner/repo, tags, env binding, health overrides; `require_operator_user` writes via `ConfirmDialog` |

The `overview` tab wireframe (the default landing):

```
┌ Overview ─────────────────────────────────────────────────────────────────────┐
│ ┌ Request rate (recharts area, deploy ▏markers) ┐ ┌ Error rate (area, danger) ┐ │
│ │ ▟▟▟▟▟▟▟▟▟▟▟▟▟▟  ▏v810  ▏v812                   │ │ ╱╲___╱╲____  threshold ───│ │
│ └────────────────────────────────────────────────┘ └───────────────────────────┘ │
│ ┌ Latency p50/p90/p95/p99 (stacked bands) ──────┐ ┌ Apdex ─┐ ┌ Top operations ─┐ │
│ │ ░░░░▒▒▒▓▓▓ p99                                 │ │ ◔ 0.71 │ │ POST /checkout  │ │
│ │            p95 ── SLO target 800ms ───────────│ │  poor  │ │  4.2k  2.1%  980 │ │
│ └────────────────────────────────────────────────┘ └────────┘ │ GET  /cart/{id} │ │
│ ┌ Recent error groups ───────────────────────────────────────┐ │  3.1k  0.4%  310│ │
│ │ ✖ NullPointerException  PaymentSvc.charge   ▲ 1.2k/h  ▸     │ └─────────────────┘ │
│ │ ✖ TimeoutException      Gateway.call        ▬ 88/h    ▸     │                     │
│ └─────────────────────────────────────────────────────────────┘                     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Page: Trace Explorer + Trace Waterfall

### 7.1 Trace Explorer (`/apm/traces` → `TraceExplorerPage.tsx`)

A query UI over spans with two modes — **Live** (recent rolling window, all spans, regardless of retention sampling) and **Indexed** (retained spans over the full range), mirroring Datadog's Trace Explorer (`01-MARKET-RESEARCH.md`). The mode is a `?mode=live|indexed` toggle. The filter bar supports the promoted hot columns (`service_name`, `name`/operation, `span_kind`, `status_code`/`has_error`, `http_route`, `http_status_code`, `db_system`, `duration` min/max) and free attribute key:value chips that map to `attributes_string` map lookups server-side.

Data: `GET /api/v1/apm/traces` with `{ mode, service, operation, span_kind, status, http_route, min_duration_ms, max_duration_ms, q (attr chips), range, sort, page }`. Returns root-span rows (`trace_id`, root service/operation, duration, span count, error flag, start ts, retain reason).

```
┌─ APM / Traces ───────────────────────────────── [ Live ●─ Indexed ]  [Range 1h ▾] ┐
│ ┌ Filters ───────────────────────────────────────────────────────────────────────┐│
│ │ Service[any ▾] Operation[any ▾] Kind[any ▾] Status[error ▾] Route[/checkout]     ││
│ │ Duration ≥ [500] ms   +attr[ http.status_code:500 ✕ ]  [+ add]      [Clear]      ││
│ └──────────────────────────────────────────────────────────────────────────────────┘│
│ ┌ Latency-vs-time scatter (echarts, color=error, click point → trace) ─────────────┐│
│ │   ·  ·   ●(err)    ·   ●        ·· ·  ●(err)   ·  ·                                ││
│ └────────────────────────────────────────────────────────────────────────────────────┘│
│ ┌──────────────────────────────────────────────────────────────────────────────────┐ │
│ │ Trace ID     Root service · operation     Duration  Spans  Status  Start   Reason │ │
│ ├──────────────────────────────────────────────────────────────────────────────────┤ │
│ │ a1b2…9f  checkout · POST /checkout          1.82 s    37    ●ERR   12:04:11 error  ▸│ │
│ │ 7c3d…12  search · GET /search               0.14 s     9    OK     12:04:10 baseline▸│ │
│ │ f0e1…aa  cart · GET /cart/{id}              0.66 s    14    OK     12:04:08 slow   ▸│ │
│ └──────────────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

The scatter plot is the **exemplar entry point**: clicking any point navigates to `/apm/traces/:traceId`. The `Reason` column renders `RetainReasonChip` (cost/attribution transparency). Row click → waterfall.

### 7.2 Trace Waterfall / Flame (`/apm/traces/:traceId` → `TraceWaterfallPage.tsx`)

The single net-new tracing component. There is no existing primitive (audit gap), so it is a custom horizontal-Gantt built from flex/SVG bars over the span tree.

Data: `GET /api/v1/apm/traces/{trace_id}` returns the full ordered span list; the page builds a tree by `parent_span_id`, computes per-span offset = `span.start - trace.start`, and lays out one row per span with depth-indentation. Width is scaled to the trace duration; a top time-ruler shows ms/s ticks.

```
┌─ ‹ Traces / a1b2…9f ─────────────────────────────────  total 1.82 s · 37 spans · ●ERROR ┐
│  [⤢ Waterfall] [🔥 Flame]   [Errors only ☐]  [Critical path ☑]   span-detail ▸ (right)    │
│  0ms        300        600        900        1200       1500      1800ms                    │
│  ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┼─────────────┤        │
│  checkout  POST /checkout                  ▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟  1.82s SERVER│
│   ├ cart-svc  GET /cart        ▟▟▟▟▟                                          120ms CLIENT  │
│   ├ payments  POST /charge          ▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟▟ (●error)          980ms CLIENT  │
│   │  └ postgres  INSERT orders            ▟▟▟▟▟▟▟▟▟▟ db.statement digest      420ms CLIENT  │
│   ├ inventory GET /stock                              ▟▟▟▟                    140ms CLIENT  │
│   └ email-svc POST /receipt                                ▟▟▟▟▟▟▟▟▟▟▟▟▟▟     360ms PRODUCER│
│                                                                                              │
│ ┌ span detail (a1b2…/ payments POST /charge) ─────────────────────────────────────────────┐│
│ │ duration 980ms · status ERROR · kind CLIENT · service payments-api · v210                ││
│ │ http.method POST · http.route /charge · http.status_code 500 · peer.service stripe-gw    ││
│ │ exception: TimeoutException "upstream 504"  [▸ View error group]                          ││
│ │ attributes_string {…}  events [exception@912ms]   [▸ View logs]  [▸ View profile]         ││
│ └──────────────────────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

Component spec:
- **Bar rendering.** Each span row: `position: relative`; the bar is an absolutely-positioned div with `left = offset/total * 100%`, `width = duration/total * 100%`, colored by `service_name` (a stable hash→hue) and outlined red if `has_error`. Span events (e.g. `exception`) render as a small marker tick on the bar at the event offset.
- **Tree.** Built client-side from `parent_span_id`; collapsible nodes (chevron); orphan spans (missing parent in the retained set) attach to a synthetic root. Depth is indentation in the left label gutter.
- **Critical path.** Optional toggle highlights the longest dependency chain (the spans that determine total duration) by dimming non-critical bars — the Dynatrace PurePath / SRE "where did time go" view (`01-MARKET-RESEARCH.md`).
- **Errors-only filter** collapses to error spans + their ancestors.
- **Flame view** is a second render mode of the same tree: stacked horizontal rects by self-time (echarts `custom` series or custom SVG), useful for fan-out-heavy traces.
- **Span detail panel** (right/below) shows promoted columns, typed attribute maps, events, and the three pivots: **View error group** (→ `/apm/errors/:group_id`), **View logs** (→ logs filtered by `trace_id`), **View profile** (→ service `?tab=profiling` keyed to span_id, P3).

---

## 8. Page: Service Map (`/apm/service-map` → `ServiceMapPage.tsx`)

The auto-derived dependency topology, rendered with the **echarts `graph` series** (`force` layout, via the existing `echarts`/`echarts-for-react` dependency — no new package; the wrapper lives under `components/apm/`). Nodes are services; directed edges are call relationships with RED on the edge. This is the substrate for blast-radius and the later graph-walk RCA (`01-MARKET-RESEARCH.md`, blueprint §9 AM-E12).

Data: `GET /api/v1/apm/service-map?range=&env=` returns `{ nodes: ServiceMapNode[], edges: ServiceMapEdge[] }` from `apm_service_graph` (edges: `client_service`, `server_service`, `request_count`, `error_count`, `duration_sum_ms`) joined to `apm_services` (nodes: name, health, RED). Edge p95/err% computed in the endpoint.

```
┌─ APM / Service Map ──────── [env prod ▾] [Range 1h ▾] [Layout force ▾] [Fit] [Health ▾] ┐
│  ┌ legend: node color=health · edge width=throughput · edge color=err% · ◌=inferred ┐    │
│                                                                                          │
│        ( gateway )───────►( checkout )══════►( payments )╌╌╌►◌stripe-gw                  │
│            │ 5.6k 0.1%        │ 4.2k 2.1%(red)   │ 2.0k 0.9%      (inferred 3rd-party)    │
│            ▼                   ▼                   ▼                                       │
│        ( search )         ( cart )           ( postgres )◌                               │
│                               │ 3.1k 0.4%                                                 │
│                               ▼                                                           │
│                          ( inventory )                                                    │
│                                                                                          │
│  ┌ selected: checkout → payments ─────────────────────────────────────────────────────┐ │
│  │ throughput 2.0k rpm · error 0.9% · p95 910ms · 18 inferred deps     [▸ View traces] │ │
│  └─────────────────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Component spec:
- **Nodes** colored by `health` (success/warning/danger/muted), sized by throughput, labelled with service + golden-signal mini-stats; **inferred** nodes (uninstrumented third parties / datastores synthesized from CLIENT spans) drawn with a dashed border (`◌`).
- **Edges** directed; width ∝ `request_count`, color ramped by `error_count/request_count`, dashed for inferred targets. Hover shows rps/err%/p95.
- **Selection** of a node focuses upstream/downstream (fade the rest) and opens a side panel; selecting an edge opens the edge RED panel with **View traces** → Trace Explorer pre-filtered to that client→server pair. A node's **Open service** → `/apm/services/:id`.
- **Controls.** Layout switch (echarts `force` / `circular`; an optional layered/hierarchical arrangement can be precomputed server-side and fed as fixed `x/y`), Fit, health filter, env, and the standard range. Large graphs (>150 nodes) cluster by `team`/namespace with expand-on-click.
- Page is `lazy()`-loaded so the service-map view is code-split off the main bundle.

---

## 9. Page: Errors Inbox (`/apm/errors` → `ErrorsInboxPage.tsx`)

A Sentry/New-Relic-style issue inbox: deduplicated **error groups** (not raw exceptions) with triage workflow. The grouping key (`group_id`) lives in ClickHouse `apm_exceptions`; triage state (status, assignee, first/last-seen mirror) lives in Postgres `apm_error_issues` (blueprint §2.5, §4.4). The list is the `ServersPage` filter+table skeleton.

Data: `GET /api/v1/apm/errors` → `{ items: ErrorGroup[], total, facets }` (`group_id`, `exception_type`, `service_name`, `last_message`, `count`, `users_affected`, `first_seen`, `last_seen`, `status`, `assignee`, `versions[]`, `trend[]` sparkline). URL params `?status=&service=&assignee=&search=&sort=&order=&page=` (`status ∈ unresolved|resolved|resolved_in_version|ignored`).

```
┌─ APM / Errors ─────────────────────────────────────────────── [Range 24h ▾] ┐
│ [Unresolved 47] [Resolved 12] [Ignored 8] [Assigned to me 3]                  │
│ ┌ Filters: 🔎[search] Service[any▾] Assignee[any▾]                  [Clear] ┐  │
│ └─────────────────────────────────────────────────────────────────────────┘  │
│ ┌──────────────────────────────────────────────────────────────────────────┐ │
│ │ ☐ Error                          Service    Events▼  Users  Trend  Last   │ │
│ ├──────────────────────────────────────────────────────────────────────────┤ │
│ │ ☐ ✖ NullPointerException         checkout    12.4k    312   ▁▃▇█  2m  ▸   │ │
│ │     PaymentSvc.charge:142                                                  │ │
│ │ ☐ ✖ TimeoutException             payments     880      41   ▁▁▃▂  6m  ▸   │ │
│ │ ☐ ✖ ConnectionResetError         cart         210       9   ▂▁▁▁  1h  ▸   │ │
│ └──────────────────────────────────────────────────────────────────────────┘ │
│ [bulk: Resolve ▾  Ignore  Assign ▾]  (appears on selection)                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 9.1 Error issue detail (`/apm/errors/:id` → `ErrorIssueDetailPage.tsx`)

```
┌─ ‹ Errors / NullPointerException ──────────────────────────────────────────────┐
│ ✖ NullPointerException  checkout · PaymentSvc.charge:142                         │
│ [Status: Unresolved ▾]  [Assign ▾ khuram]  [Ignore]   first 3d ago · last 2m ago│
│ ┌ Occurrences over time (recharts, deploy ▏markers) ┐ ┌ Facets ───────────────┐ │
│ │ ▁▃▇█▇▅▃  ▏v810   ▏v812 (spike)                     │ │ version v812 88%      │ │
│ └────────────────────────────────────────────────────┘ │ env prod · host h-12  │ │
│ ┌ Stack trace (latest, source-side scrubbed) ───────┐  │ http.route /checkout  │ │
│ │ at PaymentSvc.charge(PaymentSvc.java:142)          │  └───────────────────────┘ │
│ │ at Gateway.call(Gateway.java:88) …                 │  ┌ Occurrences ──────────┐ │
│ └────────────────────────────────────────────────────┘  │ a1b2…9f  2m ago  ▸    │ │
│  events 12.4k · users 312 · [▸ View a representative trace]                       │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Detail reads `GET /api/v1/apm/errors/{group_id}` (count trend, versions, facets, latest stack, occurrence list with `trace_id`). Triage writes `PATCH /api/v1/apm/errors/{group_id}` (status/assignee) via `require_operator_user`, with `toast` + `qc.invalidateQueries(['apm','errors'])`. **View representative trace** and each occurrence's `trace_id` pivot to `/apm/traces/:traceId` — the error→trace link. Deploy markers on the occurrence chart correlate a spike to the version that introduced it.

---

## 10. Page: RUM dashboards (`/apm/rum` → `RumPage.tsx`)

Real-user monitoring: Core Web Vitals (LCP/INP/CLS at **p75** — the CWV standard) per application + view, JS errors, and session exploration, with RUM→backend-trace stitching. Reads `apm_rum_vitals_5m` for the aggregate panels and `apm_rum_events` for the session/error lists.

Data: `GET /api/v1/apm/rum/...` (overview, vitals, views, sessions, errors). URL params `?application=&view=&country=&device=&browser=&range=`. A sub-tab toggle (`ui/Tabs`, in-panel) switches **Web Vitals · Views · Sessions · Errors**.

```
┌─ APM / RUM ─────────────── [App: storefront ▾] [Range 24h ▾]  [Web Vitals|Views|Sessions|Errors]┐
│  ┌ LCP p75 ┐ ┌ INP p75 ┐ ┌ CLS p75 ┐ ┌ Sessions ┐ ┌ JS errors ┐ ┌ Loaded p75 ┐               │
│  │ 2.1 s ✓ │ │ 180ms ✓ │ │ 0.04 ✓  │ │  18.2k   │ │   312     │ │  2.6 s     │               │
│  │  good   │ │  good   │ │  good   │ │ ▲ 6% d/d │ │ 0.9% sess │ │            │               │
│  └─────────┘ └─────────┘ └─────────┘ └──────────┘ └───────────┘ └────────────┘               │
│  ┌ Core Web Vitals over time (echarts, good/needs-improvement/poor bands) ─────────────────┐  │
│  │ LCP ╱╲___╱╲   ── 2.5s threshold ─────────────────────────────────────────────────────── │  │
│  └────────────────────────────────────────────────────────────────────────────────────────┘  │
│  ┌ Slowest views (p75 LCP) ──────────────────────┐ ┌ Errors by view ────────────────────┐    │
│  │ /product/{id}   LCP 3.8s  INP 240ms  12.1k vw │ │ /checkout  TypeError  88   ▸        │    │
│  │ /checkout       LCP 2.9s  INP 210ms   4.4k vw │ │ /cart      undefined  41   ▸        │    │
│  └────────────────────────────────────────────────┘ └─────────────────────────────────────┘    │
│  ┌ Sessions (Sessions tab) ───────────────────────────────────────────────────────────────┐  │
│  │ session  views  duration  device   country  errors  → backend trace                     │  │
│  │ s-91af    7      4m12s     mobile   US        1      a1b2…9f ▸ (View trace)              │  │
│  └──────────────────────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

CWV tiles are band-colored to Google thresholds (LCP ≤2.5s good / ≤4s NI / poor; INP ≤200ms / ≤500ms; CLS ≤0.1 / ≤0.25). A session row's `backend_trace_id` pivots to the waterfall (the RUM→trace stitch, `01-MARKET-RESEARCH.md`); a RUM error pivots to its `backend_trace_id` trace. Views/errors filter by `view_name`, `country`, `device_type`, `browser` facets. Session Replay is explicitly out of scope for v1 (blueprint §5 F22 / P4).

---

## 11. Page: Synthetics (`/apm/synthetics` → `SyntheticsPage.tsx`)

Synthetic monitors list + detail. Execution is owned by the Go poller (blueprint §5 F14, AM-E9) — the UI never runs probes; **Run now** routes to the poller via `POST /api/v1/apm/synthetics/{id}/run`. Aggregate panels read `apm_synthetic_results_5m`; raw step results read `apm_synthetic_results`.

Data: `GET/POST/PUT/DELETE /api/v1/apm/synthetics`. The list reuses the filter+table skeleton; the detail reuses the `ServiceCheckDetail.tsx` SLO/uptime shell (KPI tiles, performance chart, status-history/incidents, 30-day uptime calendar) and **adds a per-step waterfall** for multi-step/browser monitors (the audit gap).

```
┌─ APM / Synthetics ──────────────────────────────────── [+ New monitor]  [Range 24h ▾] ┐
│ [All 18] [● Up 15] [● Down 1] [● Degraded 2]                                            │
│ ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│ │ Monitor               Type      Locations  Uptime  p95     Last    Status         │  │
│ ├──────────────────────────────────────────────────────────────────────────────────┤  │
│ │ Checkout journey      browser   3          99.94%  2.1s    30s     ●up    ▸        │  │
│ │ Payments API health   api       5          99.99%  180ms   12s     ●up    ▸        │  │
│ │ Login multistep       multistep 3          98.20%  3.4s    45s     ●degr  ▸        │  │
│ └──────────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Detail (`/apm/synthetics/:id` → `SyntheticDetailPage.tsx`):

```
┌─ ‹ Synthetics / Checkout journey ──────────────── [Run now] [Edit] [Range 7d ▾] ┐
│  ┌ Uptime ┐ ┌ Avg ┐ ┌ p95 ┐ ┌ Failures ┐ ┌ MTTR ┐ ┌ Streak ┐  (ServiceCheckDetail tiles)│
│  │ 99.94% │ │1.9s │ │2.1s │ │    2     │ │ 4m   │ │ 12d    │                              │
│  └────────┘ └─────┘ └─────┘ └──────────┘ └──────┘ └────────┘                              │
│  ┌ Response time by location (recharts) ─┐ ┌ 30-day uptime calendar (heatmap) ──────────┐ │
│  │ us-east ╱╲  eu-west ╱╲  ap-south ╱╲   │ │ ▩▩▩▩▩▩▩ ▩▩▩▩▩▩▩ … (reuse UptimeCalendar)   │ │
│  └────────────────────────────────────────┘ └─────────────────────────────────────────────┘ │
│  ┌ Step waterfall (latest run, multistep/browser) ─────────────────────────────────────────┐│
│  │ 1 Open /              ▟▟▟        220ms  ✓                                                 ││
│  │ 2 Login              ▟▟▟▟▟▟      640ms  ✓   assert status=200 ✓                          ││
│  │ 3 Add to cart            ▟▟▟▟    380ms  ✓                                                 ││
│  │ 4 Checkout                  ▟▟▟▟▟▟▟▟▟▟  1.1s ✗  assert text "Order #" ✗  [▸ backend trace]││
│  └──────────────────────────────────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

The step waterfall reuses the §7.2 bar component (one bar per `step_index`/`step_name` from `apm_synthetic_results`), with per-step assertion pass/fail and a `backend_trace_id` pivot to the waterfall. `synthetic_down` alerting rides the existing service-check push path (blueprint §7.1) — the UI surfaces these in the shared Alerts center.

---

## 12. Page: SLOs (`/apm/slos` → `SlosPage.tsx`) + SLO detail

SLO list + error-budget status, and a detail with the multi-window burn-down. SLO defs live in Postgres `apm_slos`; burn is computed by `apm_slo_burn_loop` over `apm_span_metrics_5m` (blueprint §7.2).

Data: `GET/POST/PUT/DELETE /api/v1/apm/slos`; `GET /api/v1/apm/slos/{id}/budget` returns per-window burn. URL params `?service=&status=&search=`.

```
┌─ APM / SLOs ──────────────────────────────────────── [+ New SLO]  [Range 30d ▾] ┐
│ [Healthy 9] [At-risk 1] [Burning 1]                                               │
│ ┌──────────────────────────────────────────────────────────────────────────────┐ │
│ │ SLO                     Service    Type       Target  Current  Budget   Burn   │ │
│ ├──────────────────────────────────────────────────────────────────────────────┤ │
│ │ Checkout availability   checkout   availab.   99.9%   99.62%   ▮▮▱▱ 38%  6.0x ▸│ │
│ │ Checkout latency<500ms  checkout   latency    99.0%   98.10%   ▮▱▱▱ 14% 14.4x▸│ │
│ │ Search availability     search     availab.   99.95%  99.98%   ▮▮▮▮ 96%  0.4x ▸│ │
│ └──────────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────────┘
```

SLO detail (`/apm/slos/:id` → `SloDetailPage.tsx`) — the **burn-down**:

```
┌─ ‹ SLOs / Checkout latency<500ms ──────────────────────────────── [Edit] [Range 30d ▾]┐
│  ┌ Target ┐ ┌ Current SLI ┐ ┌ Budget remaining ┐ ┌ Time to exhaustion ┐               │
│  │ 99.0%  │ │   98.10%    │ │  ◔ 14%  (danger)  │ │   ~6h at current   │               │
│  └────────┘ └─────────────┘ └───────────────────┘ └────────────────────┘               │
│  ┌ Error-budget burn-down (recharts, budget line descending to 0) ──────────────────┐  │
│  │ 100% ▔▔▔▔╲▁▁▁▁╲▁▁▁▁▁╲▁▁▁  ← budget consumed; ▏deploy v812 (burn onset)            │  │
│  │   0% ─────────────────────────────────────────────────────────── exhaustion ──── │  │
│  └────────────────────────────────────────────────────────────────────────────────────┘  │
│  ┌ Multi-window burn rates (Google SRE) ─────────────────────────────────────────────┐  │
│  │ 1h window  ● 14.4x  PAGE   (short 5m also breaching ✓)                              │  │
│  │ 6h window  ● 6.0x   PAGE   (short 30m ✓)                                            │  │
│  │ 3d window  ○ 1.0x   ticket  (short 6h ✗ — not active)                               │  │
│  └─────────────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

The multi-window strip renders each canonical window (14.4x/1h page, 6x/6h page, 1x/3d ticket) with its short-window confirmation, filled when the window is actively breaching — the precise multi-window multi-burn semantics from the SRE Workbook (`01-MARKET-RESEARCH.md`). Notify-channels and burn-alert config are edited in the SLO form (reusing the channel selector from the Alerts UI). Budget gauges use band colors; the burn-down overlays deploy markers.

---

## 13. Page: Settings (`/apm/settings` → `ApmSettingsPage.tsx`) + Onboarding/Install wizard

A tabbed settings page (in-panel `ui/Tabs`) covering ingest keys, sampling, scrubbing, retention, environments — plus the **onboarding/install wizard** launched from an empty-state "No services yet" CTA on the Overview and Services pages.

Data: `/apm/ingest-keys` (CRUD), `/apm/sampling-rules` (GET/PUT), `/apm/scrubbing-rules` (GET/PUT), `/apm/environments` (via services facets / env CRUD). All writes are `require_operator_user`.

```
┌─ APM / Settings ──────────────────────────────────────────────────────────────┐
│ [Ingest Keys] [Sampling] [PII Scrubbing] [Retention] [Environments]            │
│ ┌ Ingest Keys ──────────────────────────────────────────── [+ New ingest key] ┐│
│ │ Name            Kind  Prefix  Env    Origins         Created   Status        ││
│ │ prod-collector  sdk   zpi_…   prod   —               3d ago    ●enabled  ⋯   ││
│ │ storefront-rum  rum   zpr_…   prod   app.acme.com    1d ago    ●enabled  ⋯   ││
│ └───────────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────────┘
```

Ingest-key creation shows the secret **once** (copy-to-clipboard, `toast`), then only the prefix (mirrors agent enrollment — `zpi_` SDK / `zpr_` RUM keys, blueprint §8.1). RUM keys expose an **origin allowlist** editor (CORS). Sampling tab edits head-rate + tail policies (keep errors / keep slow threshold / baseline %) per env; Scrubbing tab edits attribute allow/deny + regex masks with the default-on rule set shown read-only-by-default. Retention tab shows per-env raw-day overrides bounded by the schema CHECK (1–30d).

### 13.1 Onboarding / Install wizard

A `Dialog`-based multi-step wizard (the highest-leverage adoption surface — point an OTel SDK at ZenPlus and see a service appear):

```
┌─ Connect a service ─────────────────────────────── step 2 / 4 ─┐
│ ① Choose source   ② Get endpoint & key   ③ Send data   ④ Verify │
│                                                                  │
│  OTLP endpoint:   https://appliance.local:4318   (HTTP)          │
│                   appliance.local:4317           (gRPC)          │
│  Ingest key:      zpi_xxxxxxxx…  [Copy]   (shown once)            │
│                                                                  │
│  Env vars (copy):                                                │
│   OTEL_EXPORTER_OTLP_ENDPOINT=https://appliance.local:4318       │
│   OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer zpi_xxxx       │
│   OTEL_SERVICE_NAME=my-service                                   │
│   OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod           │
│                                                                  │
│  [ ◀ Back ]                                  [ Next: Send data ▶ ]│
└──────────────────────────────────────────────────────────────────┘
```

Step 1 picks source (OTel SDK per language / OTel Collector / eBPF agent / RUM browser snippet / synthetic monitor). Step 2 issues an ingest key (or RUM key) and shows the copy-paste env vars / snippet, language-tabbed. Step 3 shows live ingest detection. Step 4 **verifies** by polling `GET /apm/services` until the new `service.name` appears, then deep-links to its detail — closing the loop with immediate value (the SigNoz/Datadog onboarding idiom, `01-MARKET-RESEARCH.md`).

---

## 14. Interaction patterns (the correlation pivots)

These are the cross-page flows that make APM feel like one product rather than nine pages. Each is implemented purely by carrying a shared ID into the target route — no client-side joins.

### 14.1 Metric spike → exemplar trace

Every RED `MetricChartCard` data point can carry an exemplar `trace_id` (computed in the rollup/query path; blueprint §6, `01-MARKET-RESEARCH.md`). On the latency/error charts (Overview, Service Performance, error occurrence chart) a click on a spike (or the chart's "view sample traces" affordance) navigates to `/apm/traces/:traceId` for a representative slow/error trace in that bucket. Where an exact exemplar is unavailable, the click instead opens the **Trace Explorer** pre-filtered to the service + operation + time bucket + `status=error` (or `min_duration ≥ p95`), which is the deterministic fallback.

```
RED chart point  ──click──►  /apm/traces/<exemplar trace_id>     (exemplar present)
                 └─fallback► /apm/traces?service=…&operation=…&from=…&to=…&status=error
```

### 14.2 Trace → correlated logs

From any span in the waterfall (or the trace header), **View logs** opens the service-detail `?tab=logs` (or an inline logs drawer) filtered to that `trace_id` and time window, reading `apm_logs WHERE trace_id=…` via the `idx_log_trace` bloom index. The log lines render with severity dots and an inline "in this span" marker for lines whose `span_id` matches. The pivot is reversible: a log line's `trace_id` links back to `/apm/traces/:traceId`.

```
Waterfall span ─[View logs]─► tab=logs ?trace_id=a1b2…&span_id=…   (apm_logs)
Log line       ─[trace_id]──► /apm/traces/a1b2…                    (apm_spans)
```

### 14.3 Trace span → error group; error → representative trace

A span flagged `has_error` with an `exception` event shows **View error group** → `/apm/errors/:group_id`. Conversely the Errors detail's occurrences and **View representative trace** carry `apm_exceptions.trace_id` → `/apm/traces/:traceId`. This is the bidirectional Sentry-style errors↔traces stitch.

### 14.4 Trace span → profile (P3)

A span shows **View profile** → service `?tab=profiling` keyed to `span_id`/`trace_id`, rendering the flamegraph for that code path (`apm_profiles.trace_id`/`span_id`, `idx_prof_trace`). The Datadog "slow span → exact lines of code" jump (`01-MARKET-RESEARCH.md`).

### 14.5 Service map edge → traces; node → service

A map edge's **View traces** opens Trace Explorer pre-filtered to `client_service`→`server_service` for the range; a node's **Open service** → `/apm/services/:id`. This makes the topology a launchpad, not a static diagram.

### 14.6 RUM / synthetic → backend trace

A RUM session/error or a synthetic step carries `backend_trace_id` → `/apm/traces/:traceId`, giving front-to-back (or probe-to-back) visibility in one click — the front-end-through-code path competitors charge a premium for.

### 14.7 Alert / SLO burn → service

APM alerts (shared Alerts center, `metric LIKE 'apm_%'`) and SLO burn rows deep-link to the offending `/apm/services/:id?tab=performance` (or `?tab=slos`) scoped to the breach window, so triage starts on the right chart. Quiet-hours and notification routing are unchanged (reuse the alert engine; blueprint §7).

### 14.8 Deploy marker → version compare

Clicking a deployment `ReferenceLine` on any RED chart opens the service `?tab=deployments` with that version preselected for version-vs-version compare (rate/err/p95 deltas), the change-tracking regression workflow.

---

## 15. Empty, loading, error & no-data states

Consistency rules (match Servers):

- **Loading** → `Skeleton` placeholders shaped like the target (KPI tiles, table rows, chart blocks); never a spinner-only screen.
- **No data for range** → centered icon + message ("No spans in this window") with a range hint, not an empty chart axis.
- **Service `no_data`** (reporting stopped) → `ServiceHealthBadge` outline + a banner with last-seen and a link to the install wizard / ingest-key check.
- **Phase-gated tabs** (`profiling`, `database`, `infrastructure` until their epics ship) → inline "Coming with AM-E11/AM-E12 — not yet ingesting" panel, never a 404 or broken query.
- **Mutation errors** → `toast.error(message, apiErrorMessage(e))`; destructive actions (delete monitor/SLO/ingest-key, resolve-all) go through `ui/ConfirmDialog`.
- **Missing-table window** (a CH migration not yet auto-applied) → the endpoint returns empty + a soft warning; the UI shows the no-data state, never a stack trace (the host_* "table does not exist" failure mode this design avoids).

---

## 16. Accessibility, theming & responsive behavior

- **Theme.** All colors via CSS-variable tokens; the dark theme is class-based (`.dark`). Charts read `rgb(var(--token))`; echarts uses the dark palette already established by `TimeSeriesChart`. No hardcoded hex outside those palettes.
- **Color is never the only signal.** Health/status always pairs a colored dot/badge with a text label and a `title` tooltip carrying reasons (e.g. why a service is `critical`). Error-rate and apdex cells show the number alongside the bar/band.
- **Keyboard & focus.** Tables are keyboard-navigable; row "open" is an actual link (right-click/open-in-new-tab works, which matters for incident handoff). The waterfall span list and map nodes are focusable with arrow-key traversal.
- **Responsive.** KPI strips collapse `grid-cols-2 → md:grid-cols-3 → xl:grid-cols-6`; list pages offer the card-view toggle for narrow screens; the waterfall and service map are horizontally scrollable with a sticky label gutter / minimap.
- **Density.** Tables are dense (compact rows) to match Servers; numeric columns are right-aligned and monospace-tabular for scanability.

---

## 17. Build order (UI tasks mapped to epics)

The frontend lands incrementally alongside the backend epics (blueprint §9; expanded in `07-ROADMAP-AND-EPICS.md`). UI deliverables per epic:

| Epic | UI deliverables |
|---|---|
| **AM-E1** (ingest) | `/apm/settings` Ingest Keys tab + onboarding/install wizard; sidebar section, routes, breadcrumbs, `apm/shared.tsx`, `types/apm.ts` scaffolding |
| **AM-E2** (trace explorer + waterfall) | `TraceExplorerPage`, `TraceWaterfallPage` (the custom waterfall/flame component) |
| **AM-E3** (services + RED + map) | `ApmOverviewPage`, `ServicesPage`, `ServiceDetailPage` (overview/performance/dependencies tabs), `ServiceMapPage` (echarts `graph`) |
| **AM-E4** (errors) | `ErrorsInboxPage`, `ErrorIssueDetailPage`, service `?tab=errors` |
| **AM-E5** (logs + correlation) | `?tab=logs`, the trace→logs drawer, metric→exemplar wiring |
| **AM-E6** (alerting + SLO) | `SlosPage`, `SloDetailPage` (burn-down + multi-window strip), service `?tab=slos`, APM metrics in `AlertRuleFormDialog` |
| **AM-E7** (deployments) | `?tab=deployments` + deploy markers on all RED charts + version compare |
| **AM-E8** (sampling + scrubbing) | `/apm/settings` Sampling / PII Scrubbing / Retention tabs; `RetainReasonChip` in Trace Explorer |
| **AM-E9** (synthetics) | `SyntheticsPage`, `SyntheticDetailPage` (reuse `ServiceCheckDetail` shell + step waterfall) |
| **AM-E10** (RUM) | `RumPage` (Web Vitals / Views / Sessions / Errors) + RUM→trace stitch |
| **AM-E11** (DB/profiling) | service `?tab=database`, `?tab=profiling` (flamegraph), span→profile pivot |
| **AM-E12** (AI/RCA/full-stack) | service `?tab=infrastructure` (host_*→snmp_*→flow correlation), RCA grouping on the service map, anomaly bands on RED charts |

Each new chart/table reuses an existing primitive (clone `MetricChartCard`, the `ServersPage` filter skeleton, the `ServiceCheckDetail` SLO shell) so the only genuinely net-new components are the **trace waterfall/flame** and the **profiling flamegraph**; the **service map** reuses the existing echarts `graph` series (force layout) — everything else is a fork of code that already ships.

---

*End of document. Every route, table, metric key, and epic ID herein is pinned by `00-INDEX.md`'s blueprint; deviations must be raised against that contract, not decided in this file.*
