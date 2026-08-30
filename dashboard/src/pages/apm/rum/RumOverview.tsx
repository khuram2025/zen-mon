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
import { relativeTime } from '@/lib/utils'
import { fmtPct } from '@/components/apm/shared'
import { APM_SERIES, ApmKpi, ApmTimeChart, ChartPanel, RankBar, errorTone, fmtCount } from '@/components/apm/viz'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import type { RumCoverage, RumError, RumFacets, RumFilters, RumIngestHealth, RumOverview, RumTab, RumTimeseries, RumView } from '@/types/apm'
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
  formatRumVital,
} from './RumUi'

interface OverviewProps {
  overview: RumOverview
  timeseries?: RumTimeseries
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
      <CardHeader className="border-b border-border px-4 py-3">
        <CardTitle className="flex items-center gap-2 text-sm"><ServerCog className="h-4 w-4 text-primary" /> Collection health</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-4">
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
        <div className="grid grid-cols-3 gap-2 rounded-lg bg-surface2/50 p-3 text-center">
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

function FacetCard({ title, items, onSelect }: { title: string; items?: Array<{ value: string; count: number }>; onSelect: (value: string) => void }) {
  const visible = (items ?? []).slice(0, 6)
  const max = Math.max(...visible.map((item) => item.count), 1)
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border px-4 py-3"><CardTitle className="text-xs">{title}</CardTitle></CardHeader>
      <CardContent className="space-y-2 p-3">
        {visible.map((item) => (
          <button key={item.value} type="button" onClick={() => onSelect(item.value)} className="block w-full rounded-md px-1.5 py-1 text-left hover:bg-surface2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
            <div className="flex items-center justify-between gap-2 text-[11px]"><span className="truncate text-text2">{item.value || 'Unknown'}</span><span className="font-mono tabular-nums text-muted">{fmtCount(item.count)}</span></div>
            <RankBar value={item.count} max={max} />
          </button>
        ))}
        {!visible.length && <div className="py-6 text-center text-[11px] text-muted">No breakdown data</div>}
      </CardContent>
    </Card>
  )
}

export function RumOverviewPanel(props: OverviewProps) {
  const { overview: d, exploreTo } = props
  const errorRate = d.rates.error_session_rate
  const series = props.timeseries?.series ?? []
  return (
    <div className="space-y-5">
      <section aria-labelledby="rum-volume-heading">
        <RumSectionHeader id="rum-volume-heading" title="Experience volume" description="Sampled traffic, reliability and interaction load for the selected segment. Open a tile to inspect the matching explorer." />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <ApmKpi
            to={exploreTo.sessions}
            label="Sessions"
            icon={<Users className="h-4 w-4" />}
            tone="info"
            value={fmtCount(d.totals.sessions)}
            sub={`${fmtCount(d.totals.views)} sampled views`}
          />
          <ApmKpi
            to={exploreTo.views}
            label="Page views"
            icon={<Layers3 className="h-4 w-4" />}
            tone="primary"
            value={fmtCount(d.totals.views)}
            sub={`${fmtCount(d.totals.events)} collected events`}
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
          />
          <ApmKpi
            to={exploreTo.errors}
            label="Errored sessions"
            icon={<AlertTriangle className="h-4 w-4" />}
            tone={errorTone(errorRate ?? 0)}
            value={fmtPct(errorRate)}
            sub="sampled sessions with ≥1 error"
          />
          <ApmKpi
            to={exploreTo.resources}
            label="Resources"
            icon={<Network className="h-4 w-4" />}
            tone={(d.rates.resource_failure_rate ?? 0) >= 0.05 ? 'danger' : 'accent'}
            value={fmtCount(d.totals.resources)}
            sub={d.rates.resource_failure_rate == null ? 'fetch, XHR and assets' : `${fmtPct(d.rates.resource_failure_rate)} failed`}
          />
          <ApmKpi
            to={exploreTo.actions}
            label="User actions"
            icon={<MousePointerClick className="h-4 w-4" />}
            tone="warning"
            value={fmtCount(d.totals.actions)}
            sub={`${fmtCount(d.totals.long_tasks)} long tasks`}
          />
        </div>
      </section>

      <section aria-labelledby="rum-vitals-heading">
        <RumSectionHeader
          id="rum-vitals-heading"
          title="Core Web Vitals"
          description="75th percentile across finalized view measurements. Score uses the share of good LCP, INP and CLS samples."
          action={<Button asChild variant="ghost" size="sm"><Link to={exploreTo['web-vitals']}>Open Web Vitals</Link></Button>}
        />
        <div className="grid gap-3 xl:grid-cols-4">
          <RumExperienceCard vitals={d.vitals} href={exploreTo['web-vitals']} />
          <RumVitalCard name="lcp" metric={d.vitals.lcp} />
          <RumVitalCard name="inp" metric={d.vitals.inp} />
          <RumVitalCard name="cls" metric={d.vitals.cls} />
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          {(['fcp', 'ttfb', 'load'] as const).map((name) => (
            <div key={name} className="rounded-lg border border-border bg-surface px-4 py-3">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{name.toUpperCase()} p75</div>
              <div className="mt-1 text-lg font-semibold tabular-nums text-text">{formatRumVital(name, d.vitals[name].p75)}</div>
              <div className="mt-1 text-[10px] text-muted">{d.vitals[name].samples.toLocaleString()} samples · supporting navigation metric</div>
            </div>
          ))}
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr),minmax(280px,1fr)]">
        {props.trendsError ? <QueryErrorPanel label="experience trends" error={props.trendsError} onRetry={props.onRetryTrends} /> : (
          <ChartPanel title="Real-user experience over time" hint="Sampled views and sessions, plus all retained JavaScript errors">
            <ApmTimeChart
              data={series}
              loading={props.trendsLoading}
              empty="No experience samples match this segment."
              height={280}
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

      <div className="grid gap-4 xl:grid-cols-2">
        <RumTableCard
          title="Slowest views"
          description="Highest p75 LCP in the selected segment"
          actions={<Button variant="ghost" size="sm" onClick={props.onShowViews}>View all</Button>}
        >
          <Table>
            <THead><Tr><Th>View</Th><Th className="text-right">LCP p75</Th><Th className="text-right">INP p75</Th><Th className="text-right">Views</Th></Tr></THead>
            <TBody>
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
            <THead><Tr><Th>Error</Th><Th className="text-right">Sessions</Th><Th className="text-right">Events</Th><Th>Trace</Th></Tr></THead>
            <TBody>
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
            <THead><Tr><Th>Release</Th><Th className="text-right">Sessions</Th><Th className="text-right">Views</Th><Th className="text-right">Error sessions</Th><Th className="text-right">LCP p75</Th><Th className="text-right">INP p75</Th><Th className="text-right">CLS p75</Th><Th className="text-right">Last seen</Th></Tr></THead>
            <TBody>
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
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <FacetCard title="Browsers" items={props.facets?.browser} onSelect={(value) => props.onFilter('browser', value)} />
          <FacetCard title="Devices" items={props.facets?.device_type} onSelect={(value) => props.onFilter('device_type', value)} />
          <FacetCard title="Countries" items={props.facets?.country} onSelect={(value) => props.onFilter('country', value)} />
          <FacetCard title="Client IPs" items={props.facets?.client_ip} onSelect={(value) => props.onFilter('client_ip', value)} />
          <FacetCard title="Releases" items={props.facets?.service_version} onSelect={(value) => props.onFilter('service_version', value)} />
        </div>
      </section>
    </div>
  )
}

