import { useMemo, type KeyboardEvent } from 'react'
import { Gauge, Layers3, MousePointerClick, Move3D, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { ApmTimeChart, ChartPanel, fmtCount, type ApmChartMarker } from '@/components/apm/viz'
import { EXPLORER_HEAD, EXPLORER_ROWS } from '@/components/apm/explorer'
import { Badge } from '@/components/ui/Badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import type {
  RumFilters,
  RumOverview,
  RumTimeseries,
  RumVitalAttribution,
  RumVitalDimension,
  RumVitalDistribution,
  RumVitalsResponse,
} from '@/types/apm'
import {
  QueryErrorPanel,
  RumEmptyState,
  RumExperienceCard,
  RumMetricCell,
  RumSectionHeader,
  RumTableCard,
  RumVitalCard,
  formatRumVital,
} from './RumUi'
import { RUM_FILTER_LABEL, confidenceTitle, isLowConfidence, parseUtc, vitalBand, type RumVitalName } from './model'

const VITAL_NAMES: RumVitalName[] = ['lcp', 'inp', 'cls', 'fcp', 'ttfb', 'load']
const VITAL_TITLE: Record<RumVitalName, string> = {
  lcp: 'Largest Contentful Paint',
  inp: 'Interaction to Next Paint',
  cls: 'Cumulative Layout Shift',
  fcp: 'First Contentful Paint',
  ttfb: 'Time to First Byte',
  load: 'Page load',
}
const DIMENSIONS: Array<{ value: RumVitalDimension; label: string }> = [
  { value: 'view_name', label: 'Route' },
  { value: 'device_type', label: 'Device' },
  { value: 'browser', label: 'Browser' },
  { value: 'browser_version', label: 'Browser version' },
  { value: 'os', label: 'Operating system' },
  { value: 'country', label: 'Country' },
  { value: 'connection_type', label: 'Connection' },
  { value: 'service_version', label: 'Release' },
]
const BAND_FILL = { good: '#10b981', 'needs-improvement': '#f59e0b', poor: '#ef4444', 'no-data': '#64748b' } as const
const INTERACTIVE_ROW = 'cursor-pointer focus:outline-none focus-visible:bg-surface2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40'

function onRowKey(event: KeyboardEvent<HTMLTableRowElement>, open: () => void) {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  open()
}

/** Marker per release whose first event falls inside the window. */
export function releaseMarkers(
  releases: Array<{ service_version: string; first_seen?: string | null }> | undefined,
  window?: { from: string; to: string },
): ApmChartMarker[] {
  if (!releases?.length) return []
  const from = window ? parseUtc(window.from) : Number.NEGATIVE_INFINITY
  const to = window ? parseUtc(window.to) : Date.now()
  const span = Number.isFinite(from) ? Math.max(to - from, 1) : 7 * 86_400_000
  const sorted = releases
    .filter((release) => release.first_seen && parseUtc(release.first_seen) > from + 60_000)
    .map((release) => ({ ts: parseUtc(release.first_seen as string), timestamp: release.first_seen as string, label: release.service_version }))
    .sort((a, b) => a.ts - b.ts)
  // Releases closer together than ~3 % of the window would draw on top of
  // each other; fold them into one marker that names them all.
  const merged: Array<{ ts: number; timestamp: string; labels: string[] }> = []
  for (const marker of sorted) {
    const last = merged[merged.length - 1]
    if (last && marker.ts - last.ts < span * 0.03) last.labels.push(marker.label)
    else merged.push({ ts: marker.ts, timestamp: marker.timestamp, labels: [marker.label] })
  }
  return merged.map((marker) => ({ timestamp: marker.timestamp, label: marker.labels.length > 2 ? `${marker.labels[0]} +${marker.labels.length - 1}` : marker.labels.join(', ') }))
}

/**
 * Bucketed histogram of one vital. Bars take the colour of the rating their
 * bucket falls in, the two threshold boundaries are drawn as ticks, and the
 * p50 / p75 / p90 / p95 positions are marked so the tail is visible even
 * when most samples sit in the first few buckets.
 */
