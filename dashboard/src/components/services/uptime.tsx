import { cn } from '@/lib/utils'

/* Shared availability visuals for the services list and detail pages:
   the banded color scale, the pulsing status dot, and the per-day uptime
   bar strip that both pages render. */

export type UptimeBand = 'none' | 'perfect' | 'good' | 'fair' | 'poor'

export function uptimeBand(p: number | null | undefined): UptimeBand {
  if (p == null || !Number.isFinite(p)) return 'none'
  if (p >= 99.95) return 'perfect'
  if (p >= 99) return 'good'
  if (p >= 95) return 'fair'
  return 'poor'
}

export const BAND_FILL: Record<UptimeBand, string> = {
  perfect: 'rgb(var(--success))',
  good: 'rgb(var(--success) / 0.55)',
  fair: 'rgb(var(--warning))',
  poor: 'rgb(var(--danger))',
  none: 'rgb(var(--surface3) / 0.7)',
}

export const BAND_TEXT: Record<UptimeBand, string> = {
  perfect: 'text-success',
  good: 'text-success',
  fair: 'text-warning',
  poor: 'text-danger',
  none: 'text-muted',
}

export type PulseStatus = 'up' | 'down' | 'warn' | 'unknown' | 'paused'

export function pulseStatusOf(status: string | undefined, enabled = true): PulseStatus {
  if (!enabled) return 'paused'
  if (status === 'up') return 'up'
  if (status === 'down') return 'down'
  if (status === 'warning' || status === 'degraded') return 'warn'
  return 'unknown'
}

const PULSE_STYLE: Record<PulseStatus, { dot: string; ping: string | null }> = {
  up: { dot: 'bg-success', ping: 'bg-success' },
  down: { dot: 'bg-danger', ping: 'bg-danger' },
  warn: { dot: 'bg-warning', ping: 'bg-warning' },
  unknown: { dot: 'bg-muted/60', ping: null },
  paused: { dot: 'bg-muted/40', ping: null },
}

export function PulseDot({ status, size = 'md', className }: {
  status: PulseStatus
  size?: 'sm' | 'md' | 'lg'
  className?: string
}) {
  const px = size === 'lg' ? 'h-3.5 w-3.5' : size === 'md' ? 'h-2.5 w-2.5' : 'h-2 w-2'
  const s = PULSE_STYLE[status]
  return (
    <span className={cn('relative inline-flex shrink-0', px, className)} aria-hidden>
      {s.ping && <span className={cn('svc-ping absolute inline-flex h-full w-full rounded-full', s.ping)} />}
      <span className={cn('relative inline-flex rounded-full', px, s.dot)} />
    </span>
  )
}

export type DayCell = {
  key: string
  date: Date
  pct: number | null
  samples: number
}

/** Normalize daily rows onto the trailing `dayCount` calendar days ending today (local). */
export function buildDaySeries(
  rows: Array<{ date: string; uptime_pct: number | null; sample_count: number }>,
  dayCount: number,
): DayCell[] {
  const byKey = new Map<string, { pct: number | null; samples: number }>()
  for (const r of rows) {
    // Backend dates are UTC calendar days; render them as-is rather than reshifting.
    byKey.set(r.date, { pct: r.uptime_pct, samples: r.sample_count })
  }
  const out: DayCell[] = []
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  for (let i = dayCount - 1; i >= 0; i--) {
    const date = new Date(today)
    date.setDate(today.getDate() - i)
    const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
    const v = byKey.get(key)
    out.push({ key, date, pct: v?.pct ?? null, samples: v?.samples ?? 0 })
  }
  return out
}

/** The signature uptime-monitor bar strip: one full-height rounded bar per day. */
export function UptimeBars({ cells, className, barClassName, onSelect }: {
  cells: Array<{ key: string; pct: number | null; title: string }>
  className?: string
  barClassName?: string
  onSelect?: (key: string) => void
}) {
  return (
    <div className={cn('flex items-stretch gap-[3px]', className)} role="img" aria-label="Daily uptime history">
      {cells.map((c) => {
        const band = uptimeBand(c.pct)
        const Comp: any = onSelect ? 'button' : 'div'
        return (
          <Comp
            key={c.key}
            type={onSelect ? 'button' : undefined}
            onClick={onSelect ? () => onSelect(c.key) : undefined}
            title={c.title}
            className={cn(
              'min-w-[3px] flex-1 rounded-[3px] transition-transform',
              onSelect && 'hover:scale-y-110 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
              barClassName,
            )}
            style={{ backgroundColor: BAND_FILL[band] }}
          />
        )
      })}
    </div>
  )
}

export function dayTitle(d: { date: Date; pct: number | null; samples: number }): string {
  const when = d.date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
  if (d.pct == null) return `${when} — not monitored`
  return `${when} — ${d.pct.toFixed(d.pct >= 99 ? 2 : 1)}% up · ${d.samples} checks`
}
