import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { chartFont, seriesVar } from '@/lib/chartTheme'
import { regionalDate } from '@/lib/regionalTime'
import { Badge } from '@/components/ui/Badge'
import { Card } from '@/components/ui/Card'
import { KPI_LABEL, kpiTone, type KpiTone } from '@/components/ui/kpiTone'
import { SortableTh } from '@/components/ui/SortableTh'
import { chartTooltipStyle } from '@/pages/dashboards/shared'
import {
  healthTone,
  isOff,
  isOn,
  metricLabel,
  METRICS,
  metricValue,
  powerLabel,
  severityTone,
  type HistoryPoint,
} from './model'

export function ProviderLink({ url, children, className }: { url?: string | null; children: React.ReactNode; className?: string }) {
  if (!url || !url.startsWith('https://')) return null
  return (
    <a
      className={cn('inline-flex items-center gap-1 text-primary hover:underline', className)}
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
    >
      {children}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  )
}

export function HealthBadge({ health, stale }: { health: string; stale?: boolean }) {
  if (stale) return <Badge variant="warning">Stale</Badge>
  if (!health || health === 'unknown') return <span className="text-xs text-muted">No health data</span>
  return <Badge variant={healthTone(health)} className="capitalize">{health}</Badge>
}

export function PowerBadge({ power }: { power?: string | null }) {
  if (!power) return <span className="text-muted">—</span>
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      <span
        aria-hidden="true"
        className={cn('h-2 w-2 rounded-full', isOn(power) ? 'bg-success' : isOff(power) ? 'bg-border-strong' : 'bg-warning')}
      />
      {powerLabel(power)}
    </span>
  )
}

export function SeverityBadge({ severity, resolved }: { severity: string; resolved?: boolean }) {
  if (resolved) return <Badge variant="outline">Resolved</Badge>
  return <Badge variant={severityTone(severity)} className="capitalize">{severity || 'unknown'}</Badge>
}

/** Utilization meter with a value label; status colours only cross thresholds. */
export function Meter({ value, detail, warnAt = 75, dangerAt = 90, className }: {
  value?: number
  detail?: React.ReactNode
  warnAt?: number
  dangerAt?: number
  className?: string
}) {
  if (value === undefined) {
    return (
      <div className={cn('min-w-[7rem]', className)}>
        <span className="text-muted">—</span>
        {detail && <div className="text-xs text-muted">{detail}</div>}
      </div>
    )
  }
  const v = Math.max(0, Math.min(100, value))
  const tone = v >= dangerAt ? 'bg-danger' : v >= warnAt ? 'bg-warning' : 'bg-primary'
  return (
    <div className={cn('min-w-[7rem]', className)}>
      <div className="flex items-baseline justify-between gap-2 tabular-nums">
        <span className="font-medium">{value.toFixed(1)}%</span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface2" role="meter" aria-valuenow={Math.round(v)} aria-valuemin={0} aria-valuemax={100}>
        <div className={cn('h-full rounded-full', tone)} style={{ width: `${v}%` }} />
      </div>
      {detail && <div className="mt-0.5 truncate text-xs text-muted">{detail}</div>}
    </div>
  )
}

