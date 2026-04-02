import ReactECharts from 'echarts-for-react'
import type { MetricPoint } from '@/types'

interface TimeSeriesChartProps {
  data: MetricPoint[]
  height?: number
  showPacketLoss?: boolean
}

export function TimeSeriesChart({ data, height = 300, showPacketLoss = false }: TimeSeriesChartProps) {
  const timestamps = data.map((p) => p.timestamp)
  const rttValues = data.map((p) => p.rtt_ms)

  const series: any[] = [
    {
      name: 'RTT (ms)',
      type: 'line',
      data: rttValues,
      smooth: true,
      lineStyle: { width: 2, color: '#6366F1' },
      areaStyle: {
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(99, 102, 241, 0.3)' },
            { offset: 1, color: 'rgba(99, 102, 241, 0.0)' },
          ],
        },
      },
      itemStyle: { color: '#6366F1' },
      symbol: 'none',
    },
  ]

  if (showPacketLoss) {
    series.push({
      name: 'Packet Loss (%)',
      type: 'bar',
      yAxisIndex: 1,
      data: data.map((p) => (p.packet_loss !== null ? p.packet_loss * 100 : 0)),
      itemStyle: { color: 'rgba(239, 68, 68, 0.6)' },
      barMaxWidth: 4,
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
    },
    xAxis: {
      type: 'category',
      data: timestamps,
      axisLabel: {
        color: '#5F6578',
        fontSize: 11,
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
        type: 'value',
        name: 'RTT (ms)',
        nameTextStyle: { color: '#5F6578', fontSize: 11 },
        axisLabel: { color: '#5F6578', fontSize: 11 },
        splitLine: { lineStyle: { color: '#1A1D27' } },
      },
      ...(showPacketLoss
        ? [{
            type: 'value',
            name: 'Loss %',
            nameTextStyle: { color: '#5F6578', fontSize: 11 },
            axisLabel: { color: '#5F6578', fontSize: 11 },
            splitLine: { show: false },
            max: 100,
          }]
        : []),
    ],
    series,
  }

  return <ReactECharts option={option} style={{ height }} />
}
