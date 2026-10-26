import { useId, useMemo, useState, type ReactNode } from 'react'
import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { cn } from '@/lib/utils'
import { formatAbsolute, MISSING, type Stats } from './format'
import { chartFont } from '@/lib/chartTheme'

/* One time-series vocabulary for the device page: theme tokens only, fixed or
 * floor-bounded domains, gaps drawn as gaps, configured thresholds as dashed
 * lines, a crosshair shared across a chart stack (syncId) and a y-axis sized
 * to its longest label so values are never clipped. */

export type Point = { ts: number; value: number | null }

export const SERIES_COLORS = {
  primary: 'rgb(var(--primary))',
  info: 'rgb(var(--info))',
  accent: 'rgb(var(--accent))',
  success: 'rgb(var(--success))',
  warning: 'rgb(var(--warning))',
  danger: 'rgb(var(--danger))',
  muted: 'rgb(var(--muted))',
} as const

/** Insert a null where samples are further apart than ~2.5 typical intervals,
 *  so a polling gap is drawn as a gap, not a straight line across it. */
export function withGaps(points: Point[]): Point[] {
  if (points.length < 3) return points
  const sorted = [...points].sort((a, b) => a.ts - b.ts)
  const steps = sorted.slice(1).map((p, i) => p.ts - sorted[i].ts).filter((d) => d > 0).sort((a, b) => a - b)
  if (!steps.length) return sorted
  const typical = steps[Math.floor(steps.length / 2)]
  const out: Point[] = [sorted[0]]
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i].ts - sorted[i - 1].ts > typical * 2.5) out.push({ ts: sorted[i - 1].ts + typical, value: null })
    out.push(sorted[i])
  }
  return out
}

/** Y-axis width that fits the longest tick label (about 7.5 px per character at 12 px). */
export function axisWidth(labels: string[]): number {
  const longest = labels.reduce((n, l) => Math.max(n, l.length), 1)
  return Math.min(110, Math.max(36, Math.ceil(longest * 7.5) + 14))
}

/** Round, evenly spaced ticks from 0: 0/200/400/600 instead of 0/183/366/550. */
export function niceTicks(max: number, count = 3): number[] {
  if (!(max > 0)) return [0, 1]
  const raw = max / count
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag
  const ticks: number[] = []
  for (let v = 0; v < max + step * 0.999; v += step) ticks.push(Number(v.toFixed(10)))
  return ticks
}

/* ── Sparkline ──────────────────────────────────────────────────────────── */

export function Sparkline({
  values, domain, stroke = SERIES_COLORS.primary, threshold, format, className, label,
}: {
  values: Array<number | null>
  /** Fixed scale: [0, 100] for percent metrics. Never min..max of the data. */
  domain: [number, number]
  stroke?: string
  threshold?: number | null
  format?: (v: number) => string
  className?: string
  label?: string
}) {
  const id = useId().replace(/:/g, '')
  const [hover, setHover] = useState<number | null>(null)
  const w = 100
  const h = 28
  const [lo, hi] = domain
  const span = hi - lo || 1
  const y = (v: number) => h - 1 - ((Math.min(hi, Math.max(lo, v)) - lo) / span) * (h - 2)
  const step = values.length > 1 ? w / (values.length - 1) : w
  const segments: string[] = []
  let current = ''
  values.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) { if (current) segments.push(current); current = ''; return }
    current += `${current ? 'L' : 'M'}${(i * step).toFixed(2)},${y(v).toFixed(2)} `
  })
  if (current) segments.push(current)
  const known = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (known.length < 2) return <div className={cn('h-7', className)} aria-hidden="true" />
  const shown = hover != null ? values[hover] : null
  return (
    <div className={cn('relative h-7', className)}>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-full w-full overflow-visible" role="img"
        aria-label={label ? `${label} trend` : 'Trend'}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect()
          const i = Math.round(((e.clientX - box.left) / box.width) * (values.length - 1))
          setHover(Math.max(0, Math.min(values.length - 1, i)))
        }}>
        <defs>
          <linearGradient id={`spark-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.22} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
        </defs>
        {threshold != null && threshold >= lo && threshold <= hi && (
          <line x1="0" x2={w} y1={y(threshold)} y2={y(threshold)} stroke={SERIES_COLORS.danger} strokeWidth="0.8" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" />
        )}
        {segments.map((d, i) => {
          const xs = d.trim().split(' ').map((p) => Number(p.slice(1).split(',')[0]))
          return <g key={i}>
            <path d={`${d} L${xs[xs.length - 1]},${h} L${xs[0]},${h} Z`} fill={`url(#spark-${id})`} />
            <path d={d} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
          </g>
        })}
        {hover != null && <line x1={hover * step} x2={hover * step} y1="0" y2={h} stroke={SERIES_COLORS.muted} strokeWidth="0.6" vectorEffect="non-scaling-stroke" />}
      </svg>
      {hover != null && (
        <span className="pointer-events-none absolute -top-5 right-0 rounded border border-border bg-surface px-1.5 text-xs tabular-nums shadow-sm">
          {shown == null ? 'no sample' : format ? format(shown) : shown}
        </span>
      )}
    </div>
  )
}

