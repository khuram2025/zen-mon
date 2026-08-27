import type { RumFilters, RumRange } from '@/types/apm'

export type RumVitalName = 'lcp' | 'inp' | 'cls' | 'fcp' | 'ttfb' | 'load'

export const RUM_FILTER_KEYS = ['application_id', 'env', 'view_name', 'browser', 'browser_version', 'os', 'device_type', 'country', 'service_version'] as const satisfies readonly (keyof RumFilters)[]

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

export function vitalBand(name: RumVitalName, value: number | null | undefined): 'good' | 'needs-improvement' | 'poor' | 'no-data' {
  if (value == null || !Number.isFinite(value)) return 'no-data'
  const limits = VITAL_LIMITS[name]
  if (value <= limits.good) return 'good'
  if (value <= limits.poor) return 'needs-improvement'
  return 'poor'
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
