/**
 * Visual kit for the Availability dashboard: ring gauge, donuts, sparklines,
 * bullet bars, health tiles, heatmap and the availability + outage-activity
 * chart.
 *
 * Evidence rules carry over from charts.tsx: a bucket without samples is drawn
 * as "no evidence" (never 0% and never healthy), every colour has a text
 * label beside it, and availability colour bands are always stated relative
 * to the configured target.
 */
import { useId, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import ReactECharts from 'echarts-for-react'
import { cn } from '@/lib/utils'
import { useTheme } from '@/stores/theme'
import { availNumber, fmtAvail, fmtDateTime, fmtDuration, fmtTarget } from './format'
import { EvidenceEmpty } from './ui'

/* ── Theme ─────────────────────────────────────────────────────────────── */

type Token = 'text' | 'text2' | 'muted' | 'muted2' | 'border' | 'surface' | 'surface2' | 'surface3'
  | 'primary' | 'success' | 'warning' | 'danger' | 'accent' | 'info'

export interface VizColors {
  dark: boolean
  /** `c('danger')` → rgb(), `c('danger', 0.2)` → rgba(). */
  c: (token: Token | 'amber', alpha?: number) => string
}

const FALLBACK: Record<Token, string> = {
  text: '241 245 249', text2: '203 213 225', muted: '148 163 184', muted2: '100 116 139',
  border: '30 41 59', surface: '13 18 27', surface2: '20 27 40', surface3: '31 41 59',
  primary: '99 179 255', success: '74 222 128', warning: '251 146 60', danger: '248 113 113',
  accent: '192 132 252', info: '56 189 248',
}

export function useVizColors(): VizColors {
  const theme = useTheme((s) => s.theme)
  return useMemo(() => {
    const css = typeof document !== 'undefined' ? getComputedStyle(document.documentElement) : null
    const dark = theme === 'dark'
    const triplet = (t: Token | 'amber') => {
      // Amber sits between success and the (orange) warning token so the
      // "just below target" band reads as distinct from "well below".
      if (t === 'amber') return dark ? '250,204,21' : '202,138,4'
      const v = css?.getPropertyValue(`--${t}`).trim()
      return (v || FALLBACK[t]).split(/\s+/).join(',')
    }
    const cache = new Map<string, string>()
    return {
      dark,
      c: (token, alpha) => {
        const key = `${token}:${alpha ?? ''}`
        let v = cache.get(key)
        if (!v) {
          const rgb = triplet(token)
          v = alpha == null ? `rgb(${rgb})` : `rgba(${rgb},${alpha})`
          cache.set(key, v)
        }
        return v
      },
    }
  }, [theme])
}

/** `rgb(r,g,b)` / `rgba(r,g,b,a)` / `#rrggbb` → the same colour at `alpha`. */
export function withAlpha(color: string, alpha: number): string {
  const m = color.match(/rgba?\(([^)]+)\)/)
  if (m) {
    const [r, g, b] = m[1].split(',').map((x) => x.trim())
    return `rgba(${r},${g},${b},${alpha})`
  }
  if (/^#[0-9a-f]{6}$/i.test(color)) {
    const n = parseInt(color.slice(1), 16)
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`
  }
  return color
}

/* ── Availability bands (relative to target) ───────────────────────────── */

export type Band = 'met' | 'near' | 'below' | 'far' | 'none'

export function availBand(v: number | null | undefined, target: number | null): Band {
  if (v == null || !Number.isFinite(v)) return 'none'
  const t = target ?? 99.9
  const a = availNumber(v)
  if (a >= t) return 'met'
  if (a >= t - 1) return 'near'
  if (a >= t - 5) return 'below'
  return 'far'
}

export function bandLabel(band: Band, target: number | null): string {
  const t = fmtTarget(target)
  switch (band) {
    case 'met': return `At or above target (${t})`
    case 'near': return 'Up to 1 pp below target'
    case 'below': return '1–5 pp below target'
    case 'far': return 'More than 5 pp below target'
    default: return 'No evidence'
  }
}

export function bandColor(band: Band, colors: VizColors, alpha?: number): string {
  switch (band) {
    case 'met': return colors.c('success', alpha)
    case 'near': return colors.c('amber', alpha)
    case 'below': return colors.c('warning', alpha)
    case 'far': return colors.c('danger', alpha)
    default: return colors.c('muted', alpha == null ? 0.35 : alpha * 0.5)
  }
}

export function BandLegend({ target, className }: { target: number | null; className?: string }) {
  const colors = useVizColors()
  return (
    <div className={cn('flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted', className)}>
      {(['met', 'near', 'below', 'far', 'none'] as Band[]).map((b) => (
        <span key={b} className="inline-flex items-center gap-1.5">
          <span
            className="h-3 w-3 rounded-[3px]"
            style={b === 'none'
              ? { background: `repeating-linear-gradient(135deg, ${colors.c('muted', 0.45)} 0 2px, transparent 2px 5px)`, border: `1px solid ${colors.c('muted', 0.4)}` }
              : { background: bandColor(b, colors) }}
          />
          {bandLabel(b, target)}
        </span>
      ))}
    </div>
  )
}

/** A readable availability scale: near-100 values need a zoomed axis. */
export function availDomain(values: Array<number | null | undefined>, target: number | null): [number, number] {
  const vals = values.filter((v): v is number => v != null && Number.isFinite(v))
  const floor = Math.min(...vals, target ?? 100)
  if (floor >= 99.5) return [99, 100]
  if (floor >= 99) return [98, 100]
  if (floor >= 95) return [90, 100]
  if (floor >= 80) return [75, 100]
  return [0, 100]
}

/* ── Ring gauge ────────────────────────────────────────────────────────── */

const SWEEP = 270
const START_DEG = 135 // bottom-left, clockwise to bottom-right

function polar(cx: number, cy: number, r: number, deg: number) {
  const rad = (deg * Math.PI) / 180
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
}

function arcPath(cx: number, cy: number, r: number, fromDeg: number, toDeg: number) {
  const a = polar(cx, cy, r, fromDeg)
  const b = polar(cx, cy, r, toDeg)
  const large = toDeg - fromDeg > 180 ? 1 : 0
  return `M ${a.x} ${a.y} A ${r} ${r} 0 ${large} 1 ${b.x} ${b.y}`
}

export function RingGauge({
  value, target, size = 208, stroke = 16, caption, valueText,
}: {
  value: number | null
  target: number | null
  size?: number
  stroke?: number
  caption: string
  valueText?: string
}) {
  const colors = useVizColors()
  const band = availBand(value, target)
  const color = bandColor(band, colors)
  const cx = size / 2
  const cy = size / 2
  const r = size / 2 - stroke / 2 - 6
  const frac = value == null ? 0 : Math.max(0, Math.min(1, availNumber(value) / 100))
  const endDeg = START_DEG + SWEEP * frac
  const tDeg = target != null ? START_DEG + SWEEP * Math.min(1, target / 100) : null
  const gid = `ring-${band}-${useId().replace(/:/g, '')}`
  const inner = tDeg != null ? polar(cx, cy, r - stroke / 2 - 5, tDeg) : null
  const outer = tDeg != null ? polar(cx, cy, r + stroke / 2 + 5, tDeg) : null
  return (
    <div className="relative mx-auto" style={{ width: size, height: size * 0.86 }}>
      <svg
        viewBox={`0 0 ${size} ${size}`}
        width={size}
        height={size}
        role="img"
        aria-label={`${caption}: ${valueText ?? fmtAvail(value)}${target != null ? `, target ${fmtTarget(target)}` : ''}`}
      >
        <defs>
          <linearGradient id={gid} x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" stopColor={bandColor(band, colors, 0.55)} />
            <stop offset="100%" stopColor={color} />
          </linearGradient>
        </defs>
        <path d={arcPath(cx, cy, r, START_DEG, START_DEG + SWEEP)} fill="none" stroke={colors.c('surface3')} strokeWidth={stroke} strokeLinecap="round" />
        {value == null ? (
          <path
            d={arcPath(cx, cy, r, START_DEG, START_DEG + SWEEP)}
            fill="none"
            stroke={colors.c('muted', 0.45)}
            strokeWidth={stroke}
            strokeDasharray="3 6"
          />
        ) : frac > 0.002 && (
          <path
            d={arcPath(cx, cy, r, START_DEG, Math.max(START_DEG + 0.5, endDeg))}
            fill="none"
            stroke={`url(#${gid})`}
            strokeWidth={stroke}
            strokeLinecap="round"
            style={{ filter: `drop-shadow(0 0 6px ${bandColor(band, colors, 0.35)})` }}
          />
        )}
        {inner && outer && (
          <line x1={inner.x} y1={inner.y} x2={outer.x} y2={outer.y} stroke={colors.c('text')} strokeWidth={2.5} strokeLinecap="round" />
        )}
      </svg>
      <div className="pointer-events-none absolute inset-x-0 top-[30%] flex flex-col items-center text-center">
        <span className={cn('font-semibold tabular-nums tracking-tight', value == null ? 'text-2xl text-muted' : 'text-[2.35rem] leading-none text-text')}>
          {valueText ?? fmtAvail(value)}
        </span>
        <span className="mt-1.5 text-sm text-muted">{caption}</span>
        {target != null && <span className="mt-0.5 text-xs text-muted">▮ Target {fmtTarget(target)}</span>}
      </div>
    </div>
  )
}

