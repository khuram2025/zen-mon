/**
 * Response-time and failure chart (B06, B07, B08, rich chart feature).
 *
 * - Averages are means of successful probes only; failed probes are shown
 *   separately as "Failed probes %" so a partially failing bucket stays
 *   visible at every granularity.
 * - P95 is drawn only from real distributions: exact per-bucket values from
 *   the appliance, exact per-bin values computed here from raw probes, or a
 *   labelled rolling per-probe percentile when bins are too sparse. When the
 *   appliance cannot provide percentiles the line is hidden with a note.
 * - Maintenance windows, status bands and coverage gaps are shaded; drag
 *   across the chart to zoom the page to that span.
 */
import { useMemo, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Table as TableIcon } from 'lucide-react'
import { axisRightPad, cn, timeAxisTickFormatter, timeTicks } from '@/lib/utils'
import { rangePhrase } from '@/components/TimeRangePicker'
import { explainServiceFailure } from '@/components/services/failureReason'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { statusBands, type SeededEvent } from './incidents'
import { windowEnd } from './maintenance'
import { exactTime, formatDur, formatMs } from './format'
import type { MaintenanceWindow, MetricPoint, MetricResponse } from './types'

type Row = {
  ts: number
  end: number
  avg: number | null
  p95: number | null
  max: number | null
  err: number | null
  samples: number | null
  failed: number | null
  error?: string | null
  statusCode?: number | null
  /** Exact timestamp when the row holds a single raw probe. */
  probeTs?: number
}

type Built = {
  rows: Row[]
  mode: 'raw' | 'bucket' | 'legacy-bucket'
  binMs: number
  p95Label: string | null
  p95Note: string | null
}

