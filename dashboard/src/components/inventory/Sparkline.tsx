import { cn } from '@/lib/utils'
import { trendSummary, type DayValue } from './overviewModel'
import { fmtInt } from './model'

/**
 * A tiny trend line under a KPI. It is only drawn for at least three days of snapshots (the page
 * decides); the first and last value are printed under it and in its accessible name, so nothing depends on seeing it.
 *
 * The scale never stretches a small change into a full-height swing (review finding UI-21): the vertical span is at
 * least 5 % of the mean, so 94.0 % to 94.4 % is a nearly flat line, and there is no area fill to suggest a zero baseline.
 */
export function Sparkline({
  points, label, format = (n) => fmtInt(n), className,
}: {
  points: readonly DayValue[]
  label: string
  format?: (n: number) => string
  className?: string
}) {
  const W = 120
  const H = 26
  const PAD = 2
  const values = points.map((p) => p.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const mean = values.reduce((a, b) => a + b, 0) / Math.max(1, values.length)
  const span = Math.max(max - min, Math.abs(mean) * 0.05, 1e-9)
  const mid = (max + min) / 2
  const floor = mid - span / 2
  const x = (i: number) => PAD + (i / Math.max(1, points.length - 1)) * (W - PAD * 2)
  const y = (v: number) => H - PAD - ((v - floor) / span) * (H - PAD * 2)
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(' ')
  const first = points[0]
  const last = points[points.length - 1]
  return (
    <div className={className}>
      <svg
        role="img"
        aria-label={trendSummary(label, points, format)}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="block h-[26px] w-full text-primary"
        data-trend-days={points.length}
      >
        <title>{trendSummary(label, points, format)}</title>
        <path d={line} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        <circle cx={x(points.length - 1)} cy={y(last.value)} r={2} fill="currentColor" />
      </svg>
      <span aria-hidden className="mt-0.5 block text-[10px] tabular-nums text-muted">{format(first.value)} to {format(last.value)}, {points.length} days</span>
    </div>
  )
}
