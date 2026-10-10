import { useId, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { CATEGORICAL } from '@/pages/dashboards/shared'
import { fmtInt, fmtPct } from './model'
import type { Drill } from './overviewModel'

export interface DonutSlice {
  key: string
  label: string
  count: number
  pct: number
  /** Index into CATEGORICAL; absent for the unknown and folded slices. */
  color?: number
  /** Unknown is a value: hatched, never a colour of its own. */
  unknown?: boolean
  drill: Drill
}

/** The fill of a slice or a legend swatch: a categorical colour, the hatch for unknown, a neutral for the rest. */
export function sliceFill(color: number | undefined, unknown: boolean | undefined, hatchId: string): string {
  if (unknown) return `url(#${hatchId})`
  // Full strength: the folded slice must stand out from the track (3:1) and the legend names it; a faded grey measured 1.9:1.
  if (color === undefined) return 'rgb(var(--muted))'
  return CATEGORICAL[color % CATEGORICAL.length]
}

const SIZE = 132
const C = SIZE / 2
const R = 50
const STROKE = 20
const GAP_DEG = 1.6

const polar = (deg: number) => {
  const a = ((deg - 90) * Math.PI) / 180
  return [C + R * Math.cos(a), C + R * Math.sin(a)] as const
}

/** The arc of one slice as an open path, so it can be stroked (and the stroke can be a pattern). */
export function arcPath(startDeg: number, endDeg: number): string {
  const [x1, y1] = polar(startDeg)
  const [x2, y2] = polar(endDeg)
  const large = endDeg - startDeg > 180 ? 1 : 0
  return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${R} ${R} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`
}

/** Start and end angle of each slice, with a small gap between neighbours (none for a single slice). */
export function sliceAngles(counts: readonly number[]): { start: number; end: number }[] {
  const total = counts.reduce((a, b) => a + b, 0)
  if (total <= 0) return counts.map(() => ({ start: 0, end: 0 }))
  const live = counts.filter((c) => c > 0).length
  const gap = live > 1 ? GAP_DEG : 0
  let at = 0
  return counts.map((c) => {
    const sweep = (c / total) * 360
    const out = { start: at + gap / 2, end: Math.max(at + gap / 2, at + sweep - gap / 2) }
    at += sweep
    return out
  })
}

/**
 * Donut chart whose slices open the list behind them (the drill-down contract). It is a visual aid: the legend next
 * to it carries real links and is the way in for the keyboard and for screen readers. A slice is therefore a
 * clickable shape, not an anchor: an anchor inside `role="img"` is a nested interactive control that assistive
 * technology cannot reach anyway (review finding UI-22). Hatched = unknown.
 */
export function Donut({
  slices, centre, label,
}: {
  slices: readonly DonutSlice[]
  /** The big number and caption in the hole. */
  centre?: ReactNode
  /** Accessible name of the whole chart, with the numbers ("52 servers: ..."). */
  label: string
}) {
  const navigate = useNavigate()
  const hatch = useId().replace(/:/g, '')
  const angles = sliceAngles(slices.map((s) => s.count))
  const live = slices.filter((s) => s.count > 0)
  return (
    <div className="relative mx-auto h-[132px] w-[132px] shrink-0">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label={label} className="h-full w-full">
        <defs>
          <pattern id={hatch} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="6" height="6" style={{ fill: 'rgb(var(--surface2))' }} />
            <line x1="0" y1="0" x2="0" y2="6" strokeWidth="2.5" style={{ stroke: 'rgb(var(--muted) / 0.7)' }} />
          </pattern>
        </defs>
        <circle cx={C} cy={C} r={R} fill="none" strokeWidth={STROKE} style={{ stroke: 'rgb(var(--surface2))' }} />
        {slices.map((s, i) => {
          if (s.count <= 0) return null
          const fill = sliceFill(s.color, s.unknown, hatch)
          const common = {
            fill: 'none', stroke: fill, strokeWidth: STROKE,
            className: 'transition-opacity hover:opacity-75',
          }
          // A slice that is the whole ring has no end point to draw an arc to.
          const shape = live.length === 1
            ? <circle cx={C} cy={C} r={R} {...common} />
            : <path d={arcPath(angles[i].start, angles[i].end)} {...common} />
          return (
            <g
              key={s.key}
              role="presentation"
              style={{ cursor: 'pointer' }}
              onClick={() => navigate(s.drill.href)}
              data-href={s.drill.href}
              data-drill={s.drill.type}
              data-drill-count={s.drill.count ?? undefined}
              data-slice={s.key}
            >
              <title>{`${s.label}: ${fmtInt(s.count)} (${fmtPct(s.pct)})`}</title>
              {shape}
            </g>
          )
        })}
      </svg>
      {centre && <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">{centre}</div>}
    </div>
  )
}
