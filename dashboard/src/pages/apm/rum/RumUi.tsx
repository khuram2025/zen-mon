import { useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  Clock3,
  ExternalLink,
  FileWarning,
  Filter,
  Gauge,
  Globe2,
  Layers3,
  Loader2,
  MousePointerClick,
  Network,
  RefreshCw,
  RotateCcw,
  SlidersHorizontal,
  Users,
} from 'lucide-react'
import { apiErrorMessage, cn } from '@/lib/utils'
import { fmtCount } from '@/components/apm/viz'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Th } from '@/components/ui/Table'
import type {
  RumFacetValue,
  RumCoverage,
  RumFacets,
  RumFilters,
  RumRange,
  RumSamplingMetadata,
  RumTab,
  RumVitalMetric,
} from '@/types/apm'
import type { RumSortOrder } from './useRumUrlState'
import { formatDurationMs, formatRumVital, normalizeVitalDistribution, vitalBand, VITAL_LIMITS, type RumVitalName } from './model'

export { formatDurationMs, formatRumVital, vitalBand } from './model'

const ALL = '__all__'

const VITAL_LABEL: Record<RumVitalName, string> = {
  lcp: 'Largest Contentful Paint',
  inp: 'Interaction to Next Paint',
  cls: 'Cumulative Layout Shift',
  fcp: 'First Contentful Paint',
  ttfb: 'Time to First Byte',
  load: 'Page load',
}

const VITAL_SHORT: Record<RumVitalName, string> = {
  lcp: 'LCP', inp: 'INP', cls: 'CLS', fcp: 'FCP', ttfb: 'TTFB', load: 'Load',
}

const RANGES: Array<{ value: RumRange; label: string }> = [
  { value: '15m', label: 'Last 15 minutes' },
  { value: '1h', label: 'Last hour' },
  { value: '6h', label: 'Last 6 hours' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: '90d', label: 'Last 90 days' },
]

const TABS: Array<{ value: RumTab; label: string; icon: LucideIcon }> = [
  { value: 'overview', label: 'Overview', icon: BarChart3 },
  { value: 'web-vitals', label: 'Web Vitals', icon: Gauge },
  { value: 'views', label: 'Views', icon: Layers3 },
  { value: 'sessions', label: 'Sessions', icon: Users },
  { value: 'errors', label: 'Errors', icon: FileWarning },
  { value: 'resources', label: 'Resources', icon: Network },
  { value: 'actions', label: 'Actions', icon: MousePointerClick },
]

const PRIMARY_FILTERS: Array<{ key: keyof RumFilters; label: string; placeholder: string }> = [
  { key: 'application_id', label: 'Application', placeholder: 'All applications' },
  { key: 'env', label: 'Environment', placeholder: 'All environments' },
  { key: 'view_name', label: 'View', placeholder: 'All views' },
]

const ADVANCED_FILTERS: Array<{ key: keyof RumFilters; label: string; placeholder: string }> = [
  { key: 'browser', label: 'Browser', placeholder: 'All browsers' },
  { key: 'browser_version', label: 'Browser version', placeholder: 'All versions' },
  { key: 'os', label: 'Operating system', placeholder: 'All operating systems' },
  { key: 'device_type', label: 'Device', placeholder: 'All devices' },
  { key: 'country', label: 'Country', placeholder: 'All countries' },
  { key: 'service_version', label: 'Release', placeholder: 'All releases' },
]

export function RumRangePicker({ value, onChange }: { value: RumRange; onChange: (value: RumRange) => void }) {
  return (
    <label className="relative inline-flex h-9 items-center gap-2 rounded-md border border-border bg-surface px-2.5 text-xs text-text2">
      <Clock3 className="h-3.5 w-3.5 text-muted" aria-hidden />
      <span className="sr-only">Time range</span>
      <select
        aria-label="RUM time range"
        value={value}
        onChange={(event) => onChange(event.target.value as RumRange)}
        className="appearance-none bg-transparent pr-5 font-medium text-text outline-none"
      >
        {RANGES.map((range) => <option key={range.value} value={range.value}>{range.label}</option>)}
      </select>
      <span className="pointer-events-none absolute right-2 text-[9px] text-muted">▼</span>
    </label>
  )
}

