import { useMemo, type ReactNode } from 'react'
import ReactECharts from 'echarts-for-react'
import { ImageOff } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatDateTime, zoneLabel, type Branding } from '@/lib/branding'
import { useVizColors, withAlpha, type VizColors } from '@/components/availability/viz'
import type { ChartSeries, RenderChart } from '../types'
import {
  axisTimeLabel, chartModel, describeChart, drawAsBars, formatValue, slices, timeStep, unitKind,
  type CategoryModel, type ChartModel, type TimeSeriesModel,
} from './format'
import { statusTone } from './table'

type Token = Parameters<VizColors['c']>[0]

const TOKENS = new Set<string>(['primary', 'success', 'warning', 'danger', 'info', 'accent', 'muted', 'text2', 'amber'])

/** Categorical order: never the status colours, never the brand colour (UX §A.5). */
const CATEGORICAL: [Token, number?][] = [
  ['primary'], ['accent'], ['info'], ['amber'],
  ['primary', 0.55], ['accent', 0.55], ['info', 0.55], ['amber', 0.55], ['muted'],
]

const NEUTRAL_STATUS = /^(unknown|no data|disabled|paused|suppressed|unmonitored|inactive|n\/a)$/i
const TONE_TOKEN: Record<string, Token> = {
  success: 'success', warning: 'warning', danger: 'danger', info: 'info', neutral: 'muted',
}

function resolveColor(colors: VizColors, color: string | undefined, i: number): string {
  if (color && TOKENS.has(color)) return colors.c(color as Token)
  if (color && /^(#[0-9a-f]{3,8}|rgba?\()/i.test(color)) return color
  const [token, alpha] = CATEGORICAL[i % CATEGORICAL.length]
  return colors.c(token, alpha)
}

/** Status-named categories (Up / Down / Warning …) keep their status colours. */
function categoryColors(colors: VizColors, labels: string[]): string[] {
  const allStatus = labels.length > 0 && labels.every((l) => statusTone(l) !== 'neutral' || NEUTRAL_STATUS.test(l.trim()))
  return labels.map((l, i) => (allStatus ? colors.c(TONE_TOKEN[statusTone(l)] ?? 'muted') : resolveColor(colors, undefined, i)))
}

const esc = (s: string) => s.replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]!))

function tooltipBase(colors: VizColors) {
  return {
    backgroundColor: colors.c('surface'),
    borderColor: colors.c('border'),
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: colors.c('text'), fontSize: 12 },
    extraCssText: 'box-shadow: 0 8px 24px rgba(0,0,0,.18); border-radius: 8px;',
    confine: true,
  }
}

type Fmt = Pick<Branding, 'timezone' | 'date_format' | 'time_format'>

interface PlotProps {
  series: ChartSeries
  model: ChartModel
  branding: Fmt
  height: number
  description: string
  window?: { from: number; to: number }
}

