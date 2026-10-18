import { useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Download, Image as ImageIcon, X } from 'lucide-react'
import { useDeviceStatusExplanation } from '@/components/devices/DeviceStatusExplanation'
import { packetLossPercent } from '@/components/devices/availability'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { cn } from '@/lib/utils'
import { Panel, Legend, SERIES_COLORS, StatsRow, TimeSeriesPanel, type Point, type SeriesDef, type Threshold } from './charts'
import {
  formatAbsolute, formatLossPct, formatMs, formatReading, formatUsagePct, latencyDomain, parseTime, PERCENT_DOMAIN, sig3, statsOf,
} from './format'
import { exportPanelsPng } from './exportPng'
import { usePingMetrics, useSnmpMetrics, useSnmpMetricsBefore, useTemplateInsights } from './hooks'
import { exportFilename, periodDelta, previousWindow, shiftPoints, type PeriodDelta } from './performanceModel'
import type { DeviceRange, DeviceRecord, DeviceSummary, MetricSeriesMap, Thresholds } from './types'

/* Trends and capacity: one synced chart stack (shared crosshair) with the
 * configured thresholds drawn as dashed lines and min/avg/p95/max per metric.
 * A reading opened from another tab is pinned at the top. "Compare with the
 * previous period" (?compare=previous) overlays the window just before this one,
 * shifted onto the same axis, and states the change of the average in the
 * metric's own unit. Export as CSV or as one PNG of the chart stack. */

export type PinnedMetric = { seriesKey: string; title: string; unit?: string | null; warn?: number | null; crit?: number | null }

type ChartSpec = {
  id: string
  title: string
  series: SeriesDef[]
  domain: [number, number] | 'auto'
  format: (v: number) => string
  axisFormat?: (v: number) => string
  thresholds: Threshold[]
  note?: string
  /** The previous period's points for the first series (already shifted onto this window). */
  previous?: Point[]
}

function thresholdLines(th: Thresholds | null | undefined, unit = '%'): Threshold[] {
  const out: Threshold[] = []
  if (th?.warn != null) out.push({ value: th.warn, label: `warning ${sig3(th.warn)}${unit}`, tone: 'warning' })
  if (th?.crit != null) out.push({ value: th.crit, label: `critical ${sig3(th.crit)}${unit}`, tone: 'danger' })
  return out
}

function series(metrics: MetricSeriesMap | undefined, key: string): Point[] {
  return (metrics?.[key]?.points || []).map((p) => ({ ts: p.ts, value: p.value }))
}

function toCsv(charts: ChartSpec[]): string {
  const columns = charts.flatMap((c) => c.series.map((s) => ({ name: `${c.title} — ${s.name}`, points: s.points })))
  const byTs = new Map<number, Record<string, number | null>>()
  for (const col of columns) for (const p of col.points) {
    const row = byTs.get(p.ts) || {}
    row[col.name] = p.value
    byTs.set(p.ts, row)
  }
  const escape = (v: string) => /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v
  const header = ['timestamp_utc', ...columns.map((c) => c.name)].map(escape).join(',')
  const lines = [...byTs.entries()].sort((a, b) => a[0] - b[0]).map(([ts, row]) =>
    [new Date(ts).toISOString(), ...columns.map((c) => row[c.name] == null ? '' : String(row[c.name]))].map(escape).join(','))
  return [header, ...lines].join('\n')
}

