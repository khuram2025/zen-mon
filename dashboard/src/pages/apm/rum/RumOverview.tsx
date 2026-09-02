import type { KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  FileWarning,
  Gauge,
  Layers3,
  MousePointerClick,
  Network,
  ServerCog,
  Users,
} from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { fmtPct } from '@/components/apm/shared'
import { APM_SERIES, ApmKpi, ApmTimeChart, ChartPanel, RankBar, errorTone, fmtCount } from '@/components/apm/viz'
import { EXPLORER_HEAD, EXPLORER_ROWS } from '@/components/apm/explorer'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import type { RumBreakdown, RumBreakdownSide, RumCoverage, RumError, RumFacets, RumFilters, RumIngestHealth, RumOverview, RumRequestTiming, RumTab, RumTimeseries, RumView } from '@/types/apm'
import {
  QueryErrorPanel,
  RumCoverageNotice,
  RumEmptyState,
  RumExperienceCard,
  RumMetricCell,
  RumSectionHeader,
  RumTableCard,
  RumVitalCard,
  TracePivot,
  formatDurationMs,
  formatRumVital,
} from './RumUi'
import { PHASE_COLORS, PhaseBar, PhaseLegend, phaseSegments } from './RumBreakdown'
import { releaseMarkers } from './RumVitals'

interface OverviewProps {
  overview: RumOverview
  timeseries?: RumTimeseries
  breakdown?: RumBreakdown
  breakdownLoading?: boolean
  breakdownError?: unknown
  onRetryBreakdown?: () => void
  topViews?: RumView[]
  topErrors?: RumError[]
  topViewsLoading?: boolean
  topViewsError?: unknown
  topErrorsLoading?: boolean
  topErrorsError?: unknown
  health?: RumIngestHealth
  explorerCoverage?: RumCoverage
  facets?: Partial<RumFacets>
  trendsLoading?: boolean
  trendsError?: unknown
  exploreTo: Record<Extract<RumTab, 'web-vitals' | 'views' | 'sessions' | 'errors' | 'resources' | 'actions'>, string>
  onRetryTrends?: () => void
  onRetryViews?: () => void
  onRetryErrors?: () => void
  onOpenView: (view: RumView) => void
  onOpenError: (error: RumError) => void
  onShowViews: () => void
  onShowErrors: () => void
  onFilter: (key: keyof RumFilters, value: string) => void
}

const INTERACTIVE_ROW = 'cursor-pointer focus:outline-none focus-visible:bg-surface2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40'

function onRowKey(event: KeyboardEvent<HTMLTableRowElement>, open: () => void) {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  open()
}

function healthStatus(overview: RumOverview, health?: RumIngestHealth): string {
  if (health?.status) return health.status
  if (typeof overview.ingest_health === 'string') return overview.ingest_health
  if (overview.ingest_health && typeof overview.ingest_health === 'object') {
    if (overview.ingest_health.status) return overview.ingest_health.status
    if ((overview.ingest_health.storage_errors ?? 0) > 0 || (overview.ingest_health.rate_limited ?? 0) > 0) return 'degraded'
  }
  return overview.totals.events > 0 ? 'healthy' : 'no_data'
}

