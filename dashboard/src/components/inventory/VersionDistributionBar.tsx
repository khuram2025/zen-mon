import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import {
  barSummary, collapseVersionBuckets, fmtInt, fmtPct, segmentText,
  type VersionEntry, type VersionSegment,
} from './model'

/** Newest version is the strongest tint; older versions fade. A ramp, not categories: the order carries the meaning. */
function segmentStyle(s: VersionSegment): React.CSSProperties {
  if (s.kind === 'unknown') {
    // Hatched, like every "unknown" in a chart: a pattern, not a colour.
    return { background: 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.6) 0 2px, rgb(var(--surface2)) 2px 5px)' }
  }
  if (s.kind === 'other') return { background: 'rgb(var(--muted) / 0.42)' }
  const alpha = Math.max(0.45, 0.95 - s.index * 0.12)
  return { background: `rgb(var(--primary) / ${alpha.toFixed(2)})` }
}

/** The "other" bucket stands for several versions, so no single list has its total: it carries no drill count. */
const drillCount = (s: VersionSegment) => (s.kind === 'other' ? undefined : s.count)

function Segment({ s, noun, interactive, subject }: { s: VersionSegment; noun: string; interactive: boolean; subject?: string }) {
  const text = segmentText(s, noun)
  const name = subject ? `${subject} ${text}` : text
  const cls = 'block h-full w-full rounded-sm outline-none transition-opacity focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-surface hover:opacity-80'
  const body = s.href && interactive ? (
    <Link to={s.href} className={cls} style={segmentStyle(s)} title={text} aria-label={name} data-drill-count={drillCount(s)} />
  ) : (
    <span className="block h-full w-full rounded-sm" style={segmentStyle(s)} title={text} />
  )
  return (
    <li
      // Width follows the share; a floor keeps a 0.1 % version visible and clickable.
      style={{ flex: `${Math.max(s.pct, 0.0001)} 1 0`, minWidth: 4 }}
      data-kind={s.kind}
    >
      {body}
    </li>
  )
}

/**
 * Mini stacked bar of hosts per version, newest on the left, with one trailing "other" bucket and a
 * separate unknown segment (unknown is a value). Every segment with an `href` is a link to the list
 * behind its number. It is a real list, so a screen reader hears each version and its count; with
 * `showLegend` the legend carries the links and the bar becomes a visual aid.
 */
export function VersionDistributionBar({
  entries,
  maxSegments,
  otherHref,
  noun = 'host',
  height = 10,
  showLegend = false,
  className,
  label,
  total,
  subject,
}: {
  entries: readonly VersionEntry[]
  maxSegments?: number
  /** The "other" bucket has no single-version filter, so it links wherever lists the versions (the product page). */
  otherHref?: string
  noun?: string
  height?: number
  showLegend?: boolean
  className?: string
  /** Accessible name; defaults to a summary of the bar. */
  label?: string
  /**
   * The number the shares are of. Leave it out when every host is counted once (firmware: a device runs one
   * version). Pass the distinct hosts when one host can sit in several entries, or the shares are of the SUM.
   */
  total?: number
  /**
   * What the versions are OF ("FortiOS"). A legend link read on its own says "7.4.4: 5 devices (15.2%)", and the
   * heading that names the product is not part of its name; with a subject it reads "FortiOS 7.4.4: ..." (UI-15).
   */
  subject?: string
}) {
  const buckets = collapseVersionBuckets(entries, { maxSegments, otherHref, total })
  if (buckets.total === 0) {
    return <span className={cn('text-xs text-muted', className)}>No installations</span>
  }
  const name = label ?? barSummary(buckets, noun)
  return (
    <div className={cn('min-w-0', className)}>
      <ul
        role="list"
        aria-label={name}
        className="flex w-full gap-0.5 overflow-hidden rounded-full bg-surface2"
        style={{ height }}
      >
        {buckets.segments.map((s) => (
          <Segment key={s.key} s={s} noun={noun} interactive={!showLegend} subject={subject} />
        ))}
      </ul>
      {showLegend && (
        <ul role="list" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
          {buckets.segments.map((s) => {
            const swatch = <span aria-hidden className="h-2 w-2 shrink-0 rounded-sm" style={segmentStyle(s)} />
            const inner = (
              <>
                {swatch}
                <span className={cn('font-mono', s.kind === 'unknown' && 'italic text-muted')}>{s.label}</span>
                <span className="tabular-nums text-muted">{fmtInt(s.count)} · {fmtPct(s.pct)}</span>
              </>
            )
            return (
              <li key={s.key} data-kind={s.kind}>
                {s.href ? (
                  <Link to={s.href} data-drill-count={drillCount(s)} aria-label={subject ? `${subject} ${segmentText(s, noun)}` : segmentText(s, noun)} className="inline-flex items-center gap-1.5 rounded text-text2 outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary">
                    {inner}
                  </Link>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-text2">{inner}</span>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
