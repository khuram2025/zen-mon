import type { RumFilters, RumRange, RumTab, RumTimeseries, RumTimeseriesPoint, RumVitalMetric } from '@/types/apm'

export type RumVitalName = 'lcp' | 'inp' | 'cls' | 'fcp' | 'ttfb' | 'load'
export type RumVitalBand = 'good' | 'needs-improvement' | 'poor' | 'no-data'

export const RUM_FILTER_KEYS = ['application_id', 'env', 'view_name', 'browser', 'browser_version', 'os', 'device_type', 'country', 'service_version', 'client_ip', 'user_id', 'q'] as const satisfies readonly (keyof RumFilters)[]

export const RUM_FILTER_LABEL: Record<keyof RumFilters, string> = {
  application_id: 'Application',
  env: 'Environment',
  view_name: 'View',
  browser: 'Browser',
  browser_version: 'Browser version',
  os: 'Operating system',
  device_type: 'Device',
  country: 'Country',
  service_version: 'Release',
  client_ip: 'Client IP',
  user_id: 'User',
  q: 'Search',
}

/** Custom window bounds carried in the URL alongside `range=custom`. */
export interface RumCustomBounds {
  from: string
  to: string
}

/** `datetime-local` input value (viewer's zone, no seconds) → ISO with offset. */
export function localInputToIso(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toISOString()
}

/** ISO → `datetime-local` input value in the viewer's zone. */
export function isoToLocalInput(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatWindowLabel(bounds: RumCustomBounds | undefined): string {
  if (!bounds) return ''
  const from = new Date(bounds.from)
  const to = new Date(bounds.to)
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) return 'Custom'
  const sameDay = from.toDateString() === to.toDateString()
  const day = (d: Date) => d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  const time = (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return sameDay ? `${day(from)} ${time(from)} → ${time(to)}` : `${day(from)} ${time(from)} → ${day(to)} ${time(to)}`
}

export const VITAL_LIMITS: Record<RumVitalName, { good: number; poor: number }> = {
  lcp: { good: 2500, poor: 4000 },
  inp: { good: 200, poor: 500 },
  cls: { good: 0.1, poor: 0.25 },
  fcp: { good: 1800, poor: 3000 },
  ttfb: { good: 800, poor: 1800 },
  load: { good: 2500, poor: 4000 },
}

export function formatRumVital(name: RumVitalName, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return 'No data'
  if (name === 'cls') return value.toFixed(3)
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  return `${value.toFixed(value < 10 ? 1 : 0)} ms`
}

/**
 * Below this many finalized samples a p75 is dominated by individual visits
 * (one slow load moves it by whole seconds), so the UI presents it as
 * indicative rather than as a rating.
 */
export const MIN_CONFIDENT_SAMPLES = 20

export function isLowConfidence(samples: number | null | undefined): boolean {
  return samples != null && samples > 0 && samples < MIN_CONFIDENT_SAMPLES
}

export function confidenceTitle(samples: number | null | undefined): string | undefined {
  return isLowConfidence(samples)
    ? `Low confidence: ${samples} sample${samples === 1 ? '' : 's'} (fewer than ${MIN_CONFIDENT_SAMPLES}). A single visit can move this figure.`
    : undefined
}

export function vitalBand(name: RumVitalName, value: number | null | undefined): RumVitalBand {
  if (value == null || !Number.isFinite(value)) return 'no-data'
  const limits = VITAL_LIMITS[name]
  if (value <= limits.good) return 'good'
  if (value <= limits.poor) return 'needs-improvement'
  return 'poor'
}

function bandScore(band: RumVitalBand, goodShare: number | null): number | null {
  if (band === 'no-data') return null
  if (goodShare != null && Number.isFinite(goodShare)) return goodShare
  if (band === 'good') return 100
  if (band === 'needs-improvement') return 50
  return 0
}

export function coreWebVitalsAssessment(vitals: { lcp: RumVitalMetric; inp: RumVitalMetric; cls: RumVitalMetric }) {
  const names = ['lcp', 'inp', 'cls'] as const
  const parts = names.map((name) => {
    const band = vitalBand(name, vitals[name].p75)
    const goodShare = normalizeVitalDistribution(vitals[name]).good
    return { name, band, samples: vitals[name].samples, score: bandScore(band, goodShare) }
  })
  const rated = parts.filter((part) => part.score != null)
  const score = rated.length
    ? Math.round(rated.reduce((sum, part) => sum + (part.score ?? 0), 0) / rated.length)
    : null
  const goodCount = parts.filter((part) => part.band === 'good').length
  const poorCount = parts.filter((part) => part.band === 'poor').length
  const band: RumVitalBand = rated.length === 0
    ? 'no-data'
    : poorCount > 0
      ? 'poor'
      : goodCount === rated.length
        ? 'good'
        : 'needs-improvement'
  return { score, band, rated: rated.length, goodCount, parts }
}

export function normalizeVitalDistribution(metric: {
  good_pct?: number | null
  needs_improvement_pct?: number | null
  poor_pct?: number | null
}): { good: number | null; needsImprovement: number | null; poor: number | null } {
  const total = (metric.good_pct ?? 0) + (metric.needs_improvement_pct ?? 0) + (metric.poor_pct ?? 0)
  const scale = total > 0 && total <= 1.001 ? 100 : 1
  return {
    good: metric.good_pct == null ? null : metric.good_pct * scale,
    needsImprovement: metric.needs_improvement_pct == null ? null : metric.needs_improvement_pct * scale,
    poor: metric.poor_pct == null ? null : metric.poor_pct * scale,
  }
}

export function formatDurationMs(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (value >= 86_400_000) return `${(value / 86_400_000).toFixed(1)}d`
  if (value >= 3_600_000) return `${Math.floor(value / 3_600_000)}h ${Math.floor((value % 3_600_000) / 60_000)}m`
  if (value >= 60_000) return `${Math.floor(value / 60_000)}m ${Math.floor((value % 60_000) / 1000)}s`
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  return `${value.toFixed(value < 10 ? 1 : 0)} ms`
}

export function buildRumQuery(
  range: RumRange,
  filters: RumFilters,
  extra: Record<string, string | number | undefined> = {},
  bounds?: RumCustomBounds,
): string {
  const query = new URLSearchParams({ range })
  if (range === 'custom' && bounds) {
    query.set('from', bounds.from)
    query.set('to', bounds.to)
  }
  RUM_FILTER_KEYS.forEach((key) => {
    if (filters[key]) query.set(key, filters[key])
  })
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  return query.toString()
}

export function buildRumHref(tab: RumTab, range: RumRange, filters: RumFilters, bounds?: RumCustomBounds): string {
  const query = new URLSearchParams(buildRumQuery(range, filters, {}, bounds))
  if (tab === 'overview') query.delete('tab')
  else query.set('tab', tab)
  const encoded = query.toString()
  return encoded ? `/apm/rum?${encoded}` : '/apm/rum'
}

/** Length of the selected window in milliseconds (custom windows use their bounds). */
export function rangeMs(range: RumRange, bounds?: RumCustomBounds): number {
  if (range === 'custom' && bounds) {
    const span = new Date(bounds.to).getTime() - new Date(bounds.from).getTime()
    if (Number.isFinite(span) && span > 0) return span
  }
  return RUM_RANGE_MS[range === 'custom' ? '24h' : range]
}

export const RUM_RANGE_MS: Record<Exclude<RumRange, 'custom'>, number> = {
  '15m': 15 * 60_000,
  '1h': 3_600_000,
  '6h': 6 * 3_600_000,
  '24h': 86_400_000,
  '7d': 7 * 86_400_000,
  '30d': 30 * 86_400_000,
  '90d': 90 * 86_400_000,
}

/** Backend timestamps are UTC; naive values (no zone suffix) must not be read as browser-local time. */
export function parseUtc(iso: string | null | undefined): number {
  if (!iso) return Number.NaN
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)
  return new Date(hasTz ? iso : `${iso.replace(' ', 'T')}Z`).getTime()
}