/* ── Error-budget meter ────────────────────────────────────────────────── */

/** Unavailability actually incurred ÷ unavailability the target allows. */
export function budgetUsed(avail: number | null, target: number | null): number | null {
  if (avail == null || target == null || target >= 100) return null
  return Math.max(0, (100 - availNumber(avail)) / (100 - target))
}

export function BudgetMeter({ avail, target, label = 'Downtime budget used' }: { avail: number | null; target: number | null; label?: string }) {
  const colors = useVizColors()
  const used = budgetUsed(avail, target)
  const tone = used == null ? 'none' : used <= 0.75 ? 'met' : used <= 1 ? 'near' : 'far'
  const color = tone === 'none' ? colors.c('muted') : bandColor(tone as Band, colors)
  const width = used == null ? 0 : Math.min(100, used * 100)
  const text = used == null
    ? 'Not measured'
    : used <= 1 ? `${(used * 100).toFixed(used < 0.1 ? 1 : 0)}%`
      : `${used >= 10 ? Math.round(used).toLocaleString() : used.toFixed(1)}× allowed`
  return (
    <div title={`Unavailability in the period ÷ unavailability allowed by the ${fmtTarget(target)} target`}>
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="text-text2">{label}</span>
        <span className="font-semibold tabular-nums" style={{ color }}>{text}</span>
      </div>
      <div className="relative mt-1.5 h-2.5 overflow-hidden rounded-full bg-surface3">
        <div
          className="h-full rounded-full transition-[width] duration-700"
          style={{ width: `${Math.max(width, used ? 1.5 : 0)}%`, background: `linear-gradient(90deg, ${colors.c('success')}, ${color})` }}
        />
        <span className="absolute inset-y-0 left-[75%] w-px bg-text/40" aria-hidden />
      </div>
      <div className="mt-1 flex justify-between text-xs text-muted">
        <span>0%</span><span className="-ml-8">75%</span><span>100%</span>
      </div>
    </div>
  )
}

/* ── Donut ─────────────────────────────────────────────────────────────── */

export interface DonutSegment {
  key: string
  label: string
  value: number
  color: string
  to?: string
  hatch?: boolean
}

export function Donut({
  segments, size = 176, thickness = 22, centerValue, centerLabel, active, onSelect,
}: {
  segments: DonutSegment[]
  size?: number
  thickness?: number
  centerValue: ReactNode
  centerLabel?: ReactNode
  active?: string | null
  onSelect?: (key: string) => void
}) {
  const colors = useVizColors()
  const [hover, setHover] = useState<string | null>(null)
  const hatchId = `donut-hatch-${useId().replace(/:/g, '')}`
  const total = segments.reduce((s, x) => s + Math.max(0, x.value), 0)
  const r = size / 2 - thickness / 2 - 2
  const circ = 2 * Math.PI * r
  const visible = segments.filter((s) => s.value > 0)
  const gap = visible.length > 1 ? Math.min(4, circ / 120) : 0
  let offset = 0
  const focus = hover ?? active ?? null
  const hovered = segments.find((s) => s.key === focus)
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" role="img" aria-label={segments.map((s) => `${s.label} ${s.value}`).join(', ')}>
        <defs>
          <pattern id={hatchId} patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
            <rect width="6" height="6" fill={colors.c('muted', 0.18)} />
            <line x1="0" y1="0" x2="0" y2="6" stroke={colors.c('muted', 0.6)} strokeWidth="2.5" />
          </pattern>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={colors.c('surface3')} strokeWidth={thickness} />
        {total > 0 && visible.map((s) => {
          const len = (s.value / total) * circ
          const dash = Math.max(0.5, len - gap)
          const el = (
            <circle
              key={s.key}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={s.hatch ? `url(#${hatchId})` : s.color}
              strokeWidth={focus === s.key ? thickness + 6 : thickness}
              strokeDasharray={`${dash} ${circ - dash}`}
              strokeDashoffset={-offset}
              className={cn('transition-all duration-300', onSelect && 'cursor-pointer')}
              style={{ opacity: focus && focus !== s.key ? 0.35 : 1 }}
              onMouseEnter={() => setHover(s.key)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onSelect?.(s.key)}
            />
          )
          offset += len
          return el
        })}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
        {hovered ? (
          <>
            <span className="text-2xl font-semibold tabular-nums text-text">{hovered.value.toLocaleString()}</span>
            <span className="max-w-[70%] truncate text-sm text-muted">{hovered.label}</span>
            {total > 0 && <span className="text-xs text-muted">{((hovered.value / total) * 100).toFixed(1)}%</span>}
          </>
        ) : (
          <>
            <span className="text-[1.75rem] font-semibold leading-none tabular-nums text-text">{centerValue}</span>
            {centerLabel && <span className="mt-1 max-w-[72%] text-sm leading-tight text-muted">{centerLabel}</span>}
          </>
        )}
      </div>
    </div>
  )
}

