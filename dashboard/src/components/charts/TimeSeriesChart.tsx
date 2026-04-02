import ReactECharts from 'echarts-for-react'
import type { MetricPoint } from '@/types'

interface TimeSeriesChartProps {
  data: MetricPoint[]
  height?: number
  showPacketLoss?: boolean
}

export function TimeSeriesChart({ data, height = 300, showPacketLoss = false }: TimeSeriesChartProps) {
  // Separate UP and DOWN points for better visualization
  const timestamps = data.map((p) => p.timestamp)
  const rttValues = data.map((p) => {
    const isUp = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    return isUp && p.rtt_ms ? p.rtt_ms : null
  })

  // For packet loss: show 100% when device is down (is_up=0), otherwise show actual loss
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

  const series: unknown[] = [
    {
      name: 'RTT (ms)',
      type: 'line',
      data: rttValues,
      smooth: true,
      connectNulls: false,
      lineStyle: { width: 2, color: '#6366F1' },
      areaStyle: {
        color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(99, 102, 241, 0.25)' }, { offset: 1, color: 'rgba(99, 102, 241, 0.0)' }],
        },
      },
      itemStyle: { color: '#6366F1' },
      symbol: 'none',
      z: 2,
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
      barMaxWidth: 6,
      z: 1,
    })
  }

  const option = {
    backgroundColor: 'transparent',
    grid: { top: 40, right: showPacketLoss ? 60 : 20, bottom: 30, left: 60 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1A1D27',
      borderColor: '#2D3140',
      textStyle: { color: '#E8EAED', fontSize: 12 },
      formatter: (params: { axisValue: string; marker: string; seriesName: string; value: number | null }[]) => {
        const ts = params[0]?.axisValue
        const d = new Date(ts || '')
        let html = `<div style="font-size:11px;color:#9BA1B0;margin-bottom:4px">${d.toLocaleString()}</div>`
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
        color: '#5F6578', fontSize: 11,
        formatter: (val: string) => {
          const d = new Date(val)
          return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
        },
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
    series,
  }

  return <ReactECharts option={option} style={{ height }} notMerge={true} />
}
