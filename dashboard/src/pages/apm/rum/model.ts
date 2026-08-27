import type { RumFilters, RumRange, RumTab, RumVitalMetric } from '@/types/apm'

export type RumVitalName = 'lcp' | 'inp' | 'cls' | 'fcp' | 'ttfb' | 'load'
export type RumVitalBand = 'good' | 'needs-improvement' | 'poor' | 'no-data'

export const RUM_FILTER_KEYS = ['application_id', 'env', 'view_name', 'browser', 'browser_version', 'os', 'device_type', 'country', 'service_version'] as const satisfies readonly (keyof RumFilters)[]

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
): string {
  const query = new URLSearchParams({ range })
  RUM_FILTER_KEYS.forEach((key) => {
    if (filters[key]) query.set(key, filters[key])
  })
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  return query.toString()
}

export function buildRumHref(tab: RumTab, range: RumRange, filters: RumFilters): string {
  const query = new URLSearchParams(buildRumQuery(range, filters))
  if (tab === 'overview') query.delete('tab')
  else query.set('tab', tab)
  const encoded = query.toString()
  return encoded ? `/apm/rum?${encoded}` : '/apm/rum'
}
