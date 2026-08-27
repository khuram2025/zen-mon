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
  X,
} from 'lucide-react'
import { apiErrorMessage, cn } from '@/lib/utils'
import { fmtCount } from '@/components/apm/viz'
import {
  ApmExplorerFrame,
  ApmFacetSidebar,
  ApmUnderlineNav,
  VolumeHistogram,
  bucketByTime,
  type FacetGroup,
} from '@/components/apm/explorer'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
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
import {
  RUM_FILTER_KEYS,
  RUM_FILTER_LABEL,
  coreWebVitalsAssessment,
  formatDurationMs,
  formatRumVital,
  normalizeVitalDistribution,
  vitalBand,
  VITAL_LIMITS,
  type RumVitalName,
} from './model'

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

const RANGES: Array<{ value: RumRange; label: string; short: string }> = [
  { value: '15m', label: 'Last 15 minutes', short: '15m' },
  { value: '1h', label: 'Last hour', short: '1h' },
  { value: '6h', label: 'Last 6 hours', short: '6h' },
  { value: '24h', label: 'Last 24 hours', short: '24h' },
  { value: '7d', label: 'Last 7 days', short: '7d' },
  { value: '30d', label: 'Last 30 days', short: '30d' },
  { value: '90d', label: 'Last 90 days', short: '90d' },
]

export const RUM_TABS: Array<{ value: RumTab; label: string; hint: string; icon: LucideIcon }> = [
  { value: 'overview', label: 'Overview', hint: 'Experience at a glance', icon: BarChart3 },
  { value: 'web-vitals', label: 'Web Vitals', hint: 'Field performance', icon: Gauge },
  { value: 'views', label: 'Views', hint: 'Routes and pages', icon: Layers3 },
  { value: 'sessions', label: 'Sessions', hint: 'User journeys', icon: Users },
  { value: 'errors', label: 'Errors', hint: 'JavaScript failures', icon: FileWarning },
  { value: 'resources', label: 'Resources', hint: 'Assets and XHR', icon: Network },
  { value: 'actions', label: 'Actions', hint: 'Clicks and frustration', icon: MousePointerClick },
]

const PRIMARY_FILTERS: Array<{ key: keyof RumFilters; placeholder: string }> = [
  { key: 'application_id', placeholder: 'All applications' },
  { key: 'env', placeholder: 'All environments' },
  { key: 'view_name', placeholder: 'All views' },
]

const ADVANCED_FILTERS: Array<{ key: keyof RumFilters; placeholder: string }> = [
  { key: 'browser', placeholder: 'All browsers' },
  { key: 'browser_version', placeholder: 'All versions' },
  { key: 'os', placeholder: 'All operating systems' },
  { key: 'device_type', placeholder: 'All devices' },
  { key: 'country', placeholder: 'All countries' },
  { key: 'service_version', placeholder: 'All releases' },
]

export function RumRangePicker({ value, onChange }: { value: RumRange; onChange: (value: RumRange) => void }) {
  return (
    <div
      role="tablist"
      aria-label="Time range"
      className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/40 p-0.5"
    >
      <Clock3 className="ml-1 mr-0.5 h-3 w-3 text-muted" aria-hidden />
      {RANGES.map((range) => {
        const active = value === range.value
        return (
          <button
            key={range.value}
            type="button"
            role="tab"
            title={range.label}
            aria-label={range.label}
            aria-selected={active}
            onClick={() => onChange(range.value)}
            className={cn(
              'rounded px-2 py-1 text-[11px] font-semibold transition-colors',
              active ? 'bg-primary text-black' : 'text-muted hover:text-text',
            )}
          >
            {range.short}
          </button>
        )
      })}
    </div>
  )
}

export function RumTabBar({
  value,
  onChange,
  counts,
}: {
  value: RumTab
  onChange: (value: RumTab) => void
  counts?: Partial<Record<RumTab, number>>
}) {
  return (
    <ApmUnderlineNav
      items={RUM_TABS.map((tab) => ({
        key: tab.value,
        label: tab.label,
        icon: tab.icon,
        title: tab.hint,
        count: counts?.[tab.value],
        current: value === tab.value,
        onSelect: () => onChange(tab.value),
      }))}
    />
  )
}

