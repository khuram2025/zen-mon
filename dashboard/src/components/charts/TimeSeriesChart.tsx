import ReactECharts from 'echarts-for-react'
import type { MetricPoint } from '@/types'

interface TimeSeriesChartProps {
  data: MetricPoint[]
  height?: number
  showPacketLoss?: boolean
  timezone?: string
  rangeHours?: number
}

export function TimeSeriesChart({ data, height = 300, showPacketLoss = false, timezone = 'UTC', rangeHours = 24 }: TimeSeriesChartProps) {
  const timestamps = data.map((p) => p.timestamp)
  const rttValues = data.map((p) => {
    const isUp = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    return isUp && p.rtt_ms ? p.rtt_ms : null
  })

  const lossValues = data.map((p) => {
    const isUp = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    if (!isUp) return 100
    return p.packet_loss !== null ? p.packet_loss * 100 : 0
  })

  // Status background: mark DOWN periods with red zones
  const downPieces: { gt: number; lt: number; color: string }[] = []
  let inDown = false
  let downStart = 0
  data.forEach((p, i) => {
    const isDown = p.is_up === false || (p.is_up as unknown as number) === 0 || p.is_up === null
    if (isDown && !inDown) { inDown = true; downStart = i }
    if (!isDown && inDown) { inDown = false; downPieces.push({ gt: downStart - 0.5, lt: i - 0.5, color: 'rgba(239, 68, 68, 0.08)' }) }
  })
  if (inDown) downPieces.push({ gt: downStart - 0.5, lt: data.length - 0.5, color: 'rgba(239, 68, 68, 0.08)' })

  // Smart x-axis label formatting based on time range
  const isLargeRange = rangeHours > 168  // > 7 days
  const isVeryLargeRange = rangeHours > 2160  // > 90 days

  const formatAxisLabel = (val: string) => {
    const d = new Date(val)
    try {
      if (isVeryLargeRange) {
        // 90d+ : show "Jan 5" style
        return d.toLocaleDateString('en-US', { timeZone: timezone, month: 'short', day: 'numeric' })
      } else if (isLargeRange) {
        // 7d-90d : show "Jan 5 14:00"
        return d.toLocaleDateString('en-US', { timeZone: timezone, month: 'short', day: 'numeric' }) + '\n' +
          d.toLocaleTimeString('en-US', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hour12: false })
      } else {
        // <=7d : show "HH:mm"
        return d.toLocaleTimeString('en-US', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hour12: false })
      }
    } catch {
      return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
    }
  }

  // Smart interval: show fewer labels for large datasets
  const labelInterval = data.length > 3000 ? Math.floor(data.length / 12)
    : data.length > 1000 ? Math.floor(data.length / 10)
    : data.length > 200 ? Math.floor(data.length / 8)
    : 'auto'

  // For large ranges, use thinner lines and smaller bar widths
  const lineWidth = data.length > 2000 ? 1 : data.length > 500 ? 1.5 : 2
  const barMaxWidth = data.length > 2000 ? 2 : data.length > 500 ? 3 : 6

  const series: unknown[] = [
    {
      name: 'RTT (ms)',
      type: 'line',
      data: rttValues,
      smooth: data.length <= 500,
      connectNulls: false,
      lineStyle: { width: lineWidth, color: '#6366F1' },
      areaStyle: {
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(99, 102, 241, 0.25)' }, { offset: 1, color: 'rgba(99, 102, 241, 0.0)' }],
        },
      },
      itemStyle: { color: '#6366F1' },
      symbol: 'none',
      z: 2,
      sampling: data.length > 1000 ? 'lttb' : undefined,
      large: data.length > 1000,
      largeThreshold: 1000,
    },
  ]

  if (showPacketLoss) {
    series.push({
      name: 'Packet Loss / Down',
      type: 'bar',
      yAxisIndex: 1,
      data: lossValues,
      itemStyle: {
        color: (params: { value: number }) => params.value >= 100 ? 'rgba(239, 68, 68, 0.7)' : 'rgba(234, 179, 8, 0.5)',
      },
      barMaxWidth,
      z: 1,
      large: data.length > 1000,
      largeThreshold: 1000,
    })
  }

  // Show dataZoom slider for large time ranges (> 7 days or > 500 points)
  const showDataZoom = data.length > 500 || rangeHours > 168
  const dataZoom = showDataZoom ? [
    {
      type: 'slider',
      xAxisIndex: 0,
      bottom: 5,
      height: 22,
      borderColor: '#2D3140',
      backgroundColor: '#0F1117',
      fillerColor: 'rgba(99, 102, 241, 0.15)',
      handleStyle: { color: '#6366F1', borderColor: '#6366F1' },
      textStyle: { color: '#5F6578', fontSize: 10 },
      dataBackground: {
        lineStyle: { color: '#2D3140' },
        areaStyle: { color: 'rgba(99, 102, 241, 0.08)' },
      },
      selectedDataBackground: {
        lineStyle: { color: '#6366F1' },
        areaStyle: { color: 'rgba(99, 102, 241, 0.15)' },
      },
      // Default view: show last portion for very large ranges
      start: isVeryLargeRange ? 70 : isLargeRange ? 50 : 0,
      end: 100,
      labelFormatter: (value: number) => {
        const ts = timestamps[Math.round(value)]
        if (!ts) return ''
        const d = new Date(ts)
        try {
          if (rangeHours > 720) {
            return d.toLocaleDateString('en-US', { timeZone: timezone, month: 'short', day: 'numeric' })
          }
          return d.toLocaleDateString('en-US', { timeZone: timezone, month: 'short', day: 'numeric' }) + ' ' +
            d.toLocaleTimeString('en-US', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hour12: false })
        } catch {
          return d.toLocaleDateString()
        }
      },
    },
    {
      type: 'inside',
      xAxisIndex: 0,
      zoomOnMouseWheel: true,
      moveOnMouseMove: true,
    },
  ] : undefined

  const option = {
    backgroundColor: 'transparent',
    animation: data.length <= 2000,
    grid: {
      top: 40,
      right: showPacketLoss ? 60 : 20,
      bottom: showDataZoom ? 60 : 30,
      left: 60,
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1A1D27',
      borderColor: '#2D3140',
      textStyle: { color: '#E8EAED', fontSize: 12 },
      formatter: (params: { axisValue: string; marker: string; seriesName: string; value: number | null }[]) => {
        const ts = params[0]?.axisValue
        const d = new Date(ts || '')
        const dateOpts: Intl.DateTimeFormatOptions = rangeHours > 168
          ? { timeZone: timezone, year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true }
          : { timeZone: timezone, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true }
        let html = `<div style="font-size:11px;color:#9BA1B0;margin-bottom:4px">${(() => { try { return d.toLocaleString('en-US', dateOpts) } catch { return d.toLocaleString() } })()}</div>`
        params.forEach(p => {
          if (p.value !== null && p.value !== undefined) {
            const val = p.seriesName.includes('Loss')
              ? (p.value >= 100 ? '<span style="color:#EF4444">DOWN</span>' : `${p.value.toFixed(1)}%`)
              : `${p.value.toFixed(2)} ms`
            html += `<div>${p.marker} ${p.seriesName}: <b>${val}</b></div>`
          }
        })
        return html
      },
    },
    xAxis: {
      type: 'category',
      data: timestamps,
      axisLabel: {
        color: '#5F6578',
        fontSize: 10,
        formatter: formatAxisLabel,
        interval: labelInterval,
        rotate: isLargeRange && !isVeryLargeRange ? 30 : 0,
        hideOverlap: true,
      },
      axisLine: { lineStyle: { color: '#2D3140' } },
      splitLine: { show: false },
    },
    yAxis: [
      {
        type: 'value', name: 'RTT (ms)',
        nameTextStyle: { color: '#5F6578', fontSize: 11 },
        axisLabel: { color: '#5F6578', fontSize: 11 },
        splitLine: { lineStyle: { color: '#1A1D27' } },
        min: 0,
      },
      ...(showPacketLoss ? [{
        type: 'value', name: 'Loss %',
        nameTextStyle: { color: '#5F6578', fontSize: 11 },
        axisLabel: { color: '#5F6578', fontSize: 11, formatter: (v: number) => v >= 100 ? 'DOWN' : `${v}%` },
        splitLine: { show: false },
        max: 100,
      }] : []),
    ],
    visualMap: downPieces.length > 0 ? {
      show: false,
      dimension: 0,
      pieces: downPieces,
      seriesIndex: 0,
    } : undefined,
    dataZoom,
    series,
  }

  return <ReactECharts option={option} style={{ height: showDataZoom ? height + 40 : height }} notMerge={true} />
}