export function Tile({ label, value, sub, icon, tone = 'primary', onClick }: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  icon?: React.ReactNode
  tone?: KpiTone
  onClick?: () => void
}) {
  const Comp = onClick ? 'button' : 'div'
  return (
    <Card className={cn('relative overflow-hidden', onClick && 'transition hover:border-primary/40')}>
      <Comp type={onClick ? 'button' : undefined} onClick={onClick} className="block w-full text-left">
        <span className={`absolute inset-x-0 top-0 h-[3px] ${kpiTone(tone).bar}`} />
        <div className="flex items-start justify-between gap-2 px-4 pb-3 pt-3.5">
          <div className="min-w-0">
            <div className={KPI_LABEL}>{label}</div>
            <div className="mt-1.5 whitespace-nowrap text-2xl font-semibold leading-none tabular-nums tracking-tight">{value}</div>
            <div className="mt-1.5 min-h-[16px] text-xs text-muted">{sub}</div>
          </div>
          {icon && (
            <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${kpiTone(tone).chip}`}>{icon}</span>
          )}
        </div>
      </Comp>
    </Card>
  )
}

export function Panel({ title, hint, right, children, className }: {
  title: string
  hint?: React.ReactNode
  right?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <Card className={cn('flex flex-col', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
          {hint && <p className="text-xs text-muted">{hint}</p>}
        </div>
        {right}
      </div>
      <div className="flex-1 p-4">{children}</div>
    </Card>
  )
}

export const HISTORY_RANGES = [
  { key: '1h', label: '1H', hours: 1 },
  { key: '6h', label: '6H', hours: 6 },
  { key: '24h', label: '24H', hours: 24 },
  { key: '7d', label: '7D', hours: 168 },
  { key: '30d', label: '30D', hours: 720 },
] as const
export type HistoryRange = (typeof HISTORY_RANGES)[number]['key']

export function RangeSwitch({ value, onChange }: { value: HistoryRange; onChange: (v: HistoryRange) => void }) {
  return (
    <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5" role="group" aria-label="History range">
      {HISTORY_RANGES.map((r) => (
        <button
          key={r.key}
          type="button"
          aria-pressed={r.key === value}
          onClick={() => onChange(r.key)}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium transition-colors',
            r.key === value ? 'bg-primary text-on-primary shadow-sm' : 'text-text2 hover:bg-surface3 hover:text-text',
          )}
        >
          {r.label}
        </button>
      ))}
    </div>
  )
}

export function useHistory(connectorId: string, resourceId: string | undefined, range: HistoryRange) {
  const hours = HISTORY_RANGES.find((r) => r.key === range)?.hours ?? 24
  return useQuery({
    queryKey: ['virtualization', connectorId, resourceId, 'history', hours],
    queryFn: () =>
      api
        .get<HistoryPoint[]>(`/virtualization/connectors/${connectorId}/metrics`, {
          params: { resource_id: resourceId, hours },
        })
        .then((r) => r.data),
    enabled: !!connectorId && !!resourceId,
    refetchInterval: 60000,
  })
}

type SeriesDef = { metric: string; label?: string }

/** Time-series area chart for one or more metrics of the same unit from a resource's history. */
export function HistoryChart({ data, series, hours, height = 200 }: {
  data: HistoryPoint[]
  series: SeriesDef[]
  hours: number
  height?: number
}) {
  const rows = useMemo(() => {
    const byTime = new Map<number, Record<string, number>>()
    const wanted = new Set(series.map((s) => s.metric))
    for (const p of data) {
      if (!wanted.has(p.metric)) continue
      const t = new Date(p.time.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(p.time) ? p.time : `${p.time}Z`).getTime()
      const row = byTime.get(t) ?? { t }
      row[p.metric] = p.value
      byTime.set(t, row)
    }
    return [...byTime.values()].sort((a, b) => a.t - b.t)
  }, [data, series])
  const first = series[0]?.metric ?? ''
  const spec = METRICS[first]
  const peak = Math.max(0, ...rows.flatMap((row) => series.map((s) => row[s.metric] ?? 0)))
  const pctMax = [4, 10, 20, 40, 60, 80, 100].find((m) => m >= peak * 1.1) ?? 100
  const tick = (t: number) =>
    regionalDate(new Date(t), hours > 24 ? 'datetime' : 'time', [], hours > 24
      ? { month: 'short', day: 'numeric', hour: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' })
  if (!rows.length) return <div className="flex items-center justify-center text-sm text-muted" style={{ height }}>No samples in this range.</div>
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <defs>
            {series.map((s, i) => (
              <linearGradient key={s.metric} id={`vz-grad-${s.metric}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={seriesVar(i)} stopOpacity={0.3} />
                <stop offset="100%" stopColor={seriesVar(i)} stopOpacity={0.02} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" opacity={0.15} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tick}
            tick={{ fontSize: chartFont(11) }} minTickGap={40} />
          <YAxis
            width={64}
            tick={{ fontSize: chartFont(11) }}
            domain={spec?.percent ? [0, pctMax] : [0, 'auto']}
            ticks={spec?.percent ? [0, pctMax / 4, pctMax / 2, (pctMax * 3) / 4, pctMax] : undefined}
            tickFormatter={(v: number) => (spec?.axis ? spec.axis(v) : spec?.percent ? `${+v.toFixed(1)}%` : v.toLocaleString(undefined, { maximumFractionDigits: 1 }))}
          />
          <Tooltip
            contentStyle={chartTooltipStyle}
            labelFormatter={(t) => regionalDate(new Date(Number(t)), 'datetime')}
            formatter={(v: number, key: string) => [metricValue(key, v), series.find((s) => s.metric === key)?.label ?? metricLabel(key)]}
          />
          {series.map((s, i) => (
            <Area
              key={s.metric}
              type="monotone"
              dataKey={s.metric}
              name={s.label ?? metricLabel(s.metric)}
              stroke={seriesVar(i)}
              strokeWidth={2}
              fill={`url(#vz-grad-${s.metric})`}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

export function HistoryState({ query, children }: { query: { isLoading: boolean; error: unknown }; children: React.ReactNode }) {
  if (query.error) return <p className="text-sm text-danger">{apiErrorMessage(query.error)}</p>
  if (query.isLoading) return <div className="h-[200px] animate-pulse rounded-md bg-surface2" />
  return <>{children}</>
}

export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="flex flex-wrap gap-3 text-xs text-text2">
      {items.map((i) => (
        <span key={i.label} className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: i.color }} />
          {i.label}
        </span>
      ))}
    </div>
  )
}

/* ── Sortable tables ────────────────────────────────────────────────────── */

export type Column<T> = {
  key: string
  label: string
  sort?: (row: T) => number | string | undefined
  render: (row: T) => React.ReactNode
  className?: string
}

export function DataTable<T extends { id: string }>({ rows, columns, onRowClick, empty, initialSort, pageSize = 50 }: {
  rows: T[]
  columns: Column<T>[]
  onRowClick?: (row: T) => void
  empty: string
  initialSort?: { key: string; order: 'asc' | 'desc' }
  pageSize?: number
}) {
  const [sort, setSort] = useState(initialSort?.key ?? '')
  const [order, setOrder] = useState<'asc' | 'desc'>(initialSort?.order ?? 'asc')
  const [page, setPage] = useState(0)
  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort)
    if (!col?.sort) return rows
    const get = col.sort
    return [...rows].sort((a, b) => {
      const x = get(a)
      const y = get(b)
      if (x === undefined && y === undefined) return 0
      if (x === undefined) return 1 // unknown values always last
      if (y === undefined) return -1
      const c = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })
      return order === 'asc' ? c : -c
    })
  }, [rows, columns, sort, order])
  const pages = Math.max(1, Math.ceil(sorted.length / pageSize))
  const current = Math.min(page, pages - 1)
  const onSort = (key: string) => {
    if (sort === key) setOrder((o) => (o === 'asc' ? 'desc' : 'asc'))
    else {
      setSort(key)
      setOrder(key === 'name' ? 'asc' : 'desc')
    }
    setPage(0)
  }
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-y border-border bg-surface2/60">
            <tr>
              {columns.map((c) =>
                c.sort ? (
                  <SortableTh key={c.key} column={c.key} label={c.label} sort={sort} order={order} onSort={onSort} />
                ) : (
                  <th key={c.key} className="h-10 whitespace-nowrap px-3 text-2xs font-semibold uppercase tracking-wider text-muted">{c.label}</th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {sorted.slice(current * pageSize, (current + 1) * pageSize).map((row) => (
              <tr
                key={row.id}
                className={cn('border-b border-border/70 transition-colors hover:bg-surface2/60', onRowClick && 'cursor-pointer')}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((c) => (
                  <td key={c.key} className={cn('px-3 py-2.5 align-middle', c.className)}>{c.render(row)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!rows.length && <p className="p-6 text-center text-sm text-muted">{empty}</p>}
      {rows.length > pageSize && (
        <div className="flex items-center justify-between p-3 text-sm text-muted">
          <span>
            {rows.length.toLocaleString()} rows · page {current + 1} of {pages}
          </span>
          <div className="flex gap-2">
            <button type="button" className="rounded-md border border-border px-2.5 py-1 disabled:opacity-50" disabled={current === 0} onClick={() => setPage(current - 1)}>
              Previous
            </button>
            <button type="button" className="rounded-md border border-border px-2.5 py-1 disabled:opacity-50" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export const selectStyle = 'h-9 rounded-md border border-border bg-surface px-3 text-sm'