function TimePlot({ series, model, branding, height, description, window }: PlotProps & { model: TimeSeriesModel }) {
  const colors = useVizColors()
  const option = useMemo(() => {
    const unit = series.unit
    const kind = series.kind
    const multi = model.series.length > 1
    const isBar = drawAsBars(kind, model)
    const filled = kind === 'area' || kind === 'stacked'
    // Bars sit on bucket starts: pad half a bucket so the first and last are not clipped.
    const half = isBar ? Math.max(timeStep(model), 60_000) / 2 : 0
    const lo = !isBar && window ? Math.min(window.from, model.start) : model.start - half
    const hi = !isBar && window ? Math.max(window.to, model.end) : model.end + half
    const span = Math.max(hi - lo, 60_000)
    const muted = colors.c('muted')
    const zone = zoneLabel(branding.timezone, model.end)
    return {
      backgroundColor: 'transparent',
      animation: false,
      aria: { enabled: true, label: { description } },
      grid: { top: multi ? 30 : 12, right: 14, bottom: 4, left: 4, containLabel: true },
      legend: multi
        ? { type: 'scroll', top: 0, left: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 4, textStyle: { color: colors.c('text2'), fontSize: 11 }, pageIconColor: colors.c('primary'), pageTextStyle: { color: muted } }
        : undefined,
      tooltip: {
        ...tooltipBase(colors),
        trigger: 'axis',
        axisPointer: { type: isBar ? 'shadow' : 'line', lineStyle: { color: muted, opacity: 0.5 }, shadowStyle: { color: colors.c('muted', 0.08) } },
        formatter: (params: unknown) => {
          const list = (Array.isArray(params) ? params : [params]) as { marker: string; seriesName: string; value: [number, number | null] }[]
          const t = list[0]?.value?.[0]
          let html = t != null
            ? `<div style="font-size:11px;color:${muted};margin-bottom:4px">${esc(formatDateTime(t, branding))} ${esc(zone)}</div>`
            : ''
          for (const p of list) {
            const v = p.value?.[1]
            html += `<div style="display:flex;align-items:center;justify-content:space-between;gap:16px">`
              + `<span>${p.marker}${esc(p.seriesName)}</span>`
              + `<b style="font-variant-numeric:tabular-nums">${v == null ? 'no data' : esc(formatValue(v, unit))}</b></div>`
          }
          return html
        },
      },
      xAxis: {
        type: 'time',
        min: lo,
        max: hi,
        axisLabel: { color: muted, fontSize: 10, hideOverlap: true, formatter: (v: number) => axisTimeLabel(v, span, branding) },
        axisLine: { lineStyle: { color: colors.c('border') } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        min: series.y_domain?.[0] ?? (filled || isBar ? 0 : undefined),
        max: series.y_domain?.[1],
        scale: !series.y_domain && !filled && !isBar,
        axisLabel: { color: muted, fontSize: 10, formatter: (v: number) => formatValue(v, unit, { compact: true }) },
        splitLine: { lineStyle: { color: colors.c('border', 0.9), type: 'dashed' } },
      },
      series: model.series.map((s, i) => {
        const color = resolveColor(colors, s.color, i)
        const base = {
          name: s.name,
          data: s.data,
          color,
          emphasis: multi ? { focus: 'series' } : { disabled: true },
          stack: kind === 'stacked' ? 'total' : undefined,
        }
        if (isBar) {
          return { ...base, type: 'bar', barMaxWidth: 18, itemStyle: { borderRadius: kind === 'bars' && !multi ? [2, 2, 0, 0] : 0 } }
        }
        return {
          ...base,
          type: 'line',
          showSymbol: s.data.length <= 48,
          symbolSize: 4,
          connectNulls: false,
          sampling: s.data.length > 600 ? 'lttb' : undefined,
          lineStyle: { width: 1.6 },
          areaStyle: filled
            ? {
                color: {
                  type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                  colorStops: [{ offset: 0, color: withAlpha(color, kind === 'stacked' ? 0.35 : 0.26) }, { offset: 1, color: withAlpha(color, 0.03) }],
                },
              }
            : undefined,
        }
      }),
    }
  }, [colors, series, model, branding, description, window])

  return <ReactECharts option={option} notMerge style={{ height, width: '100%' }} opts={{ renderer: 'svg' }} />
}

function CategoryPlot({ series, model, height, description }: PlotProps & { model: CategoryModel }) {
  const colors = useVizColors()
  const horizontal = series.kind === 'rank'
  const option = useMemo(() => {
    const unit = series.unit
    const multi = model.series.length > 1
    const muted = colors.c('muted')
    const cat = {
      type: 'category',
      data: model.labels,
      inverse: horizontal,
      axisLine: { lineStyle: { color: colors.c('border') } },
      axisTick: { show: false },
      axisLabel: { color: colors.c('text2'), fontSize: 10, width: horizontal ? 130 : 90, overflow: 'truncate', hideOverlap: !horizontal },
    }
    const val = {
      type: 'value',
      min: series.y_domain?.[0],
      max: series.y_domain?.[1],
      axisLabel: { color: muted, fontSize: 10, formatter: (v: number) => formatValue(v, unit, { compact: true }) },
      splitLine: { lineStyle: { color: colors.c('border', 0.9), type: 'dashed' } },
    }
    return {
      backgroundColor: 'transparent',
      animation: false,
      aria: { enabled: true, label: { description } },
      grid: { top: multi ? 30 : 10, right: 14, bottom: 4, left: 4, containLabel: true },
      legend: multi
        ? { type: 'scroll', top: 0, left: 0, icon: 'roundRect', itemWidth: 10, itemHeight: 4, textStyle: { color: colors.c('text2'), fontSize: 11 } }
        : undefined,
      tooltip: {
        ...tooltipBase(colors),
        trigger: 'axis',
        axisPointer: { type: 'shadow', shadowStyle: { color: colors.c('muted', 0.08) } },
        formatter: (params: unknown) => {
          const list = (Array.isArray(params) ? params : [params]) as { marker: string; seriesName: string; name: string; value: number | null }[]
          let html = `<div style="font-size:11px;color:${muted};margin-bottom:4px">${esc(list[0]?.name ?? '')}</div>`
          for (const p of list) {
            html += `<div style="display:flex;align-items:center;justify-content:space-between;gap:16px"><span>${p.marker}${esc(p.seriesName)}</span>`
              + `<b style="font-variant-numeric:tabular-nums">${p.value == null ? 'no data' : esc(formatValue(p.value, unit))}</b></div>`
          }
          return html
        },
      },
      xAxis: horizontal ? val : cat,
      yAxis: horizontal ? cat : val,
      series: model.series.map((s, i) => {
        const color = resolveColor(colors, s.color, i)
        const base = {
          name: s.name,
          data: s.values,
          color,
          stack: series.kind === 'stacked' ? 'total' : undefined,
          emphasis: multi ? { focus: 'series' } : { disabled: true },
        }
        // Line / area over categories (e.g. per-day labels) stay lines; everything else is a bar.
        if (!horizontal && (series.kind === 'line' || series.kind === 'area')) {
          return {
            ...base,
            type: 'line',
            connectNulls: false,
            symbolSize: 4,
            lineStyle: { width: 1.6 },
            areaStyle: series.kind === 'area' ? { color: withAlpha(color, 0.18) } : undefined,
          }
        }
        return {
          ...base,
          type: 'bar',
          barMaxWidth: horizontal ? 14 : 22,
          itemStyle: { borderRadius: horizontal ? [0, 2, 2, 0] : [2, 2, 0, 0] },
        }
      }),
    }
  }, [colors, series, model, horizontal, description])
  const h = horizontal ? Math.max(height, Math.min(520, model.labels.length * 24 + 40)) : height
  return <ReactECharts option={option} notMerge style={{ height: h, width: '100%' }} opts={{ renderer: 'svg' }} />
}

/** Single-series ranking as an accessible list (crisp text, prints well). */
function RankList({ series }: { series: ChartSeries }) {
  const colors = useVizColors()
  const rows = slices(series.points, true)
  if (!rows.length) return <NoData />
  const max = rows[0].value || 1
  const color = resolveColor(colors, series.color, 0)
  return (
    <ol className="max-w-3xl space-y-1.5" role="list">
      {rows.map((r, i) => (
        <li key={`${r.label}-${i}`} className="flex items-center gap-2.5 text-xs">
          <span className="w-5 shrink-0 text-right tabular-nums text-muted">{i + 1}</span>
          <span className="w-[clamp(6rem,34%,15rem)] min-w-0 shrink-0 truncate font-medium text-text2" title={r.label}>{r.label}</span>
          <span aria-hidden className="h-3 min-w-0 flex-1 overflow-hidden rounded-sm bg-surface2">
            <span className="block h-full rounded-sm" style={{ width: `${Math.max(1.5, (r.value / max) * 100)}%`, background: color }} />
          </span>
          <span className="w-24 shrink-0 text-right tabular-nums text-text">
            {formatValue(r.value, series.unit)}
            <span className="sr-only">, {r.share} of the total</span>
          </span>
        </li>
      ))}
    </ol>
  )
}

function Donut({ series, height, description }: { series: ChartSeries; height: number; description: string }) {
  const colors = useVizColors()
  const rows = useMemo(() => slices(series.points), [series.points])
  const palette = useMemo(() => categoryColors(colors, rows.map((r) => r.label)), [colors, rows])
  const total = rows.reduce((a, r) => a + r.value, 0)
  const option = useMemo(() => ({
    backgroundColor: 'transparent',
    animation: false,
    aria: { enabled: true, label: { description } },
    tooltip: {
      ...tooltipBase(colors),
      trigger: 'item',
      formatter: (p: { name: string; value: number; marker: string }) =>
        `${p.marker}${esc(p.name)}<br/><b style="font-variant-numeric:tabular-nums">${esc(formatValue(p.value, series.unit))}</b>`
        + ` <span style="color:${colors.c('muted')}">· ${esc(rows.find((r) => r.label === p.name)?.share ?? '')}</span>`,
    },
    series: [{
      type: 'pie',
      radius: ['60%', '86%'],
      avoidLabelOverlap: true,
      label: { show: false },
      labelLine: { show: false },
      padAngle: rows.length > 1 ? 1.5 : 0,
      itemStyle: { borderRadius: 3, borderColor: colors.c('surface'), borderWidth: 1 },
      emphasis: { scale: true, scaleSize: 4 },
      data: rows.map((r, i) => ({ name: r.label, value: r.value, itemStyle: { color: palette[i] } })),
    }],
  }), [colors, rows, palette, series.unit, description])
  if (!rows.length) return <NoData />
  const size = Math.min(height, 200)
  return (
    <div className="flex flex-col items-stretch gap-4 sm:flex-row sm:items-center sm:gap-6">
      <div className="relative shrink-0 self-center" style={{ width: size, height: size }}>
        <ReactECharts option={option} notMerge style={{ height: size, width: size }} opts={{ renderer: 'svg' }} />
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
          <span className="text-lg font-semibold tabular-nums text-text">{formatValue(total, series.unit, { compact: true })}</span>
          <span className="text-2xs uppercase tracking-wide text-muted">Total{series.unit && !['%'].includes(series.unit) ? ` ${series.unit}` : ''}</span>
        </div>
      </div>
      <ul role="list" className="min-w-0 flex-1 space-y-1.5">
        {rows.map((r, i) => (
          <li key={`${r.label}-${i}`} className="flex items-center gap-2 text-xs">
            <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: palette[i] }} />
            <span className="min-w-0 truncate font-medium text-text2" title={r.label}>{r.label}</span>
            <span className="ml-auto shrink-0 tabular-nums text-muted">{formatValue(r.value, series.unit)}</span>
            <span className="w-12 shrink-0 text-right tabular-nums text-text">{r.share}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function NoData() {
  return (
    <p className="flex min-h-[96px] items-center justify-center rounded-lg border border-dashed border-border bg-surface2/40 px-3 py-6 text-center text-xs text-muted">
      No data for this window
    </p>
  )
}

/** Server-rendered PNG fallback, blended onto the surface so dark mode never shows a white block. */
function ImageChart({ src, title }: { src: string; title: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface2/60 p-3">
      <img
        src={src}
        alt={title || 'Chart'}
        loading="lazy"
        className={cn(
          'mx-auto block h-auto max-h-[300px] w-auto max-w-full mix-blend-multiply',
          'dark:mix-blend-screen dark:[filter:invert(1)_hue-rotate(180deg)]',
          'print:!mix-blend-normal print:![filter:none]',
        )}
      />
    </div>
  )
}

export interface ChartBlockProps {
  chart: RenderChart
  branding: Fmt
  compact?: boolean
  /** The report window (ms), so line charts show gaps at its edges. */
  window?: { from: number; to: number }
}

/** One chart: theme-aware ECharts for native series (area, line, bars, stacked, rank, donut), the server PNG otherwise. */
export function ChartBlock({ chart, branding, compact, window }: ChartBlockProps) {
  const series = chart.series
  const model = useMemo(() => (series ? chartModel(series, chart.title || 'Value') : null), [series, chart.title])
  const description = useMemo(() => {
    if (!series || !model) return chart.title || 'Chart'
    return describeChart(chart.title || 'Chart', model, series.unit, (ms) => formatDateTime(ms, branding))
  }, [series, model, chart.title, branding])
  const height = compact ? 190 : 240

  let body: ReactNode
  if (series) {
    if (series.kind === 'donut') body = <Donut series={series} height={height} description={description} />
    else if (series.kind === 'rank' && !(series.series && series.series.length > 1)) body = <RankList series={series} />
    else if (!model) body = <NoData />
    else if (model.kind === 'time') body = <TimePlot series={series} model={model} branding={branding} height={height} description={description} window={window} />
    else body = <CategoryPlot series={series} model={model} branding={branding} height={height} description={description} />
  } else if (chart.data_uri) {
    body = <ImageChart src={chart.data_uri} title={chart.title} />
  } else {
    body = (
      <p className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-border px-3 py-6 text-xs text-muted">
        <ImageOff className="h-3.5 w-3.5" aria-hidden /> Chart unavailable
      </p>
    )
  }

  return (
    <figure className="zp-avoid-break zp-chart min-w-0">
      {chart.title && (
        <figcaption className="mb-1.5 flex items-baseline gap-2 text-xs font-semibold uppercase tracking-wide text-muted">
          <span className="min-w-0 truncate">{chart.title}</span>
          {series?.unit && series.kind !== 'donut' && unitKind(series.unit) === 'count' && (
            // Counted units ("alerts", "sessions") are not in the axis labels; measured ones (%, ms, bps) are.
            <span className="shrink-0 font-normal normal-case tracking-normal text-muted">{series.unit}</span>
          )}
        </figcaption>
      )}
      {body}
    </figure>
  )
}
