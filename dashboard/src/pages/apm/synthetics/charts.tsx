/**
 * Synthetics charts: availability per bucket and the per-step stacked duration. Theme-aware through
 * lib/chartTheme (fonts follow the display density, colours are tokens).
 */
import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { chartFont, chartTheme, seriesColors, tokenColor } from '@/lib/chartTheme'
import { regionalDate } from '@/lib/regionalTime'
import { useTheme } from '@/stores/theme'
import { fmtMs } from '@/components/apm/shared'
import { formatChartTick } from '@/components/apm/viz'
import {
  type SynSeriesPoint, type SynStepStats,
  availabilityBars, fmtUptime, stackStepSeries,
} from '@/features/apm/synthetics'

function windowMs(times: string[]): number {
  if (times.length < 2) return 0
  return Math.abs(Date.parse(times[times.length - 1]) - Date.parse(times[0]))
}

function Empty({ height, text }: { height: number; text: string }) {
  return <div className="flex items-center justify-center text-xs text-muted" style={{ height }}>{text}</div>
}

function baseAxes(times: string[], fmtY: (v: number) => string) {
  const muted = tokenColor('muted')
  const border = tokenColor('border')
  const span = windowMs(times)
  return {
    xAxis: {
      type: 'category' as const,
      data: times,
      axisLabel: {
        color: muted, fontSize: chartFont(10), hideOverlap: true,
        formatter: (v: string) => formatChartTick(v, span),
      },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: border } },
    },
    yAxis: {
      type: 'value' as const,
      min: 0,
      axisLabel: { color: muted, fontSize: chartFont(11), formatter: fmtY },
      splitLine: { lineStyle: { color: border, opacity: 0.55, type: 'dashed' as const } },
    },
  }
}

/** Passed and failed runs per bucket; a click selects that bucket's window. */
export function AvailabilityChart({ series, bucketSeconds, height = 200, onBucketClick }: {
  series: ReadonlyArray<SynSeriesPoint>
  bucketSeconds?: number
  height?: number
  onBucketClick?: (fromISO: string, toISO: string) => void
}) {
  const theme = useTheme((s) => s.theme)
  const bars = useMemo(() => availabilityBars(series), [series])
  const option = useMemo(() => {
    const times = bars.map((b) => b.t)
    const muted = tokenColor('muted')
    return {
      backgroundColor: 'transparent',
      animation: false,
      grid: { top: 28, right: 12, bottom: 28, left: 44 },
      legend: { top: 0, right: 8, textStyle: { color: muted, fontSize: chartFont(11) }, itemWidth: 10, itemHeight: 8 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        textStyle: { fontSize: chartFont(12) },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const b = bars[ps[0]?.dataIndex ?? 0]
          if (!b) return ''
          const runs = b.passed + b.failed
          return `<div style="color:${muted}">${regionalDate(new Date(b.t), 'datetime')}</div>`
            + `<div>${runs ? `${runs} run${runs === 1 ? '' : 's'} · <b>${fmtUptime(b.uptime_pct)}</b> up` : 'No runs'}</div>`
            + (b.failed ? `<div>${b.failed} failed</div>` : '')
        },
      },
      ...baseAxes(times, (v: number) => String(v)),
      series: [
        { name: 'Passed', type: 'bar', stack: 'runs', barCategoryGap: '20%', itemStyle: { color: tokenColor('success', 0.75), borderRadius: 0 }, data: bars.map((b) => b.passed) },
        { name: 'Failed', type: 'bar', stack: 'runs', itemStyle: { color: tokenColor('danger'), borderRadius: [2, 2, 0, 0] }, data: bars.map((b) => b.failed) },
      ],
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, theme])

  if (!bars.length || bars.every((b) => !b.passed && !b.failed)) return <Empty height={height} text="No runs in this range" />
  return (
    <ReactECharts
      theme={chartTheme()}
      option={option}
      style={{ height }}
      notMerge
      onEvents={onBucketClick ? {
        click: (p: { dataIndex?: number }) => {
          const b = p.dataIndex != null ? bars[p.dataIndex] : undefined
          if (!b) return
          const start = Date.parse(b.t)
          const size = (bucketSeconds || 60) * 1000
          if (Number.isFinite(start)) onBucketClick(new Date(start).toISOString(), new Date(start + size).toISOString())
        },
      } : undefined}
    />
  )
}

/** Average duration per step, stacked, so the slow step in a slow run stands out. Colour follows the step. */
export function StepStackChart({ steps, height = 240 }: { steps: ReadonlyArray<SynStepStats>; height?: number }) {
  const theme = useTheme((s) => s.theme)
  const rows = useMemo(() => stackStepSeries(steps), [steps])
  const option = useMemo(() => {
    const times = rows.map((r) => String(r.t))
    const colors = seriesColors()
    const muted = tokenColor('muted')
    return {
      backgroundColor: 'transparent',
      animation: false,
      grid: { top: 32, right: 12, bottom: 28, left: 56 },
      legend: { top: 0, left: 0, type: 'scroll', textStyle: { color: muted, fontSize: chartFont(11) }, itemWidth: 10, itemHeight: 8 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        textStyle: { fontSize: chartFont(12) },
        valueFormatter: (v: number | null) => (v == null ? '—' : fmtMs(Number(v))),
      },
      ...baseAxes(times, (v: number) => fmtMs(v)),
      series: steps.map((s) => ({
        name: `${s.index + 1}. ${s.name}`,
        type: 'bar',
        stack: 'steps',
        barCategoryGap: '20%',
        itemStyle: { color: colors[s.index % colors.length], borderRadius: 0 },
        data: rows.map((r) => (r[`s${s.index}`] ?? null)),
      })),
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, steps, theme])

  if (!rows.length) return <Empty height={height} text="No step timings in this range" />
  return <ReactECharts theme={chartTheme()} option={option} style={{ height }} notMerge />
}
