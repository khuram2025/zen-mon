/**
 * Shared APM visualization primitives: theme-aware charts, KPI cards, and
 * metric cells. Pages compose these; they do not invent their own palettes.
 */
import { type ReactNode, useMemo } from 'react'
import { Link } from 'react-router-dom'
import ReactECharts from 'echarts-for-react'
import { GitBranch, Bug, ArrowRight } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { cn } from '@/lib/utils'
import { useTheme } from '@/stores/theme'
import { HEALTH_COLOR, fmtMs, fmtPct, fmtRps } from './shared'

export const APM_SERIES = {
  throughput: '#0284c7',
  latency: '#7c3aed',
  latencyP50: '#6366f1',
  errors: '#db2777',
  apdex: '#047857',
  users: '#0891b2',
  requests: '#0284c7',
} as const

const KPI_BAR: Record<string, string> = {
  success: 'from-emerald-500/80 to-green-500/80',
  danger: 'from-rose-500/80 to-red-500/80',
  warning: 'from-amber-500/80 to-orange-500/80',
  info: 'from-sky-500/80 to-cyan-500/80',
  accent: 'from-violet-500/80 to-indigo-500/80',
  primary: 'from-blue-500/80 to-indigo-500/80',
  muted: 'from-slate-400/70 to-slate-500/70',
}

export type ApmTone = keyof typeof KPI_BAR

export function errorTone(rate: number): ApmTone {
  if (rate >= 0.05) return 'danger'
  if (rate >= 0.01) return 'warning'
  return 'success'
}

export function latencyTone(ms: number): ApmTone {
  if (ms >= 2000) return 'danger'
  if (ms >= 800) return 'warning'
  return 'success'
}

export function apdexTone(v: number): ApmTone {
  if (v >= 0.94) return 'success'
  if (v >= 0.85) return 'info'
  if (v >= 0.7) return 'warning'
  return 'danger'
}

export function healthTone(health: string): ApmTone {
  if (health === 'critical') return 'danger'
  if (health === 'degraded') return 'warning'
  if (health === 'healthy') return 'success'
  return 'muted'
}

export function fmtCount(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}K`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return `${Math.round(n)}`
}

function cssRgb(token: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--${token}`).trim()
  return v ? `rgb(${v})` : fallback
}

function hexAlpha(hex: string, a: number): string {
  const n = hex.replace('#', '')
  const r = parseInt(n.slice(0, 2), 16)
  const g = parseInt(n.slice(2, 4), 16)
  const b = parseInt(n.slice(4, 6), 16)
  return `rgba(${r},${g},${b},${a})`
}

function spanMs(times: Array<string | number>): number {
  if (times.length < 2) return 0
  const a = new Date(times[0]).getTime()
  const b = new Date(times[times.length - 1]).getTime()
  return Math.abs(b - a)
}

