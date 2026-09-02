import { useEffect, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CalendarRange,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  Clock3,
  Download,
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
  Search,
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
  type FacetGroup,
  type HistogramBucket,
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
  MIN_CONFIDENT_SAMPLES,
  RUM_FILTER_LABEL,
  confidenceTitle,
  coreWebVitalsAssessment,
  formatDurationMs,
  formatRumVital,
  formatWindowLabel,
  isLowConfidence,
  isoToLocalInput,
  localInputToIso,
  normalizeVitalDistribution,
  vitalBand,
  VITAL_LIMITS,
  type RumCustomBounds,
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

const RANGES: Array<{ value: Exclude<RumRange, 'custom'>; label: string; short: string }> = [
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
  { key: 'client_ip', placeholder: 'All client IPs' },
  { key: 'user_id', placeholder: 'All users' },
  { key: 'service_version', placeholder: 'All releases' },
]

/** Free-text search box: commits on Enter or after a short pause, clears with the × button. */
function SearchBox({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [draft, setDraft] = useState(value)
  useEffect(() => { setDraft(value) }, [value])
  useEffect(() => {
    if (draft === value) return
    const handle = window.setTimeout(() => onChange(draft.trim()), 450)
    return () => window.clearTimeout(handle)
  }, [draft, onChange, value])
  return (
    <label className="relative min-w-[180px] flex-1">
      <span className="sr-only">Search sessions, users, views, URLs and errors</span>
      <Search className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted" aria-hidden />
      <input
        type="search"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => { if (event.key === 'Enter') onChange(draft.trim()) }}
        placeholder="Search session, user, URL, error…"
        className="h-7 w-full rounded-md border border-border bg-surface pl-6 pr-6 text-[11px] text-text outline-none placeholder:text-muted focus:border-primary focus:ring-2 focus:ring-primary/20"
      />
      {draft && (
        <button type="button" aria-label="Clear search" onClick={() => { setDraft(''); onChange('') }} className="absolute right-1.5 top-1/2 -translate-y-1/2 text-muted hover:text-text">
          <X className="h-3 w-3" />
        </button>
      )}
    </label>
  )
}

export function RumRangePicker({ value, bounds, onChange, onCustom }: {
  value: RumRange
  bounds?: RumCustomBounds
  onChange: (value: RumRange) => void
  onCustom: (from: string, to: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [error, setError] = useState<string | null>(null)
  const openEditor = () => {
    const end = bounds ? new Date(bounds.to) : new Date()
    const start = bounds ? new Date(bounds.from) : new Date(end.getTime() - 24 * 3_600_000)
    setFrom(isoToLocalInput(start.toISOString()))
    setTo(isoToLocalInput(end.toISOString()))
    setError(null)
    setOpen(true)
  }
  const apply = () => {
    const startIso = localInputToIso(from)
    const endIso = localInputToIso(to)
    if (!startIso || !endIso) return setError('Pick both a start and an end.')
    const span = new Date(endIso).getTime() - new Date(startIso).getTime()
    if (span <= 0) return setError('The end must be after the start.')
    if (span > 90 * 86_400_000) return setError('Windows are limited to 90 days.')
    if (Date.now() - new Date(startIso).getTime() > 91 * 86_400_000) return setError('RUM data is retained for 90 days.')
    setOpen(false)
    onCustom(startIso, endIso)
  }
  const custom = value === 'custom'
  return (
    <div className="relative">
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
        <button
          type="button"
          role="tab"
          aria-selected={custom}
          aria-expanded={open}
          title={custom ? formatWindowLabel(bounds) : 'Pick an absolute start and end'}
          onClick={openEditor}
          className={cn(
            'inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-semibold transition-colors',
            custom ? 'bg-primary text-black' : 'text-muted hover:text-text',
          )}
        >
          <CalendarRange className="h-3 w-3" aria-hidden />
          {custom ? formatWindowLabel(bounds) : 'Custom'}
        </button>
      </div>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-[300px] rounded-md border border-border bg-surface p-3 shadow-lg" role="dialog" aria-label="Custom time range">
          <div className="grid gap-2">
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted">
              From
              <input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} className="mt-1 h-8 w-full rounded-md border border-border bg-surface2 px-2 text-xs font-normal normal-case tracking-normal text-text" />
            </label>
            <label className="text-[10px] font-semibold uppercase tracking-wider text-muted">
              To
              <input type="datetime-local" value={to} onChange={(event) => setTo(event.target.value)} className="mt-1 h-8 w-full rounded-md border border-border bg-surface2 px-2 text-xs font-normal normal-case tracking-normal text-text" />
            </label>
            {error && <p className="text-[11px] text-danger">{error}</p>}
            <p className="text-[10px] text-muted">Times are in your local zone. Windows longer than 14 days read the 5-minute rollup.</p>
            <div className="flex justify-end gap-1.5">
              <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px]" onClick={() => setOpen(false)}>Cancel</Button>
              <Button size="sm" className="h-7 px-2.5 text-[11px]" onClick={apply}>Apply</Button>
            </div>
          </div>
        </div>
      )}
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
    <label className="min-w-[132px] flex-1">
      <span className="sr-only">{RUM_FILTER_LABEL[filterKey]}</span>
      <Select value={filters[filterKey] || ALL} onValueChange={(value) => onChange(filterKey, value === ALL ? '' : value)}>
        <SelectTrigger className="h-7 min-w-0 bg-surface text-[11px]" aria-label={`Filter by ${RUM_FILTER_LABEL[filterKey].toLowerCase()}`}>
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
  // The three primary filters and the search box already read back from their
  // own controls, so only the secondary ones (set from "More" or the facet
  // sidebar) need a chip.
  const chips = ADVANCED_FILTERS.map(({ key }) => key).filter((key) => filters[key])
  return (
    <div className="rounded-md border border-border bg-surface2/35">
      <div className="flex flex-wrap items-center gap-1.5 px-2 py-1.5">
        <span className="inline-flex shrink-0 items-center gap-1 pl-0.5 pr-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
          <Filter className="h-3 w-3 text-primary" aria-hidden />
          Segment
        </span>
        {PRIMARY_FILTERS.map((filter) => (
          <FilterSelect key={filter.key} filterKey={filter.key} placeholder={filter.placeholder} filters={filters} facets={facets} onChange={onChange} />
        ))}
        <SearchBox value={filters.q} onChange={(value) => onChange('q', value)} />
        {chips.map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => onChange(key, '')}
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded border border-primary/25 bg-primary/10 px-1.5 text-[11px] font-medium text-primary hover:bg-primary/15"
            aria-label={`Remove ${RUM_FILTER_LABEL[key]} filter`}
            title={`${RUM_FILTER_LABEL[key]}: ${filters[key]}`}
          >
            <span className="max-w-[140px] truncate">{filters[key]}</span>
            <X className="h-3 w-3 shrink-0" />
          </button>
        ))}
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          {loading && <Loader2 className="h-3 w-3 animate-spin text-muted" aria-label="Loading filter values" />}
          {error && (
            <button type="button" className="px-1 text-[10px] text-warning hover:underline" onClick={onRetry}>
              Filter values unavailable · retry
            </button>
          )}
          {!compact && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 px-1.5 text-[11px] text-muted"
              onClick={() => setAdvancedOpen((open) => !open)}
              aria-expanded={advancedOpen}
            >
              <SlidersHorizontal className="h-3 w-3" />
              More{advancedActive > 0 ? ` (${advancedActive})` : ''}
              {advancedOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </Button>
          )}
          {activeCount > 0 && (
            <Button variant="ghost" size="sm" className="h-7 gap-1 px-1.5 text-[11px] text-muted" onClick={onClear}>
              <RotateCcw className="h-3 w-3" /> Clear
            </Button>
          )}
        </div>
      </div>
      {!compact && advancedOpen && (
        <div className="grid grid-cols-2 gap-1.5 border-t border-border/60 px-2 py-1.5 md:grid-cols-4 xl:grid-cols-7">
          {ADVANCED_FILTERS.map((filter) => (
            <FilterSelect key={filter.key} filterKey={filter.key} placeholder={filter.placeholder} filters={filters} facets={facets} onChange={onChange} />
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

export function RumVitalCard({ name, metric, compact = false, delta }: { name: RumVitalName; metric: RumVitalMetric; compact?: boolean; delta?: ReactNode }) {
  const band = vitalBand(name, metric.p75)
  const lowConfidence = isLowConfidence(metric.samples)
  const style = BAND_STYLE[band]
  const limits = VITAL_LIMITS[name]
  const max = limits.poor * 1.35 || 1
  const width = metric.p75 == null ? 0 : Math.max(2, Math.min(100, metric.p75 / max * 100))
  const hasDistribution = metric.good_pct != null || metric.needs_improvement_pct != null || metric.poor_pct != null
  const distribution = normalizeVitalDistribution(metric)
  return (
    <Card className={cn('relative h-full overflow-hidden', style.border)}>
      <div className={cn('absolute inset-x-0 top-0 h-0.5', style.bar)} />
      <CardContent className={cn('p-3', compact && 'p-2.5')}>
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{VITAL_SHORT[name]} p75</div>
            <div className={cn('mt-0.5 font-bold tabular-nums tracking-tight', compact ? 'text-xl' : 'text-[1.5rem] leading-none', style.text)}>
              {formatRumVital(name, metric.p75)}
            </div>
            <div className="mt-0.5 truncate text-[10px] text-muted">{VITAL_LABEL[name]}</div>
          </div>
          <Badge variant={lowConfidence ? 'outline' : style.badge} title={confidenceTitle(metric.samples)}>{lowConfidence ? `${style.label} · indicative` : style.label}</Badge>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface2" aria-hidden>
          <div className={cn('h-full rounded-full', style.bar)} style={{ width: `${width}%` }} />
        </div>
        <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted">
          <span title={confidenceTitle(metric.samples)}>{metric.samples.toLocaleString()} {metric.samples === 1 ? 'sample' : 'samples'}{lowConfidence ? ` · fewer than ${MIN_CONFIDENT_SAMPLES}` : ''}</span>
          {delta}
          <span>Good ≤ {formatRumVital(name, limits.good)}</span>
        </div>
        {hasDistribution && (
          <div className="mt-2">
            <div className="flex h-1.5 overflow-hidden rounded-full bg-surface2" aria-label="Experience distribution">
              <div className="bg-success" style={{ width: `${Math.max(0, distribution.good ?? 0)}%` }} />
              <div className="bg-warning" style={{ width: `${Math.max(0, distribution.needsImprovement ?? 0)}%` }} />
              <div className="bg-danger" style={{ width: `${Math.max(0, distribution.poor ?? 0)}%` }} />
            </div>
            <div className="mt-1 flex justify-between text-[10px] text-muted">
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
      <CardContent className="flex h-full items-center gap-3 p-3">
        <div className="relative h-[76px] w-[76px] shrink-0">
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
          <div className="text-sm font-semibold text-text">{style.label}</div>
          <p className="mt-0.5 text-[10px] leading-snug text-muted">
            {assessment.rated === 0
              ? 'Waiting for finalized Core Web Vital samples.'
              : `${assessment.goodCount} of ${assessment.rated} Core Web Vitals are in the good range at p75.`}
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1">
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
  const lowConfidence = isLowConfidence(samples)
  return (
    <div className="text-right" title={confidenceTitle(samples)}>
      <div className={cn(
        'whitespace-nowrap font-mono text-xs tabular-nums',
        band === 'good' && 'text-success',
        band === 'needs-improvement' && 'text-warning',
        band === 'poor' && 'text-danger',
        band === 'no-data' && 'text-muted',
        lowConfidence && 'opacity-60',
      )}>{value == null ? '—' : formatRumVital(name, value)}{lowConfidence && <span aria-hidden className="ml-0.5 text-muted">~</span>}</div>
      {samples != null && <div className="text-[9px] text-muted">n={samples.toLocaleString()}{lowConfidence ? ' · low' : ''}</div>}
    </div>
  )
}

/** Labelled p75 tile with its Core Web Vitals rating, for detail panels. */
export function RumVitalTile({ name, value, samples }: { name: RumVitalName; value: number | null | undefined; samples?: number }) {
  const band = vitalBand(name, value)
  const style = BAND_STYLE[band]
  const lowConfidence = isLowConfidence(samples)
  return (
    <div className={cn('rounded-lg border bg-surface2/35 p-2.5', lowConfidence ? 'border-dashed border-border' : style.border)} title={confidenceTitle(samples)}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[9px] font-semibold uppercase tracking-wider text-muted">{VITAL_SHORT[name]} p75</span>
        <span className={cn('h-1.5 w-1.5 rounded-full', style.bar, lowConfidence && 'opacity-50')} aria-hidden />
      </div>
      <div className={cn('mt-1 font-mono text-sm font-semibold tabular-nums', style.text, lowConfidence && 'opacity-70')}>{formatRumVital(name, value)}{lowConfidence && <span aria-hidden className="ml-0.5 text-muted">~</span>}</div>
      <div className="mt-0.5 truncate text-[10px] text-muted" title={VITAL_LABEL[name]}>
        {band === 'no-data' ? VITAL_LABEL[name] : lowConfidence ? `Indicative · n=${samples!.toLocaleString()}` : `${style.label}${samples != null ? ` · n=${samples.toLocaleString()}` : ''}`}
      </div>
    </div>
  )
}

export function QueryErrorPanel({ error, label = 'RUM data', onRetry }: { error: unknown; label?: string; onRetry?: () => void }) {
  return (
    <Card className="border-danger/30">
      <CardContent className="flex flex-col items-center py-8 text-center">
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
    <div className="flex flex-col items-center justify-center px-4 py-10 text-center">
      <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10 text-primary"><Icon className="h-4 w-4" /></span>
      <h3 className="mt-2.5 text-[13px] font-semibold text-text">{title}</h3>
      <p className="mt-1 max-w-xl text-[11px] leading-relaxed text-muted">{description}</p>
      {action && <div className="mt-3">{action}</div>}
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
    <div className="mb-1.5 flex items-end justify-between gap-3">
      <div className="min-w-0">
        <h3 id={id} className="text-[13px] font-semibold tracking-tight text-text">{title}</h3>
        {description && <p className="text-[11px] text-muted">{description}</p>}
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

export function RumExplorerShell({
  noun,
  total,
  rangeLabel,
  volume,
  filters,
  facets,
  onFilter,
  buckets,
  okLabel = 'Healthy',
  errLabel = 'Significant',
  onExport,
  exporting,
  children,
}: {
  noun: string
  total?: number
  rangeLabel?: string
  /** Event-level volume behind the grouped rows, e.g. "176 page views". */
  volume?: string
  filters: RumFilters
  facets?: Partial<RumFacets>
  onFilter: (key: keyof RumFilters, value: string) => void
  /** Volume over time for the whole range, from the RUM timeseries. */
  buckets: HistogramBucket[]
  okLabel?: string
  errLabel?: string
  /** Download the whole explorer (current filters and sort) as CSV. */
  onExport?: () => void
  exporting?: boolean
  children: ReactNode
}) {
  const shown = total ?? 0
  const label = shown === 1 && noun.endsWith('s') ? noun.slice(0, -1) : noun
  return (
    <ApmExplorerFrame
      summary={(
        <span className="flex flex-wrap items-center justify-between gap-2">
          <span>Displaying {fmtCount(shown)} {label}{volume ? ` · ${volume}` : ''}{rangeLabel ? ` · ${rangeLabel}` : ''}</span>
          {onExport && (
            <button
              type="button"
              onClick={onExport}
              disabled={exporting || shown === 0}
              className="inline-flex items-center gap-1 rounded border border-border bg-surface px-1.5 py-0.5 text-[10px] font-medium text-text2 hover:border-primary/40 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
              title={`Download up to 5,000 ${noun} matching the current filters as CSV`}
            >
              {exporting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
              Export CSV
            </button>
          )}
        </span>
      )}
      histogram={<VolumeHistogram buckets={buckets} okLabel={okLabel} errLabel={errLabel} />}
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
      <CardHeader className="flex-row items-center justify-between gap-3 border-b border-border px-3 py-2">
        <div className="min-w-0">
          <CardTitle className="text-[13px]">{title}</CardTitle>
          {description && <p className="text-[11px] text-muted">{description}</p>}
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
    <div role="status" className="flex items-start gap-2 border-b border-warning/25 bg-warning/10 px-3 py-1.5 text-[11px] leading-snug text-text2">
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
    <div role="status" className="flex items-start gap-2 border-b border-info/25 bg-info/5 px-3 py-1.5 text-[11px] leading-snug text-text2">
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
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-1.5 text-[11px]">
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
      <CardContent className="flex items-start gap-2.5 p-3">
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