function quantile(sorted: number[], q: number): number | null {
  if (sorted.length === 0) return null
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

function binLabel(ms: number): string {
  if (ms % 3_600_000 === 0) return `${ms / 3_600_000}-hour`
  if (ms % 60_000 === 0) return `${ms / 60_000}-minute`
  return `${Math.round(ms / 1000)}-second`
}

export function buildChartRows(
  resp: MetricResponse | undefined,
  points: MetricPoint[],
  intervalMs: number,
  fromTs: number,
  toTs: number,
): Built {
  const granularity = resp?.granularity || 'raw'
  if (granularity !== 'raw') {
    const bucketMs = (resp?.bucket_sec ?? (granularity === '1h' ? 3600 : 300)) * 1000
    const v2 = points.some((p) => p.sample_count != null)
    const rows: Row[] = points
      .map((p) => {
        const ts = Date.parse(p.timestamp)
        const samples = p.sample_count ?? null
        const failed = p.failed_count ?? null
        const err = samples != null && samples > 0 && failed != null
          ? (failed / samples) * 100
          : p.uptime_pct != null ? 100 - p.uptime_pct
            : p.is_up === false ? 100 : p.is_up === true ? 0 : null
        return {
          ts, end: ts + bucketMs, avg: p.response_ms, p95: p.p95_response_ms ?? null, max: p.max_response_ms ?? null,
          err, samples, failed,
        }
      })
      .filter((r) => Number.isFinite(r.ts))
      .sort((a, b) => a.ts - b.ts)
    const basis = resp?.percentile_basis
    const hasP95 = v2 && basis !== 'unavailable' && rows.some((r) => r.p95 != null)
    return {
      rows,
      mode: v2 ? 'bucket' : 'legacy-bucket',
      binMs: bucketMs,
      p95Label: hasP95 ? `P95 per ${binLabel(bucketMs)} bucket (exact, successful probes)` : null,
      p95Note: hasP95 ? null
        : !v2 ? 'P95 hidden: this appliance\'s rollups keep only averages and an up/down flag, so partial failures and percentiles are not available at this range.'
          : 'P95 unavailable for this range: data older than raw retention keeps only averages.',
    }
  }

  // Raw probes: bin for display so large windows stay readable, keeping exact stats per bin.
  const probes = points
    .map((p) => ({ ts: Date.parse(p.timestamp), p }))
    .filter((x) => Number.isFinite(x.ts))
    .sort((a, b) => a.ts - b.ts)
  const span = Math.max(1, toTs - fromTs)
  // A whole number of probe intervals per bin, at most ~240 bins, so bins hold even sample counts.
  const binMs = intervalMs * Math.max(1, Math.ceil(span / 240 / intervalMs))
  const bins = new Map<number, { ts: number; ok: number[]; samples: number; failed: number; last?: MetricPoint }>()
  for (const { ts, p } of probes) {
    const k = fromTs + Math.floor((ts - fromTs) / binMs) * binMs
    let b = bins.get(k)
    if (!b) { b = { ts: k, ok: [], samples: 0, failed: 0 }; bins.set(k, b) }
    if (p.is_up == null) continue
    b.samples++
    if (p.is_up === false) b.failed++
    else if (p.response_ms != null && Number.isFinite(p.response_ms)) b.ok.push(p.response_ms)
    b.last = p
  }
  const sortedBins = [...bins.values()].sort((a, b) => a.ts - b.ts)
  const counts = sortedBins.map((b) => b.samples).sort((a, b) => a - b)
  const medianSamples = counts.length ? counts[Math.floor(counts.length / 2)] : 0
  const exactPerBin = medianSamples >= 5

  const rows: Row[] = sortedBins.map((b) => {
    const ok = [...b.ok].sort((x, y) => x - y)
    const single = b.samples === 1 ? b.last : undefined
    return {
      ts: b.ts,
      end: b.ts + binMs,
      avg: ok.length ? ok.reduce((s, x) => s + x, 0) / ok.length : null,
      p95: exactPerBin && ok.length >= 3 ? quantile(ok, 0.95) : null,
      max: ok.length ? ok[ok.length - 1] : null,
      err: b.samples > 0 ? (b.failed / b.samples) * 100 : null,
      samples: b.samples,
      failed: b.failed,
      error: single?.is_up === false ? single.error_message : b.failed > 0 ? b.last?.error_message : null,
      statusCode: single?.status_code ?? null,
      probeTs: single ? Date.parse(single.timestamp) : undefined,
    }
  })

  if (!exactPerBin) {
    // Too few probes per bin for a per-bin percentile: rolling P95 over the last 20 successful probes.
    const window: number[] = []
    let i = 0
    for (const row of rows) {
      while (i < probes.length && probes[i].ts < row.end) {
        const p = probes[i].p
        if (p.is_up === true && p.response_ms != null && Number.isFinite(p.response_ms)) {
          window.push(p.response_ms)
          if (window.length > 20) window.shift()
        }
        i++
      }
      row.p95 = window.length >= 5 ? quantile([...window].sort((a, b) => a - b), 0.95) : null
    }
  }
  const hasP95 = rows.some((r) => r.p95 != null)
  return {
    rows,
    mode: 'raw',
    binMs,
    p95Label: !hasP95 ? null
      : exactPerBin ? `P95 per ${binLabel(binMs)} bin (exact, successful probes)` : 'P95 rolling over the last 20 successful probes',
    p95Note: hasP95 ? null : 'P95 needs at least 5 successful probes.',
  }
}

function coverageGaps(rows: Row[], fromTs: number, toTs: number, nowMs: number, createdTs: number, thresholdMs: number) {
  const lo = Math.max(fromTs, Number.isFinite(createdTs) ? createdTs : fromTs)
  const hi = Math.min(toTs, nowMs)
  const gaps: Array<{ start: number; end: number }> = []
  if (hi <= lo) return gaps
  const measured = rows.filter((r) => (r.samples ?? 1) > 0)
  let cursor = lo
  for (const r of measured) {
    if (r.ts - cursor > thresholdMs) gaps.push({ start: cursor, end: r.ts })
    cursor = Math.max(cursor, r.end)
  }
  if (hi - cursor > thresholdMs) gaps.push({ start: cursor, end: hi })
  return gaps.filter((g) => g.end > g.start)
}

export function PerformanceChart({
  response, points, statusHistory, maintenance, intervalSec, createdAt, rangeLabel, rangeHours, fromTs, toTs, nowMs,
  loading, error, onRetry, stats, onZoom,
}: {
  response?: MetricResponse
  points: MetricPoint[]
  statusHistory: SeededEvent[]
  maintenance: MaintenanceWindow[]
  intervalSec: number
  createdAt?: string | null
  rangeLabel: string
  rangeHours: number
  fromTs: number
  toTs: number
  nowMs: number
  loading: boolean
  error: boolean
  onRetry: () => void
  stats?: Array<{ label: string; value: string; title?: string }>
  onZoom?: (fromISO: string, toISO: string) => void
}) {
  const intervalMs = Math.max(1, intervalSec) * 1000
  const built = useMemo(() => buildChartRows(response, points, intervalMs, fromTs, toTs), [response, points, intervalMs, fromTs, toTs])
  const bands = useMemo(() => statusBands(statusHistory, fromTs, toTs, nowMs), [statusHistory, fromTs, toTs, nowMs])
  const gaps = useMemo(
    () => coverageGaps(built.rows, fromTs, toTs, nowMs, createdAt ? Date.parse(createdAt) : NaN, Math.max(2 * intervalMs, 2 * built.binMs)),
    [built, fromTs, toTs, nowMs, createdAt, intervalMs],
  )
  const maint = useMemo(
    () => maintenance
      .map((w) => ({ start: Math.max(Date.parse(w.starts_at), fromTs), end: Math.min(windowEnd(w), toTs) }))
      .filter((w) => Number.isFinite(w.start) && Number.isFinite(w.end) && w.end > w.start),
    [maintenance, fromTs, toTs],
  )
  const [showTable, setShowTable] = useState(false)
  const [drag, setDrag] = useState<{ a: number; b: number } | null>(null)

  const rows = built.rows
  const axisData = rows.length > 0 ? rows : [{ ts: fromTs, end: fromTs, avg: null, p95: null, max: null, err: null, samples: null, failed: null }, { ts: toTs, end: toTs, avg: null, p95: null, max: null, err: null, samples: null, failed: null }]
  const tickFormatter = useMemo(() => timeAxisTickFormatter(rangeHours), [rangeHours])
  const ticks = useMemo(() => timeTicks(fromTs, toTs, rangeHours), [fromTs, toTs, rangeHours])
  const hasSeries = rows.some((m) => m.avg != null || m.p95 != null)
  const singlePoint = rows.filter((m) => m.avg != null).length === 1
  const failedTotal = rows.reduce((s, r) => s + (r.failed ?? 0), 0)
  const sampleTotal = rows.reduce((s, r) => s + (r.samples ?? 0), 0)
  const partialBuckets = rows.filter((r) => (r.failed ?? 0) > 0 && (r.samples ?? 0) > (r.failed ?? 0)).length

  const truncatedNote = response?.truncated
    ? `Showing the newest ${points.length.toLocaleString()} of ${(response.total_samples ?? points.length).toLocaleString()} probes; older probes in this window are not drawn. Narrow the range to see all of them.`
    : response?.granularity === 'raw' && response?.truncated == null && points.length >= 5000
      ? 'This appliance returns at most 5,000 probes per request, so the most recent probes in this window may be missing. Narrow the range to see all of them.'
      : null

  const rowLabel = (r: Row) => {
    if (r.probeTs != null && Number.isFinite(r.probeTs)) return `Probe at ${exactTime(r.probeTs)}`
    return `${exactTime(r.ts)} – ${exactTime(r.end)}`
  }

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight text-text">Response & failures</h3>
          <div className="mt-0.5 flex flex-wrap items-center gap-3 text-[11px] text-muted">
            <LegendDot color="rgb(var(--info))" label="Avg (successful probes)" />
            {built.p95Label && <LegendDot color="rgb(var(--accent))" label={built.p95Label} />}
            <LegendDot color="rgb(var(--warning))" label="Failed probes %" />
            <LegendDot color="rgb(var(--danger) / 0.4)" label="Confirmed outage" />
            {maint.length > 0 && <LegendDot color="rgb(var(--info) / 0.35)" label="Maintenance" />}
            {gaps.length > 0 && <LegendDot color="rgb(var(--muted) / 0.35)" label="No evidence" />}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {stats?.map((s) => (
            <span key={s.label} title={s.title} className="inline-flex items-baseline gap-1 rounded-md border border-border bg-surface2/50 px-2 py-0.5">
              <span className="text-[9.5px] font-semibold uppercase tracking-wider text-muted">{s.label}</span>
              <span className="font-mono text-[11px] font-medium tabular-nums text-text">{s.value}</span>
            </span>
          ))}
          <span className="text-[11px] font-medium text-muted">{rangeLabel}</span>
        </div>
      </div>
      <div className="flex flex-1 flex-col p-4">
        {loading ? (
          <div className="flex items-center justify-center py-16 text-xs text-muted">Loading probe samples…</div>
        ) : error ? (
          <div className="py-12 text-center text-xs text-danger">
            Could not load probe metrics.
            <button type="button" onClick={onRetry} className="ml-1.5 font-medium underline">Retry</button>
          </div>
        ) : (
          <>
            {truncatedNote && (
              <div role="status" className="mb-2 rounded-md border border-warning/30 bg-warning/5 px-2.5 py-1.5 text-[11px] text-warning">{truncatedNote}</div>
            )}
            <div className="min-h-[236px] w-full flex-1 select-none" aria-label={`Response time and failed probes for ${rangePhrase(rangeLabel)}`} role="img">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart
                  data={axisData}
                  margin={{ top: 8, right: axisRightPad(rangeHours) + 18, bottom: 0, left: 0 }}
                  onMouseDown={(e: any) => { if (onZoom && e?.activeLabel != null) setDrag({ a: Number(e.activeLabel), b: Number(e.activeLabel) }) }}
                  onMouseMove={(e: any) => { if (drag && e?.activeLabel != null) setDrag({ ...drag, b: Number(e.activeLabel) }) }}
                  onMouseUp={() => {
                    if (drag && onZoom) {
                      const lo = Math.min(drag.a, drag.b)
                      const hi = Math.max(drag.a, drag.b) + built.binMs
                      if (hi - lo >= 2 * 60_000 && Math.abs(drag.b - drag.a) > 0) onZoom(new Date(lo).toISOString(), new Date(Math.min(hi, toTs)).toISOString())
                    }
                    setDrag(null)
                  }}
                >
                  <defs>
                    <linearGradient id="svcRespG" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--border) / 0.4)" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="ts" type="number" domain={[fromTs, toTs]} scale="time" ticks={ticks} interval={0}
                    tickFormatter={tickFormatter} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                  <YAxis yAxisId="ms" tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} width={52} axisLine={false} tickLine={false}
                    tickFormatter={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`)} />
                  <YAxis yAxisId="err" orientation="right" tick={{ fontSize: 10, fill: 'rgb(var(--warning))' }} width={36} axisLine={false}
                    tickLine={false} tickFormatter={(v) => `${Number(v).toFixed(0)}%`} domain={[0, (dataMax: number) => Math.max(10, Math.min(100, dataMax * 1.2))]} />
                  <Tooltip content={<ChartTooltip rowLabel={rowLabel} p95Label={built.p95Label} />} />
                  {gaps.map((g, i) => (
                    <ReferenceArea key={`gap-${i}`} yAxisId="ms" x1={g.start} x2={g.end} stroke="none" fill="rgb(var(--muted))" fillOpacity={0.12} />
                  ))}
                  {maint.map((m, i) => (
                    <ReferenceArea key={`mnt-${i}`} yAxisId="ms" x1={m.start} x2={m.end} stroke="rgb(var(--info) / 0.5)" strokeDasharray="3 3" fill="rgb(var(--info))" fillOpacity={0.1} />
                  ))}
                  {bands.map((b, i) => (
                    <ReferenceArea key={`band-${i}`} yAxisId="ms" x1={Math.max(b.start, fromTs)} x2={Math.min(b.end, toTs)} stroke="none"
                      fill={b.kind === 'down' ? 'rgb(var(--danger))' : 'rgb(var(--warning))'} fillOpacity={0.16} />
                  ))}
                  {drag && drag.a !== drag.b && (
                    <ReferenceArea yAxisId="ms" x1={Math.min(drag.a, drag.b)} x2={Math.max(drag.a, drag.b)} stroke="rgb(var(--primary))" fill="rgb(var(--primary))" fillOpacity={0.12} />
                  )}
                  <Area yAxisId="err" type="stepAfter" dataKey="err" name="Failed probes %" stroke="rgb(var(--warning))" fill="rgb(var(--warning) / 0.12)"
                    strokeWidth={1.25} isAnimationActive={false} dot={false} connectNulls={false} />
                  <Area yAxisId="ms" type="monotone" dataKey="avg" name="Avg" stroke="rgb(var(--info))" fill="url(#svcRespG)" strokeWidth={2}
                    isAnimationActive={false} connectNulls={false} dot={singlePoint ? { r: 3 } : false} />
                  {built.p95Label && (
                    <Line yAxisId="ms" type="monotone" dataKey="p95" name="P95" stroke="rgb(var(--accent))" strokeWidth={1.5}
                      isAnimationActive={false} connectNulls={false} dot={false} />
                  )}
                </ComposedChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted">
              <span>
                {!hasSeries
                  ? bands.length > 0
                    ? 'No response samples in this window — outage intervals are shaded from the status log.'
                    : `No probe samples in ${rangePhrase(rangeLabel)}.`
                  : sampleTotal > 0
                    ? `${failedTotal.toLocaleString()} failed of ${sampleTotal.toLocaleString()} probes${partialBuckets > 0 ? ` · ${partialBuckets} ${built.mode === 'raw' ? 'bin' : 'bucket'}${partialBuckets === 1 ? '' : 's'} with partial failures` : ''}`
                    : built.mode === 'legacy-bucket' ? 'Rolled-up averages' : ''}
                {built.p95Note && <span className="ml-2">{built.p95Note}</span>}
                {onZoom && hasSeries && <span className="ml-2 hidden md:inline">Drag across the chart to zoom.</span>}
              </span>
              {rows.length > 0 && (
                <button type="button" aria-expanded={showTable} onClick={() => setShowTable((v) => !v)}
                  className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
                  <TableIcon className="h-3 w-3" /> {showTable ? 'Hide data table' : 'Show data table'}
                </button>
              )}
            </div>
            {showTable && <ChartDataTable rows={rows} rowLabel={rowLabel} showP95={!!built.p95Label} />}
          </>
        )}
      </div>
    </div>
  )
}

function ChartTooltip({ active, payload, rowLabel, p95Label }: any) {
  if (!active || !payload?.length) return null
  const r: Row = payload[0]?.payload
  if (!r || r.samples == null && r.avg == null) return null
  return (
    <div className="max-w-[280px] rounded-md border border-border bg-surface px-2.5 py-2 text-[11px] text-text shadow-lg">
      <div className="mb-1 font-mono text-[10px] text-muted">{rowLabel(r)}</div>
      <div>Avg: <b className="font-mono">{formatMs(r.avg, 'No successful probes')}</b></div>
      {p95Label && <div>P95: <b className="font-mono">{formatMs(r.p95, 'No data')}</b></div>}
      {r.max != null && <div>Max: <b className="font-mono">{formatMs(r.max)}</b></div>}
      {r.samples != null && (
        <div className={cn((r.failed ?? 0) > 0 && 'text-warning')}>
          {r.failed ?? 0} failed of {r.samples} probe{r.samples === 1 ? '' : 's'}
          {r.err != null && r.samples > 1 ? ` (${r.err.toFixed(1)}%)` : ''}
        </div>
      )}
      {r.samples == null && r.err != null && <div>Up flag: {r.err >= 50 ? 'down' : 'up'} (legacy rollup)</div>}
      {r.statusCode != null && <div>HTTP {r.statusCode}</div>}
      {r.error && <div className="mt-1 text-danger">{explainServiceFailure(r.error)}</div>}
    </div>
  )
}

function ChartDataTable({ rows, rowLabel, showP95 }: { rows: Row[]; rowLabel: (r: Row) => string; showP95: boolean }) {
  const shown = [...rows].reverse().slice(0, 200)
  return (
    <div className="mt-3 max-h-[320px] overflow-auto rounded-md border border-border">
      <Table>
        <THead>
          <Tr>
            <Th>Period</Th>
            <Th className="text-right">Avg</Th>
            {showP95 && <Th className="text-right">P95</Th>}
            <Th className="text-right">Max</Th>
            <Th className="text-right">Probes</Th>
            <Th className="text-right">Failed</Th>
          </Tr>
        </THead>
        <TBody>
          {shown.map((r) => (
            <Tr key={r.ts}>
              <Td className="whitespace-nowrap font-mono text-[10.5px]">{rowLabel(r)}</Td>
              <Td className="text-right font-mono text-[11px]">{formatMs(r.avg)}</Td>
              {showP95 && <Td className="text-right font-mono text-[11px]">{formatMs(r.p95)}</Td>}
              <Td className="text-right font-mono text-[11px]">{formatMs(r.max)}</Td>
              <Td className="text-right font-mono text-[11px]">{r.samples ?? '—'}</Td>
              <Td className={cn('text-right font-mono text-[11px]', (r.failed ?? 0) > 0 && 'text-warning')}>{r.failed ?? '—'}</Td>
            </Tr>
          ))}
        </TBody>
      </Table>
      {rows.length > shown.length && (
        <div className="border-t border-border px-3 py-1.5 text-[10px] text-muted">Showing the newest 200 of {rows.length} rows. Narrow the range for more detail.</div>
      )}
      <div className="border-t border-border px-3 py-1.5 text-[10px] text-muted">Periods are {formatDur(((rows[0]?.end ?? 0) - (rows[0]?.ts ?? 0)) / 1000)} wide.</div>
    </div>
  )
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      <span>{label}</span>
    </span>
  )
}