/* ── Time-series panel ──────────────────────────────────────────────────── */

export type Threshold = { value: number; label: string; tone: 'warning' | 'danger' }
/** `dashed`: a comparison series (the previous period), drawn lighter and dashed. */
export type SeriesDef = { key: string; name: string; color: string; points: Point[]; dashed?: boolean }

function axisTimeFormatter(rangeHours: number): (ts: number) => string {
  if (rangeHours <= 12) return (ts) => new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (rangeHours <= 24 * 7) {
    return (ts) => {
      const d = new Date(ts)
      return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`
    }
  }
  return (ts) => new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function TimeSeriesPanel({
  series, fromTs, toTs, rangeHours, domain, format, axisFormat, thresholds = [], syncId, height = 180, emptyText = 'No samples in this period',
}: {
  series: SeriesDef[]
  fromTs: number
  toTs: number
  rangeHours: number
  /** Fixed or floor-bounded y domain. 'auto' lets the max follow the data from 0. */
  domain: [number, number] | 'auto'
  format: (v: number) => string
  /** Compact tick labels (defaults to `format`). */
  axisFormat?: (v: number) => string
  thresholds?: Threshold[]
  syncId?: string
  height?: number
  emptyText?: string
}) {
  const data = useMemo(() => {
    const byTs = new Map<number, Record<string, number | null>>()
    for (const s of series) {
      for (const p of withGaps(s.points)) {
        const row = byTs.get(p.ts) || { ts: p.ts }
        row[s.key] = p.value
        byTs.set(p.ts, row)
      }
    }
    return [...byTs.values()].sort((a, b) => (a.ts as number) - (b.ts as number))
  }, [series])
  const hasData = series.some((s) => s.points.some((p) => p.value != null))
  // Interior ticks only: a label centred on either edge of the window is cut in half.
  const ticks = useMemo(() => {
    if (!(toTs > fromTs)) return []
    const n = rangeHours > 12 && rangeHours <= 24 * 7 ? 3 : 4
    return Array.from({ length: n }, (_, i) => fromTs + ((i + 1) * (toTs - fromTs)) / (n + 1))
  }, [fromTs, toTs, rangeHours])
  const tickFormatter = useMemo(() => axisTimeFormatter(rangeHours), [rangeHours])
  const maxValue = useMemo(() => {
    let m = 0
    for (const s of series) for (const p of s.points) if (p.value != null && p.value > m) m = p.value
    for (const t of thresholds) if (t.value > m) m = t.value
    return m
  }, [series, thresholds])
  const fixedTop = domain === 'auto' ? (maxValue > 0 ? maxValue * 1.05 : 1) : domain[1]
  const yTicks = domain !== 'auto' && domain[0] !== 0 ? undefined : niceTicks(fixedTop)
  const yDomain: [number, number] = domain === 'auto' || domain[0] === 0 ? [0, yTicks![yTicks!.length - 1]] : domain
  const tickFmt = axisFormat || format
  const yLabels = (yTicks || [yDomain[0], yDomain[1]]).map(tickFmt)
  if (!hasData) {
    return <div className="flex items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted" style={{ height }}>{emptyText}</div>
  }
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} syncId={syncId} margin={{ top: 6, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border) / 0.4)" vertical={false} />
          <XAxis dataKey="ts" type="number" scale="time" domain={[fromTs, toTs]} ticks={ticks} interval="preserveStartEnd" minTickGap={16}
            tickFormatter={tickFormatter} tick={{ fontSize: chartFont(12), fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
          <YAxis domain={yDomain} allowDataOverflow ticks={yTicks} tickCount={yTicks ? undefined : 4} interval={0} width={axisWidth(yLabels)}
            tick={{ fontSize: chartFont(12), fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false}
            tickFormatter={(v: number) => tickFmt(Number(v))} />
          <Tooltip
            contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 6, fontSize: chartFont(12), color: 'rgb(var(--text))' }}
            labelFormatter={(ts) => formatAbsolute(Number(ts))}
            formatter={(v: unknown, name: string) => [v == null ? MISSING : format(Number(v)), name]}
          />
          {thresholds.map((t) => (
            <ReferenceLine key={`${t.label}-${t.value}`} y={t.value} ifOverflow="extendDomain"
              stroke={t.tone === 'danger' ? SERIES_COLORS.danger : SERIES_COLORS.warning} strokeDasharray="4 4"
              label={{ value: t.label, position: t.tone === 'danger' ? 'insideTopRight' : 'insideBottomRight', fontSize: chartFont(12), fill: 'rgb(var(--muted))' }} />
          ))}
          {series.map((s) => (
            <Line key={s.key} type="monotone" dataKey={s.key} name={s.name} stroke={s.color} strokeWidth={s.dashed ? 1.4 : 1.8}
              strokeDasharray={s.dashed ? '5 4' : undefined} strokeOpacity={s.dashed ? 0.65 : 1}
              dot={s.points.length === 1} connectNulls={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function Legend({ items }: { items: Array<{ name: string; color: string; dashed?: boolean }> }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
      {items.map((i) => (
        <span key={i.name} className="inline-flex items-center gap-1.5">
          <span className="h-0.5 w-3 rounded" style={i.dashed ? { borderTop: `2px dashed ${i.color}`, opacity: 0.7 } : { background: i.color }} aria-hidden="true" />{i.name}
        </span>
      ))}
    </div>
  )
}

export function StatsRow({ stats, format }: { stats: Stats | null; format: (v: number) => string }) {
  const cells: Array<[string, number | undefined]> = [['Min', stats?.min], ['Avg', stats?.avg], ['p95', stats?.p95], ['Max', stats?.max]]
  return (
    <dl className="grid grid-cols-4 gap-2 text-xs">
      {cells.map(([k, v]) => (
        <div key={k}><dt className="text-muted">{k}</dt><dd className="font-medium tabular-nums">{v == null ? MISSING : format(v)}</dd></div>
      ))}
    </dl>
  )
}

/** Card frame used by every panel on the device page: title, optional meta and actions. */
export function Panel({ title, meta, actions, children, className, id }: {
  title: ReactNode; meta?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; id?: string
}) {
  return (
    <section id={id} className={cn('flex h-full min-w-0 flex-col rounded-xl border border-border bg-surface p-4', className)}>
      <header className="mb-3 flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">{title}</h3>
          {meta && <div className="mt-0.5 text-xs text-muted">{meta}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2 text-xs">{actions}</div>}
      </header>
      <div className="min-h-0 flex-1">{children}</div>
    </section>
  )
}
