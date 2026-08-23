// Shared APM UI primitives (health badge, formatters, query keys).

export type Health = 'healthy' | 'degraded' | 'critical' | 'no_data'

export const HEALTH_COLOR: Record<Health, string> = {
  healthy: '#22c55e',
  degraded: '#f59e0b',
  critical: '#ef4444',
  no_data: '#6b7280',
}

const HEALTH_LABEL: Record<Health, string> = {
  healthy: 'Healthy', degraded: 'Degraded', critical: 'Critical', no_data: 'No data',
}

export function HealthBadge({ health }: { health: string }) {
  const h = (health as Health) in HEALTH_COLOR ? (health as Health) : 'no_data'
  const c = HEALTH_COLOR[h]
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ backgroundColor: `${c}1a`, color: c, border: `1px solid ${c}40` }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: c }} />
      {HEALTH_LABEL[h]}
    </span>
  )
}

export function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`
  return `${v.toFixed(v < 10 ? 1 : 0)} ms`
}

export function fmtRps(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1) return `${v.toFixed(1)}/s`
  return `${(v * 60).toFixed(1)}/min`
}

export function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(v < 0.01 ? 2 : 1)}%`
}

export function KpiTile({ label, value, accent }: { label: string; value: React.ReactNode; accent?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className="text-xl font-semibold mt-0.5 text-text" style={accent ? { color: accent } : undefined}>{value}</div>
    </div>
  )
}