export function formatChartTick(t: string | number, windowMs: number): string {
  const d = new Date(t)
  if (Number.isNaN(d.getTime())) return ''
  if (windowMs >= 14 * 86_400_000) {
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }
  if (windowMs >= 3 * 86_400_000) {
    // Multi-day windows with sub-day buckets: label midnight ticks with the
    // date and intraday ticks with the time, so consecutive ticks never repeat.
    if (d.getHours() === 0 && d.getMinutes() === 0) return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  if (windowMs >= 12 * 3_600_000) {
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export interface ApmChartSeries {
  key: string
  name: string
  color: string
  type?: 'line' | 'bar'
  yAxisIndex?: 0 | 1
  fmt?: (v: number) => string
  dashed?: boolean
  area?: boolean
}

export interface ApmChartMarker {
  timestamp: string
  label: string
}

export interface ApmTimeChartProps {
  data: ReadonlyArray<Record<string, any>>
  timeKey?: string
  series: ApmChartSeries[]
  height?: number
  loading?: boolean
  empty?: string
  onPointClick?: (timestamp: string) => void
  markers?: ApmChartMarker[]
}

export function ApmTimeChart({
  data,
  timeKey = 'timestamp',
  series,
  height = 248,
  loading,
  empty = 'No telemetry in this window',
  onPointClick,
  markers,
}: ApmTimeChartProps) {
  const theme = useTheme((s) => s.theme)

  const option = useMemo(() => {
    const muted = cssRgb('muted', '#94a3b8')
    const border = cssRgb('border', '#1e293b')
    const surface = cssRgb('surface', '#0d121b')
    const text = cssRgb('text', '#e5e7eb')
    const times = data.map((d) => String(d[timeKey] ?? ''))
    const window = spanMs(times)
    const dual = series.some((s) => s.yAxisIndex === 1)
    const showZoom = data.length > 48 || window >= 24 * 3_600_000

    const yAxes = [
      {
        type: 'value' as const,
        axisLabel: {
          color: muted,
          fontSize: 11,
          formatter: (v: number) => (series[0]?.fmt ? series[0].fmt(v) : String(v)),
        },
        splitLine: { lineStyle: { color: border, opacity: 0.55, type: 'dashed' as const } },
        axisLine: { show: false },
        axisTick: { show: false },
        min: 0,
      },
      ...(dual
        ? [{
            type: 'value' as const,
            axisLabel: {
              color: muted,
              fontSize: 11,
              formatter: (v: number) => {
                const s = series.find((x) => x.yAxisIndex === 1)
                return s?.fmt ? s.fmt(v) : String(v)
              },
            },
            splitLine: { show: false },
            axisLine: { show: false },
            axisTick: { show: false },
            min: 0,
          }]
        : []),
    ]

    return {
      backgroundColor: 'transparent',
      animation: data.length <= 400,
      grid: { top: series.length > 1 ? 32 : 16, right: dual ? 52 : 16, bottom: showZoom ? 52 : 28, left: 54, containLabel: false },
      tooltip: {
        trigger: 'axis',
        backgroundColor: surface,
        borderColor: border,
        borderWidth: 1,
        textStyle: { color: text, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: muted, opacity: 0.4 } },
        formatter: (params: Array<{ axisValue: string; marker: string; seriesName: string; value: [string, number]; seriesIndex: number }>) => {
          const ts = params[0]?.axisValue
          const label = ts ? new Date(ts).toLocaleString() : ''
          let html = `<div style="font-size:11px;color:${muted};margin-bottom:6px">${label}</div>`
          params.forEach((p) => {
            const raw = Array.isArray(p.value) ? p.value[1] : p.value
            if (raw == null) return
            const fmt = series[p.seriesIndex]?.fmt
            const shown = fmt ? fmt(Number(raw)) : Number(raw).toLocaleString()
            html += `<div style="display:flex;justify-content:space-between;gap:16px">${p.marker}<span>${p.seriesName}</span><b style="margin-left:12px">${shown}</b></div>`
          })
          return html
        },
      },
      legend: series.length > 1
        ? { top: 0, right: 8, textStyle: { color: muted, fontSize: 11 }, icon: 'roundRect', itemWidth: 10, itemHeight: 6 }
        : undefined,
      xAxis: {
        type: 'time',
        axisLabel: {
          color: muted,
          fontSize: 10,
          hideOverlap: true,
          formatter: (val: number) => formatChartTick(val, window),
        },
        axisLine: { lineStyle: { color: border } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: yAxes,
      dataZoom: showZoom
        ? [
            { type: 'inside', xAxisIndex: 0, zoomOnMouseWheel: true, moveOnMouseMove: true },
            {
              type: 'slider',
              height: 18,
              bottom: 6,
              borderColor: border,
              backgroundColor: cssRgb('surface2', '#141b28'),
              fillerColor: hexAlpha(series[0]?.color || APM_SERIES.throughput, 0.18),
              handleStyle: { color: series[0]?.color || APM_SERIES.throughput },
              textStyle: { color: muted, fontSize: 10 },
              start: window >= 7 * 86_400_000 ? 50 : 0,
              end: 100,
            },
          ]
        : undefined,
      series: series.map((s, i) => ({
        name: s.name,
        type: s.type || 'line',
        yAxisIndex: s.yAxisIndex ?? 0,
        showSymbol: false,
        smooth: 0.2,
        sampling: data.length > 400 ? 'lttb' : undefined,
        lineStyle: { width: s.dashed ? 1.5 : 2.25, color: s.color, type: s.dashed ? 'dashed' : 'solid' },
        itemStyle: { color: s.color },
        areaStyle: s.type === 'bar' || s.area === false || s.dashed ? undefined : {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: hexAlpha(s.color, 0.32) },
              { offset: 1, color: hexAlpha(s.color, 0) },
            ],
          },
        },
        data: data.map((d) => [d[timeKey], d[s.key] == null ? null : Number(d[s.key])]),
        markLine: i === 0 && markers?.length
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.25 },
              label: { color: muted, fontSize: 10, formatter: (p: { name?: string }) => p.name || '' },
              data: markers.map((m) => ({ xAxis: m.timestamp, name: m.label })),
            }
          : undefined,
      })),
    }
  }, [data, series, theme, timeKey, markers])

  if (loading) {
    return <div className="flex h-full items-center justify-center text-xs text-muted" style={{ height }}>Loading trend…</div>
  }
  if (!data.length) {
    return <div className="flex h-full items-center justify-center text-xs text-muted" style={{ height }}>{empty}</div>
  }

  return (
    <ReactECharts
      option={option}
      style={{ height }}
      notMerge
      onEvents={onPointClick ? {
        click: (p: { value?: [string, number] }) => {
          const ts = p?.value?.[0]
          if (ts) onPointClick(String(ts))
        },
      } : undefined}
    />
  )
}