function facetOptions(items: RumFacetValue[] | undefined, selected: string): RumFacetValue[] {
  const list = items ?? []
  if (!selected || list.some((item) => item.value === selected)) return list
  return [{ value: selected, count: 0 }, ...list]
}

function FilterSelect({
  filterKey,
  placeholder,
  filters,
  facets,
  onChange,
}: {
  filterKey: keyof RumFilters
  placeholder: string
  filters: RumFilters
  facets?: Partial<RumFacets>
  onChange: (key: keyof RumFilters, value: string) => void
}) {
  const values = facetOptions(facets?.[filterKey], filters[filterKey])
  return (
    <label className="min-w-[148px] flex-1">
      <span className="sr-only">{RUM_FILTER_LABEL[filterKey]}</span>
      <Select value={filters[filterKey] || ALL} onValueChange={(value) => onChange(filterKey, value === ALL ? '' : value)}>
        <SelectTrigger className="h-8 min-w-0 bg-surface text-xs" aria-label={`Filter by ${RUM_FILTER_LABEL[filterKey].toLowerCase()}`}>
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

export function RumFilterBar({
  filters,
  facets,
  loading,
  error,
  activeCount,
  onChange,
  onClear,
  onRetry,
  compact,
}: {
  filters: RumFilters
  facets?: Partial<RumFacets>
  loading?: boolean
  error?: boolean
  activeCount: number
  onChange: (key: keyof RumFilters, value: string) => void
  onClear: () => void
  onRetry?: () => void
  compact?: boolean
}) {
  const advancedActive = ADVANCED_FILTERS.filter(({ key }) => filters[key]).length
  const [advancedOpen, setAdvancedOpen] = useState(advancedActive > 0)
  const chips = RUM_FILTER_KEYS.filter((key) => filters[key])
  return (
    <div className="rounded-lg border border-border bg-surface2/35">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">
          <Filter className="h-3.5 w-3.5 text-primary" aria-hidden />
          Segment
        </span>
        {PRIMARY_FILTERS.map((filter) => (
          <FilterSelect key={filter.key} filterKey={filter.key} placeholder={filter.placeholder} filters={filters} facets={facets} onChange={onChange} />
        ))}
        <div className="ml-auto flex items-center gap-1">
          {loading && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" aria-label="Loading filter values" />}
          {error && (
            <button type="button" className="text-[10px] text-warning hover:underline" onClick={onRetry}>
              Filter values unavailable · retry
            </button>
          )}
          {!compact && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 px-2 text-[11px] text-muted"
              onClick={() => setAdvancedOpen((open) => !open)}
              aria-expanded={advancedOpen || advancedActive > 0}
            >
              <SlidersHorizontal className="h-3 w-3" />
              More{advancedActive > 0 ? ` (${advancedActive})` : ''}
              {advancedOpen || advancedActive > 0 ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </Button>
          )}
          {activeCount > 0 && (
            <Button variant="ghost" size="sm" className="h-8 px-2 text-[11px] text-muted" onClick={onClear}>
              <RotateCcw className="h-3 w-3" /> Clear
            </Button>
          )}
        </div>
      </div>
      {!compact && (advancedOpen || advancedActive > 0) && (
        <div className="grid grid-cols-2 gap-2 border-t border-border/60 px-3 py-2 md:grid-cols-3 xl:grid-cols-6">
          {ADVANCED_FILTERS.map((filter) => (
            <FilterSelect key={filter.key} filterKey={filter.key} placeholder={filter.placeholder} filters={filters} facets={facets} onChange={onChange} />
          ))}
        </div>
      )}
      {chips.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-border/60 px-3 py-2">
          {chips.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onChange(key, '')}
              className="inline-flex items-center gap-1 rounded-full border border-primary/25 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary hover:bg-primary/15"
              aria-label={`Remove ${RUM_FILTER_LABEL[key]} filter`}
            >
              <span className="text-muted">{RUM_FILTER_LABEL[key]}</span>
              {filters[key]}
              <X className="h-3 w-3" />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const BAND_STYLE = {
  good: { label: 'Good', text: 'text-success', bar: 'bg-success', fill: 'stroke-success', border: 'border-success/30', badge: 'success' as const },
  'needs-improvement': { label: 'Needs improvement', text: 'text-warning', bar: 'bg-warning', fill: 'stroke-warning', border: 'border-warning/30', badge: 'warning' as const },
  poor: { label: 'Poor', text: 'text-danger', bar: 'bg-danger', fill: 'stroke-danger', border: 'border-danger/30', badge: 'danger' as const },
  'no-data': { label: 'No data', text: 'text-muted', bar: 'bg-muted/40', fill: 'stroke-muted', border: 'border-border', badge: 'outline' as const },
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
    <Card className={cn('relative h-full overflow-hidden', style.border)}>
      <div className={cn('absolute inset-x-0 top-0 h-0.5', style.bar)} />
      <CardContent className={cn('p-4', compact && 'p-3.5')}>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{VITAL_SHORT[name]} p75</div>
            <div className={cn('mt-1 font-bold tabular-nums tracking-tight', compact ? 'text-xl' : 'text-[1.65rem] leading-none', style.text)}>
              {formatRumVital(name, metric.p75)}
            </div>
            <div className="mt-1 truncate text-[11px] text-muted">{VITAL_LABEL[name]}</div>
          </div>
          <Badge variant={style.badge}>{style.label}</Badge>
        </div>
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface2" aria-hidden>
          <div className={cn('h-full rounded-full', style.bar)} style={{ width: `${width}%` }} />
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] text-muted">
          <span>{metric.samples.toLocaleString()} {metric.samples === 1 ? 'sample' : 'samples'}</span>
          <span>Good ≤ {formatRumVital(name, limits.good)}</span>
        </div>
        {hasDistribution && (
          <div className="mt-3">
            <div className="flex h-1.5 overflow-hidden rounded-full bg-surface2" aria-label="Experience distribution">
              <div className="bg-success" style={{ width: `${Math.max(0, distribution.good ?? 0)}%` }} />
              <div className="bg-warning" style={{ width: `${Math.max(0, distribution.needsImprovement ?? 0)}%` }} />
              <div className="bg-danger" style={{ width: `${Math.max(0, distribution.poor ?? 0)}%` }} />
            </div>
            <div className="mt-1.5 flex justify-between text-[10px] text-muted">
              <span>{distribution.good == null ? '—' : `${distribution.good.toFixed(0)}% good`}</span>
              <span>{distribution.poor == null ? '—' : `${distribution.poor.toFixed(0)}% poor`}</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function RumExperienceCard({
  vitals,
  href,
}: {
  vitals: { lcp: RumVitalMetric; inp: RumVitalMetric; cls: RumVitalMetric }
  href?: string
}) {
  const assessment = coreWebVitalsAssessment(vitals)
  const style = BAND_STYLE[assessment.band]
  const radius = 34
  const circumference = 2 * Math.PI * radius
  const progress = assessment.score == null ? 0 : Math.max(0, Math.min(100, assessment.score))
  const offset = circumference - (progress / 100) * circumference
  const inner = (
    <Card className={cn('relative h-full overflow-hidden', style.border, href && 'transition hover:border-primary/40')}>
      <div className={cn('absolute inset-x-0 top-0 h-0.5', style.bar)} />
      <CardContent className="flex h-full items-center gap-4 p-4">
        <div className="relative h-[88px] w-[88px] shrink-0">
          <svg viewBox="0 0 88 88" className="-rotate-90" aria-hidden>
            <circle cx="44" cy="44" r={radius} fill="none" strokeWidth="8" className="stroke-surface2" />
            <circle
              cx="44"
              cy="44"
              r={radius}
              fill="none"
              strokeWidth="8"
              strokeLinecap="round"
              strokeDasharray={circumference}
              strokeDashoffset={offset}
              className={style.fill}
            />
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center">
            <span className={cn('text-xl font-bold tabular-nums leading-none', style.text)}>
              {assessment.score == null ? '—' : assessment.score}
            </span>
            <span className="mt-0.5 text-[9px] font-semibold uppercase tracking-wider text-muted">score</span>
          </div>
        </div>
        <div className="min-w-0">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Field experience</div>
          <div className="mt-1 text-base font-semibold text-text">{style.label}</div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            {assessment.rated === 0
              ? 'Waiting for finalized Core Web Vital samples.'
              : `${assessment.goodCount} of ${assessment.rated} Core Web Vitals are in the good range at p75.`}
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {assessment.parts.map((part) => (
              <Badge key={part.name} variant={BAND_STYLE[part.band].badge} className="uppercase">
                {part.name} {part.band === 'no-data' ? 'n/a' : BAND_STYLE[part.band].label}
              </Badge>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  )
  return href ? <Link to={href} className="block h-full">{inner}</Link> : inner
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
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        {Array.from({ length: 6 }).map((_, index) => <Skeleton key={index} className="h-24" />)}
      </div>
      <div className="grid gap-3 xl:grid-cols-4">
        <Skeleton className="h-36" />
        <Skeleton className="h-36" />
        <Skeleton className="h-36" />
        <Skeleton className="h-36" />
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
    <div className="flex flex-col items-center justify-center px-4 py-14 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 text-primary"><Icon className="h-5 w-5" /></span>
      <h3 className="mt-3 text-sm font-semibold text-text">{title}</h3>
      <p className="mt-1 max-w-xl text-xs leading-relaxed text-muted">{description}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function RumSectionHeader({
  id,
  title,
  description,
  action,
}: {
  id?: string
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="mb-2 flex items-end justify-between gap-3">
      <div className="min-w-0">
        <h3 id={id} className="text-sm font-semibold tracking-tight text-text">{title}</h3>
        {description && <p className="mt-0.5 text-[11px] text-muted">{description}</p>}
      </div>
      {action}
    </div>
  )
}

export function rumClientFacetGroups(
  filters: RumFilters,
  facets: Partial<RumFacets> | undefined,
  onChange: (key: keyof RumFilters, value: string) => void,
): FacetGroup[] {
  return ADVANCED_FILTERS.map(({ key }) => ({
    title: RUM_FILTER_LABEL[key],
    items: (facets?.[key] ?? []).map((item) => ({
      value: item.value,
      count: item.count,
      active: filters[key] === item.value,
      onSelect: () => onChange(key, filters[key] === item.value ? '' : item.value),
    })),
  }))
}

export function RumExplorerShell<T>({
  noun,
  total,
  rangeLabel,
  filters,
  facets,
  onFilter,
  items,
  getTime,
  isErr,
  okLabel = 'Healthy',
  errLabel = 'Significant',
  children,
}: {
  noun: string
  total?: number
  rangeLabel?: string
  filters: RumFilters
  facets?: Partial<RumFacets>
  onFilter: (key: keyof RumFilters, value: string) => void
  items?: T[]
  getTime: (item: T) => string
  isErr: (item: T) => boolean
  okLabel?: string
  errLabel?: string
  children: ReactNode
}) {
  const rows = items ?? []
  const shown = total ?? rows.length
  const label = shown === 1 && noun.endsWith('s') ? noun.slice(0, -1) : noun
  return (
    <ApmExplorerFrame
      summary={<>Displaying {fmtCount(shown)} {label}{rangeLabel ? ` · ${rangeLabel}` : ''}</>}
      histogram={<VolumeHistogram buckets={bucketByTime(rows, getTime, isErr)} okLabel={okLabel} errLabel={errLabel} />}
      sidebar={<ApmFacetSidebar title="Client analytics" groups={rumClientFacetGroups(filters, facets, onFilter)} />}
    >
      {children}
    </ApmExplorerFrame>
  )
}

export function RumTableCard({ title, description, actions, notice, children, footer, embedded }: {
  title: string
  description?: string
  actions?: ReactNode
  notice?: ReactNode
  children: ReactNode
  footer?: ReactNode
  embedded?: boolean
}) {
  if (embedded) {
    return (
      <div>
        {notice}
        {children}
        {footer}
      </div>
    )
  }
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
  const nounLabel = total === 1 && noun.endsWith('s') ? noun.slice(0, -1) : noun
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2.5 text-xs">
      <span className="text-muted">{first.toLocaleString()}–{last.toLocaleString()} of <span className="font-medium text-text2">{total.toLocaleString()}</span> {nounLabel}</span>
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
