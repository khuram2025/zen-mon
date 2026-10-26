/** Design primitives for the server detail page.
 *
 *  Every tab builds from these so the page reads as one product: panels with
 *  a consistent header, stat tiles with sparklines, meters with shared
 *  thresholds, and time-series charts that share a crosshair (hovering one
 *  chart marks the same moment on every other chart of the page). */

import type { ComponentType, ReactNode } from 'react'
import {
  Area, AreaChart, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { Inbox } from 'lucide-react'
import { cn, timeTooltipLabelFormatter } from '@/lib/utils'
import { chartFont } from '@/lib/chartTheme'

type Icon = ComponentType<{ className?: string }>
export type Tone = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'muted'

/* ── Colour ──────────────────────────────────────────────────────── */

/** Series colours, from the theme so light and dark both work. */
export const C = {
  blue: 'rgb(var(--primary))',
  violet: 'rgb(var(--accent))',
  sky: 'rgb(var(--info))',
  green: 'rgb(var(--success))',
  orange: 'rgb(var(--warning))',
  red: 'rgb(var(--danger))',
  teal: 'rgb(45 212 191)',
  slate: 'rgb(var(--muted))',
} as const

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-text',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  info: 'text-info',
  muted: 'text-muted',
}

const TONE_SOFT: Record<Tone, string> = {
  default: 'bg-primary/10 text-primary',
  success: 'bg-success/10 text-success',
  warning: 'bg-warning/10 text-warning',
  danger: 'bg-danger/10 text-danger',
  info: 'bg-info/10 text-info',
  muted: 'bg-surface2 text-muted',
}

/** Threshold tone for a utilisation percentage. */
export function pctTone(pct: number | null | undefined, warn = 80, crit = 90): Tone {
  if (pct == null || Number.isNaN(pct)) return 'muted'
  return pct >= crit ? 'danger' : pct >= warn ? 'warning' : 'default'
}

/* ── Layout ──────────────────────────────────────────────────────── */