export function ChartPanel({ title, hint, right, children, className }: {
  title: string
  hint?: string
  right?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <Card className={cn('overflow-hidden', className)}>
      <div className="flex items-start justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</div>
          {hint && <div className="mt-0.5 truncate text-[11px] text-text2">{hint}</div>}
        </div>
        {right}
      </div>
      <div className="px-2 pb-2 pt-1">{children}</div>
    </Card>
  )
}

export function ApmKpi({ to, label, icon, tone = 'primary', value, sub, foot }: {
  to?: string
  label: string
  icon?: ReactNode
  tone?: ApmTone
  value: ReactNode
  sub?: ReactNode
  foot?: ReactNode
}) {
  const inner = (
    <Card className={cn('relative h-full overflow-hidden', to && 'transition hover:border-primary/40')}>
      <span className={cn('absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r', KPI_BAR[tone])} />
      <div className="flex items-start justify-between gap-2 px-4 pt-3">
        <div className="min-w-0">
          <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className="mt-1 text-2xl font-bold tabular-nums leading-none text-text">{value}</div>
          {sub && <div className="mt-1.5 min-h-[14px] text-[11px] text-text2">{sub}</div>}
        </div>
        {icon && (
          <span className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-white shadow', KPI_BAR[tone])}>
            {icon}
          </span>
        )}
      </div>
      {foot && <div className="px-4 pb-3 pt-2">{foot}</div>}
      {!foot && <div className="h-3" />}
    </Card>
  )
  return to ? <Link to={to} className="block h-full">{inner}</Link> : inner
}

export function HealthShareBar({ healthy, degraded, critical, noData }: {
  healthy: number; degraded: number; critical: number; noData?: number
}) {
  const silent = noData ?? 0
  const total = healthy + degraded + critical + silent
  if (!total) return <div className="h-1.5 rounded-full bg-surface2" />
  const seg = (n: number) => `${(n / total) * 100}%`
  return (
    <div className="flex h-1.5 w-full gap-px overflow-hidden rounded-full bg-surface2">
      {healthy > 0 && <div style={{ width: seg(healthy), background: HEALTH_COLOR.healthy }} />}
      {degraded > 0 && <div style={{ width: seg(degraded), background: HEALTH_COLOR.degraded }} />}
      {critical > 0 && <div style={{ width: seg(critical), background: HEALTH_COLOR.critical }} />}
      {silent > 0 && <div style={{ width: seg(silent), background: HEALTH_COLOR.no_data }} />}
    </div>
  )
}