export function DonutLegend({
  segments, active, onSelect, format,
}: {
  segments: DonutSegment[]
  active?: string | null
  onSelect?: (key: string) => void
  format?: (v: number) => string
}) {
  const total = segments.reduce((s, x) => s + Math.max(0, x.value), 0)
  return (
    <ul className="w-full min-w-0 max-w-sm flex-1 space-y-1">
      {segments.map((s) => {
        const body = (
          <>
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={s.hatch ? { background: `repeating-linear-gradient(135deg, ${s.color} 0 2px, transparent 2px 4px)`, border: `1px solid ${s.color}` } : { background: s.color }}
            />
            <span className="min-w-0 flex-1 truncate text-text2">{s.label}</span>
            <span className="font-semibold tabular-nums text-text">{format ? format(s.value) : s.value.toLocaleString()}</span>
            <span className="w-12 text-right text-xs tabular-nums text-muted">{total ? `${((s.value / total) * 100).toFixed(0)}%` : '—'}</span>
          </>
        )
        const cls = cn(
          'flex min-h-[32px] w-full items-center gap-2 rounded-md px-2 text-sm transition-colors',
          (onSelect || s.to) && 'hover:bg-surface2',
          active === s.key && 'bg-surface2 ring-1 ring-border',
          s.value === 0 && 'opacity-55',
        )
        return (
          <li key={s.key}>
            {s.to ? <Link to={s.to} className={cls}>{body}</Link>
              : onSelect ? <button type="button" className={cls} aria-pressed={active === s.key} onClick={() => onSelect(s.key)}>{body}</button>
                : <div className={cls}>{body}</div>}
          </li>
        )
      })}
    </ul>
  )
}

/* ── Sparklines ────────────────────────────────────────────────────────── */

export function Sparkline({
  values, color, height = 40, kind = 'area', min, max, className,
}: {
  values: Array<number | null>
  color: string
  height?: number
  kind?: 'area' | 'bars'
  min?: number
  max?: number
  className?: string
}) {
  const gid = `spk-${useId().replace(/:/g, '')}`
  const nums = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (values.length < 2 || !nums.length) {
    return <div className={cn('flex items-end text-xs text-muted', className)} style={{ height }}>No trend data</div>
  }
  const lo = min ?? Math.min(...nums)
  const hi = max ?? Math.max(...nums)
  const span = hi - lo || 1
  const W = 100
  const H = height
  const x = (i: number) => (i / (values.length - 1)) * W
  const y = (v: number) => H - 2 - ((v - lo) / span) * (H - 4)
  if (kind === 'bars') {
    const bw = W / values.length
    return (
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className={cn('w-full', className)} style={{ height }} aria-hidden>
        {values.map((v, i) => v != null && v > 0 && (
          <rect key={i} x={i * bw + bw * 0.12} width={bw * 0.76} y={H - Math.max(1.5, ((v - Math.min(0, lo)) / (hi - Math.min(0, lo) || 1)) * (H - 2))} height={Math.max(1.5, ((v - Math.min(0, lo)) / (hi - Math.min(0, lo) || 1)) * (H - 2))} rx={0.6} fill={color} />
        ))}
        <line x1="0" x2={W} y1={H - 0.5} y2={H - 0.5} stroke={color} strokeOpacity={0.25} strokeWidth={1} vectorEffect="non-scaling-stroke" />
      </svg>
    )
  }
  // Line segments break at missing buckets.
  const runs: Array<Array<[number, number]>> = []
  let cur: Array<[number, number]> = []
  values.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) {
      if (cur.length) runs.push(cur)
      cur = []
    } else cur.push([x(i), y(v)])
  })
  if (cur.length) runs.push(cur)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className={cn('w-full', className)} style={{ height }} aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {runs.map((run, i) => {
        const line = run.map(([px, py], j) => `${j ? 'L' : 'M'}${px},${py}`).join(' ')
        const area = run.length > 1 ? `${line} L${run[run.length - 1][0]},${H} L${run[0][0]},${H} Z` : ''
        return (
          <g key={i}>
            {area && <path d={area} fill={`url(#${gid})`} />}
            <path d={run.length > 1 ? line : `M${run[0][0] - 0.8},${run[0][1]} L${run[0][0] + 0.8},${run[0][1]}`} fill="none" stroke={color} strokeWidth={1.75} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
          </g>
        )
      })}
    </svg>
  )
}

/* ── KPI tile ──────────────────────────────────────────────────────────── */

export function KpiTile({
  label, value, unit, tone = 'neutral', chip, sub, visual, to, title, dimmed,
}: {
  label: string
  value: ReactNode
  unit?: string
  tone?: 'neutral' | 'met' | 'risk' | 'breach' | 'unknown'
  chip?: { text: string; tone: 'met' | 'risk' | 'breach' | 'unknown' | 'neutral' } | null
  sub?: ReactNode
  visual?: ReactNode
  to?: string
  title?: string
  dimmed?: boolean
}) {
  const accent = {
    neutral: 'before:bg-primary/60', met: 'before:bg-success', risk: 'before:bg-warning', breach: 'before:bg-danger', unknown: 'before:bg-muted/60',
  }[tone]
  const chipCls = chip && {
    met: 'bg-success/12 text-success', risk: 'bg-warning/12 text-warning', breach: 'bg-danger/12 text-danger',
    unknown: 'bg-surface3 text-muted', neutral: 'bg-surface3 text-text2',
  }[chip.tone]
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <span className="text-sm font-medium text-text2" title={title}>{label}</span>
        {chip && <span className={cn('whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-semibold', chipCls)}>{chip.text}</span>}
      </div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={cn('text-[1.9rem] font-semibold leading-9 tabular-nums tracking-tight', tone === 'unknown' ? 'text-muted' : 'text-text')}>{value}</span>
        {unit && <span className="text-sm font-medium text-muted">{unit}</span>}
      </div>
      {sub && <div className="mt-0.5 min-h-[20px] text-sm text-muted">{sub}</div>}
      {visual && <div className="mt-auto pt-2">{visual}</div>}
    </>
  )
  const cls = cn(
    'group relative flex min-h-[168px] flex-col overflow-hidden rounded-xl border border-border bg-surface p-4 shadow-card transition dark:shadow-card-dark',
    'before:absolute before:inset-x-0 before:top-0 before:h-[3px]', accent,
    to && 'hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-lg',
    dimmed && 'opacity-60',
  )
  return to ? <Link to={to} className={cls}>{body}</Link> : <div className={cls}>{body}</div>
}

