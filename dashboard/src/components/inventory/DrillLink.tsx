import { useId } from 'react'
import { Link, type LinkProps } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { assetsLink, rejectedText, type AssetFilters, type ListState } from '@/pages/inventory/filters'

export interface DrillLinkProps extends Omit<LinkProps, 'to'> {
  /** The asset filter vocabulary and nothing else: a key outside it does not compile. */
  filters: AssetFilters
  /** Optional ordering for the list that opens. */
  list?: ListState
  /**
   * The number this link stands for. Written to `data-drill-count`, so the drill-down equality
   * check (T-UI2) can follow every link on a page and compare the drilled list's total to it.
   */
  count?: number
}

/**
 * A `<Link>` to /inventory/assets with `filters` serialised as query parameters. Every KPI, chart
 * segment, bar, table count and health item that names a number uses it (the drill-down contract).
 *
 * A filter value the API would refuse (longer than its limit, or with a control character) cannot be
 * written into an address, and a link without it would open a WIDER list than the number says. Such a
 * number is shown as plain text with the reason in its tooltip instead (`data-drill-unavailable`).
 */
export function DrillLink({ filters, list, count, className, children, title, ...rest }: DrillLinkProps) {
  const reasonId = useId()
  const { href, rejected } = assetsLink(filters, list)
  if (rejected.length > 0) {
    const why = `No list can be opened for this number: ${rejected.map(rejectedText).join('; ')}.`
    // What a link would have carried over keeps its meaning on the plain number: the descriptive name
    // (`aria-label`, data-* markers, id), and the reason in text a keyboard and a screen reader can reach, not
    // only in a hover tooltip (review finding NF-08).
    const kept = Object.fromEntries(Object.entries(rest).filter(([k]) => k.startsWith('data-') || k === 'id'))
    const label = (rest as { 'aria-label'?: string })['aria-label']
    const shared = {
      ...kept,
      title: title ? `${title} ${why}` : why,
      'data-drill-unavailable': '',
      'data-drill-count': count,
      className: cn('tabular-nums', className),
    }
    if (label) {
      return (
        <>
          <span {...shared} role="img" aria-label={label} aria-describedby={reasonId}>{children}</span>
          <span id={reasonId} hidden>{why}</span>
        </>
      )
    }
    return <span {...shared}>{children}<span className="sr-only"> ({why})</span></span>
  }
  return (
    <Link
      {...rest}
      to={href}
      title={title}
      data-drill-count={count}
      className={cn('rounded-sm tabular-nums outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary', className)}
    >
      {children}
    </Link>
  )
}
