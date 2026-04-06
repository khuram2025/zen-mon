/**
 * ZenPlus Device Health Report — PDF Generator
 *
 * Produces a professional multi-page PDF with:
 *   1. Cover / header with branding & report period
 *   2. Executive summary KPIs
 *   3. Response-time trend chart (ECharts image)
 *   4. Packet-loss trend chart (ECharts image)
 *   5. Uptime timeline bar
 *   6. Performance statistics table
 *   7. Incident history table (auto-paginated)
 *   8. Device information panel
 */

import { jsPDF } from 'jspdf'
import autoTable from 'jspdf-autotable'
import * as echarts from 'echarts'
import type { MetricPoint, Device } from '@/types'

// ─── Colour palette ─────────────────────────────────────────────
const C = {
  primary:   '#4F46E5',
  success:   '#16A34A',
  danger:    '#DC2626',
  warning:   '#CA8A04',
  text:      '#1F2937',
  textSec:   '#6B7280',
} as const

const stColors: Record<string, string> = {
  up: C.success, down: C.danger, degraded: C.warning,
  unknown: C.textSec, maintenance: '#2563EB',
}

// ─── Helpers ─────────────────────────────────────────────────────
function fmtRtt(ms: number | null) {
  if (ms === null || ms === undefined) return '--'
  if (ms < 1) return `${(ms * 1000).toFixed(0)} us`
  if (ms < 100) return `${ms.toFixed(2)} ms`
  return `${ms.toFixed(1)} ms`
}

function fmtDuration(sec: number | null) {
  if (!sec || sec <= 0) return '--'
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h < 24) return `${h}h ${m}m`
  return `${Math.floor(h / 24)}d ${h % 24}h ${m}m`
}

function fmtDate(d: string | null, tz: string) {
  if (!d) return '--'
  try { return new Date(d).toLocaleString('en-US', { timeZone: tz, year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true }) } catch { return new Date(d).toLocaleString() }
}

function fmtShortDate(d: string | null, tz: string) {
  if (!d) return '--'
  try { return new Date(d).toLocaleDateString('en-US', { timeZone: tz, month: 'short', day: 'numeric', year: 'numeric' }) } catch { return new Date(d).toLocaleDateString() }
}

function rangeLabel(hours: number): string {
  if (hours <= 1) return 'Last 1 Hour'
  if (hours <= 6) return 'Last 6 Hours'
  if (hours <= 24) return 'Last 24 Hours'
  if (hours <= 168) return 'Last 7 Days'
  if (hours <= 720) return 'Last 30 Days'
  if (hours <= 2160) return 'Last 90 Days'
  return 'Last 1 Year'
}