/* ── Bullet bars (availability vs target) ──────────────────────────────── */

export interface BulletRow {
  key: string
  label: ReactNode
  sub?: ReactNode
  value: number | null
  target: number | null
  to?: string
  right?: ReactNode
  muted?: boolean
}

export function BulletList({ rows, domain, empty }: { rows: BulletRow[]; domain: [number, number]; empty?: ReactNode }) {
  const colors = useVizColors()
  if (!rows.length) return <>{empty}</>
  const [lo, hi] = domain
  const pos = (v: number) => Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100))
  return (
    <div>
      <ul className="space-y-2.5">
        {rows.map((r) => {
          const band = availBand(r.value, r.target)
          const inner = (
            <>
              <div className="flex items-baseline justify-between gap-3">
                <div className="min-w-0 truncate">
                  <span className={cn('text-base font-medium', r.muted ? 'text-muted' : 'text-text')}>{r.label}</span>
                  {r.sub && <span className="ml-2 text-sm text-muted">{r.sub}</span>}
                </div>
                <div className="flex shrink-0 items-baseline gap-3">
                  {r.right}
                  <span className={cn('w-[4.75rem] text-right font-semibold tabular-nums', r.value == null && 'text-sm font-normal text-muted')} style={r.value != null ? { color: bandColor(band, colors) } : undefined}>
                    {fmtAvail(r.value)}
                  </span>
                </div>
              </div>
              <div className="relative mt-1.5 h-2.5 rounded-full bg-surface3">
                {r.value == null ? (
                  <div className="absolute inset-0 rounded-full" style={{ background: `repeating-linear-gradient(135deg, ${colors.c('muted', 0.35)} 0 3px, transparent 3px 7px)` }} />
                ) : (
                  <div
                    className="absolute inset-y-0 left-0 rounded-full transition-[width] duration-700"
                    style={{ width: `${Math.max(1.2, pos(availNumber(r.value)))}%`, background: `linear-gradient(90deg, ${bandColor(band, colors, 0.55)}, ${bandColor(band, colors)})` }}
                  />
                )}
                {r.target != null && r.target >= lo && (
                  <span className="absolute -top-1 bottom-[-4px] w-0.5 rounded bg-text/80" style={{ left: `calc(${pos(r.target)}% - 1px)` }} title={`Target ${fmtTarget(r.target)}`} />
                )}
              </div>
            </>
          )
          return (
            <li key={r.key}>
              {r.to
                ? <Link to={r.to} className="block rounded-lg px-2 py-1.5 transition hover:bg-surface2/70">{inner}</Link>
                : <div className="px-2 py-1.5">{inner}</div>}
            </li>
          )
        })}
      </ul>
      <div className="mt-2 flex justify-between px-2 text-xs text-muted">
        <span>{lo}%</span>
        <span>Scale {lo}–{hi}% · ▮ target</span>
        <span>{hi}%</span>
      </div>
    </div>
  )
}

/* ── Ranked horizontal bars ────────────────────────────────────────────── */

export interface RankRow {
  key: string
  label: ReactNode
  sub?: ReactNode
  value: number
  display: string
  color: string
  to?: string
  badge?: ReactNode
}

export function RankBars({ rows, empty, max }: { rows: RankRow[]; empty?: ReactNode; max?: number }) {
  if (!rows.length) return <>{empty}</>
  const top = max ?? Math.max(1, ...rows.map((r) => r.value))
  return (
    <ol className="space-y-2">
      {rows.map((r, i) => {
        const inner = (
          <>
            <div className="flex items-center gap-2">
              <span className="w-5 shrink-0 text-right text-xs font-semibold tabular-nums text-muted">{i + 1}</span>
              <span className="min-w-0 flex-1 truncate text-base text-text">{r.label}</span>
              {r.badge}
              <span className="shrink-0 text-sm font-semibold tabular-nums text-text">{r.display}</span>
            </div>
            <div className="ml-7 mt-1 flex items-center gap-2">
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface3">
                <div className="h-full rounded-full transition-[width] duration-700" style={{ width: `${Math.max(1.5, (r.value / top) * 100)}%`, background: `linear-gradient(90deg, ${withAlpha(r.color, 0.55)}, ${r.color})` }} />
              </div>
            </div>
            {r.sub && <div className="ml-7 mt-0.5 truncate text-xs text-muted">{r.sub}</div>}
          </>
        )
        return (
          <li key={r.key}>
            {r.to ? <Link to={r.to} className="block rounded-lg px-1.5 py-1 transition hover:bg-surface2/70">{inner}</Link> : <div className="px-1.5 py-1">{inner}</div>}
          </li>
        )
      })}
    </ol>
  )
}

/* ── Column histogram ──────────────────────────────────────────────────── */