export interface VolumeBucket {
  ok: number
  err: number
  label: string
}

/**
 * Turn the RUM timeseries into evenly spaced volume buckets covering the whole
 * selected range (empty intervals included), for the explorer histograms.
 * `err` is a subset of `ok` when `errWithinOk` is set (e.g. failed resources
 * within all resources) and is subtracted so the stack sums to the total.
 */
export function volumeBuckets(
  timeseries: RumTimeseries | undefined,
  range: RumRange,
  keys: { ok?: keyof RumTimeseriesPoint; err?: keyof RumTimeseriesPoint; errWithinOk?: boolean },
  labels: { ok: string; err: string },
  bounds?: RumCustomBounds,
): VolumeBucket[] {
  if (!timeseries || !timeseries.bucket_seconds || !timeseries.series.length) return []
  const bucketMs = timeseries.bucket_seconds * 1000
  const end = range === 'custom' && bounds ? new Date(bounds.to).getTime() : Date.now()
  const now = Number.isFinite(end) ? end : Date.now()
  const start = Math.floor((now - rangeMs(range, bounds)) / bucketMs) * bucketMs
  const slots = Math.min(400, Math.max(2, Math.ceil((now - start) / bucketMs)))
  const buckets: VolumeBucket[] = Array.from({ length: slots }, (_, index) => ({ ok: 0, err: 0, label: formatBucketLabel(start + index * bucketMs, bucketMs) }))
  timeseries.series.forEach((point) => {
    const ts = parseUtc(point.timestamp)
    if (!Number.isFinite(ts)) return
    const index = Math.floor((ts - start) / bucketMs)
    if (index < 0 || index >= slots) return
    const okValue = keys.ok ? Number(point[keys.ok] ?? 0) : 0
    const errValue = keys.err ? Number(point[keys.err] ?? 0) : 0
    buckets[index].ok += keys.errWithinOk ? Math.max(0, okValue - errValue) : okValue
    buckets[index].err += errValue
  })
  buckets.forEach((bucket) => {
    const parts = [`${bucket.ok.toLocaleString()} ${labels.ok.toLowerCase()}`]
    if (bucket.err > 0) parts.push(`${bucket.err.toLocaleString()} ${labels.err.toLowerCase()}`)
    bucket.label = `${bucket.label} · ${parts.join(' · ')}`
  })
  return buckets
}

function formatBucketLabel(startMs: number, bucketMs: number): string {
  const date = new Date(startMs)
  if (bucketMs >= 86_400_000) return date.toLocaleDateString([], { month: 'short', day: 'numeric' })
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