/** The card every section sits in. */
export function Panel({
  title, subtitle, icon: IconC, actions, children, className, bodyClassName, flush, id,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  icon?: Icon
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  /** No body padding, for tables and edge-to-edge content. */
  flush?: boolean
  id?: string
}) {
  return (
    <section
      id={id}
      className={cn(
        'scroll-mt-28 overflow-hidden rounded-xl border border-border bg-surface shadow-card dark:shadow-card-dark',
        className,
      )}
    >
      {(title || actions) && (
        <header className="flex min-h-[48px] flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-border/70 px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            {IconC && (
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface2 text-muted">
                <IconC className="h-3.5 w-3.5" />
              </span>
            )}
            <div className="min-w-0">
              {title && <h3 className="truncate text-sm font-semibold leading-5 text-text">{title}</h3>}
              {subtitle && <p className="truncate text-xs leading-4 text-muted">{subtitle}</p>}
            </div>
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn(!flush && 'p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

/** A heading between groups of panels, anchored for in-page navigation. */
export function SectionHeading({ id, title, hint, actions }: {
  id?: string
  title: ReactNode
  hint?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div id={id} className="flex scroll-mt-28 flex-wrap items-end justify-between gap-2 pt-2">
      <div>
        <h2 className="text-base font-semibold tracking-tight text-text">{title}</h2>
        {hint && <p className="text-xs text-muted">{hint}</p>}
      </div>
      {actions}
    </div>
  )
}

/** Segmented control for sections inside a tab. */
export function Segmented<K extends string>({ items, value, onChange, size = 'md' }: {
  items: readonly { key: K; label: ReactNode; icon?: Icon; count?: number; tone?: Tone }[]
  value: K
  onChange: (k: K) => void
  size?: 'sm' | 'md'
}) {
  return (
    <div role="tablist" className="inline-flex flex-wrap gap-0.5 rounded-lg border border-border bg-surface2/50 p-0.5">
      {items.map((it) => {
        const active = it.key === value
        return (
          <button
            key={it.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(it.key)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md font-medium transition-colors',
              size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3 py-1.5 text-sm',
              active ? 'bg-surface text-text shadow-sm ring-1 ring-border' : 'text-muted hover:text-text',
            )}
          >
            {it.icon && <it.icon className="h-3.5 w-3.5" />}
            {it.label}
            {!!it.count && <CountBadge count={it.count} tone={it.tone ?? 'muted'} />}
          </button>
        )
      })}
    </div>
  )
}

export function CountBadge({ count, tone = 'muted' }: { count: number; tone?: Tone }) {
  if (!count) return null
  return (
    <span className={cn('rounded-full px-1.5 py-px text-2xs font-semibold leading-4 tabular-nums', TONE_SOFT[tone])}>
      {count > 99 ? '99+' : count}
    </span>
  )
}

/* ── Values ──────────────────────────────────────────────────────── */

/** Status pill with a dot, for health and state. */
export function Pill({ tone = 'muted', children, title, pulse }: {
  tone?: Tone
  children: ReactNode
  title?: string
  pulse?: boolean
}) {
  return (
    <span
      title={title}
      className={cn('inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium', TONE_SOFT[tone])}
    >
      <span className="relative flex h-1.5 w-1.5">
        {pulse && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />}
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
      </span>
      {children}
    </span>
  )
}

/** Horizontal usage meter with shared thresholds. */
export function Meter({ pct, warn = 80, crit = 90, className, thin }: {
  pct: number | null | undefined
  warn?: number
  crit?: number
  className?: string
  thin?: boolean
}) {
  const v = pct == null || Number.isNaN(pct) ? 0 : Math.max(0, Math.min(100, pct))
  const tone = pctTone(pct, warn, crit)
  return (
    <div className={cn('overflow-hidden rounded-full bg-surface2', thin ? 'h-1' : 'h-1.5', className)}>
      <div
        className={cn(
          'h-full rounded-full transition-[width] duration-500',
          tone === 'danger' ? 'bg-danger' : tone === 'warning' ? 'bg-warning' : 'bg-primary',
        )}
        style={{ width: `${v}%` }}
      />
    </div>
  )
}

/** Label/value pairs in a responsive grid. */
export function KeyValues({ items, columns = 2, className }: {
  items: (readonly [ReactNode, ReactNode] | null | false | undefined)[]
  columns?: 1 | 2 | 3 | 4
  className?: string
}) {
  const cols = { 1: 'grid-cols-1', 2: 'sm:grid-cols-2', 3: 'sm:grid-cols-2 lg:grid-cols-3', 4: 'sm:grid-cols-2 lg:grid-cols-4' }[columns]
  return (
    <dl className={cn('grid gap-x-6 gap-y-3', cols, className)}>
      {items.filter(Boolean).map((row, i) => {
        const [label, value] = row as readonly [ReactNode, ReactNode]
        return (
          <div key={i} className="min-w-0">
            <dt className="text-xs font-medium uppercase tracking-wide text-muted">{label}</dt>
            <dd className="mt-0.5 truncate text-sm text-text">{value ?? <span className="text-muted">—</span>}</dd>
          </div>
        )
      })}
    </dl>
  )
}

/** Headline number with label, context line, optional meter and sparkline. */
export function StatTile({
  icon: IconC, label, value, unit, sub, tone = 'default', pct, spark, sparkColor = C.blue, onClick, active,
}: {
  icon?: Icon
  label: string
  value: ReactNode
  unit?: string
  sub?: ReactNode
  tone?: Tone
  pct?: number | null
  spark?: (number | null)[]
  sparkColor?: string
  onClick?: () => void
  active?: boolean
}) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={cn(
        'group relative flex min-w-0 flex-col gap-1 overflow-hidden rounded-xl border border-border bg-surface p-3.5 text-left transition-colors',
        onClick && 'hover:border-border-strong hover:bg-surface2/40',
        active && 'border-primary/50 ring-1 ring-primary/30',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
          {IconC && <IconC className="h-3.5 w-3.5" />}
          {label}
        </span>
      </div>
      <div className={cn('flex items-baseline gap-1 text-2xl font-semibold leading-7 tabular-nums', TONE_TEXT[tone === 'default' ? 'default' : tone])}>
        {value}
        {unit && <span className="text-sm font-medium text-muted">{unit}</span>}
      </div>
      {pct !== undefined && <Meter pct={pct} thin className="mt-0.5" />}
      {sub && <div className="truncate text-xs text-muted">{sub}</div>}
      {spark && spark.some((v) => v != null) && (
        <div className="-mx-3.5 -mb-3.5 mt-1 h-9 opacity-80 transition-opacity group-hover:opacity-100">
          <Sparkline data={spark} color={sparkColor} />
        </div>
      )}
    </Tag>
  )
}

/** Axis-free trend line. */
export function Sparkline({ data, color = C.blue, height = 36 }: { data: (number | null)[]; color?: string; height?: number }) {
  const rows = data.map((v, i) => ({ i, v }))
  const gid = `spark-${color.replace(/[^a-z0-9]/gi, '')}`
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={rows} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <YAxis hide domain={[0, 'auto']} />
        <Area
          type="monotone" dataKey="v" stroke={color} strokeWidth={1.5} fill={`url(#${gid})`}
          isAnimationActive={false} connectNulls dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}

/** Empty or unavailable content, sized to sit inside a panel. */
export function EmptyState({ icon: IconC = Inbox, title, hint, action, compact }: {
  icon?: Icon
  title: ReactNode
  hint?: ReactNode
  action?: ReactNode
  compact?: boolean
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-2 text-center', compact ? 'py-6' : 'py-12')}>
      <span className="flex h-10 w-10 items-center justify-center rounded-full bg-surface2 text-muted">
        <IconC className="h-5 w-5" />
      </span>
      <div className="text-sm font-medium text-text">{title}</div>
      {hint && <div className="max-w-md text-xs text-muted">{hint}</div>}
      {action}
    </div>
  )
}

/* ── Charts ──────────────────────────────────────────────────────── */

/** Shared by every time-series chart so their crosshairs move together. */
export const SYNC_ID = 'server-detail'

export interface SeriesDef {
  key: string
  label: string
  color: string
  /** Stacked series share a stack id. */
  stack?: string
  /** Lines are drawn without a fill. */
  line?: boolean
  dashed?: boolean
}

function tooltipStyle() {
  return {
    contentStyle: {
      backgroundColor: 'rgb(var(--surface))',
      border: '1px solid rgb(var(--border-strong))',
      borderRadius: 10,
      boxShadow: '0 10px 30px rgb(0 0 0 / 0.25)',
      color: 'rgb(var(--text))',
      fontSize: chartFont(12),
      padding: '8px 10px',
    },
    itemStyle: { paddingTop: 1, paddingBottom: 1 },
    labelStyle: { color: 'rgb(var(--muted))', marginBottom: 4 },
    labelFormatter: timeTooltipLabelFormatter,
    cursor: { stroke: 'rgb(var(--muted))', strokeDasharray: '3 3', strokeWidth: 1 },
  }
}

/** Area/line time series over epoch-ms rows with a `ts` column. */
export function TimeSeries({
  data, series, height = 220, yFormat, tickFormat, yDomain, references, legend = true,
}: {
  data: Record<string, number | null>[]
  series: SeriesDef[]
  height?: number
  yFormat: (v: number) => string
  tickFormat: (ts: number) => string
  yDomain?: [number | 'auto' | 'dataMin' | 'dataMax', number | 'auto' | 'dataMin' | 'dataMax']
  references?: { y: number; label: string; tone: 'warning' | 'danger' }[]
  legend?: boolean
}) {
  const tt = tooltipStyle()
  return (
    <div>
      {legend && series.length > 1 && (
        <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1">
          {series.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5 text-xs text-muted">
              <span className="h-2 w-2 rounded-sm" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
      <ResponsiveContainer width="100%" height={height}>
        {/* ComposedChart, not AreaChart: an AreaChart silently drops Line children. */}
        <ComposedChart data={data} syncId={SYNC_ID} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <defs>
            {series.map((s) => (
              <linearGradient key={s.key} id={`fill-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={s.stack ? 0.5 : 0.28} />
                <stop offset="100%" stopColor={s.color} stopOpacity={s.stack ? 0.15 : 0} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']}
            tickFormatter={tickFormat} tick={{ fontSize: chartFont(11), fill: 'rgb(var(--muted))' }}
            tickLine={false} axisLine={false} minTickGap={48}
          />
          <YAxis
            tickFormatter={yFormat} tick={{ fontSize: chartFont(11), fill: 'rgb(var(--muted))' }}
            tickLine={false} axisLine={false} width={56} domain={yDomain ?? [0, 'auto']}
          />
          <Tooltip {...tt} formatter={(v: number | string, name: string) => [typeof v === 'number' ? yFormat(v) : v, name]} />
          {references?.map((r) => (
            <ReferenceLine
              key={r.label} y={r.y} stroke={r.tone === 'danger' ? C.red : C.orange} strokeDasharray="4 4" strokeOpacity={0.7}
              label={{ value: r.label, position: 'insideTopRight', fontSize: chartFont(10), fill: r.tone === 'danger' ? C.red : C.orange }}
            />
          ))}
          {series.map((s) =>
            s.line ? (
              <Line
                key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color}
                strokeWidth={1.5} strokeDasharray={s.dashed ? '4 3' : undefined}
                dot={false} isAnimationActive={false} connectNulls
              />
            ) : (
              <Area
                key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color} strokeWidth={1.5}
                fill={`url(#fill-${s.key})`} stackId={s.stack} dot={false} activeDot={{ r: 3 }}
                isAnimationActive={false} connectNulls
              />
            ),
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Current / average / peak figures shown above a chart. */
export function ChartStats({ items }: { items: { label: string; value: ReactNode; tone?: Tone }[] }) {
  return (
    <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
      {items.map((it) => (
        <div key={it.label}>
          <div className="text-xs font-medium uppercase tracking-wide text-muted">{it.label}</div>
          <div className={cn('text-sm font-semibold tabular-nums', TONE_TEXT[it.tone ?? 'default'])}>{it.value}</div>
        </div>
      ))}
    </div>
  )
}

/* ── Numbers ─────────────────────────────────────────────────────── */

export function fmtPct(v: number | null | undefined, digits = 1): string {
  return v == null || Number.isNaN(v) ? '—' : `${v.toFixed(digits)}%`
}

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(digits)}M`
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(digits)}k`
  return v.toFixed(v % 1 === 0 ? 0 : digits)
}

export function fmtMs(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  // Fast disks answer in hundredths of a millisecond.
  if (v >= 100) return `${v.toFixed(0)} ms`
  return v >= 1 ? `${v.toFixed(1)} ms` : `${v.toFixed(2)} ms`
}

/** Series statistics over non-null values. */
export function stats(values: (number | null | undefined)[]) {
  const vals = values.filter((v): v is number => v != null && !Number.isNaN(v))
  if (!vals.length) return null
  const sorted = [...vals].sort((a, b) => a - b)
  return {
    current: vals[vals.length - 1],
    avg: vals.reduce((a, b) => a + b, 0) / vals.length,
    peak: sorted[sorted.length - 1],
    p95: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))],
  }
}
