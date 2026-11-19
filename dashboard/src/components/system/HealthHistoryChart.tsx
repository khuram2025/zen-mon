/**
 * CPU, memory and disk wait of the appliance over the last hours, as percentages on one axis.
 * Theme-aware through lib/chartTheme (fonts follow the display density, colours are the categorical tokens).
 * A bucket nothing was sampled in stays empty: the line breaks instead of joining across an outage.
 */
import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { chartFont, chartTheme, seriesColors, tokenColor } from '@/lib/chartTheme'
import { regionalDate } from '@/lib/regionalTime'
import { timeAxisTickFormatter } from '@/lib/utils'
import { useTheme } from '@/stores/theme'
import { chartSeries, type HistoryPoint } from './healthMath'

export default function HealthHistoryChart({ points, hours, height = 240 }: { points: HistoryPoint[]; hours: number; height?: number }) {
  const theme = useTheme((s) => s.theme)
  const option = useMemo(() => {
    const [cpuColor, memColor, waitColor] = seriesColors()
    const muted = tokenColor('muted')
    const border = tokenColor('border')
    const tick = timeAxisTickFormatter(hours)
    const line = (name: string, key: keyof HistoryPoint, color: string, extra: Record<string, unknown> = {}) => ({
      name, type: 'line', showSymbol: false, connectNulls: false, sampling: 'lttb',
      lineStyle: { width: 2, color }, itemStyle: { color }, data: chartSeries(points, key), ...extra,
    })
    return {
      backgroundColor: 'transparent',
      animation: false,
      grid: { top: 32, right: 16, bottom: 28, left: 44 },
      legend: { top: 0, left: 0, textStyle: { color: muted, fontSize: chartFont(11) }, itemWidth: 14, itemHeight: 8 },
      tooltip: {
        trigger: 'axis',
        textStyle: { fontSize: chartFont(12) },
        formatter: (ps: Array<{ seriesName: string; value: [number, number | null]; marker: string }>) => {
          if (!ps.length) return ''
          const at = regionalDate(new Date(ps[0].value[0]), 'datetime', [], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
          const rows = ps.map((p) => `<div>${p.marker} ${p.seriesName}: <b>${p.value[1] == null ? 'not sampled' : `${p.value[1].toFixed(1)}%`}</b></div>`)
          return `<div style="color:${muted}">${at}</div>${rows.join('')}`
        },
      },
      xAxis: {
        type: 'time' as const,
        axisLabel: { color: muted, fontSize: chartFont(10), hideOverlap: true, formatter: (v: number) => tick(v) },
        axisLine: { lineStyle: { color: border } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value' as const, min: 0, max: 100, interval: 25,
        axisLabel: { color: muted, fontSize: chartFont(11), formatter: (v: number) => `${v}%` },
        splitLine: { lineStyle: { color: border, opacity: 0.55, type: 'dashed' as const } },
      },
      series: [
        line('CPU', 'cpu', cpuColor),
        // The busiest minute of each step: a short spike the average would hide.
        line('CPU peak', 'cpu_max', cpuColor, {
          lineStyle: { width: 1, color: cpuColor, type: 'dashed', opacity: 0.6 },
          // A paler marker, so the legend tells the peak from the average of the same colour.
          itemStyle: { color: tokenColor('series-1', 0.45) },
        }),
        line('Memory', 'mem', memColor),
        line('Waiting for disk', 'iowait', waitColor, { lineStyle: { width: 1.5, color: waitColor } }),
      ],
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, hours, theme])

  return (
    <div role="img" aria-label={`CPU, memory and disk wait of the appliance over the last ${hours} hour${hours === 1 ? '' : 's'}`}>
      <ReactECharts theme={chartTheme()} option={option} style={{ height }} notMerge />
    </div>
  )
}