export function ColumnChart({
  bins, color, height = 150, caption,
}: {
  bins: Array<{ key: string; label: string; count: number }>
  color: string
  height?: number
  caption?: string
}) {
  const max = Math.max(1, ...bins.map((b) => b.count))
  return (
    <figure>
      <div className="flex items-end gap-2" style={{ height }} role="img" aria-label={`${caption ?? 'Distribution'}: ${bins.map((b) => `${b.label} ${b.count}`).join(', ')}`}>
        {bins.map((b) => (
          <div key={b.key} className="flex h-full min-w-0 flex-1 flex-col items-center justify-end gap-1">
            <span className={cn('text-sm font-semibold tabular-nums', b.count ? 'text-text' : 'text-muted')}>{b.count}</span>
            <div
              className="w-full max-w-[56px] rounded-t-md transition-[height] duration-700"
              style={{ height: `${b.count ? Math.max(4, (b.count / max) * (height - 28)) : 2}px`, background: b.count ? `linear-gradient(180deg, ${color}, ${withAlpha(color, 0.35)})` : 'rgb(var(--surface3))' }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex gap-2 border-t border-border pt-1.5">
        {bins.map((b) => <span key={b.key} className="min-w-0 flex-1 text-center text-xs leading-tight text-muted">{b.label}</span>)}
      </div>
    </figure>
  )
}

/* ── Stacked bar (composition) ─────────────────────────────────────────── */

export function StackedBar({ parts, height = 10 }: { parts: Array<{ key: string; value: number; color: string; label: string; hatch?: boolean }>; height?: number }) {
  const total = parts.reduce((s, p) => s + p.value, 0)
  if (!total) return <div className="rounded-full bg-surface3" style={{ height }} />
  return (
    <div className="flex w-full gap-0.5 overflow-hidden rounded-full" style={{ height }} role="img" aria-label={parts.map((p) => `${p.label} ${p.value}`).join(', ')}>
      {parts.filter((p) => p.value > 0).map((p) => (
        <div
          key={p.key}
          title={`${p.label}: ${p.value}`}
          className="h-full first:rounded-l-full last:rounded-r-full"
          style={{
            width: `${(p.value / total) * 100}%`,
            background: p.hatch ? `repeating-linear-gradient(135deg, ${p.color} 0 3px, transparent 3px 6px)` : p.color,
          }}
        />
      ))}
    </div>
  )
}

/* ── Health tiles (sites / groups) ─────────────────────────────────────── */

export interface HealthTile {
  key: string
  name: string
  unassigned?: boolean
  availability: number | null
  devices: number
  measured?: number | null
  downNow: number
  downtimeMin?: number | null
  to?: string
}

export function HealthTiles({ tiles, target, empty }: { tiles: HealthTile[]; target: number | null; empty?: ReactNode }) {
  const colors = useVizColors()
  if (!tiles.length) return <>{empty}</>
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-4 min-[2000px]:grid-cols-5 min-[2400px]:grid-cols-6">
      {tiles.map((t) => {
        const band = availBand(t.availability, target)
        const color = bandColor(band, colors)
        const body = (
          <>
            <div className="flex items-start justify-between gap-1">
              <span className={cn('line-clamp-2 text-sm font-medium leading-tight', t.unassigned ? 'italic text-muted' : 'text-text')} title={t.name}>{t.name}</span>
              {t.downNow > 0 && (
                <span className="shrink-0 rounded-full bg-danger px-1.5 text-xs font-bold leading-5 text-white" title={`${t.downNow} down now`}>{t.downNow}↓</span>
              )}
            </div>
            <div className="mt-2 text-xl font-semibold tabular-nums leading-none" style={{ color: band === 'none' ? undefined : color }}>
              {t.availability == null ? <span className="text-sm font-normal text-muted">Not measured</span> : `${availNumber(t.availability).toFixed(2)}%`}
            </div>
            <div className="mt-1.5 text-xs text-muted">
              {t.devices} device{t.devices === 1 ? '' : 's'}
              {t.measured != null && t.measured < t.devices && <span className="text-warning"> · {t.measured} measured</span>}
              {t.downtimeMin ? <> · {fmtDuration(t.downtimeMin)} down</> : null}
            </div>
          </>
        )
        const style = {
          background: band === 'none'
            ? `repeating-linear-gradient(135deg, ${colors.c('muted', 0.08)} 0 6px, transparent 6px 12px)`
            : `linear-gradient(160deg, ${bandColor(band, colors, colors.dark ? 0.2 : 0.14)}, ${bandColor(band, colors, 0.03)})`,
          borderColor: bandColor(band, colors, band === 'none' ? 0.5 : 0.45),
        }
        const cls = 'block rounded-lg border p-2.5 transition hover:-translate-y-0.5 hover:shadow-md'
        return t.to
          ? <Link key={t.key} to={t.to} className={cls} style={style}>{body}</Link>
          : <div key={t.key} className={cls} style={style}>{body}</div>
      })}
    </div>
  )
}

/* ── ECharts helpers ───────────────────────────────────────────────────── */

function medianStep(ts: number[], fallback: number): number {
  const diffs = ts.slice(1).map((t, i) => t - ts[i]).filter((d) => d > 0).sort((a, b) => a - b)
  return diffs.length ? diffs[Math.floor(diffs.length / 2)] : fallback
}

function axisTime(t: number, spanMs: number): string {
  const d = new Date(t)
  if (spanMs > 3 * 86_400_000) return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  if (spanMs > 20 * 3_600_000) {
    return d.getHours() === 0 && d.getMinutes() === 0
      ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
      : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function tooltipBase(colors: VizColors) {
  return {
    backgroundColor: colors.c('surface'),
    borderColor: colors.c('border'),
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: colors.c('text'), fontSize: 12 },
    extraCssText: 'box-shadow: 0 8px 24px rgba(0,0,0,.25); border-radius: 8px;',
  }
}

/* ── Availability + outage activity ────────────────────────────────────── */

export interface ActivityPoint { ts: string; entities_down: number; outages_started: number; downtime_minutes: number }

export function AvailabilityActivityChart({
  trend, activity, target, fromMs, toMs, height = 340, entity = 'devices',
}: {
  trend: Array<{ ts: string; availability_pct: number | null }> | null
  activity: ActivityPoint[] | null
  target: number | null
  fromMs: number
  toMs: number
  height?: number
  entity?: string
}) {
  const colors = useVizColors()
  const option = useMemo(() => {
    const tr = (trend || []).map((p) => ({ t: Date.parse(p.ts), v: p.availability_pct })).filter((p) => Number.isFinite(p.t)).sort((a, b) => a.t - b.t)
    const act = (activity || []).map((p) => ({ ...p, t: Date.parse(p.ts) })).filter((p) => Number.isFinite(p.t)).sort((a, b) => a.t - b.t)
    const hasTrend = tr.length > 0
    const step = medianStep((hasTrend ? tr : act).map((p) => p.t), Math.max(60_000, (toMs - fromMs) / 48))
    const span = toMs - fromMs
    const domain = availDomain(tr.map((p) => p.v), target)
    // No-evidence spans (null buckets merged).
    const gaps: Array<[number, number]> = []
    for (const p of tr) {
      if (p.v != null) continue
      const last = gaps[gaps.length - 1]
      if (last && p.t <= last[1] + 1) last[1] = p.t + step
      else gaps.push([p.t, p.t + step])
    }
    const t = target ?? 99.9
    const muted = colors.c('muted')
    const grid = colors.c('border', 0.8)
    const axisLabel = { color: muted, fontSize: 11, hideOverlap: true, formatter: (v: number) => axisTime(v, span) }
    const topH = hasTrend && act.length ? '58%' : '78%'
    const grids = hasTrend && act.length
      ? [{ left: 52, right: 16, top: 16, height: topH }, { left: 52, right: 16, top: '76%', bottom: 28 }]
      : [{ left: 52, right: 16, top: 16, bottom: 28 }]
    const xAxes = grids.map((_, i) => ({
      type: 'time' as const,
      gridIndex: i,
      min: fromMs,
      max: toMs,
      axisLine: { lineStyle: { color: grid } },
      axisTick: { show: false },
      axisLabel: i === grids.length - 1 ? axisLabel : { show: false },
      splitLine: { show: false },
    }))
    const yAxes: any[] = []
    const series: any[] = []
    if (hasTrend) {
      yAxes.push({
        type: 'value', gridIndex: 0, min: domain[0], max: domain[1],
        axisLabel: { color: muted, fontSize: 11, formatter: (v: number) => `${v}%` },
        splitLine: { lineStyle: { color: grid, type: 'dashed' } },
      })
      series.push({
        name: 'Availability',
        type: 'line',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: tr.map((p) => [p.t + step / 2, p.v == null ? null : availNumber(p.v)]),
        connectNulls: false,
        showSymbol: tr.length <= 36,
        symbolSize: 5,
        smooth: 0.25,
        lineStyle: { width: 2.5 },
        areaStyle: { opacity: 0.16, origin: 'start' },
        emphasis: { focus: 'series' },
        markLine: target != null && target >= domain[0] ? {
          silent: true,
          symbol: 'none',
          lineStyle: { color: colors.c('text', 0.7), type: 'dashed', width: 1.5 },
          label: { formatter: `Target ${fmtTarget(target)}`, color: colors.c('text2'), fontSize: 11, position: 'insideStartTop', backgroundColor: colors.c('surface', 0.85), padding: [1, 4], borderRadius: 3 },
          data: [{ yAxis: target }],
        } : undefined,
        markArea: gaps.length ? {
          silent: true,
          itemStyle: { color: colors.c('muted', 0.1), borderColor: colors.c('muted', 0.3), borderWidth: 1, borderType: 'dashed' },
          label: { show: false },
          data: gaps.map(([a, b]) => [
            { xAxis: a, label: { show: b - a > span / 8, position: 'insideBottom', color: muted, fontSize: 11, formatter: 'No evidence' } },
            { xAxis: Math.min(b, toMs) },
          ]),
        } : undefined,
      })
    }
    if (act.length) {
      const gi = hasTrend ? 1 : 0
      const maxDown = Math.max(1, ...act.map((p) => p.entities_down))
      yAxes.push({
        type: 'value', gridIndex: gi, min: 0, max: Math.max(2, Math.ceil(maxDown * 1.15)), minInterval: 1, splitNumber: 2,
        axisLabel: { color: muted, fontSize: 10 },
        splitLine: { lineStyle: { color: grid, type: 'dashed' } },
      })
      series.push({
        name: `${entity[0].toUpperCase()}${entity.slice(1)} down`,
        type: 'bar',
        xAxisIndex: gi,
        yAxisIndex: yAxes.length - 1,
        barWidth: '70%',
        barMaxWidth: 18,
        data: act.map((p) => [p.t + step / 2, p.entities_down]),
        itemStyle: {
          borderRadius: [3, 3, 0, 0],
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: colors.c('danger', 0.95) }, { offset: 1, color: colors.c('danger', 0.35) }],
          },
        },
      }, {
        name: 'Outages started',
        type: 'scatter',
        xAxisIndex: gi,
        yAxisIndex: yAxes.length - 1,
        symbol: 'triangle',
        symbolSize: (v: number[]) => (v[2] ? Math.min(14, 6 + v[2] * 1.5) : 0),
        data: act.filter((p) => p.outages_started > 0).map((p) => [p.t + step / 2, p.entities_down, p.outages_started]),
        itemStyle: { color: colors.c('amber'), borderColor: colors.c('surface'), borderWidth: 1 },
        z: 5,
      })
    }
    const byT = new Map<number, { v?: number | null; a?: ActivityPoint }>()
    for (const p of tr) byT.set(p.t, { ...(byT.get(p.t) || {}), v: p.v })
    for (const p of act) byT.set(p.t, { ...(byT.get(p.t) || {}), a: p })
    const keys = [...byT.keys()].sort((a, b) => a - b)
    return {
      animationDuration: 600,
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      visualMap: hasTrend ? {
        show: false,
        seriesIndex: 0,
        dimension: 1,
        pieces: [
          { gte: t, color: colors.c('success') },
          { gte: t - 1, lt: t, color: colors.c('amber') },
          { gte: t - 5, lt: t - 1, color: colors.c('warning') },
          { lt: t - 5, color: colors.c('danger') },
        ],
        outOfRange: { color: muted },
      } : undefined,
      dataZoom: [{ type: 'inside', xAxisIndex: grids.map((_, i) => i), filterMode: 'none' }],
      tooltip: {
        ...tooltipBase(colors),
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: colors.c('muted', 0.6) } },
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params]
          const x = Number(arr[0]?.value?.[0] ?? arr[0]?.axisValue) - step / 2
          const k = keys.reduce((best, cur) => (Math.abs(cur - x) < Math.abs(best - x) ? cur : best), keys[0] ?? x)
          const row = byT.get(k) || {}
          const lines = [`<div style="font-weight:600;margin-bottom:4px">${fmtDateTime(k)} – ${new Date(k + step).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</div>`]
          if (hasTrend) {
            const band = availBand(row.v ?? null, target)
            lines.push(`<div style="display:flex;justify-content:space-between;gap:16px"><span>Availability</span><b style="color:${bandColor(band, colors)}">${row.v == null ? 'No evidence' : `${availNumber(row.v).toFixed(2)}%`}</b></div>`)
          }
          if (row.a) {
            lines.push(`<div style="display:flex;justify-content:space-between;gap:16px"><span>${entity[0].toUpperCase()}${entity.slice(1)} down</span><b>${row.a.entities_down}</b></div>`)
            lines.push(`<div style="display:flex;justify-content:space-between;gap:16px"><span>Outages started</span><b>${row.a.outages_started}</b></div>`)
            lines.push(`<div style="display:flex;justify-content:space-between;gap:16px"><span>Downtime in bucket</span><b>${fmtDuration(row.a.downtime_minutes, '0 min')}</b></div>`)
          }
          return lines.join('')
        },
      },
      series,
    }
  }, [trend, activity, target, fromMs, toMs, colors, entity])
  return <ReactECharts option={option} notMerge style={{ height, width: '100%' }} opts={{ renderer: 'canvas' }} />
}

