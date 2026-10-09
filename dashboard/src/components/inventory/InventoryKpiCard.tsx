import type { ReactNode } from 'react'
import { Card } from '@/components/ui/Card'
import { KPI_ACCENT } from '@/pages/dashboards/shared'
import { cn } from '@/lib/utils'
import { DrillTarget } from './DrillTarget'
import { fmtInt } from './model'
import type { KpiModel } from './overviewModel'

/**
 * A KPI card whose every figure is a link (the drill-down contract). The whole card opens the list
 * behind its big number through a stretched link (the number's own `<a>` covers the card), and the
 * secondary figures are separate links above it, so no link is nested inside another. The big number
 * is a derived figure for the freshness card ("94.4%"): it opens the list of fresh assets, whose
 * total is the count in the line below.
 */
export function InventoryKpiCard({
  model, icon, accent, trend, foot,
}: {
  model: KpiModel
  icon: ReactNode
  accent: keyof typeof KPI_ACCENT
  /** A sparkline, only when there is one to draw. */
  trend?: ReactNode
  /** Extra line under the figures (the endpoint-protection coverage under the software card). */
  foot?: ReactNode
}) {
  const bigName = model.valueCount !== null ? `${model.label}: ${fmtInt(model.valueCount)}` : `${model.label}: ${model.value}`
  return (
    <Card data-kpi={model.id} className="group relative flex h-full flex-col overflow-hidden transition hover:border-primary/40 focus-within:border-primary/60">
      <span aria-hidden className={cn('absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r', KPI_ACCENT[accent])} />
      <div className="flex items-start justify-between gap-2 px-4 pt-3">
        <div className="min-w-0">
          <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted">{model.label}</div>
          <div className="mt-1 text-2xl font-bold leading-none tabular-nums text-text">
            <DrillTarget
              drill={model.drill}
              ariaLabel={bigName}
              className="after:absolute after:inset-0 after:content-[''] hover:no-underline group-hover:underline"
            >
              {model.valueCount !== null ? fmtInt(model.valueCount) : model.value}
            </DrillTarget>
          </div>
        </div>
        <span aria-hidden className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-white shadow', KPI_ACCENT[accent])}>
          {icon}
        </span>
      </div>
      <div className="flex flex-1 flex-col gap-1.5 px-4 pb-3 pt-1.5">
        {model.parts.length > 0 && (
          <ul role="list" className="relative z-10 flex flex-wrap gap-x-2.5 gap-y-0.5 text-[11px] leading-4 text-muted">
            {model.parts.map((p) => (
              <li key={p.text} className="whitespace-nowrap">
                {p.drill && p.value !== undefined ? (
                  <DrillTarget drill={p.drill} className="text-text2 hover:text-primary">
                    <strong className="font-semibold text-text">{fmtInt(p.value)}</strong> {p.text}
                  </DrillTarget>
                ) : (
                  <span>{p.value !== undefined ? <><strong className="font-semibold text-text2">{fmtInt(p.value)}</strong> </> : null}{p.text}</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {model.note && <p className="relative z-10 text-[11px] leading-4 text-muted" data-kpi-note="">{model.note}</p>}
        {foot && <div className="relative z-10 text-[11px] leading-4 text-muted">{foot}</div>}
        {trend && <div className="mt-auto pt-1">{trend}</div>}
      </div>
    </Card>
  )
}