export function RankBar({ value, max, color = APM_SERIES.throughput }: { value: number; max: number; color?: string }) {
  const pct = max > 0 ? Math.max(3, (value / max) * 100) : 0
  return (
    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
    </div>
  )
}

export function ErrorRateCell({ rate, max = 0.25 }: { rate: number; max?: number }) {
  const tone = errorTone(rate)
  const color = tone === 'danger' ? HEALTH_COLOR.critical : tone === 'warning' ? HEALTH_COLOR.degraded : HEALTH_COLOR.healthy
  return (
    <div className="min-w-[4.5rem]">
      <div className="text-right font-mono text-xs tabular-nums" style={{ color }}>{fmtPct(rate)}</div>
      <RankBar value={rate} max={max} color={color} />
    </div>
  )
}

export function LatencyCell({ ms }: { ms: number }) {
  const tone = latencyTone(ms)
  const color = tone === 'danger' ? HEALTH_COLOR.critical : tone === 'warning' ? HEALTH_COLOR.degraded : undefined
  return <span className="font-mono text-xs tabular-nums" style={color ? { color } : undefined}>{fmtMs(ms)}</span>
}

export function ApdexCell({ value }: { value: number }) {
  const tone = apdexTone(value)
  const color = tone === 'danger' ? HEALTH_COLOR.critical : tone === 'warning' ? HEALTH_COLOR.degraded : HEALTH_COLOR.healthy
  return (
    <div className="min-w-[3.5rem]">
      <div className="text-right font-mono text-xs tabular-nums" style={{ color }}>{value.toFixed(2)}</div>
      <RankBar value={value} max={1} color={color} />
    </div>
  )
}

export function ThroughputCell({ rps, count, maxRps }: { rps: number; count?: number; maxRps?: number }) {
  return (
    <div className="min-w-[4.5rem] text-right">
      <div className="font-mono text-xs tabular-nums text-text">{fmtRps(rps)}</div>
      {count != null && <div className="text-[10px] text-muted">{fmtCount(count)} req</div>}
      {maxRps != null && <RankBar value={rps} max={maxRps} />}
    </div>
  )
}

export function DeepLinks({ service, range, operation }: { service: string; range?: string; operation?: string }) {
  const q = new URLSearchParams()
  q.set('mode', 'indexed')
  q.set('service', service)
  if (range) q.set('range', range)
  if (operation) q.set('operation', operation)
  return (
    <div className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
      <Link
        to={`/apm/traces?${q}`}
        title="Traces"
        className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-1.5 text-[11px] font-medium text-muted hover:border-primary/40 hover:text-primary"
      >
        <GitBranch className="h-3 w-3" /> Traces
      </Link>
      <Link
        to={`/apm/errors?service=${encodeURIComponent(service)}`}
        title="Errors"
        className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-1.5 text-[11px] font-medium text-muted hover:border-primary/40 hover:text-primary"
      >
        <Bug className="h-3 w-3" /> Errors
      </Link>
    </div>
  )
}

export function DeepDiveLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
      {children} <ArrowRight className="h-3 w-3" />
    </Link>
  )
}

export function VitalGauge({ label, value, good, poor, format }: {
  label: string
  value: number
  good: number
  poor: number
  format: (v: number) => string
}) {
  const tone: ApmTone = value <= good ? 'success' : value <= poor ? 'warning' : 'danger'
  const pct = Math.max(4, Math.min(100, (value / (poor * 1.4 || 1)) * 100))
  const color = tone === 'success' ? HEALTH_COLOR.healthy : tone === 'warning' ? HEALTH_COLOR.degraded : HEALTH_COLOR.critical
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-xl font-bold tabular-nums" style={{ color }}>{format(value)}</div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface2">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <div className="mt-1 text-[10px] text-muted">good ≤ {format(good)} · poor &gt; {format(poor)}</div>
    </div>
  )
}