/* ── Availability heatmap ──────────────────────────────────────────────── */

export function AvailabilityHeatmap({
  points, target, height,
}: {
  points: Array<{ ts: string; availability_pct: number | null }>
  target: number | null
  height?: number
}) {
  const colors = useVizColors()
  const model = useMemo(() => {
    const pts = points.map((p) => ({ t: Date.parse(p.ts), v: p.availability_pct })).filter((p) => Number.isFinite(p.t)).sort((a, b) => a.t - b.t)
    if (!pts.length) return null
    const step = medianStep(pts.map((p) => p.t), 3_600_000)
    const span = pts[pts.length - 1].t - pts[0].t + step
    const dayKey = (t: number) => new Date(t).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
    type Cell = { sum: number; n: number; any: boolean; start: number }
    const cells = new Map<string, Cell>()
    const rows: string[] = []
    let cols: string[] = []
    let mode: 'strip' | 'day-hour' | 'day-part' | 'calendar'
    if (span <= 20 * 3_600_000) {
      mode = 'strip'
      rows.push('Fleet')
      cols = pts.map((p) => new Date(p.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
      pts.forEach((p, i) => cells.set(`0:${i}`, { sum: p.v ?? 0, n: p.v == null ? 0 : 1, any: true, start: p.t }))
    } else if (step <= 3_600_000) {
      mode = 'day-hour'
      cols = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, '0')}:00`)
      for (const p of pts) {
        const d = new Date(p.t)
        const rk = dayKey(p.t)
        if (!rows.includes(rk)) rows.push(rk)
        const k = `${rows.indexOf(rk)}:${d.getHours()}`
        const c = cells.get(k) ?? { sum: 0, n: 0, any: true, start: new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours()).getTime() }
        if (p.v != null) { c.sum += p.v; c.n += 1 }
        cells.set(k, c)
      }
    } else if (step < 86_400_000) {
      mode = 'day-part'
      const hours = [...new Set(pts.map((p) => new Date(p.t).getHours()))].sort((a, b) => a - b)
      const stepH = Math.round(step / 3_600_000)
      cols = hours.map((h) => `${String(h).padStart(2, '0')}–${String((h + stepH) % 24).padStart(2, '0')}`)
      for (const p of pts) {
        const rk = dayKey(p.t)
        if (!rows.includes(rk)) rows.push(rk)
        const k = `${rows.indexOf(rk)}:${hours.indexOf(new Date(p.t).getHours())}`
        cells.set(k, { sum: p.v ?? 0, n: p.v == null ? 0 : 1, any: true, start: p.t })
      }
    } else {
      mode = 'calendar'
      cols = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
      for (const p of pts) {
        const d = new Date(p.t)
        const wd = (d.getDay() + 6) % 7
        const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - wd)
        const rk = `Week of ${monday.toLocaleDateString([], { month: 'short', day: 'numeric' })}`
        if (!rows.includes(rk)) rows.push(rk)
        cells.set(`${rows.indexOf(rk)}:${wd}`, { sum: p.v ?? 0, n: p.v == null ? 0 : 1, any: true, start: p.t })
      }
    }
    const data: Array<[number, number, number, number]> = []
    const empty: Array<[number, number, number, number]> = []
    for (const [k, c] of cells) {
      const [r, col] = k.split(':').map(Number)
      if (c.n) data.push([col, r, availNumber(c.sum / c.n), c.start])
      else empty.push([col, r, -1, c.start])
    }
    return { rows, cols, data, empty, mode, step }
  }, [points])

  const option = useMemo(() => {
    if (!model) return null
    const t = target ?? 99.9
    const muted = colors.c('muted')
    const cellGap = colors.c('surface')
    return {
      animationDuration: 500,
      grid: { left: model.mode === 'strip' ? 52 : 108, right: 12, top: 8, bottom: model.mode === 'strip' ? 36 : 30 },
      xAxis: {
        type: 'category', data: model.cols, splitArea: { show: false },
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 10, interval: model.mode === 'day-hour' ? 2 : 'auto', hideOverlap: true },
      },
      yAxis: {
        type: 'category', data: model.rows, inverse: true,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { color: colors.c('text2'), fontSize: 11 },
      },
      visualMap: {
        show: false,
        seriesIndex: 0,
        dimension: 2,
        pieces: [
          { value: -1, color: colors.c('muted', 0.14) },
          { gte: t, color: colors.c('success', 0.85) },
          { gte: t - 1, lt: t, color: colors.c('amber', 0.9) },
          { gte: t - 5, lt: t - 1, color: colors.c('warning', 0.9) },
          { gte: 0, lt: t - 5, color: colors.c('danger', 0.9) },
        ],
      },
      tooltip: {
        ...tooltipBase(colors),
        formatter: (p: any) => {
          const [, , v, start] = p.value as number[]
          const when = model.mode === 'strip' ? fmtDateTime(start) : `${model.rows[p.value[1]]} · ${model.cols[p.value[0]]}`
          const band = availBand(v < 0 ? null : v, target)
          return `<div style="font-weight:600;margin-bottom:2px">${when}</div><b style="color:${bandColor(band, colors)}">${v < 0 ? 'No evidence' : `${v.toFixed(2)}%`}</b><div style="color:${muted};font-size:11px">${bandLabel(band, target)}</div>`
        },
      },
      series: [
        {
          type: 'heatmap', data: [...model.data, ...model.empty],
          itemStyle: { borderColor: cellGap, borderWidth: 2, borderRadius: 3 },
          emphasis: { itemStyle: { borderColor: colors.c('text'), borderWidth: 1.5 } },
        },
      ],
    }
  }, [model, colors, target])

  if (!model || !option) return null
  const h = height ?? (model.mode === 'strip' ? 96 : Math.min(460, Math.max(140, model.rows.length * 26 + 44)))
  return <ReactECharts option={option} notMerge style={{ height: h, width: '100%' }} />
}

/* ── Simple bar chart (categorical, echarts) ───────────────────────────── */

export function CategoryBars({
  items, height = 240, unit = '', color, horizontal = true, valueFormat,
}: {
  items: Array<{ key: string; label: string; value: number; color?: string }>
  height?: number
  unit?: string
  color?: string
  horizontal?: boolean
  valueFormat?: (v: number) => string
}) {
  const colors = useVizColors()
  const option = useMemo(() => {
    const muted = colors.c('muted')
    const base = color ?? colors.c('primary')
    const fmt = valueFormat ?? ((v: number) => `${v.toLocaleString()}${unit}`)
    const cat = { type: 'category', data: items.map((i) => i.label), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: colors.c('text2'), fontSize: 11, width: 120, overflow: 'truncate' as const }, inverse: horizontal }
    const val = { type: 'value', axisLabel: { color: muted, fontSize: 10, formatter: fmt }, splitLine: { lineStyle: { color: colors.c('border', 0.8), type: 'dashed' } } }
    return {
      animationDuration: 500,
      grid: horizontal ? { left: 128, right: 56, top: 6, bottom: 20 } : { left: 44, right: 12, top: 16, bottom: 36 },
      xAxis: horizontal ? val : cat,
      yAxis: horizontal ? cat : val,
      tooltip: { ...tooltipBase(colors), trigger: 'item', formatter: (p: any) => `${p.name}<br/><b>${fmt(p.value)}</b>` },
      series: [{
        type: 'bar',
        barMaxWidth: 20,
        data: items.map((i) => ({
          value: i.value,
          itemStyle: {
            borderRadius: horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0],
            color: {
              type: 'linear', x: 0, y: 0, x2: horizontal ? 1 : 0, y2: horizontal ? 0 : 1,
              colorStops: horizontal
                ? [{ offset: 0, color: withAlpha(i.color ?? base, 0.45) }, { offset: 1, color: i.color ?? base }]
                : [{ offset: 0, color: i.color ?? base }, { offset: 1, color: withAlpha(i.color ?? base, 0.45) }],
            },
          },
        })),
        label: { show: horizontal, position: 'right', color: colors.c('text2'), fontSize: 11, formatter: (p: any) => fmt(p.value) },
      }],
    }
  }, [items, colors, color, unit, horizontal, valueFormat])
  return <ReactECharts option={option} notMerge style={{ height, width: '100%' }} />
}

/* ── Service downtime activity (client-side from confirmed episodes) ───── */

export function episodeActivity(
  episodes: Array<{ id: string; startMs: number | null; endMs: number | null; ongoing: boolean }>,
  fromMs: number,
  toMs: number,
): ActivityPoint[] {
  const span = toMs - fromMs
  if (!(span > 0)) return []
  const step = span <= 6 * 3_600_000 ? 300_000 : span <= 2 * 86_400_000 ? 1_800_000 : span <= 14 * 86_400_000 ? 3_600_000 : span <= 60 * 86_400_000 ? 6 * 3_600_000 : 86_400_000
  const first = Math.floor(fromMs / step) * step
  const n = Math.max(1, Math.ceil((toMs - first) / step))
  const started = new Array(n).fill(0)
  const down = new Array(n).fill(0)
  const affected: Array<Set<string>> = Array.from({ length: n }, () => new Set<string>())
  for (const e of episodes) {
    const lo = Math.max(fromMs, e.startMs ?? fromMs)
    const hi = Math.min(toMs, e.endMs ?? (e.ongoing ? toMs : lo))
    if (e.startMs != null && e.startMs >= fromMs && e.startMs < toMs) started[Math.floor((e.startMs - first) / step)] += 1
    if (hi <= lo) continue
    for (let i = Math.max(0, Math.floor((lo - first) / step)); i <= Math.min(n - 1, Math.floor((hi - first) / step)); i++) {
      const a = Math.max(lo, first + i * step)
      const b = Math.min(hi, first + (i + 1) * step)
      if (b > a) {
        down[i] += (b - a) / 60_000
        affected[i].add(e.id)
      }
    }
  }
  return started.map((s, i) => ({
    ts: new Date(first + i * step).toISOString(),
    outages_started: s,
    entities_down: affected[i].size,
    downtime_minutes: Math.round(down[i] * 100) / 100,
  }))
}

/* ── Operations-view charts ────────────────────────────────────────────── */

export function AlertVolumeChart({
  points, fromMs, toMs, height = 220,
}: {
  points: Array<{ ts: string; critical: number; warning: number; info: number }>
  fromMs: number
  toMs: number
  height?: number
}) {
  const colors = useVizColors()
  const option = useMemo(() => {
    const span = toMs - fromMs
    // The report buckets by hour up to two days, by UTC day beyond.
    const step = span <= 2 * 86_400_000 ? 3_600_000 : 86_400_000
    const pts = points.map((p) => ({ ...p, t: Date.parse(p.ts) })).filter((p) => Number.isFinite(p.t))
    const muted = colors.c('muted')
    const sev = [
      { key: 'critical' as const, name: 'Critical', color: colors.c('danger') },
      { key: 'warning' as const, name: 'Warning', color: colors.c('warning') },
      { key: 'info' as const, name: 'Info', color: colors.c('muted', 0.6) },
    ]
    return {
      animationDuration: 500,
      grid: { left: 44, right: 12, top: 12, bottom: 28 },
      xAxis: {
        type: 'time', min: fromMs, max: toMs,
        axisLine: { lineStyle: { color: colors.c('border') } }, axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11, hideOverlap: true, formatter: (v: number) => axisTime(v, span) },
        splitLine: { show: false },
      },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { color: muted, fontSize: 10 }, splitLine: { lineStyle: { color: colors.c('border', 0.8), type: 'dashed' } } },
      tooltip: {
        ...tooltipBase(colors),
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params]
          const t = Number(arr[0]?.value?.[0]) - step / 2
          const head = step >= 86_400_000
            ? new Date(t).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' }) + ' (UTC day)'
            : fmtDateTime(t)
          return `<div style="font-weight:600;margin-bottom:4px">${head}</div>` + arr.map((p: any) =>
            `<div style="display:flex;justify-content:space-between;gap:16px"><span>${p.marker}${p.seriesName}</span><b>${p.value[1]}</b></div>`).join('')
        },
      },
      series: sev.map((s, i) => ({
        name: s.name,
        type: 'bar',
        stack: 'alerts',
        barMaxWidth: 28,
        barMinWidth: 2,
        data: pts.map((p) => [p.t + step / 2, p[s.key] ?? 0]),
        itemStyle: { color: s.color, borderRadius: i === 0 ? [3, 3, 0, 0] : 0 },
      })),
    }
  }, [points, fromMs, toMs, colors])
  if (!points.length) return <EvidenceEmpty kind="no_findings">No alerts raised in this period.</EvidenceEmpty>
  return (
    <div>
      <ReactECharts option={option} notMerge style={{ height, width: '100%' }} />
      <div className="mt-2 flex flex-wrap gap-x-4 text-sm text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm bg-danger" />Critical</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm bg-warning" />Warning</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm bg-muted/60" />Info</span>
        <span>Alert volume is operational noise, not availability loss.</span>
      </div>
    </div>
  )
}
