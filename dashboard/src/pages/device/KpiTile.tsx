import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, Minus } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Trend } from './format'

/* A KPI tile: label, value (22 px tabular numerals), one line of context, an
 * optional trend as an absolute delta and an optional sparkline or bar. Values
 * stay neutral; only state gets colour (`tone`). */

export function KpiTile({
  label, value, sub, trend, tone = 'neutral', chart, onClick, loading,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  trend?: Trend | null
  /** Colour the value only for a state (breach, outage). */
  tone?: 'neutral' | 'warning' | 'critical'
  chart?: ReactNode
  onClick?: () => void
  loading?: boolean
}) {
  const body = (
    <>
      <div className="text-xs font-medium text-muted">{label}</div>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className={cn('text-2xl font-semibold tabular-nums leading-tight', tone === 'critical' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-text', loading && 'text-muted')}>{value}</span>
        {trend && <TrendBadge trend={trend} />}
      </div>
      {sub && <div className="text-xs text-muted">{sub}</div>}
      {chart && <div className="mt-auto pt-1">{chart}</div>}
    </>
  )
  const cls = 'flex h-full min-h-[118px] w-full flex-col gap-1 rounded-xl border border-border bg-surface p-3 text-left'
  if (!onClick) return <div className={cls}>{body}</div>
  return <button type="button" onClick={onClick} className={cn(cls, 'transition-colors hover:border-primary/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary')}>{body}</button>
}

export function TrendBadge({ trend }: { trend: Trend }) {
  const Icon = trend.direction === 'flat' ? Minus : trend.direction === 'up' ? ArrowUp : ArrowDown
  return (
    <span className={cn('inline-flex items-center gap-0.5 text-xs font-medium tabular-nums', trend.significant ? 'text-warning' : 'text-muted')}>
      <Icon className="h-3 w-3" aria-hidden />{trend.text}<span className="font-normal text-muted"> {trend.baseline}</span>
    </span>
  )
}

/** A used-of-total bar with the value's own tone (no colour below the warning threshold). */
export function UsageBar({ pct, warn, crit }: { pct: number | null; warn?: number | null; crit?: number | null }) {
  if (pct == null) return null
  const tone = crit != null && pct >= crit ? 'bg-danger' : warn != null && pct >= warn ? 'bg-warning' : 'bg-primary'
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface2" role="img" aria-label={`${Math.round(pct)}% used`}>
      <div className={cn('h-full rounded-full', tone)} style={{ width: `${Math.max(pct > 0 ? 2 : 0, Math.min(100, pct))}%` }} />
    </div>
  )
}