export function RumTabBar({ value, onChange }: { value: RumTab; onChange: (value: RumTab) => void }) {
  return (
    <Tabs value={value} onValueChange={(next) => onChange(next as RumTab)}>
      <div className="overflow-x-auto pb-0.5">
        <TabsList className="h-10 min-w-max bg-surface2/70">
          {TABS.map((tab) => {
            const Icon = tab.icon
            return (
              <TabsTrigger key={tab.value} value={tab.value} className="gap-1.5 px-2.5 text-xs sm:px-3">
                <Icon className="h-3.5 w-3.5" aria-hidden />{tab.label}
              </TabsTrigger>
            )
          })}
        </TabsList>
      </div>
    </Tabs>
  )
}

function facetOptions(items: RumFacetValue[] | undefined, selected: string): RumFacetValue[] {
  const list = items ?? []
  if (!selected || list.some((item) => item.value === selected)) return list
  return [{ value: selected, count: 0 }, ...list]
}

export function RumFilterBar({
  filters,
  facets,
  loading,
  error,
  activeCount,
  onChange,
  onClear,
  onRetry,
}: {
  filters: RumFilters
  facets?: Partial<RumFacets>
  loading?: boolean
  error?: boolean
  activeCount: number
  onChange: (key: keyof RumFilters, value: string) => void
  onClear: () => void
  onRetry?: () => void
}) {
  const advancedActive = ADVANCED_FILTERS.filter(({ key }) => filters[key]).length
  const [advancedOpen, setAdvancedOpen] = useState(advancedActive > 0)
  const renderFilter = ({ key, label, placeholder }: typeof PRIMARY_FILTERS[number]) => {
    const values = facetOptions(facets?.[key], filters[key])
    return (
      <label key={key} className={cn('min-w-0', key === 'view_name' && 'col-span-2 md:col-span-1')}>
        <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</span>
        <Select value={filters[key] || ALL} onValueChange={(value) => onChange(key, value === ALL ? '' : value)}>
          <SelectTrigger className="h-8 min-w-0 text-xs" aria-label={`Filter by ${label.toLowerCase()}`}>
            <SelectValue placeholder={placeholder} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{placeholder}</SelectItem>
            {values.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.value || 'Unknown'}{item.count > 0 ? ` (${fmtCount(item.count)})` : ''}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>
    )
  }
  return (
    <Card>
      <CardContent className="p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-medium text-text2">
            <Filter className="h-3.5 w-3.5 text-primary" aria-hidden /> Segment experience
            {activeCount > 0 && <Badge variant="info">{activeCount} active</Badge>}
            {loading && <Loader2 className="h-3 w-3 animate-spin text-muted" aria-label="Loading filter values" />}
            {error && <button type="button" className="text-[10px] font-normal text-warning hover:underline" onClick={onRetry}>Filter values unavailable · retry</button>}
          </div>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] text-muted" onClick={() => setAdvancedOpen((open) => !open)} aria-expanded={advancedOpen || advancedActive > 0}>
              <SlidersHorizontal className="h-3 w-3" /> More{advancedActive > 0 ? ` (${advancedActive})` : ''}{advancedOpen || advancedActive > 0 ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </Button>
            {activeCount > 0 && (
              <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] text-muted" onClick={onClear}>
                <RotateCcw className="h-3 w-3" /> Clear
              </Button>
            )}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3">{PRIMARY_FILTERS.map(renderFilter)}</div>
        {(advancedOpen || advancedActive > 0) && <div className="mt-3 grid grid-cols-2 gap-2 border-t border-border/60 pt-3 md:grid-cols-3 xl:grid-cols-6">{ADVANCED_FILTERS.map(renderFilter)}</div>}
      </CardContent>
    </Card>
  )
}

