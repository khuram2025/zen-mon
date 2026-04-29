import { TrendingUp, TrendingDown, Minus } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ReactNode } from 'react'

interface KpiTileProps {
  label: string
  value: string | number
  unit?: string
  delta?: number | null
  /** When true, a positive delta is bad (e.g. incident count). */
  invertDelta?: boolean
  accent?: 'primary' | 'success' | 'warning' | 'danger' | 'info'
  icon?: ReactNode
  subtitle?: string
}

const ACCENT_CLASSES: Record<NonNullable<KpiTileProps['accent']>, { bar: string; icon: string; value: string }> = {
  primary: { bar: 'bg-indigo-500', icon: 'text-indigo-400 bg-indigo-500/10', value: 'text-text' },
  success: { bar: 'bg-emerald-500', icon: 'text-emerald-400 bg-emerald-500/10', value: 'text-text' },
  warning: { bar: 'bg-amber-500', icon: 'text-amber-400 bg-amber-500/10', value: 'text-text' },
  danger: { bar: 'bg-rose-500', icon: 'text-rose-400 bg-rose-500/10', value: 'text-text' },
  info: { bar: 'bg-sky-500', icon: 'text-sky-400 bg-sky-500/10', value: 'text-text' },
}

export function KpiTile({
  label,
  value,
  unit,
  delta,
  invertDelta = false,
  accent = 'primary',
  icon,
  subtitle,
}: KpiTileProps) {
  const accentCls = ACCENT_CLASSES[accent]

  let deltaColor = 'text-muted'
  let DeltaIcon = Minus
  let deltaText = '—'
  if (delta !== null && delta !== undefined && !Number.isNaN(delta)) {
    if (delta === 0) {
      deltaText = 'No change'
    } else {
      const isUp = delta > 0
      const isPositive = invertDelta ? !isUp : isUp
      deltaColor = isPositive ? 'text-emerald-400' : 'text-rose-400'
      DeltaIcon = isUp ? TrendingUp : TrendingDown
      const abs = Math.abs(delta)
      deltaText = `${isUp ? '+' : '−'}${abs.toFixed(abs < 10 ? 2 : 1)}`
    }
  }

  return (
    <div className="relative overflow-hidden rounded-lg border border-border bg-surface p-4 shadow-card transition-all hover:border-primary/30 dark:shadow-card-dark">
      <div className={cn('absolute left-0 top-0 h-full w-[3px]', accentCls.bar)} />
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</p>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className={cn('text-2xl font-bold leading-tight tracking-tight', accentCls.value)}>
              {value}
            </span>
            {unit && <span className="text-xs font-medium text-muted">{unit}</span>}
          </div>
          {subtitle && <p className="mt-1 truncate text-[11px] text-muted">{subtitle}</p>}
        </div>
        {icon && (
          <div className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-md', accentCls.icon)}>
            {icon}
          </div>
        )}
      </div>
      {delta !== undefined && (
        <div className={cn('mt-3 flex items-center gap-1 text-[11px] font-medium', deltaColor)}>
          <DeltaIcon className="h-3 w-3" />
          <span>{deltaText}</span>
          <span className="text-muted">vs prior period</span>
        </div>
      )}
    </div>
  )
}
