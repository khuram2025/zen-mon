import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { DrillLink } from './DrillLink'
import type { Drill } from './overviewModel'

/**
 * One number's link, whatever it opens: a list of assets (DrillLink, which serialises the filter
 * vocabulary), the Changes page or another Inventory page. `data-drill-count` carries the number the
 * link stands for, so the drill-down equality check can follow every link and compare the drilled
 * list's total to it; `data-drill` says which kind of target it is.
 */
export function DrillTarget({
  drill, className, children, title, ariaLabel,
}: {
  drill: Drill
  className?: string
  children: ReactNode
  title?: string
  ariaLabel?: string
}) {
  if (drill.type === 'assets') {
    return (
      <DrillLink filters={drill.filters} count={drill.count ?? undefined} className={className} title={title} aria-label={ariaLabel} data-drill="assets">
        {children}
      </DrillLink>
    )
  }
  return (
    <Link
      to={drill.href}
      data-drill={drill.type}
      data-drill-count={drill.count ?? undefined}
      title={title}
      aria-label={ariaLabel}
      className={cn('rounded-sm tabular-nums outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary', className)}
    >
      {children}
    </Link>
  )
}
