import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

// ─────────────────────────────────────────────────────────────────
// KPI card (NF-30): the deciding value is never truncated. Number and
// unit are split so the unit stays visible, the value wraps instead of
// ending in an ellipsis, and the exact figure is one hover (or one
// screen-reader label) away. Neutral ink: KPIs carry no status colour.
// ─────────────────────────────────────────────────────────────────

export interface KpiCardProps {
  icon: LucideIcon
  label: string
  /** Formatted value, e.g. "113.5 GB"; "—" (or null) means unknown. */
  value: string | null | undefined
  /** Exact value or explanation for hover and assistive technology. */
  title?: string
  sub?: ReactNode
  /** A small chip beside the value (e.g. change vs the previous period). */
  badge?: ReactNode
  loading?: boolean
}

/** "113.5 GB" → ["113.5", "GB"]; values without a unit stay whole. */
export function splitUnit(value: string): [string, string] {
  const m = /^([−+-]?[<>]?[\d.,]+(?:[kMBT])?)\s+(\S.*)$/.exec(value)
  return m ? [m[1], m[2]] : [value, '']
}

export function KpiCard({ icon: Icon, label, value, title, sub, badge, loading }: KpiCardProps) {
  const shown = value == null || value === '' ? '—' : value
  const [num, unit] = splitUnit(shown)
  return (
    <div
      role="group"
      aria-label={`${label}: ${title ?? shown}`}
      className="flex min-w-0 flex-col gap-1 rounded-lg border border-border bg-surface p-3 shadow-card dark:shadow-card-dark"
    >
      <div className="flex items-center gap-1.5 text-xs font-medium text-text2">
        <Icon className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
        <span className="min-w-0 break-words leading-snug">{label}</span>
      </div>
      {loading ? (
        <Skeleton className="h-7 w-24" />
      ) : (
        <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-1" title={title}>
          <span className="break-all text-xl font-semibold leading-tight tabular-nums text-text sm:text-2xl">{num}</span>
          {unit && <span className="text-sm font-medium text-text2">{unit}</span>}
          {badge}
        </div>
      )}
      {sub && <div className={cn('text-xs leading-snug text-text2', loading && 'invisible')}>{sub}</div>}
    </div>
  )
}