function HealthPanel({ overview, health }: { overview: RumOverview; health?: RumIngestHealth }) {
  const status = healthStatus(overview, health)
  const details = health ?? (typeof overview.ingest_health === 'object' ? overview.ingest_health : undefined)
  const good = status === 'healthy'
  const warn = status === 'degraded'
  const Icon = good ? CheckCircle2 : status === 'no_data' ? Clock3 : AlertTriangle
  const sdkVersions = details?.sdk_versions?.filter(Boolean) ?? (details?.sdk_version ? [details.sdk_version] : [])
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border px-3 py-2">
        <CardTitle className="flex items-center gap-2 text-[13px]"><ServerCog className="h-3.5 w-3.5 text-primary" /> Collection health</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2.5 p-3">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className={`flex h-8 w-8 items-center justify-center rounded-lg ${good ? 'bg-success/10 text-success' : warn ? 'bg-warning/10 text-warning' : 'bg-surface2 text-muted'}`}>
              <Icon className="h-4 w-4" />
            </span>
            <div>
              <div className="text-sm font-medium capitalize text-text">{status.replace('_', ' ')}</div>
              <div className="text-[11px] text-muted">Browser SDK ingest pipeline</div>
            </div>
          </div>
          {!!sdkVersions.length && <Badge variant="outline" title={sdkVersions.join(', ')}>SDK {sdkVersions.slice(0, 2).join(', ')}{sdkVersions.length > 2 ? ` +${sdkVersions.length - 2}` : ''}</Badge>}
        </div>
        <div className="grid grid-cols-3 gap-2 rounded-lg bg-surface2/50 p-2 text-center">
          <div><div className="font-mono text-sm font-semibold text-text">{fmtCount(details?.accepted)}</div><div className="text-[9px] uppercase tracking-wider text-muted">Accepted</div></div>
          <div><div className="font-mono text-sm font-semibold text-warning">{fmtCount(details?.rejected)}</div><div className="text-[9px] uppercase tracking-wider text-muted">Rejected</div></div>
          <div><div className="font-mono text-sm font-semibold text-danger">{fmtCount(details?.dropped)}</div><div className="text-[9px] uppercase tracking-wider text-muted">Dropped</div></div>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-[10px] sm:grid-cols-4">
          <div><div className="font-mono font-semibold text-text2">{fmtCount(details?.accepted_since_process_start)}</div><div className="text-muted">Process accepted</div></div>
          <div><div className="font-mono font-semibold text-text2">{fmtCount(details?.duplicates)}</div><div className="text-muted">Duplicates</div></div>
          <div><div className="font-mono font-semibold text-warning">{fmtCount(details?.rate_limited)}</div><div className="text-muted">Rate limited</div></div>
          <div><div className="font-mono font-semibold text-danger">{fmtCount(details?.storage_errors)}</div><div className="text-muted">Write errors</div></div>
        </div>
        <div className="flex items-center justify-between text-[11px] text-muted">
          <span>Last event</span><span>{details?.last_event_at ? relativeTime(details.last_event_at) : 'not reported'}</span>
        </div>
        {!!details?.issues?.length && (
          <div className="rounded-md border border-warning/30 bg-warning/10 p-2.5 text-[11px] text-warning">
            {details.issues.slice(0, 3).map((issue) => <div key={issue}>• {issue}</div>)}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function sideTiming(side?: RumBreakdownSide): RumRequestTiming | null {
  if (!side || side.samples <= 0) return null
  return {
    redirect_ms: side.phases.redirect ?? 0,
    dns_ms: side.phases.dns ?? 0,
    connect_ms: side.phases.connect ?? 0,
    tls_ms: side.phases.tls ?? 0,
    wait_ms: side.phases.wait ?? 0,
    download_ms: side.phases.download ?? 0,
    blocked_ms: side.phases.blocked ?? 0,
    processing_ms: side.phases.processing ?? 0,
    server_ms: side.server_p75 ?? 0,
    db_ms: side.db_p75 ?? 0,
    has_server_timing: side.server_samples > 0,
  }
}

function BreakdownCard({ title, hint, side }: { title: string; hint: string; side?: RumBreakdownSide }) {
  const timing = sideTiming(side)
  const segments = timing ? phaseSegments(timing) : []
  const hasServer = (side?.server_samples ?? 0) > 0
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-[13px]">{title}</CardTitle>
          <span className="text-[10px] text-muted">{hint}</span>
        </div>
      </CardHeader>
      <CardContent className="space-y-2.5 p-3">
        {segments.length ? (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-lg font-semibold tabular-nums text-text">{formatDurationMs(side?.duration_p75)}</span>
              <span className="text-[10px] text-muted">p75 · {fmtCount(side?.samples)} measured{hasServer ? ` · ${fmtCount(side?.server_samples)} with app/db split` : ''}</span>
            </div>
            <PhaseBar segments={segments} height={12} />
            <PhaseLegend segments={segments} />
            {hasServer && (
              <div className="grid grid-cols-3 gap-2 rounded-lg bg-surface2/50 p-2 text-center">
                <div><div className="font-mono text-sm font-semibold" style={{ color: PHASE_COLORS.network }}>{formatDurationMs(Math.max(0, (side!.phases.wait ?? 0) - Math.min(side!.server_p75 ?? 0, side!.phases.wait ?? 0)))}</div><div className="text-[9px] uppercase tracking-wider text-muted">Network</div></div>
                <div><div className="font-mono text-sm font-semibold" style={{ color: PHASE_COLORS.server }}>{formatDurationMs(Math.max(0, (side!.server_p75 ?? 0) - (side!.db_p75 ?? 0)))}</div><div className="text-[9px] uppercase tracking-wider text-muted">App execution</div></div>
                <div><div className="font-mono text-sm font-semibold" style={{ color: PHASE_COLORS.db }}>{formatDurationMs(side!.db_p75)}</div><div className="text-[9px] uppercase tracking-wider text-muted">Database</div></div>
              </div>
            )}
            {!hasServer && <p className="text-[10px] text-muted">Add a <span className="font-mono">Server-Timing</span> header (e.g. <span className="font-mono">app;dur=12, db;dur=4</span>) or instrument the backend with APM to split wait time into network vs. execution.</p>}
          </>
        ) : (
          <div className="py-8 text-center text-[11px] text-muted">No timing-capable samples in this segment yet. Requires browser SDK 2.1+.</div>
        )}
      </CardContent>
    </Card>
  )
}

/** Explain an empty Countries card from what the collector actually knows. */
function countryEmptyHint(health?: RumIngestHealth): string {
  const geo = health?.geoip
  if (!geo) return 'No country data in this segment.'
  if (!geo.available) return 'GeoIP database not installed — countries come only from a CDN header (CF-IPCountry / X-Country-Code). Run scripts/fetch-geoip.py on the controller to resolve visitor addresses.'
  if (geo.distinct_client_ips > 0 && geo.events_with_country === 0) return `GeoIP is active, but the ${geo.distinct_client_ips.toLocaleString()} client address${geo.distinct_client_ips === 1 ? '' : 'es'} in this segment are private or reserved (lab traffic) and have no country.`
  return 'No country data in this segment.'
}

function FacetCard({ title, items, onSelect, empty = 'No breakdown data' }: { title: string; items?: Array<{ value: string; count: number }>; onSelect: (value: string) => void; empty?: string }) {
  const visible = (items ?? []).slice(0, 6)
  const max = Math.max(...visible.map((item) => item.count), 1)
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border bg-surface2/30 px-3 py-1.5"><CardTitle className="text-[11px] uppercase tracking-wider text-muted">{title}</CardTitle></CardHeader>
      <CardContent className="space-y-1 p-2">
        {visible.map((item) => (
          <button key={item.value} type="button" onClick={() => onSelect(item.value)} className="block w-full rounded-md px-1.5 py-1 text-left hover:bg-surface2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
            <div className="flex items-center justify-between gap-2 text-[11px]"><span className="truncate text-text2">{item.value || 'Unknown'}</span><span className="font-mono tabular-nums text-muted">{fmtCount(item.count)}</span></div>
            <RankBar value={item.count} max={max} />
          </button>
        ))}
        {!visible.length && <div className="px-2 py-6 text-center text-[11px] leading-snug text-muted">{empty}</div>}
      </CardContent>
    </Card>
  )
}

/**
 * Change against the previous window of equal length. `lowerIsBetter` flips the
 * colouring for error rates and latencies; percentage points are used for
 * rates because "+2 pp" reads better than "+18 %" of a small rate.
 */
function Delta({ current, previous, lowerIsBetter = false, pointsOf, format, label, detail = true }: {
  current: number | null | undefined
  previous: number | null | undefined
  lowerIsBetter?: boolean
  /** When set, express the change as percentage points of this scale (1 = fraction, 100 = percent). */
  pointsOf?: number
  format?: (value: number) => string
  label: string
  /** Show "(before → after)" inline; off for tight cards, where it stays in the tooltip. */
  detail?: boolean
}) {
  if (current == null || previous == null || !Number.isFinite(current) || !Number.isFinite(previous)) {
    return <span className="text-[10px] text-muted">no prior data</span>
  }
  const diff = current - previous
  if (Math.abs(diff) < 1e-9) return <span className="text-[10px] text-muted">unchanged vs. {label}</span>
  const better = lowerIsBetter ? diff < 0 : diff > 0
  const text = pointsOf != null
    ? `${diff > 0 ? '+' : '−'}${(Math.abs(diff) * pointsOf).toFixed(1)} pp`
    : previous === 0
      ? (format ? `${diff > 0 ? '+' : '−'}${format(Math.abs(diff))}` : 'new')
      : `${diff > 0 ? '+' : '−'}${Math.abs((diff / previous) * 100).toFixed(Math.abs(diff / previous) >= 1 ? 0 : 1)}%`
  const change = format ? `${format(previous)} → ${format(current)}` : `${previous} → ${current}`
  return (
    <span className={cn('whitespace-nowrap text-[10px] tabular-nums', better ? 'text-success' : 'text-danger')} title={`${label}: ${change}`}>
      {diff > 0 ? '▲' : '▼'} {text} vs. {label}{detail && format && <span className="text-muted"> ({change})</span>}
    </span>
  )
}

function previousLabel(d: RumOverview): string {
  const range = d.range === 'custom' && d.window ? `${Math.max(1, Math.round(d.window.seconds / 3600))} h` : d.range
  return d.range === 'custom' ? `prior ${range}` : `prior ${range}`
}

export function RumOverviewPanel(props: OverviewProps) {
  const { overview: d, exploreTo } = props
  const errorRate = d.rates.error_session_rate
  const series = props.timeseries?.series ?? []
  const prev = d.previous ?? null
  const prior = previousLabel(d)
  const kpiDelta = (current: number | null | undefined, previous: number | null | undefined, options: { lowerIsBetter?: boolean; pointsOf?: number; format?: (value: number) => string } = {}) => (
    prev ? <Delta current={current} previous={previous} label={prior} {...options} /> : undefined
  )
  return (
    <div className="space-y-4">
      <section aria-labelledby="rum-volume-heading">
        <RumSectionHeader id="rum-volume-heading" title="Experience volume" description={prev ? `Compared with the ${prior} window. Open a tile to inspect the matching explorer.` : 'Open a tile to inspect the matching explorer.'} />
        <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6">
          <ApmKpi
            to={exploreTo.sessions}
            label="Sessions"
            icon={<Users className="h-4 w-4" />}
            tone="info"
            value={fmtCount(d.totals.sessions)}
            sub={d.totals.sessions > 0 ? `${(d.totals.views / d.totals.sessions).toFixed(1)} views per session` : 'no sampled sessions yet'}
            foot={kpiDelta(d.totals.sessions, prev?.totals.sessions)}
          />
          <ApmKpi
            to={exploreTo.views}
            label="Page views"
            icon={<Layers3 className="h-4 w-4" />}
            tone="primary"
            value={fmtCount(d.totals.views)}
            sub={`${fmtCount(d.totals.events)} collected events`}
            foot={kpiDelta(d.totals.views, prev?.totals.views)}
          />
          <ApmKpi
            to={exploreTo.errors}
            label="JS errors"
            icon={<FileWarning className="h-4 w-4" />}
            tone={d.totals.errors > 0 ? 'danger' : 'success'}
            value={fmtCount(d.totals.errors)}
            sub={(d.totals.unsampled_errors ?? 0) > 0
              ? `${fmtCount(d.totals.sampled_errors ?? 0)} sampled · ${fmtCount(d.totals.unsampled_errors ?? 0)} retained`
              : `${fmtCount(d.totals.error_sessions)} affected sessions`}
            foot={kpiDelta(d.totals.errors, prev?.totals.errors, { lowerIsBetter: true })}
          />
          <ApmKpi
            to={exploreTo.errors}
            label="Errored sessions"
            icon={<AlertTriangle className="h-4 w-4" />}
            tone={errorTone(errorRate ?? 0)}
            value={fmtPct(errorRate)}
            sub="sampled sessions with ≥1 error"
            foot={kpiDelta(errorRate, prev?.rates.error_session_rate, { lowerIsBetter: true, pointsOf: 100 })}
          />
          <ApmKpi
            to={exploreTo.resources}
            label="Resources"
            icon={<Network className="h-4 w-4" />}
            tone={(d.rates.resource_failure_rate ?? 0) >= 0.05 ? 'danger' : 'accent'}
            value={fmtCount(d.totals.resources)}
            sub={d.rates.resource_failure_rate == null ? 'fetch, XHR and assets' : `${fmtPct(d.rates.resource_failure_rate)} failed`}
            foot={kpiDelta(d.rates.resource_failure_rate, prev?.rates.resource_failure_rate, { lowerIsBetter: true, pointsOf: 100 })}
          />
          <ApmKpi
            to={exploreTo.actions}
            label="User actions"
            icon={<MousePointerClick className="h-4 w-4" />}
            tone="warning"
            value={fmtCount(d.totals.actions)}
            sub={`${fmtCount(d.totals.long_tasks)} long tasks`}
            foot={kpiDelta(d.totals.actions, prev?.totals.actions)}
          />
        </div>
      </section>

      <section aria-labelledby="rum-vitals-heading">
        <RumSectionHeader
          id="rum-vitals-heading"
          title="Core Web Vitals"
          description="p75 across finalized view measurements."
          action={<Button asChild variant="ghost" size="sm" className="h-7 px-2 text-[11px]"><Link to={exploreTo['web-vitals']}>Open Web Vitals</Link></Button>}
        />
        <div className="grid gap-2.5 xl:grid-cols-4">
          <RumExperienceCard vitals={d.vitals} href={exploreTo['web-vitals']} />
          <RumVitalCard name="lcp" metric={d.vitals.lcp} delta={prev ? <Delta current={d.vitals.lcp.p75} previous={prev.vitals.lcp.p75} lowerIsBetter label={prior} format={(value) => formatRumVital('lcp', value)} detail={false} /> : undefined} />
          <RumVitalCard name="inp" metric={d.vitals.inp} delta={prev ? <Delta current={d.vitals.inp.p75} previous={prev.vitals.inp.p75} lowerIsBetter label={prior} format={(value) => formatRumVital('inp', value)} detail={false} /> : undefined} />
          <RumVitalCard name="cls" metric={d.vitals.cls} delta={prev ? <Delta current={d.vitals.cls.p75} previous={prev.vitals.cls.p75} lowerIsBetter label={prior} format={(value) => formatRumVital('cls', value)} detail={false} /> : undefined} />
        </div>
        <div className="mt-2.5 grid grid-cols-3 divide-x divide-border overflow-hidden rounded-lg border border-border bg-surface">
          {(['fcp', 'ttfb', 'load'] as const).map((name) => (
            <div key={name} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-3 py-2">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">{name.toUpperCase()} p75</span>
              <span className="text-base font-semibold tabular-nums text-text">{formatRumVital(name, d.vitals[name].p75)}</span>
              <span className="text-[10px] text-muted">{d.vitals[name].samples.toLocaleString()} samples</span>
              {prev && <Delta current={d.vitals[name].p75} previous={prev.vitals[name].p75} lowerIsBetter label={prior} format={(value) => formatRumVital(name, value)} />}
            </div>
          ))}
        </div>
      </section>

      <section aria-labelledby="rum-breakdown-heading">
        <RumSectionHeader
          id="rum-breakdown-heading"
          title="End-to-end latency"
          description="p75 split of real-user time across DNS, connection, network, application execution, database and transfer."
        />
        {props.breakdownError ? <QueryErrorPanel label="latency breakdown" error={props.breakdownError} onRetry={props.onRetryBreakdown} /> : (
          <>
            <div className="grid gap-2.5 xl:grid-cols-2">
              <BreakdownCard title="Page loads" hint="finalized navigations" side={props.breakdown?.page_loads} />
              <BreakdownCard title="API requests" hint="fetch and XHR calls" side={props.breakdown?.api_requests} />
            </div>
            {!!props.breakdown?.slowest_endpoints?.length && (
              <div className="mt-2.5">
                <RumTableCard title="Slowest endpoints" description="fetch/XHR targets by p75 total time; app and DB figures come from Server-Timing headers or correlated APM traces">
                  <Table>
                    <THead className={EXPLORER_HEAD}><Tr><Th>Endpoint</Th><Th className="text-right">Requests</Th><Th className="text-right">Total p75</Th><Th className="text-right">Wait p75</Th><Th className="text-right">App p75</Th><Th className="text-right">DB p75</Th><Th className="text-right">Failures</Th></Tr></THead>
                    <TBody className={EXPLORER_ROWS}>
                      {props.breakdown.slowest_endpoints.map((endpoint) => (
                        <Tr key={`${endpoint.method}:${endpoint.url}`}>
                          <Td><div className="max-w-[340px] truncate font-mono text-xs text-text" title={endpoint.url}>{endpoint.method ? `${endpoint.method} ` : ''}{endpoint.url}</div></Td>
                          <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(endpoint.count)}</Td>
                          <Td className="text-right font-mono text-xs tabular-nums">{formatDurationMs(endpoint.duration_p75)}</Td>
                          <Td className="text-right font-mono text-xs tabular-nums">{formatDurationMs(endpoint.wait_p75)}</Td>
                          <Td className="text-right font-mono text-xs tabular-nums" title={endpoint.server_source === 'trace' ? `Average over ${endpoint.server_samples} correlated APM traces` : endpoint.server_source ? 'From the Server-Timing header' : undefined}>{endpoint.server_p75 != null ? formatDurationMs(endpoint.server_p75) : '—'}{endpoint.server_source === 'trace' && <span className="ml-1 text-[9px] text-muted">trace</span>}</Td>
                          <Td className="text-right font-mono text-xs tabular-nums">{endpoint.server_p75 != null ? formatDurationMs(endpoint.db_p75) : '—'}</Td>
                          <Td className="text-right"><span className={endpoint.failures ? 'font-mono text-xs text-danger' : 'font-mono text-xs text-muted'}>{fmtCount(endpoint.failures)}</span></Td>
                        </Tr>
                      ))}
                    </TBody>
                  </Table>
                </RumTableCard>
              </div>
            )}
          </>
        )}
      </section>

      <div className="grid gap-2.5 xl:grid-cols-[minmax(0,2fr),minmax(280px,1fr)]">
        {props.trendsError ? <QueryErrorPanel label="experience trends" error={props.trendsError} onRetry={props.onRetryTrends} /> : (
          <ChartPanel title="Real-user experience over time" hint="Sampled views and sessions, plus all retained JavaScript errors. Dashed markers: first traffic of a release.">
            <ApmTimeChart
              data={series}
              loading={props.trendsLoading}
              empty="No experience samples match this segment."
              height={250}
              markers={releaseMarkers(d.releases, d.window)}
              series={[
                { key: 'views', name: 'Views', color: APM_SERIES.throughput, type: 'bar', fmt: fmtCount },
                { key: 'sessions', name: 'Sessions', color: APM_SERIES.users, fmt: fmtCount },
                { key: 'errors', name: 'JS errors', color: APM_SERIES.errors, yAxisIndex: 1, fmt: fmtCount },
              ]}
            />
          </ChartPanel>
        )}
        <HealthPanel overview={d} health={props.health} />
      </div>

      {props.explorerCoverage?.partial && <div className="overflow-hidden rounded-lg border border-border"><RumCoverageNotice coverage={props.explorerCoverage} /></div>}

      <div className="grid gap-2.5 xl:grid-cols-2">
        <RumTableCard
          title="Slowest views"
          description="Highest p75 LCP in the selected segment"
          actions={<Button variant="ghost" size="sm" onClick={props.onShowViews}>View all</Button>}
        >
          <Table>
            <THead className={EXPLORER_HEAD}><Tr><Th>View</Th><Th className="text-right">LCP p75</Th><Th className="text-right">INP p75</Th><Th className="text-right">Views</Th></Tr></THead>
            <TBody className={EXPLORER_ROWS}>
              {props.topViewsLoading ? <Tr><Td colSpan={4} className="py-10 text-center text-xs text-muted">Loading measured views…</Td></Tr> : props.topViewsError ? <Tr><Td colSpan={4} className="py-8 text-center text-xs text-danger">Could not load view performance. <button className="font-medium underline" onClick={props.onRetryViews}>Retry</button></Td></Tr> : (props.topViews ?? []).map((view) => (
                <Tr key={`${view.application_id}:${view.env}:${view.view_name}`} className={INTERACTIVE_ROW} tabIndex={0} onClick={() => props.onOpenView(view)} onKeyDown={(event) => onRowKey(event, () => props.onOpenView(view))}>
                  <Td><div className="max-w-[260px] truncate font-mono text-xs text-text" title={view.view_name}>{view.view_name}</div><div className="text-[10px] text-muted">{view.application_id} · {view.env}</div></Td>
                  <Td><RumMetricCell name="lcp" value={view.lcp_p75} samples={view.lcp_samples} /></Td>
                  <Td><RumMetricCell name="inp" value={view.inp_p75} samples={view.inp_samples} /></Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(view.views)}</Td>
                </Tr>
              ))}
              {!props.topViewsLoading && !props.topViewsError && !props.topViews?.length && <Tr><Td colSpan={4}><RumEmptyState icon={Gauge} title="No measured views" description="View-level performance will appear after finalized Web Vital samples arrive." /></Td></Tr>}
            </TBody>
          </Table>
        </RumTableCard>

        <RumTableCard
          title="Top JavaScript errors"
          description="Most frequent browser failures"
          actions={<Button variant="ghost" size="sm" onClick={props.onShowErrors}>View all</Button>}
        >
          <Table>
            <THead className={EXPLORER_HEAD}><Tr><Th>Error</Th><Th className="text-right">Sessions</Th><Th className="text-right">Events</Th><Th>Trace</Th></Tr></THead>
            <TBody className={EXPLORER_ROWS}>
              {props.topErrorsLoading ? <Tr><Td colSpan={4} className="py-10 text-center text-xs text-muted">Loading browser errors…</Td></Tr> : props.topErrorsError ? <Tr><Td colSpan={4} className="py-8 text-center text-xs text-danger">Could not load error groups. <button className="font-medium underline" onClick={props.onRetryErrors}>Retry</button></Td></Tr> : (props.topErrors ?? []).map((error) => (
                <Tr key={error.fingerprint} className={INTERACTIVE_ROW} tabIndex={0} onClick={() => props.onOpenError(error)} onKeyDown={(event) => onRowKey(event, () => props.onOpenError(error))}>
                  <Td><div className="max-w-[280px] truncate text-xs font-medium text-text" title={error.message}>{error.message}</div><div className="text-[10px] text-muted">{error.view_name || 'Unknown view'}</div></Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(error.sessions)}</Td>
                  <Td className="text-right font-mono text-xs tabular-nums text-danger">{fmtCount(error.count)}</Td>
                  <Td><TracePivot traceId={error.backend_trace_id} compact /></Td>
                </Tr>
              ))}
              {!props.topErrorsLoading && !props.topErrorsError && !props.topErrors?.length && <Tr><Td colSpan={4}><RumEmptyState icon={CheckCircle2} title="No JavaScript errors" description="No browser errors match the current range and filters." /></Td></Tr>}
            </TBody>
          </Table>
        </RumTableCard>
      </div>

      {!!d.releases?.length && (
        <RumTableCard title="Release health" description="Compare real-user reliability and Core Web Vitals by deployed version. Select a release to segment the dashboard.">
          <Table>
            <THead className={EXPLORER_HEAD}><Tr><Th>Release</Th><Th className="text-right">Sessions</Th><Th className="text-right">Views</Th><Th className="text-right">Error sessions</Th><Th className="text-right">LCP p75</Th><Th className="text-right">INP p75</Th><Th className="text-right">CLS p75</Th><Th className="text-right">Last seen</Th></Tr></THead>
            <TBody className={EXPLORER_ROWS}>
              {d.releases.slice(0, 10).map((release) => (
                <Tr
                  key={release.service_version}
                  className={INTERACTIVE_ROW}
                  tabIndex={0}
                  aria-label={`Filter by release ${release.service_version}`}
                  onClick={() => props.onFilter('service_version', release.service_version)}
                  onKeyDown={(event) => onRowKey(event, () => props.onFilter('service_version', release.service_version))}
                >
                  <Td><div className="max-w-[240px] truncate font-mono text-xs font-medium text-text" title={release.service_version}>{release.service_version || 'Unknown'}</div></Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(release.sessions)}</Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(release.views)}</Td>
                  <Td className="text-right"><span className={(release.error_session_rate ?? 0) >= 0.05 ? 'font-mono text-xs text-danger' : 'font-mono text-xs'}>{fmtPct(release.error_session_rate)}</span><div className="text-[9px] text-muted">{fmtCount(release.errors)} errors</div></Td>
                  <Td><RumMetricCell name="lcp" value={release.lcp_p75} samples={release.lcp_samples} /></Td>
                  <Td><RumMetricCell name="inp" value={release.inp_p75} samples={release.inp_samples} /></Td>
                  <Td><RumMetricCell name="cls" value={release.cls_p75} samples={release.cls_samples} /></Td>
                  <Td className="whitespace-nowrap text-right text-xs text-muted">{relativeTime(release.last_seen)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </RumTableCard>
      )}

      <section aria-labelledby="rum-audience-heading">
        <RumSectionHeader id="rum-audience-heading" title="Audience and release context" description="Select a segment to apply it to every RUM view." />
        <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-5">
          <FacetCard title="Browsers" items={props.facets?.browser} onSelect={(value) => props.onFilter('browser', value)} />
          <FacetCard title="Devices" items={props.facets?.device_type} onSelect={(value) => props.onFilter('device_type', value)} />
          <FacetCard title="Countries" items={props.facets?.country} onSelect={(value) => props.onFilter('country', value)} empty={countryEmptyHint(props.health)} />
          <FacetCard title="Client IPs" items={props.facets?.client_ip} onSelect={(value) => props.onFilter('client_ip', value)} />
          <FacetCard title="Releases" items={props.facets?.service_version} onSelect={(value) => props.onFilter('service_version', value)} />
        </div>
      </section>
    </div>
  )
}