export function PerformanceTab({ device, summary, range, pinned, onClearPin }: {
  device: DeviceRecord
  summary?: DeviceSummary
  range: DeviceRange
  pinned?: PinnedMetric | null
  onClearPin: () => void
}) {
  const id = device.id
  const snmp = !!device.snmp_enabled
  const hours = Math.max(1, Math.round(range.hours))
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const [params, setParams] = useSearchParams()
  const compare = params.get('compare') === 'previous'
  const setCompare = (on: boolean) => setParams((prev) => {
    const p = new URLSearchParams(prev)
    if (on) p.set('compare', 'previous'); else p.delete('compare')
    return p
  }, { replace: true })
  const before = previousWindow(fromTs, toTs)
  const ping = usePingMetrics(id, range, !!device.ping_enabled)
  const metrics = useSnmpMetrics(id, hours, snmp)
  const prevRange: DeviceRange = { ...range, fromISO: new Date(before.fromTs).toISOString(), toISO: new Date(before.toTs).toISOString(), isCustom: true }
  const prevPing = usePingMetrics(id, prevRange, !!device.ping_enabled && compare)
  const prevMetrics = useSnmpMetricsBefore(id, hours, hours, snmp && compare)
  const stackRef = useRef<HTMLDivElement>(null)
  const [exporting, setExporting] = useState(false)
  const insights = useTemplateInsights(id, snmp && !!device.profile_name)
  const { data: explanation } = useDeviceStatusExplanation(id, !!device.ping_enabled)
  const isF5 = /f5/i.test(device.vendor || '') || /f5 big-ip/i.test(device.profile_name || '')

  const charts = useMemo<ChartSpec[]>(() => {
    const out: ChartSpec[] = []
    const m = metrics.data
    // Previous period: SNMP windows are whole hours back from now, ping windows the range just before.
    const pm = compare ? prevMetrics.data : undefined
    const snmpShift = hours * 3_600_000
    const prevSnmp = (key: string) => (pm ? shiftPoints(series(pm, key), snmpShift) : undefined)
    const prevPoints = compare ? prevPing.data?.points || [] : []
    if (pinned) {
      out.push({
        id: 'pinned', title: pinned.title, series: [{ key: 'pinned', name: pinned.title, color: SERIES_COLORS.primary, points: series(m, pinned.seriesKey) }],
        domain: pinned.unit === '%' ? PERCENT_DOMAIN : 'auto', format: (v) => formatReading(v, pinned.unit, pinned.title), previous: prevSnmp(pinned.seriesKey),
        thresholds: thresholdLines({ warn: pinned.warn, crit: pinned.crit }, pinned.unit === '%' ? '%' : pinned.unit ? ` ${pinned.unit}` : ''),
      })
    }
    if (snmp) {
      const cpu = series(m, 'cpu')
      if (cpu.length) out.push({ id: 'cpu', title: 'CPU', series: [{ key: 'cpu', name: 'CPU', color: SERIES_COLORS.info, points: cpu }], domain: PERCENT_DOMAIN, format: formatUsagePct, axisFormat: (v) => `${v}%`, thresholds: thresholdLines(summary?.kpis.cpu_thresholds), previous: prevSnmp('cpu') })
      const memKey = isF5 ? 'f5_system_memory_pct' : 'memory'
      const mem = series(m, memKey)
      if (mem.length) out.push({ id: 'memory', title: isF5 ? 'System RAM' : 'Memory', series: [{ key: 'memory', name: isF5 ? 'System RAM (used / total)' : 'Memory', color: SERIES_COLORS.accent, points: mem }], domain: PERCENT_DOMAIN, format: formatUsagePct, axisFormat: (v) => `${v}%`, thresholds: thresholdLines(summary?.kpis.memory_thresholds), previous: prevSnmp(memKey) })
      if (isF5) {
        const parts: SeriesDef[] = [
          { key: 'tmm', name: 'TMM', color: SERIES_COLORS.primary, points: series(m, 'f5_tmm_memory_pct') },
          { key: 'host', name: 'Non-TMM', color: SERIES_COLORS.accent, points: series(m, 'f5_host_memory_pct') },
          { key: 'swap', name: 'Swap', color: SERIES_COLORS.warning, points: series(m, 'f5_swap_memory_pct') },
        ].filter((s) => s.points.length)
        if (parts.length) out.push({ id: 'f5-memory', title: 'F5 memory by pool', series: parts, domain: PERCENT_DOMAIN, format: (v) => `${v.toFixed(1)}%`, axisFormat: (v) => `${v}%`, thresholds: [], note: 'TMM, non-TMM and swap are separate pools; overall RAM is the System RAM chart.' })
      }
    }
    if (device.ping_enabled) {
      const points = ping.data?.points || []
      const rtt = points.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: p.rtt_ms ?? null }))
      const loss = points.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: packetLossPercent(p.packet_loss) }))
      const jitter = points.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: p.jitter_ms ?? null }))
      const rttLimit = explanation?.thresholds?.degraded_rtt_ms ?? null
      const lossLimit = explanation?.thresholds?.degraded_loss_pct ?? null
      out.push({ id: 'latency', title: 'Latency', series: [
        { key: 'rtt', name: 'Round-trip time', color: SERIES_COLORS.primary, points: rtt },
        ...(jitter.some((p) => p.value != null) ? [{ key: 'jitter', name: 'Jitter', color: SERIES_COLORS.muted, points: jitter }] : []),
      ], domain: latencyDomain(rtt.map((p) => p.value), rttLimit), format: formatMs, axisFormat: (v) => `${sig3(v)} ms`,
      thresholds: rttLimit != null ? [{ value: rttLimit, label: `Degraded limit ${sig3(rttLimit)} ms`, tone: 'warning' }] : [],
      previous: compare ? shiftPoints(prevPoints.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: p.rtt_ms ?? null })), before.shiftMs) : undefined })
      out.push({ id: 'loss', title: 'Packet loss', series: [{ key: 'loss', name: 'Packet loss', color: SERIES_COLORS.warning, points: loss }],
        domain: [0, Math.min(100, Math.max(10, (lossLimit ?? 0) * 1.5, ...loss.map((p) => p.value ?? 0)))], format: formatLossPct, axisFormat: (v) => `${Math.round(v)}%`,
        thresholds: lossLimit != null ? [{ value: lossLimit, label: `Degraded limit ${sig3(lossLimit)}%`, tone: 'warning' }] : [],
        previous: compare ? shiftPoints(prevPoints.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: packetLossPercent(p.packet_loss) })), before.shiftMs) : undefined })
    }
    const sessions = summary?.kpis.highlights.find((h) => h.kind === 'sessions')
    if (sessions?.series_key && m?.[sessions.series_key]) {
      out.push({ id: 'sessions', title: 'Sessions', series: [{ key: 'sessions', name: 'Active sessions', color: SERIES_COLORS.primary, points: series(m, sessions.series_key) }], domain: 'auto', format: (v) => Math.round(v).toLocaleString(), thresholds: [], previous: prevSnmp(sessions.series_key) })
    }
    // Temperature: the three hottest sensors the template reports in °C.
    const temps: Array<{ name: string; key: string; value: number }> = []
    for (const g of insights.data?.groups || []) {
      if (g.applicable === false) continue
      const celsius = (u?: string | null) => /^°?c$|celsius/i.test((u || '').trim())
      if (g.kind === 'table') {
        const col = (g.columns || []).find((c) => c.type !== 'enum' && c.type !== 'string' && celsius(c.unit))
        if (col) for (const r of g.rows || []) {
          const cell = r.cells[col.key]
          if (cell?.value != null && cell.series_key) temps.push({ name: r.label, key: cell.series_key, value: cell.value })
        }
      } else {
        for (const mt of g.metrics || []) if (mt.has_data && mt.value != null && celsius(mt.unit)) temps.push({ name: mt.name, key: mt.series_key, value: mt.value })
      }
    }
    const hottest = temps.sort((a, b) => b.value - a.value).slice(0, 3)
    const tempColors = [SERIES_COLORS.danger, SERIES_COLORS.warning, SERIES_COLORS.info]
    const tempSeries = hottest.map((t, i) => ({ key: `t${i}`, name: t.name, color: tempColors[i], points: series(m, t.key) })).filter((s) => s.points.length)
    if (tempSeries.length) out.push({ id: 'temperature', title: 'Temperature', series: tempSeries, domain: 'auto', format: (v) => `${sig3(v)} °C`, thresholds: [], note: 'The hottest sensors reported by the template.' })
    return out
  }, [metrics.data, ping.data, insights.data, explanation, summary, pinned, snmp, isF5, device.ping_enabled, compare, prevMetrics.data, prevPing.data, hours, before.shiftMs])

  const download = () => {
    const blob = new Blob([toCsv(charts)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = exportFilename(device.hostname, fromTs, 'csv')
    a.click()
    URL.revokeObjectURL(url)
  }
  const downloadPng = async () => {
    if (!stackRef.current) return
    setExporting(true)
    try {
      await exportPanelsPng(stackRef.current, {
        title: `${device.hostname} · Performance`,
        subtitle: `${range.label} · ${formatAbsolute(fromTs)} – ${formatAbsolute(toTs)}${compare ? ' · dashed: previous period' : ''}`,
        filename: exportFilename(device.hostname, fromTs, 'png'),
      })
    } catch (e) {
      toast.error('Could not export the charts', e instanceof Error ? e.message : undefined)
    } finally {
      setExporting(false)
    }
  }
  const periodName = range.label.replace(/^Last\s+/i, '')

  const loading = (snmp && metrics.isPending) || (device.ping_enabled && ping.isPending)
  return (
    <section aria-label="Device performance" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">{range.label} · {formatAbsolute(fromTs)} – {formatAbsolute(toTs)} · hover any chart to compare the same moment across all of them</p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="inline-flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} className="h-4 w-4 rounded border-border" />
            Compare with the previous {periodName}
          </label>
          <Button size="sm" variant="outline" disabled={!charts.length} onClick={download}><Download className="h-3.5 w-3.5" />Export CSV</Button>
          <Button size="sm" variant="outline" disabled={!charts.length || exporting} onClick={() => void downloadPng()}><ImageIcon className="h-3.5 w-3.5" />{exporting ? 'Exporting…' : 'Export PNG'}</Button>
        </div>
      </div>
      {loading && !charts.length ? (
        <div className="rounded-xl border border-border bg-surface p-6 text-sm text-muted">Loading performance data…</div>
      ) : !charts.length ? (
        <div className="rounded-xl border border-border bg-surface p-6 text-sm text-muted">No performance data for this device in {range.label.replace(/^Last /, 'the last ')}. Ping and SNMP polling feed these charts.</div>
      ) : (
        <div ref={stackRef} className="grid gap-3 xl:grid-cols-2">
          {charts.map((c) => {
            const values = c.series[0]?.points.map((p) => p.value) || []
            const prev = compare && c.previous && c.series[0]
              ? { ...c.series[0], key: `${c.series[0].key}-previous`, name: `${c.series[0].name} · previous ${periodName}`, points: c.previous, dashed: true }
              : null
            const shown = prev ? [...c.series, prev] : c.series
            const stats = statsOf(values)
            // Percentages change in percentage points ("+3 pt"), everything else in its own unit ("+22 ms").
            const deltaFormat = c.domain === PERCENT_DOMAIN ? (v: number) => c.format(v).replace(/\s*%$/, ' pt') : c.format
            const delta: PeriodDelta | null = prev ? periodDelta(values, prev.points.map((p) => p.value), deltaFormat) : null
            const statsText = [stats ? `Min ${c.format(stats.min)} · Avg ${c.format(stats.avg)} · p95 ${c.format(stats.p95)} · Max ${c.format(stats.max)}` : 'No samples',
              delta ? `Avg vs previous ${periodName}: ${delta.text}` : ''].filter(Boolean).join(' · ')
            return (
              <div key={c.id} className={cn('min-w-0', c.id === 'pinned' && 'xl:col-span-2')} data-export-panel data-export-title={c.title}
                data-export-stats={statsText} data-export-full={c.id === 'pinned' ? 'true' : undefined}>
                <Panel title={c.title} className={c.id === 'pinned' ? 'border-primary/50' : undefined}
                  meta={c.note}
                  actions={c.id === 'pinned' ? <button type="button" onClick={onClearPin} className="inline-flex items-center gap-1 text-muted hover:text-text"><X className="h-3.5 w-3.5" />Unpin</button> : shown.length > 1 ? <Legend items={shown.map((s) => ({ name: s.name, color: s.color, dashed: s.dashed }))} /> : undefined}>
                  <TimeSeriesPanel series={shown} fromTs={fromTs} toTs={toTs} rangeHours={range.hours} domain={c.domain} format={c.format}
                    axisFormat={c.axisFormat} thresholds={c.thresholds} syncId="device-performance" height={c.id === 'pinned' ? 220 : 170} />
                  <div className="mt-2 border-t border-border/60 pt-2">
                    <StatsRow stats={stats} format={c.format} />
                    {delta && <p className="mt-1 text-xs text-muted">Average vs the previous {periodName}: <span className="font-medium tabular-nums text-text">{delta.text}</span>
                      {delta.previous != null && <> (previous {c.format(delta.previous)})</>}</p>}
                  </div>
                </Panel>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