const hexToRgb = (hex: string): [number, number, number] => {
  const n = parseInt(hex.replace('#', ''), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

// ─── Off-screen chart renderer ────────────────────────────────
function renderChartToImage(option: echarts.EChartsOption, width: number, height: number): string {
  const container = document.createElement('div')
  container.style.cssText = `width:${width}px;height:${height}px;position:absolute;left:-9999px`
  document.body.appendChild(container)
  const chart = echarts.init(container, undefined, { renderer: 'canvas', width, height })
  chart.setOption(option)
  const url = chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#FFFFFF' })
  chart.dispose()
  document.body.removeChild(container)
  return url
}

// ─── Chart options for PDF (light theme, print-friendly) ──────
function axisLabelFmt(tz: string, rangeHours: number) {
  return (val: string) => {
    const d = new Date(val)
    try {
      if (rangeHours > 2160) return d.toLocaleDateString('en-US', { timeZone: tz, month: 'short', day: 'numeric' })
      if (rangeHours > 168) return d.toLocaleDateString('en-US', { timeZone: tz, month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString('en-US', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
      return d.toLocaleTimeString('en-US', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false })
    } catch { return '' }
  }
}

function chartInterval(len: number) {
  if (len > 2000) return Math.floor(len / 8)
  if (len > 500) return Math.floor(len / 6)
  return 'auto' as const
}

function buildRttChartOption(points: MetricPoint[], tz: string, rangeHours: number): echarts.EChartsOption {
  const ts = points.map(p => p.timestamp)
  const vals = points.map(p => {
    const up = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    return up && p.rtt_ms ? p.rtt_ms : null
  })
  return {
    backgroundColor: '#FFFFFF', animation: false,
    title: { text: 'Response Time (ms)', left: 16, top: 10, textStyle: { color: C.text, fontSize: 13, fontWeight: 'bold' } },
    grid: { top: 50, right: 30, bottom: 45, left: 65 },
    xAxis: { type: 'category', data: ts, boundaryGap: false, axisLabel: { color: '#6B7280', fontSize: 9, formatter: axisLabelFmt(tz, rangeHours), interval: chartInterval(points.length), hideOverlap: true }, axisLine: { lineStyle: { color: '#E5E7EB' } }, splitLine: { show: false } },
    yAxis: { type: 'value', name: 'ms', nameTextStyle: { color: '#6B7280', fontSize: 9 }, axisLabel: { color: '#6B7280', fontSize: 9 }, splitLine: { lineStyle: { color: '#F3F4F6', type: 'dashed' } }, min: 0 },
    series: [{ type: 'line', data: vals, smooth: points.length <= 500, symbol: 'none', connectNulls: false, lineStyle: { width: points.length > 1500 ? 1 : 1.5, color: C.primary }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(79,70,229,0.18)' }, { offset: 1, color: 'rgba(79,70,229,0.0)' }] } }, sampling: points.length > 1000 ? 'lttb' : undefined }],
  }
}

function buildLossChartOption(points: MetricPoint[], tz: string, rangeHours: number): echarts.EChartsOption {
  const ts = points.map(p => p.timestamp)
  const vals = points.map(p => {
    const up = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    if (!up) return 100
    return p.packet_loss !== null ? p.packet_loss * 100 : 0
  })
  return {
    backgroundColor: '#FFFFFF', animation: false,
    title: { text: 'Packet Loss & Downtime (%)', left: 16, top: 10, textStyle: { color: C.text, fontSize: 13, fontWeight: 'bold' } },
    grid: { top: 50, right: 30, bottom: 45, left: 65 },
    xAxis: { type: 'category', data: ts, boundaryGap: true, axisLabel: { color: '#6B7280', fontSize: 9, formatter: axisLabelFmt(tz, rangeHours), interval: chartInterval(points.length), hideOverlap: true }, axisLine: { lineStyle: { color: '#E5E7EB' } }, splitLine: { show: false } },
    yAxis: { type: 'value', name: '%', nameTextStyle: { color: '#6B7280', fontSize: 9 }, max: 100, axisLabel: { color: '#6B7280', fontSize: 9, formatter: (v: number) => v >= 100 ? 'DOWN' : `${v}%` }, splitLine: { lineStyle: { color: '#F3F4F6', type: 'dashed' } } },
    series: [{ type: 'bar', data: vals, barMaxWidth: points.length > 1500 ? 2 : points.length > 500 ? 3 : 8, itemStyle: { color: ((params: unknown) => { const p = params as { value: number }; return p.value >= 100 ? 'rgba(220,38,38,0.75)' : 'rgba(202,138,4,0.60)' }) as unknown as string }, large: true, largeThreshold: 500 }],
  }
}

// ─── Uptime bar — canvas-rendered ─────────────────────────────
function buildUptimeBarImage(points: MetricPoint[], tz: string, width: number, height: number): string {
  const canvas = document.createElement('canvas')
  canvas.width = width * 2; canvas.height = height * 2
  const ctx = canvas.getContext('2d')!
  ctx.scale(2, 2)
  ctx.fillStyle = '#FFFFFF'; ctx.fillRect(0, 0, width, height)
  if (points.length === 0) return canvas.toDataURL('image/png')

  const barY = 25, barH = 18, barL = 10, barR = width - 10
  const barW = barR - barL, segW = barW / points.length

  points.forEach((p, i) => {
    const up = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    ctx.fillStyle = up ? '#16A34A' : '#DC2626'
    ctx.fillRect(barL + i * segW, barY, Math.max(segW, 1), barH)
  })

  const upCount = points.filter(p => p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)).length
  const pct = ((upCount / points.length) * 100).toFixed(2)

  ctx.font = '9px sans-serif'; ctx.fillStyle = '#6B7280'; ctx.textAlign = 'left'
  ctx.fillText('Uptime Timeline', barL, 16)
  ctx.textAlign = 'right'
  ctx.fillStyle = parseFloat(pct) > 99 ? '#16A34A' : parseFloat(pct) > 95 ? '#CA8A04' : '#DC2626'
  ctx.font = 'bold 10px sans-serif'
  ctx.fillText(`${pct}% uptime`, barR, 16)

  ctx.fillStyle = '#9CA3AF'; ctx.font = '8px sans-serif'
  ctx.textAlign = 'left'; ctx.fillText(fmtDate(points[0]!.timestamp, tz), barL, barY + barH + 14)
  ctx.textAlign = 'right'; ctx.fillText(fmtDate(points[points.length - 1]!.timestamp, tz), barR, barY + barH + 14)

  ctx.textAlign = 'center'
  ctx.fillStyle = '#16A34A'; ctx.fillRect(width / 2 - 30, barY + barH + 6, 8, 8)
  ctx.fillStyle = '#6B7280'; ctx.font = '8px sans-serif'; ctx.textAlign = 'left'; ctx.fillText('Up', width / 2 - 20, barY + barH + 13)
  ctx.fillStyle = '#DC2626'; ctx.fillRect(width / 2 + 5, barY + barH + 6, 8, 8)
  ctx.fillStyle = '#6B7280'; ctx.fillText('Down', width / 2 + 15, barY + barH + 13)

  return canvas.toDataURL('image/png')
}

// ─── Public types ─────────────────────────────────────────────
export interface StatusEvent {
  device_id: string
  old_status: string
  new_status: string
  reason: string
  timestamp: string
  duration_sec: number | null
}

export interface ReportData {
  device: Device
  points: MetricPoint[]
  incidents: StatusEvent[]
  rangeHours: number
  fromDate: Date
  toDate: Date
  timezone: string
}

// ─── MAIN: PDF Builder ────────────────────────────────────────
export async function generateDeviceReport(data: ReportData): Promise<void> {
  const { device, points, incidents, rangeHours, fromDate, toDate, timezone } = data

  const doc = new jsPDF({ orientation: 'portrait', unit: 'mm', format: 'a4' })
  const pw = doc.internal.pageSize.getWidth()   // 210
  const ph = doc.internal.pageSize.getHeight()   // 297
  const ml = 15, mr = 15
  const cw = pw - ml - mr                       // 180

  let y = 0

  // ─── Footer (added at end to all pages) ───
  const addFooters = () => {
    const pages = (doc as unknown as { internal: { getNumberOfPages: () => number } }).internal.getNumberOfPages()
    for (let i = 1; i <= pages; i++) {
      doc.setPage(i)
      doc.setDrawColor(229, 231, 235); doc.setLineWidth(0.3)
      doc.line(ml, ph - 12, pw - mr, ph - 12)
      doc.setFontSize(7); doc.setTextColor(160, 160, 160)
      doc.text(`ZenPlus Device Report  |  ${device.hostname} (${device.ip_address})  |  Generated ${new Date().toLocaleString()}`, pw / 2, ph - 8, { align: 'center' })
      doc.text(`Page ${i} of ${pages}`, pw - mr, ph - 8, { align: 'right' })
    }
  }

  // ─── Section header helper ───
  const sectionHeader = (title: string, yPos: number): number => {
    if (yPos + 20 > ph - 20) { doc.addPage(); yPos = 15 }
    doc.setFontSize(13); doc.setTextColor(17, 24, 39); doc.setFont('helvetica', 'bold')
    doc.text(title, ml, yPos)
    doc.setDrawColor(79, 70, 229); doc.setLineWidth(0.8)
    doc.line(ml, yPos + 2, ml + doc.getTextWidth(title) + 2, yPos + 2)
    doc.setDrawColor(229, 231, 235); doc.setLineWidth(0.3)
    doc.line(ml + doc.getTextWidth(title) + 2, yPos + 2, pw - mr, yPos + 2)
    return yPos + 8
  }

  // ════════════════════════════════════════════════════════════════
  // PAGE 1: COVER + EXECUTIVE SUMMARY + CHARTS
  // ════════════════════════════════════════════════════════════════

  // Top accent bar
  doc.setFillColor(79, 70, 229)
  doc.rect(0, 0, pw, 4, 'F')

  // Branding
  y = 18
  doc.setFontSize(22); doc.setTextColor(79, 70, 229); doc.setFont('helvetica', 'bold')
  doc.text('ZenPlus', ml, y)
  doc.setFontSize(9); doc.setTextColor(107, 114, 128); doc.setFont('helvetica', 'normal')
  doc.text('Network Monitoring Platform', ml + 40, y)

  // Title
  y = 32
  doc.setFontSize(20); doc.setTextColor(17, 24, 39); doc.setFont('helvetica', 'bold')
  doc.text('Device Health Report', ml, y)

  // Device name + badge
  y = 41
  doc.setFontSize(14); doc.setTextColor(79, 70, 229); doc.setFont('helvetica', 'bold')
  doc.text(device.hostname, ml, y)
  doc.setFontSize(10); doc.setTextColor(107, 114, 128); doc.setFont('helvetica', 'normal')
  doc.text(device.ip_address, ml + doc.getTextWidth(device.hostname) + 5, y)

  // Status badge
  doc.setFont('helvetica', 'bold'); doc.setFontSize(9)
  const stLabel = device.status.toUpperCase()
  const stCol = stColors[device.status] || C.textSec
  const [sr, sg, sb] = hexToRgb(stCol)
  const badgeW = doc.getTextWidth(stLabel) + 10
  doc.setFillColor(sr, sg, sb)
  doc.roundedRect(pw - mr - badgeW, 35, badgeW, 7, 2, 2, 'F')
  doc.setTextColor(255, 255, 255)
  doc.text(stLabel, pw - mr - badgeW / 2, 40, { align: 'center' })

  // Report period box
  y = 48
  doc.setFillColor(249, 250, 251); doc.setDrawColor(229, 231, 235)
  doc.roundedRect(ml, y, cw, 16, 2, 2, 'FD')
  doc.setFontSize(8); doc.setFont('helvetica', 'normal'); doc.setTextColor(107, 114, 128)
  doc.text('Report Period:', ml + 5, y + 6)
  doc.setFont('helvetica', 'bold'); doc.setTextColor(31, 41, 55)
  doc.text(`${rangeLabel(rangeHours)}  —  ${fmtShortDate(fromDate.toISOString(), timezone)}  to  ${fmtShortDate(toDate.toISOString(), timezone)}`, ml + 33, y + 6)
  doc.setFont('helvetica', 'normal'); doc.setTextColor(107, 114, 128)
  doc.text('Generated:', ml + 5, y + 12)
  doc.setFont('helvetica', 'bold'); doc.setTextColor(31, 41, 55)
  try {
    doc.text(new Date().toLocaleString('en-US', { timeZone: timezone, year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true }), ml + 30, y + 12)
  } catch {
    doc.text(new Date().toLocaleString(), ml + 30, y + 12)
  }

  // ─── EXECUTIVE SUMMARY ───
  y = 72
  y = sectionHeader('Executive Summary', y)

  // Compute all stats
  const upPoints = points.filter(p => {
    const up = p.is_up === true || (p.is_up as unknown as number) === 1 || (typeof p.is_up === 'number' && (p.is_up as number) > 0.5)
    return up && p.rtt_ms !== null && p.rtt_ms > 0
  })
  const rtts = upPoints.map(p => p.rtt_ms!)
  const avg = rtts.length > 0 ? rtts.reduce((a, b) => a + b, 0) / rtts.length : 0
  const minRtt = rtts.length > 0 ? Math.min(...rtts) : 0
  const maxRtt = rtts.length > 0 ? Math.max(...rtts) : 0
  const sortedRtts = [...rtts].sort((a, b) => a - b)
  const p95 = sortedRtts.length > 0 ? (sortedRtts[Math.floor(sortedRtts.length * 0.95)] || maxRtt) : 0
  const p99 = sortedRtts.length > 0 ? (sortedRtts[Math.floor(sortedRtts.length * 0.99)] || maxRtt) : 0
  const totalChecks = points.length
  const downChecks = points.filter(p => p.is_up === false || (p.is_up as unknown as number) === 0).length
  const availability = totalChecks > 0 ? ((totalChecks - downChecks) / totalChecks * 100) : 0
  const totalIncidents = incidents.filter(e => e.new_status === 'down' || e.new_status === 'degraded').length
  const totalDowntimeSec = incidents.filter(e => e.new_status === 'down' || e.new_status === 'degraded').reduce((acc, e) => acc + (e.duration_sec || 0), 0)
  const jitters = upPoints.filter(p => p.jitter_ms !== null && p.jitter_ms! > 0).map(p => p.jitter_ms!)
  const avgJitter = jitters.length > 0 ? jitters.reduce((a, b) => a + b, 0) / jitters.length : 0

  // KPI cards (2 rows × 3 cols)
  const kpiW = (cw - 8) / 3
  const kpiH = 22
  const drawKpi = (x: number, yy: number, label: string, value: string, accent: string) => {
    doc.setFillColor(249, 250, 251); doc.setDrawColor(229, 231, 235)
    doc.roundedRect(x, yy, kpiW, kpiH, 2, 2, 'FD')
    const [ar, ag, ab] = hexToRgb(accent)
    doc.setFillColor(ar, ag, ab)
    doc.rect(x, yy + 2, 1.5, kpiH - 4, 'F')
    doc.setFontSize(7.5); doc.setTextColor(107, 114, 128); doc.setFont('helvetica', 'normal')
    doc.text(label.toUpperCase(), x + 6, yy + 7)
    doc.setFontSize(13); doc.setTextColor(ar, ag, ab); doc.setFont('helvetica', 'bold')
    doc.text(value, x + 6, yy + 17)
  }

  const kpis = [
    { label: 'Availability', value: `${availability.toFixed(2)}%`, color: availability >= 99 ? C.success : availability >= 95 ? C.warning : C.danger },
    { label: 'Avg Response Time', value: fmtRtt(avg), color: C.primary },
    { label: 'P95 Response Time', value: fmtRtt(p95), color: '#7C3AED' },
    { label: 'Total Incidents', value: String(totalIncidents), color: totalIncidents === 0 ? C.success : C.danger },
    { label: 'Total Downtime', value: fmtDuration(totalDowntimeSec), color: totalDowntimeSec === 0 ? C.success : C.warning },
    { label: 'Total Checks', value: totalChecks.toLocaleString(), color: C.textSec },
  ]

  kpis.forEach((kpi, i) => {
    drawKpi(ml + (i % 3) * (kpiW + 4), y + Math.floor(i / 3) * (kpiH + 3), kpi.label, kpi.value, kpi.color)
  })

  y += 2 * (kpiH + 3) + 6

  // ─── CHARTS ───
  const chartW = 1200, chartH = 360
  const imgH = (cw / chartW) * chartH  // ≈54mm

  if (points.length > 0) {
    // Response Time chart
    y = sectionHeader('Response Time Trend', y)
    const rttImg = renderChartToImage(buildRttChartOption(points, timezone, rangeHours), chartW, chartH)
    doc.addImage(rttImg, 'PNG', ml, y, cw, imgH)
    y += imgH + 5

    // Packet Loss chart
    if (y + imgH + 12 > ph - 20) { doc.addPage(); y = 15 }
    y = sectionHeader('Packet Loss & Downtime', y)
    const lossImg = renderChartToImage(buildLossChartOption(points, timezone, rangeHours), chartW, chartH)
    doc.addImage(lossImg, 'PNG', ml, y, cw, imgH)
    y += imgH + 5

    // Uptime timeline
    if (y + 22 > ph - 20) { doc.addPage(); y = 15 }
    y = sectionHeader('Uptime Timeline', y)
    const uptimeImg = buildUptimeBarImage(points, timezone, 900, 60)
    const uptimeH = (cw / 900) * 60
    doc.addImage(uptimeImg, 'PNG', ml, y, cw, uptimeH)
    y += uptimeH + 5
  }

  // ─── PERFORMANCE STATISTICS TABLE ───
  if (y + 55 > ph - 20) { doc.addPage(); y = 15 }
  y = sectionHeader('Performance Statistics', y)

  autoTable(doc, {
    startY: y,
    head: [['Metric', 'Value', 'Metric', 'Value']],
    body: [
      ['Availability', `${availability.toFixed(3)}%`, 'Total Checks', totalChecks.toLocaleString()],
      ['Avg RTT', fmtRtt(avg), 'Min RTT', fmtRtt(minRtt)],
      ['Max RTT', fmtRtt(maxRtt), 'P95 RTT', fmtRtt(p95)],
      ['P99 RTT', fmtRtt(p99), 'Avg Jitter', fmtRtt(avgJitter)],
      ['Up Checks', `${totalChecks - downChecks}`, 'Down Checks', `${downChecks}`],
      ['Total Incidents', `${totalIncidents}`, 'Total Downtime', fmtDuration(totalDowntimeSec)],
    ],
    margin: { left: ml, right: mr },
    styles: { fontSize: 8.5, cellPadding: 3, lineColor: [229, 231, 235], lineWidth: 0.2 },
    headStyles: { fillColor: [79, 70, 229], textColor: 255, fontStyle: 'bold', fontSize: 8 },
    alternateRowStyles: { fillColor: [249, 250, 251] },
    columnStyles: {
      0: { fontStyle: 'bold', textColor: [107, 114, 128], cellWidth: 35 },
      1: { textColor: [31, 41, 55], cellWidth: 35 },
      2: { fontStyle: 'bold', textColor: [107, 114, 128], cellWidth: 35 },
      3: { textColor: [31, 41, 55], cellWidth: 35 },
    },
    theme: 'grid',
  })
  y = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 6

  // ─── INCIDENT HISTORY TABLE ───
  if (incidents.length > 0) {
    if (y + 30 > ph - 20) { doc.addPage(); y = 15 }
    y = sectionHeader(`Incident History (${incidents.length} events)`, y)

    const incRows = incidents.map(e => [
      fmtDate(e.timestamp, timezone),
      `${e.old_status.toUpperCase()} → ${e.new_status.toUpperCase()}`,
      e.reason || '--',
      fmtDuration(e.duration_sec),
    ])

    autoTable(doc, {
      startY: y,
      head: [['Time', 'Status Change', 'Reason', 'Duration']],
      body: incRows,
      margin: { left: ml, right: mr },
      styles: { fontSize: 7.5, cellPadding: 2.5, lineColor: [229, 231, 235], lineWidth: 0.2, overflow: 'linebreak' },
      headStyles: { fillColor: [79, 70, 229], textColor: 255, fontStyle: 'bold', fontSize: 7.5 },
      alternateRowStyles: { fillColor: [249, 250, 251] },
      columnStyles: {
        0: { cellWidth: 42, textColor: [55, 65, 81] },
        1: { cellWidth: 32, fontStyle: 'bold' },
        2: { textColor: [107, 114, 128] },
        3: { cellWidth: 22, halign: 'right' as const, fontStyle: 'bold', textColor: [55, 65, 81] },
      },
      didParseCell: (hookData) => {
        const { section, column, cell, row } = hookData
        if (section === 'body' && column.index === 1) {
          const val = (row.raw as string[])?.[1] || ''
          if (val.includes('DOWN') || val.includes('DEGRADED')) {
            cell.styles.textColor = [220, 38, 38]
          } else if (val.endsWith('UP')) {
            cell.styles.textColor = [22, 163, 74]
          }
        }
      },
      theme: 'grid',
    })
    y = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 6
  }

  // ─── DEVICE INFORMATION TABLE ───
  if (y + 50 > ph - 20) { doc.addPage(); y = 15 }
  y = sectionHeader('Device Information', y)

  autoTable(doc, {
    startY: y,
    body: [
      ['Hostname', device.hostname, 'IP Address', device.ip_address],
      ['Type', device.device_type.replace('_', ' '), 'Group', device.group_name || 'None'],
      ['Location', device.location || 'Not set', 'Ping Interval', `${device.ping_interval}s`],
      ['Ping Enabled', device.ping_enabled ? 'Yes' : 'No', 'Description', device.description || 'None'],
      ['Created', fmtDate(device.created_at, timezone), 'Last Updated', fmtDate(device.updated_at, timezone)],
      ['Last Seen', device.last_seen ? fmtDate(device.last_seen, timezone) : 'Never', 'Tags', device.tags?.join(', ') || 'None'],
    ],
    margin: { left: ml, right: mr },
    styles: { fontSize: 8.5, cellPadding: 3, lineColor: [229, 231, 235], lineWidth: 0.2 },
    alternateRowStyles: { fillColor: [249, 250, 251] },
    columnStyles: {
      0: { fontStyle: 'bold', textColor: [107, 114, 128], cellWidth: 30 },
      1: { textColor: [31, 41, 55], cellWidth: 55 },
      2: { fontStyle: 'bold', textColor: [107, 114, 128], cellWidth: 30 },
      3: { textColor: [31, 41, 55] },
    },
    theme: 'grid',
  })

  // ─── ADD FOOTERS TO ALL PAGES ───
  addFooters()

  // ─── SAVE ───
  const filename = `${device.hostname}_Health_Report_${rangeLabel(rangeHours).replace(/\s+/g, '_')}_${new Date().toISOString().slice(0, 10)}.pdf`
  doc.save(filename)
}