function VitalHistogram({ name, dist }: { name: RumVitalName; dist: RumVitalDistribution }) {
  const buckets = dist.buckets
  const max = Math.max(1, ...buckets.map((bucket) => bucket.count))
  const width = 100 / Math.max(buckets.length, 1)
  const format = (value: number) => (name === 'cls' ? value.toFixed(value < 0.1 ? 3 : 2) : value >= 1000 ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)}s` : `${value}`)
  const bandOf = (from: number) => vitalBand(name, from + 1e-9)
  const lowConfidence = isLowConfidence(dist.samples)
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border px-3 py-2">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-[13px]">{VITAL_TITLE[name]}</CardTitle>
            <div className="text-[10px] text-muted">{fmtCount(dist.samples)} finalized views{lowConfidence ? ' · indicative' : ''}</div>
          </div>
          <div className="flex gap-2 font-mono text-[10px] tabular-nums text-text2">
            {(['p50', 'p75', 'p90', 'p95'] as const).map((key) => (
              <span key={key} title={`${key} · ${formatRumVital(name, dist.percentiles[key])}`}>
                <span className="text-muted">{key} </span>{formatRumVital(name, dist.percentiles[key])}
              </span>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent className="p-3">
        {dist.samples === 0 ? (
          <div className="py-8 text-center text-[11px] text-muted">No finalized samples in this segment.</div>
        ) : buckets.length === 0 ? (
          <div className="space-y-2 py-2">
            <div className="flex h-2 overflow-hidden rounded-full bg-surface2">
              <div style={{ width: `${dist.good_pct ?? 0}%`, background: BAND_FILL.good }} />
              <div style={{ width: `${dist.needs_improvement_pct ?? 0}%`, background: BAND_FILL['needs-improvement'] }} />
              <div style={{ width: `${dist.poor_pct ?? 0}%`, background: BAND_FILL.poor }} />
            </div>
            <p className="text-[10px] text-muted">Windows longer than 14 days read the 5-minute rollup, which keeps percentiles and the good / needs-improvement / poor split{dist.rated_samples != null && dist.rated_samples < dist.samples ? ` (over ${fmtCount(dist.rated_samples)} rated views)` : ''} but not the per-bucket histogram.</p>
          </div>
        ) : (
          <>
            <svg viewBox="0 0 400 96" className="h-24 w-full" role="img" aria-label={`${VITAL_TITLE[name]} distribution`}>
              {buckets.map((bucket, index) => {
                const height = (bucket.count / max) * 70
                const x = index * (400 * width / 100)
                const barWidth = 400 * width / 100 - 2
                return (
                  <g key={index}>
                    <rect x={x + 1} y={76 - height} width={Math.max(barWidth, 1)} height={height} rx={1.5} fill={BAND_FILL[bandOf(bucket.from)]} opacity={bucket.count ? 0.9 : 0.25}>
                      <title>{`${format(bucket.from)} – ${bucket.to == null ? '∞' : format(bucket.to)}: ${bucket.count.toLocaleString()} views (${((bucket.count / Math.max(dist.samples, 1)) * 100).toFixed(1)}%)`}</title>
                    </rect>
                    {(index % 2 === 0 || index === buckets.length - 1) && (
                      <text x={x + 1} y={90} fontSize={8} fill="currentColor" className="text-muted" opacity={0.8}>{format(bucket.from)}{bucket.to == null ? '+' : ''}</text>
                    )}
                  </g>
                )
              })}
              {(['p50', 'p75', 'p95'] as const).map((key) => {
                const value = dist.percentiles[key]
                if (value == null) return null
                const index = buckets.findIndex((bucket) => value >= bucket.from && (bucket.to == null || value < bucket.to))
                if (index < 0) return null
                const bucket = buckets[index]
                const span = bucket.to == null ? bucket.from * 0.5 || 1 : bucket.to - bucket.from
                const x = (index + Math.min(1, (value - bucket.from) / Math.max(span, 1e-9))) * (400 * width / 100)
                return (
                  <g key={key}>
                    <line x1={x} x2={x} y1={4} y2={78} stroke="currentColor" strokeDasharray="2 2" opacity={key === 'p75' ? 0.9 : 0.45} className="text-text2" />
                    <text x={x + 2} y={key === 'p75' ? 10 : 20} fontSize={8} fill="currentColor" className="text-text2">{key}</text>
                  </g>
                )
              })}
            </svg>
            <div className="mt-1 flex items-center justify-between text-[10px] text-muted">
              <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm" style={{ background: BAND_FILL.good }} />Good ≤ {formatRumVital(name, dist.thresholds.good)} · {dist.good_pct == null ? '—' : `${dist.good_pct.toFixed(0)}%`}</span>
              <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm" style={{ background: BAND_FILL['needs-improvement'] }} />{dist.needs_improvement_pct == null ? '—' : `${dist.needs_improvement_pct.toFixed(0)}%`}</span>
              <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-sm" style={{ background: BAND_FILL.poor }} />Poor &gt; {formatRumVital(name, dist.thresholds.poor)} · {dist.poor_pct == null ? '—' : `${dist.poor_pct.toFixed(0)}%`}</span>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function BandBar({ good, ni, poor }: { good: number | null; ni: number | null; poor: number | null }) {
  if (good == null && ni == null && poor == null) return <div className="h-1.5 rounded-full bg-surface2" />
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-surface2" aria-hidden>
      <div style={{ width: `${Math.max(0, good ?? 0)}%`, background: BAND_FILL.good }} />
      <div style={{ width: `${Math.max(0, ni ?? 0)}%`, background: BAND_FILL['needs-improvement'] }} />
      <div style={{ width: `${Math.max(0, poor ?? 0)}%`, background: BAND_FILL.poor }} />
    </div>
  )
}

function SlowSegments({ data, dimension, vital, onDimension, onVital, onFilter, loading }: {
  data?: RumVitalsResponse
  dimension: RumVitalDimension
  vital: RumVitalName
  onDimension: (value: RumVitalDimension) => void
  onVital: (value: RumVitalName) => void
  onFilter: (key: keyof RumFilters, value: string) => void
  loading?: boolean
}) {
  const rows = data?.breakdown.rows ?? []
  const dimensionLabel = DIMENSIONS.find((item) => item.value === dimension)?.label ?? dimension
  const filterable = dimension !== 'connection_type'
  return (
    <RumTableCard
      title="Where it is slow"
      description={`Segments ranked by the share of poor ${vital.toUpperCase()} experiences. Select a row to apply it as a filter across every RUM page.`}
      actions={(
        <div className="flex items-center gap-1.5">
          <Select value={dimension} onValueChange={(value) => onDimension(value as RumVitalDimension)}>
            <SelectTrigger className="h-7 w-[150px] text-[11px]" aria-label="Break down by"><SelectValue /></SelectTrigger>
            <SelectContent>{DIMENSIONS.map((item) => <SelectItem key={item.value} value={item.value}>By {item.label.toLowerCase()}</SelectItem>)}</SelectContent>
          </Select>
          <Select value={vital} onValueChange={(value) => onVital(value as RumVitalName)}>
            <SelectTrigger className="h-7 w-[110px] text-[11px]" aria-label="Vital"><SelectValue /></SelectTrigger>
            <SelectContent>{VITAL_NAMES.map((name) => <SelectItem key={name} value={name}>{name.toUpperCase()}</SelectItem>)}</SelectContent>
          </Select>
        </div>
      )}
    >
      {data && !data.breakdown.available ? (
        <div className="px-4 py-8 text-center text-[11px] text-muted">The 5-minute rollup used for windows over 14 days does not carry the connection type. Pick a shorter window or another dimension.</div>
      ) : (
        <Table>
          <THead className={EXPLORER_HEAD}><Tr>
            <Th>{dimensionLabel}</Th>
            <Th className="text-right">Views</Th>
            <Th className="text-right">{vital.toUpperCase()} p75</Th>
            <Th className="w-[220px]">Experience split</Th>
            <Th className="text-right">Poor</Th>
            <Th className="text-right">LCP</Th>
            <Th className="text-right">INP</Th>
            <Th className="text-right">CLS</Th>
          </Tr></THead>
          <TBody className={EXPLORER_ROWS}>
            {loading && !rows.length && <Tr><Td colSpan={8} className="py-8 text-center text-xs text-muted">Measuring segments…</Td></Tr>}
            {!loading && !rows.length && <Tr><Td colSpan={8}><RumEmptyState icon={Search} title="No rated views" description={`No finalized ${vital.toUpperCase()} samples in this window for a ${dimensionLabel.toLowerCase()} breakdown.`} /></Td></Tr>}
            {rows.map((row) => {
              const label = row.value || (dimension === 'connection_type' ? 'unknown' : 'Unknown')
              const open = () => { if (filterable && row.value) onFilter(dimension as keyof RumFilters, row.value) }
              return (
                <Tr key={row.value || '∅'} className={filterable && row.value ? INTERACTIVE_ROW : undefined} tabIndex={filterable && row.value ? 0 : undefined} onClick={open} onKeyDown={(event) => onRowKey(event, open)} title={filterable && row.value ? `Filter by ${RUM_FILTER_LABEL[dimension as keyof RumFilters] ?? dimensionLabel}: ${label}` : undefined}>
                  <Td><div className="max-w-[260px] truncate font-mono text-xs text-text" title={label}>{label}</div></Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(row.views)}</Td>
                  <Td><RumMetricCell name={vital} value={row.p75} samples={row.samples} /></Td>
                  <Td><BandBar good={row.good_pct} ni={row.needs_improvement_pct} poor={row.poor_pct} /></Td>
                  <Td className="text-right"><span className={cn('font-mono text-xs tabular-nums', (row.poor_pct ?? 0) >= 25 ? 'text-danger' : (row.poor_pct ?? 0) > 0 ? 'text-warning' : 'text-success')} title={confidenceTitle(row.rated_samples ?? row.samples)}>{row.poor_pct == null ? '—' : `${row.poor_pct.toFixed(0)}%`}</span></Td>
                  <Td><RumMetricCell name="lcp" value={row.vitals.lcp.p75} /></Td>
                  <Td><RumMetricCell name="inp" value={row.vitals.inp.p75} /></Td>
                  <Td><RumMetricCell name="cls" value={row.vitals.cls.p75} /></Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      )}
    </RumTableCard>
  )
}

const ATTRIBUTION_META = {
  lcp: { title: 'LCP elements', hint: 'The element painted last on each route, with its resource URL', icon: Layers3, detail: 'Image / resource', empty: 'LCP attribution arrives with finalized views from SDK 2.1+.' },
  cls: { title: 'CLS sources', hint: 'The element that moved most during the largest layout-shift window', icon: Move3D, detail: null, empty: 'No layout shifts with a known source in this window.' },
  inp: { title: 'INP targets', hint: 'The slowest interaction target and its event type', icon: MousePointerClick, detail: 'Event', empty: 'No interactions have been finalized in this window.' },
} as const

function AttributionCard({ name, rows, onFilter }: { name: 'lcp' | 'cls' | 'inp'; rows?: RumVitalAttribution[]; onFilter: (key: keyof RumFilters, value: string) => void }) {
  const meta = ATTRIBUTION_META[name]
  const Icon = meta.icon
  return (
    <Card className="h-full">
      <CardHeader className="border-b border-border px-3 py-2">
        <CardTitle className="flex items-center gap-2 text-[13px]"><Icon className="h-3.5 w-3.5 text-primary" />{meta.title}</CardTitle>
        <p className="text-[10px] text-muted">{meta.hint}</p>
      </CardHeader>
      <CardContent className="p-0">
        {!rows?.length ? (
          <div className="px-4 py-8 text-center text-[11px] text-muted">{meta.empty}</div>
        ) : (
          <ul className="divide-y divide-border/60">
            {rows.map((row, index) => {
              const band = vitalBand(name, row.p75)
              return (
                <li key={`${row.view_name}:${row.element}:${row.detail ?? ''}:${index}`} className="px-3 py-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate font-mono text-xs text-text" title={row.element}>{row.element}</div>
                      <button type="button" className="mt-0.5 max-w-full truncate text-left text-[10px] text-muted hover:text-text hover:underline" title={`Filter by view ${row.view_name}`} onClick={() => onFilter('view_name', row.view_name)}>{row.view_name || '/'}</button>
                      {row.detail && <div className="truncate text-[10px] text-muted" title={row.detail}>{meta.detail}: <span className="font-mono">{row.detail}</span></div>}
                    </div>
                    <div className="shrink-0 text-right">
                      <div className={cn('font-mono text-xs tabular-nums', band === 'good' && 'text-success', band === 'needs-improvement' && 'text-warning', band === 'poor' && 'text-danger')}>{formatRumVital(name, row.p75)}</div>
                      <div className="text-[9px] text-muted">{fmtCount(row.count)} views · {row.poor_pct == null ? '—' : `${row.poor_pct.toFixed(0)}% poor`}</div>
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

export function RumWebVitalsPanel({ overview, timeseries, vitals, vitalsLoading, vitalsError, onRetryVitals, loading, error, onRetry, exploreTo, dimension, vital, onDimension, onVital, onFilter }: {
  overview: RumOverview
  timeseries?: RumTimeseries
  vitals?: RumVitalsResponse
  vitalsLoading?: boolean
  vitalsError?: unknown
  onRetryVitals?: () => void
  loading?: boolean
  error?: unknown
  onRetry?: () => void
  exploreTo?: string
  dimension: RumVitalDimension
  vital: RumVitalName
  onDimension: (value: RumVitalDimension) => void
  onVital: (value: RumVitalName) => void
  onFilter: (key: keyof RumFilters, value: string) => void
}) {
  const series = timeseries?.series ?? []
  const totalSamples = overview.vitals.lcp.samples + overview.vitals.inp.samples + overview.vitals.cls.samples
  const markers = useMemo(() => releaseMarkers(vitals?.releases ?? overview.releases, overview.window), [overview.releases, overview.window, vitals?.releases])
  return (
    <div className="space-y-4">
      <section aria-labelledby="rum-vitals-cards">
        <div className="grid gap-2.5 xl:grid-cols-4">
          <RumExperienceCard vitals={overview.vitals} href={exploreTo} />
          <RumVitalCard name="lcp" metric={overview.vitals.lcp} />
          <RumVitalCard name="inp" metric={overview.vitals.inp} />
          <RumVitalCard name="cls" metric={overview.vitals.cls} />
        </div>
        <div className="mt-2.5 grid gap-2.5 md:grid-cols-3">
          <RumVitalCard name="fcp" metric={overview.vitals.fcp} compact />
          <RumVitalCard name="ttfb" metric={overview.vitals.ttfb} compact />
          <RumVitalCard name="load" metric={overview.vitals.load} compact />
        </div>
        <div className="mt-2.5 rounded-md border border-info/25 bg-info/5 px-3 py-1.5 text-[11px] leading-snug text-text2">
          <div className="flex items-start gap-2"><Gauge className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" /><p><span className="font-semibold text-text">Field performance at p75.</span> At least 75% of measured visits experienced a value at or below each result. Missing samples remain “No data” and are excluded from scoring; figures under 20 samples are marked indicative.</p></div>
        </div>
      </section>

      {totalSamples === 0 && !loading ? (
        <Card><RumEmptyState icon={Gauge} title="No finalized Web Vital samples" description="Keep the page open long enough for the browser to finalize its view, then refresh this dashboard. Unsupported browsers are excluded rather than reported as zero." /></Card>
      ) : (
        <>
          <section aria-labelledby="rum-vitals-distribution">
            <RumSectionHeader id="rum-vitals-distribution" title="Distribution" description="How individual page views are spread across the good, needs-improvement and poor bands, one sample per view." />
            {vitalsError ? <QueryErrorPanel label="Web Vital distributions" error={vitalsError} onRetry={onRetryVitals} /> : vitals ? (
              <div className="grid gap-2.5 xl:grid-cols-3">
                {(['lcp', 'inp', 'cls'] as const).map((name) => <VitalHistogram key={name} name={name} dist={vitals.distribution[name]} />)}
              </div>
            ) : (
              <div className="grid gap-2.5 xl:grid-cols-3"><Skeleton className="h-44" /><Skeleton className="h-44" /><Skeleton className="h-44" /></div>
            )}
          </section>

          <section aria-labelledby="rum-vitals-segments">
            <SlowSegments data={vitals} dimension={dimension} vital={vital} onDimension={onDimension} onVital={onVital} onFilter={onFilter} loading={vitalsLoading} />
          </section>

          <section aria-labelledby="rum-vitals-attribution">
            <RumSectionHeader id="rum-vitals-attribution" title="What is behind the numbers" description="Page elements the browser attributed each vital to, ranked by poor experiences. Select a route to focus every RUM page on it." />
            <div className="grid gap-2.5 xl:grid-cols-3">
              <AttributionCard name="lcp" rows={vitals?.attribution.lcp} onFilter={onFilter} />
              <AttributionCard name="cls" rows={vitals?.attribution.cls} onFilter={onFilter} />
              <AttributionCard name="inp" rows={vitals?.attribution.inp} onFilter={onFilter} />
            </div>
          </section>

          <section aria-labelledby="rum-vitals-trends">
            <RumSectionHeader id="rum-vitals-trends" title="Trends" description={markers.length ? `p75 over time. Dashed lines mark the first traffic of each release (${markers.map((marker) => marker.label).join(', ')}).` : 'p75 over time. Releases seen in this window are drawn as dashed markers.'} />
            {error ? <QueryErrorPanel label="Web Vital trends" error={error} onRetry={onRetry} /> : (
              <div className="grid gap-2.5 xl:grid-cols-2">
                <ChartPanel title="Largest Contentful Paint" hint="Good ≤ 2.5s · poor > 4s">
                  <ApmTimeChart data={series} loading={loading} empty="No LCP samples in this window." height={220} markers={markers} series={[{ key: 'lcp_p75', name: 'LCP p75', color: '#7c3aed', fmt: (value) => formatRumVital('lcp', value) }]} />
                </ChartPanel>
                <ChartPanel title="Interaction to Next Paint" hint="Good ≤ 200ms · poor > 500ms">
                  <ApmTimeChart data={series} loading={loading} empty="No INP samples in this window." height={220} markers={markers} series={[{ key: 'inp_p75', name: 'INP p75', color: '#0284c7', fmt: (value) => formatRumVital('inp', value) }]} />
                </ChartPanel>
                <ChartPanel title="Cumulative Layout Shift" hint="Good ≤ 0.1 · poor > 0.25">
                  <ApmTimeChart data={series} loading={loading} empty="No CLS samples in this window." height={220} markers={markers} series={[{ key: 'cls_p75', name: 'CLS p75', color: '#db2777', fmt: (value) => formatRumVital('cls', value) }]} />
                </ChartPanel>
                <ChartPanel title="Navigation timing" hint="FCP, TTFB and full page load">
                  <ApmTimeChart data={series} loading={loading} empty="No navigation timing samples in this window." height={220} markers={markers} series={[
                    { key: 'fcp_p75', name: 'FCP p75', color: '#0891b2', fmt: (value) => formatRumVital('fcp', value) },
                    { key: 'ttfb_p75', name: 'TTFB p75', color: '#f59e0b', fmt: (value) => formatRumVital('ttfb', value) },
                    { key: 'load_p75', name: 'Load p75', color: '#6366f1', fmt: (value) => formatRumVital('load', value) },
                  ]} />
                </ChartPanel>
              </div>
            )}
          </section>
          {vitals?.coverage.partial && <Badge variant="outline">Distribution, segments and attribution cover the most recent 14 days of raw events; trends cover the full window.</Badge>}
        </>
      )}
    </div>
  )
}