const BAND_STYLE = {
  good: { label: 'Good', text: 'text-success', bar: 'bg-success', border: 'border-success/30' },
  'needs-improvement': { label: 'Needs improvement', text: 'text-warning', bar: 'bg-warning', border: 'border-warning/30' },
  poor: { label: 'Poor', text: 'text-danger', bar: 'bg-danger', border: 'border-danger/30' },
  'no-data': { label: 'No data', text: 'text-muted', bar: 'bg-muted/40', border: 'border-border' },
} as const

export function RumVitalCard({ name, metric, compact = false }: { name: RumVitalName; metric: RumVitalMetric; compact?: boolean }) {
  const band = vitalBand(name, metric.p75)
  const style = BAND_STYLE[band]
  const limits = VITAL_LIMITS[name]
  const max = limits.poor * 1.35 || 1
  const width = metric.p75 == null ? 0 : Math.max(2, Math.min(100, metric.p75 / max * 100))
  const hasDistribution = metric.good_pct != null || metric.needs_improvement_pct != null || metric.poor_pct != null
  const distribution = normalizeVitalDistribution(metric)
  return (
    <Card className={cn('relative overflow-hidden', style.border)}>
      <div className={cn('absolute inset-x-0 top-0 h-0.5', style.bar)} />
      <CardContent className={cn('p-4', compact && 'p-3')}>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{VITAL_SHORT[name]} p75</div>
            <div className={cn('mt-1 font-bold tabular-nums', compact ? 'text-xl' : 'text-2xl', style.text)}>
              {formatRumVital(name, metric.p75)}
            </div>
          </div>
          <Badge variant={band === 'good' ? 'success' : band === 'poor' ? 'danger' : band === 'needs-improvement' ? 'warning' : 'outline'}>
            {style.label}
          </Badge>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface2" aria-hidden>
          <div className={cn('h-full rounded-full', style.bar)} style={{ width: `${width}%` }} />
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] text-muted">
          <span>{metric.samples.toLocaleString()} {metric.samples === 1 ? 'sample' : 'samples'}</span>
          <span title={VITAL_LABEL[name]}>Good ≤ {formatRumVital(name, limits.good)}</span>
        </div>
        {hasDistribution && (
          <div className="mt-3">
            <div className="flex h-1.5 overflow-hidden rounded-full bg-surface2" aria-label="Experience distribution">
              <div className="bg-success" style={{ width: `${Math.max(0, distribution.good ?? 0)}%` }} />
              <div className="bg-warning" style={{ width: `${Math.max(0, distribution.needsImprovement ?? 0)}%` }} />
              <div className="bg-danger" style={{ width: `${Math.max(0, distribution.poor ?? 0)}%` }} />
            </div>
            <div className="mt-1 flex justify-between text-[9px] text-muted">
              <span>{distribution.good == null ? '—' : `${distribution.good.toFixed(0)}% good`}</span>
              <span>{distribution.poor == null ? '—' : `${distribution.poor.toFixed(0)}% poor`}</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function RumMetricCell({ name, value, samples }: { name: RumVitalName; value: number | null | undefined; samples?: number }) {
  const band = vitalBand(name, value)
  return (
    <div className="text-right">
      <div className={cn(
        'whitespace-nowrap font-mono text-xs tabular-nums',
        band === 'good' && 'text-success',
        band === 'needs-improvement' && 'text-warning',
        band === 'poor' && 'text-danger',
        band === 'no-data' && 'text-muted',
      )}>{value == null ? '—' : formatRumVital(name, value)}</div>
      {samples != null && <div className="text-[9px] text-muted">n={samples.toLocaleString()}</div>}
    </div>
  )
}

export function QueryErrorPanel({ error, label = 'RUM data', onRetry }: { error: unknown; label?: string; onRetry?: () => void }) {
  return (
    <Card className="border-danger/30">
      <CardContent className="flex flex-col items-center py-10 text-center">
        <AlertTriangle className="h-7 w-7 text-danger/70" aria-hidden />
        <div className="mt-2 text-sm font-semibold text-text">Could not load {label}</div>
        <p className="mt-1 max-w-lg text-xs text-muted">{apiErrorMessage(error, 'The analytics service did not respond.')}</p>
        {onRetry && <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}><RefreshCw className="h-3.5 w-3.5" /> Retry</Button>}
      </CardContent>
    </Card>
  )
}

export function RumPageSkeleton() {
  return (
    <div className="space-y-4" aria-label="Loading RUM dashboard">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => <Skeleton key={index} className="h-28" />)}
      </div>
      <Skeleton className="h-72" />
      <div className="grid gap-3 lg:grid-cols-2"><Skeleton className="h-60" /><Skeleton className="h-60" /></div>
    </div>
  )
}