export function CompareDelta({ current, previous, invert, suffix = '' }: {
  current: number
  previous: number | null | undefined
  invert?: boolean
  suffix?: string
}) {
  if (previous == null || (!previous && !current)) return null
  if (!previous) return <span className="text-muted">new vs prev</span>
  const d = ((current - previous) / Math.abs(previous)) * 100
  const worse = invert ? d > 0.5 : d < -0.5
  const better = invert ? d < -0.5 : d > 0.5
  const color = worse ? HEALTH_COLOR.critical : better ? HEALTH_COLOR.healthy : undefined
  const sign = d > 0 ? '+' : ''
  return (
    <span style={color ? { color } : undefined}>
      {sign}{d.toFixed(0)}%{suffix} vs prev
    </span>
  )
}

export function ApmLatencyHeatmap({
  buckets,
  points,
  height = 280,
  loading,
  empty = 'No latency samples in this window',
  onCellClick,
}: {
  buckets: string[]
  points: Array<{ timestamp: string; counts: number[] }>
  height?: number
  loading?: boolean
  empty?: string
  onCellClick?: (timestamp: string, bucketIndex: number) => void
}) {
  const theme = useTheme((s) => s.theme)
  const option = useMemo(() => {
    const muted = cssRgb('muted', '#94a3b8')
    const border = cssRgb('border', '#1e293b')
    const surface = cssRgb('surface', '#0d121b')
    const text = cssRgb('text', '#e5e7eb')
    const times = points.map((p) => p.timestamp)
    const window = spanMs(times)
    const data: Array<[number, number, number]> = []
    let max = 1
    points.forEach((p, xi) => {
      (p.counts || []).forEach((c, yi) => {
        if (c > 0) {
          data.push([xi, yi, c])
          if (c > max) max = c
        }
      })
    })
    return {
      backgroundColor: 'transparent',
      animation: false,
      grid: { top: 8, right: 16, bottom: 36, left: 64, containLabel: false },
      tooltip: {
        backgroundColor: surface,
        borderColor: border,
        textStyle: { color: text, fontSize: 12 },
        formatter: (p: { value: [number, number, number] }) => {
          const [xi, yi, v] = p.value
          const ts = times[xi]
          const when = ts ? new Date(ts).toLocaleString() : ''
          return `<div style="font-size:11px;color:${muted}">${when}</div><div>${buckets[yi] || ''} · <b>${v.toLocaleString()}</b> spans</div>`
        },
      },
      xAxis: {
        type: 'category',
        data: times,
        axisLabel: {
          color: muted,
          fontSize: 10,
          hideOverlap: true,
          formatter: (_: string, idx: number) => formatChartTick(times[idx], window),
        },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: border } },
        splitArea: { show: false },
      },
      yAxis: {
        type: 'category',
        data: buckets,
        axisLabel: { color: muted, fontSize: 10 },
        axisTick: { show: false },
        axisLine: { show: false },
        splitLine: { show: false },
      },
      visualMap: {
        min: 0,
        max,
        calculable: false,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        itemWidth: 10,
        itemHeight: 8,
        textStyle: { color: muted, fontSize: 10 },
        inRange: { color: ['#0f172a', '#1e3a5f', '#0369a1', '#7c3aed', '#db2777'] },
      },
      series: [{
        type: 'heatmap',
        data,
        emphasis: { itemStyle: { shadowBlur: 4, shadowColor: 'rgba(0,0,0,0.4)' } },
      }],
    }
  }, [buckets, points, theme])

  if (loading) {
    return <div className="flex items-center justify-center text-xs text-muted" style={{ height }}>Loading heatmap…</div>
  }
  if (!points.length) {
    return <div className="flex items-center justify-center text-xs text-muted" style={{ height }}>{empty}</div>
  }

  return (
    <ReactECharts
      option={option}
      style={{ height }}
      notMerge
      onEvents={onCellClick ? {
        click: (p: { value?: [number, number, number] }) => {
          const xi = p?.value?.[0]
          const yi = p?.value?.[1]
          if (xi == null || yi == null) return
          const ts = points[xi]?.timestamp
          if (ts) onCellClick(ts, yi)
        },
      } : undefined}
    />
  )
}
