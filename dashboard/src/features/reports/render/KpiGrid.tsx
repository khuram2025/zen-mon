import { AlertTriangle, CheckCircle2, OctagonAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { KpiAccent, RenderKpi } from '../types'

const STRIPE: Record<KpiAccent, string> = {
  primary: 'bg-primary',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-info',
  neutral: 'bg-border-strong',
}

const VALUE: Record<KpiAccent, string> = {
  primary: 'text-text',
  success: 'text-success-fg',
  warning: 'text-warning-fg',
  danger: 'text-danger-fg',
  info: 'text-text',
  neutral: 'text-text',
}

/** Accent meaning for screen readers (colour is never the only cue). */
const MEANING: Partial<Record<KpiAccent, string>> = {
  success: 'healthy',
  warning: 'needs attention',
  danger: 'critical',
}

const ICON: Partial<Record<KpiAccent, typeof AlertTriangle>> = {
  success: CheckCircle2,
  warning: AlertTriangle,
  danger: OctagonAlert,
}

/** KPI tiles: label, value (tabular), subtitle, and an accent stripe drawn from theme tokens. */
export function KpiGrid({ kpis, compact }: { kpis: RenderKpi[]; compact?: boolean }) {
  if (!kpis.length) return null
  return (
    <dl
      className={cn(
        'zp-avoid-break grid gap-2.5',
        // Fill the row (no empty trailing columns), but keep one or two tiles from stretching across a wide section.
        compact ? 'grid-cols-[repeat(auto-fit,minmax(8.5rem,1fr))]' : 'grid-cols-[repeat(auto-fit,minmax(10rem,1fr))]',
        kpis.length === 1 && 'max-w-[16rem]',
        kpis.length === 2 && 'max-w-xl',
        kpis.length === 3 && 'max-w-3xl',
      )}
    >
      {kpis.map((kpi, i) => {
        const accent: KpiAccent = kpi.accent ?? 'primary'
        const Icon = ICON[accent]
        const meaning = MEANING[accent]
        const value = kpi.value === '' || kpi.value == null ? '—' : String(kpi.value)
        return (
          <div
            key={i}
            className="relative flex min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-surface px-3.5 pb-3 pt-3.5"
          >
            <span aria-hidden className={cn('absolute inset-x-0 top-0 h-[3px]', STRIPE[accent])} />
            <dt className="truncate text-[11px] font-medium uppercase tracking-wide text-muted" title={kpi.label}>
              {kpi.label}
            </dt>
            <dd className="mt-1 flex min-w-0 items-center gap-1.5">
              <span
                className={cn(
                  'truncate font-semibold leading-tight tabular-nums',
                  compact ? 'text-lg' : 'text-xl',
                  VALUE[accent],
                )}
                title={value}
              >
                {value}
              </span>
              {Icon && accent !== 'success' && <Icon className={cn('h-3.5 w-3.5 shrink-0', VALUE[accent])} aria-hidden />}
              {meaning && <span className="sr-only">({meaning})</span>}
            </dd>
            {kpi.subtitle && (
              <dd className="mt-0.5 line-clamp-2 text-xs text-muted" title={kpi.subtitle}>
                {kpi.subtitle}
              </dd>
            )}
          </div>
        )
      })}
    </dl>
  )
}