export function RumEmptyState({
  title,
  description,
  action,
  icon: Icon = Globe2,
}: {
  title: string
  description: string
  action?: ReactNode
  icon?: LucideIcon
}) {
  return (
    <div className="flex flex-col items-center justify-center px-4 py-12 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-primary/10 text-primary"><Icon className="h-5 w-5" /></span>
      <h3 className="mt-3 text-sm font-semibold text-text">{title}</h3>
      <p className="mt-1 max-w-xl text-xs leading-relaxed text-muted">{description}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function RumTableCard({ title, description, actions, notice, children, footer }: {
  title: string
  description?: string
  actions?: ReactNode
  notice?: ReactNode
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-row items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <CardTitle className="text-sm">{title}</CardTitle>
          {description && <p className="mt-0.5 text-[11px] text-muted">{description}</p>}
        </div>
        {actions}
      </CardHeader>
      {notice}
      <CardContent className="p-0">{children}</CardContent>
      {footer}
    </Card>
  )
}

export function RumCoverageNotice({ coverage, showRetention = false }: { coverage?: RumCoverage; showRetention?: boolean }) {
  if (!coverage || (!coverage.partial && !showRetention)) return null
  return (
    <div role="status" className="flex items-start gap-2 border-b border-warning/25 bg-warning/10 px-4 py-2.5 text-[11px] leading-relaxed text-text2">
      <Clock3 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
      {coverage.partial ? <span>
        <span className="font-semibold text-text">90-day rollup:</span>{' '}
        overview and trends cover {coverage.rollup_retention_days} days; event-level rows cover the latest {coverage.raw_retention_days} days.
      </span> : <span>{coverage.message || `Event-level detail is retained for ${coverage.raw_retention_days} days.`}</span>}
    </div>
  )
}

export function RumSamplingNotice({ sampling }: { sampling?: RumSamplingMetadata }) {
  if (!sampling?.includes_retained_unsampled_errors) return null
  return (
    <div role="status" className="flex items-start gap-2 border-b border-info/25 bg-info/5 px-4 py-2.5 text-[11px] leading-relaxed text-text2">
      <Activity className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" aria-hidden />
      <span><span className="font-semibold text-text">Sampling-aware errors:</span> forced unsampled errors remain visible here; aggregate session-impact rates use the sampled cohort.</span>
    </div>
  )
}

export function SortableTh({
  label,
  sortKey,
  activeSort,
  order,
  onSort,
  className,
}: {
  label: string
  sortKey: string
  activeSort: string
  order: RumSortOrder
  onSort: (key: string) => void
  className?: string
}) {
  const active = activeSort === sortKey
  return (
    <Th className={className} aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn('inline-flex w-full items-center gap-1 whitespace-nowrap hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40', className?.includes('text-right') && 'justify-end')}
      >
        {label}<span aria-hidden className={active ? 'text-primary' : 'text-muted/40'}>{active ? order === 'asc' ? '↑' : '↓' : '↕'}</span>
      </button>
    </Th>
  )
}

export function RumPager({
  page,
  pageSize,
  total,
  noun,
  onPage,
  onPageSize,
}: {
  page: number
  pageSize: number
  total: number
  noun: string
  onPage: (page: number) => void
  onPageSize: (size: number) => void
}) {
  if (!total) return null
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const safePage = Math.min(page, pageCount)
  const first = (safePage - 1) * pageSize + 1
  const last = Math.min(total, safePage * pageSize)
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2.5 text-xs">
      <span className="text-muted">{first.toLocaleString()}–{last.toLocaleString()} of <span className="font-medium text-text2">{total.toLocaleString()}</span> {noun}</span>
      <div className="flex items-center gap-2">
        <select
          aria-label={`${noun} per page`}
          value={pageSize}
          onChange={(event) => onPageSize(Number(event.target.value))}
          className="h-7 rounded-md border border-border bg-surface px-1.5 text-xs text-text2"
        >
          {[25, 50, 100].map((size) => <option key={size} value={size}>{size} / page</option>)}
        </select>
        <Button size="sm" variant="outline" className="h-7 w-7 p-0" aria-label="Previous page" disabled={safePage <= 1} onClick={() => onPage(safePage - 1)}><ChevronLeft className="h-3.5 w-3.5" /></Button>
        <span className="min-w-[64px] text-center tabular-nums text-muted">{safePage} / {pageCount}</span>
        <Button size="sm" variant="outline" className="h-7 w-7 p-0" aria-label="Next page" disabled={safePage >= pageCount} onClick={() => onPage(safePage + 1)}><ChevronRight className="h-3.5 w-3.5" /></Button>
      </div>
    </div>
  )
}