export function RumWebVitalsPanel({ overview, timeseries, loading, error, onRetry, exploreTo }: {
  overview: RumOverview
  timeseries?: RumTimeseries
  loading?: boolean
  error?: unknown
  onRetry?: () => void
  exploreTo?: string
}) {
  const series = timeseries?.series ?? []
  const totalSamples = overview.vitals.lcp.samples + overview.vitals.inp.samples + overview.vitals.cls.samples
  return (
    <div className="space-y-5">
      <div className="grid gap-3 xl:grid-cols-4">
        <RumExperienceCard vitals={overview.vitals} href={exploreTo} />
        <RumVitalCard name="lcp" metric={overview.vitals.lcp} />
        <RumVitalCard name="inp" metric={overview.vitals.inp} />
        <RumVitalCard name="cls" metric={overview.vitals.cls} />
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <RumVitalCard name="fcp" metric={overview.vitals.fcp} compact />
        <RumVitalCard name="ttfb" metric={overview.vitals.ttfb} compact />
        <RumVitalCard name="load" metric={overview.vitals.load} compact />
      </div>
      <div className="rounded-lg border border-info/25 bg-info/5 px-4 py-3 text-xs text-text2">
        <div className="flex items-start gap-2"><Gauge className="mt-0.5 h-4 w-4 shrink-0 text-info" /><p><span className="font-semibold text-text">Field performance at p75.</span> At least 75% of measured visits experienced a value at or below each result. Missing samples remain “No data” and are excluded from scoring.</p></div>
      </div>

      {error ? <QueryErrorPanel label="Web Vital trends" error={error} onRetry={onRetry} /> : totalSamples === 0 && !loading ? (
        <Card><RumEmptyState icon={Gauge} title="No finalized Web Vital samples" description="Keep the page open long enough for the browser to finalize its view, then refresh this dashboard. Unsupported browsers are excluded rather than reported as zero." /></Card>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          <ChartPanel title="Largest Contentful Paint" hint="Good ≤ 2.5s · poor > 4s">
            <ApmTimeChart data={series} loading={loading} empty="No LCP samples in this window." height={250} series={[{ key: 'lcp_p75', name: 'LCP p75', color: '#7c3aed', fmt: (value) => formatRumVital('lcp', value) }]} />
          </ChartPanel>
          <ChartPanel title="Interaction to Next Paint" hint="Good ≤ 200ms · poor > 500ms">
            <ApmTimeChart data={series} loading={loading} empty="No INP samples in this window." height={250} series={[{ key: 'inp_p75', name: 'INP p75', color: '#0284c7', fmt: (value) => formatRumVital('inp', value) }]} />
          </ChartPanel>
          <ChartPanel title="Cumulative Layout Shift" hint="Good ≤ 0.1 · poor > 0.25">
            <ApmTimeChart data={series} loading={loading} empty="No CLS samples in this window." height={250} series={[{ key: 'cls_p75', name: 'CLS p75', color: '#db2777', fmt: (value) => formatRumVital('cls', value) }]} />
          </ChartPanel>
          <ChartPanel title="Navigation timing" hint="FCP, TTFB and full page load">
            <ApmTimeChart data={series} loading={loading} empty="No navigation timing samples in this window." height={250} series={[
              { key: 'fcp_p75', name: 'FCP p75', color: '#0891b2', fmt: (value) => formatRumVital('fcp', value) },
              { key: 'ttfb_p75', name: 'TTFB p75', color: '#f59e0b', fmt: (value) => formatRumVital('ttfb', value) },
              { key: 'load_p75', name: 'Load p75', color: '#6366f1', fmt: (value) => formatRumVital('load', value) },
            ]} />
          </ChartPanel>
        </div>
      )}
    </div>
  )
}
