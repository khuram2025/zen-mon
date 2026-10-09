import type { CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import type { VersionBar } from '@/pages/inventory/softwareSettings'
import { versionBarModel, type BarSegment } from './softwareModel'

/** The newest version is the strongest tint and older ones fade: a ramp, not categories, so the order carries the meaning. */
function segmentStyle(s: BarSegment): CSSProperties {
  if (s.kind === 'other') return { background: 'rgb(var(--muted) / 0.42)' }
  const alpha = Math.max(0.45, 0.95 - s.index * 0.12)
  return { background: `rgb(var(--primary) / ${alpha.toFixed(2)})` }
}

const focusRing = 'outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-surface'

/**
 * The mini bar of a catalog row: hosts per version, the server's order (newest first within each scheme group),
 * and one trailing bucket for the versions beyond the first eight. Every version is a link to the assets that run
 * it, and the host count in its tooltip is the total of that list. The bucket of further versions has no list of
 * its own, so it is text. It is a real list, so a screen reader hears each version and its count.
 *
 * This is not the shared VersionDistributionBar: that one prints each segment's share of the SUM of the
 * per-version host counts, which is wrong for a product whose hosts run several versions (14 of 46 hosts is
 * 30 %, but 14 of the 72 installations is 19.4 %), and it cannot take a bucket of versions it was never given.
 */
export function SoftwareVersionBar({
  productKey, bar, label, height = 10, className, focusable = true,
}: {
  productKey: string
  bar: VersionBar
  /** Accessible name of the list. */
  label: string
  height?: number
  className?: string
  /**
   * `false` takes the segments out of the Tab order (a table of many rows would otherwise cost the keyboard a
   * hundred stops); the same links are on the product page. They stay pointer targets with an accessible name.
   */
  focusable?: boolean
}) {
  const model = versionBarModel(productKey, bar)
  if (model.segments.length === 0) {
    return <span className={cn('text-xs text-muted', className)}>No version reported</span>
  }
  return (
    <ul role="list" aria-label={label} className={cn('flex w-full gap-0.5 overflow-hidden rounded-full bg-surface2', className)} style={{ height }}>
      {model.segments.map((s) => (
        <li
          key={s.key}
          // Width follows the hosts; a floor keeps a 1-host version visible and clickable.
          style={{ flex: `${Math.max(s.width, 0.0001)} 1 0`, minWidth: 4 }}
          data-kind={s.kind}
        >
          {s.href ? (
            <Link
              to={s.href}
              title={s.text}
              aria-label={s.text}
              data-drill="assets"
              data-drill-count={s.hosts}
              tabIndex={focusable ? undefined : -1}
              className={cn('block h-full w-full rounded-sm transition-opacity hover:opacity-80', focusRing)}
              style={segmentStyle(s)}
            />
          ) : (
            <span title={s.text} role="img" aria-label={s.text} className="block h-full w-full rounded-sm" style={segmentStyle(s)} />
          )}
        </li>
      ))}
    </ul>
  )
}