export function TracePivot({ traceId, label = 'View trace', compact = false }: { traceId?: string | null; label?: string; compact?: boolean }) {
  const location = useLocation()
  if (!traceId) return <span className="text-xs text-muted">—</span>
  const returnTo = `${location.pathname}${location.search}`
  const query = new URLSearchParams({ from: 'rum', rum_return: returnTo })
  return (
    <Link
      to={`/apm/traces/${traceId}?${query}`}
      state={{ returnTo }}
      onClick={(event) => event.stopPropagation()}
      className={cn('inline-flex items-center gap-1 font-medium text-primary hover:underline', compact ? 'font-mono text-[10px]' : 'text-xs')}
      title={`Open backend trace ${traceId}`}
    >
      {compact ? `${traceId.slice(0, 10)}…` : label}<ExternalLink className="h-3 w-3" aria-hidden />
    </Link>
  )
}

export function RefreshIndicator({ active }: { active: boolean }) {
  return active ? <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" aria-label="Refreshing RUM data" /> : null
}

export function SignalTile({ icon: Icon = Activity, label, value, hint, tone = 'primary' }: {
  icon?: LucideIcon
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: 'primary' | 'success' | 'warning' | 'danger' | 'info' | 'accent' | 'muted'
}) {
  const colors = {
    primary: 'bg-primary/10 text-primary', success: 'bg-success/10 text-success', warning: 'bg-warning/10 text-warning',
    danger: 'bg-danger/10 text-danger', info: 'bg-info/10 text-info', accent: 'bg-accent/10 text-accent', muted: 'bg-surface2 text-muted',
  }
  return (
    <Card>
      <CardContent className="flex items-start gap-3 p-4">
        <span className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-lg', colors[tone])}><Icon className="h-4 w-4" /></span>
        <div className="min-w-0">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className="mt-0.5 text-xl font-bold tabular-nums text-text">{value}</div>
          {hint && <div className="mt-1 text-[10px] text-muted">{hint}</div>}
        </div>
      </CardContent>
    </Card>
  )
}
