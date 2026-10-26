import { useId, useMemo, useState } from 'react'
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
import { Table } from 'lucide-react'
import { useTheme } from '@/stores/theme'
import { formatBps, formatBpsAxis, formatBytes, formatDateTime, formatInterval } from '@/lib/netflow/format'
import {
  chartRows,
  formatTick,
  isolatedRows,
  maxForScale,
  stateSpans,
  summarizeSeries,
  timeTicks,
  type ChartRow,
  type SeriesPoint,
} from '../overviewModel'
import { chartFont } from '@/lib/chartTheme'

// Validated two-series palette (dataviz reference slots 1–2, checked against this app's light
// #ffffff and dark #0d121b surfaces): throughput blue, previous period orange (dashed).
const SERIES = {
  light: { current: '#2a78d6', prior: '#eb6834', hatch: '#94a3b8' },
  dark: { current: '#3987e5', prior: '#d95926', hatch: '#64748b' },
}

export interface ThroughputChartProps {
  points: SeriesPoint[]
  /** Previous-period buckets, paired by index. */
  prior?: SeriesPoint[] | null
  bucketS: number
  fromMs: number
  toMs: number
  tz: string
  /** Short label for the comparison series ("Previous 24h", "Same time last week"). */
  priorLabel?: string
  /** Height of the plot area (CSS classes; taller on wide screens where the card has room). */
  plotClassName?: string
}

/**
 * Throughput on a real time axis (X-35): the x scale is time, not bucket index, so a collection
 * gap keeps its width, is drawn as a hatched band and breaks the line (null, not zero). Buckets
 * where records arrived but nothing matched are real zeros. Straight segments between bucket
 * averages — no smoothing that invents peaks — and the still-filling last bucket is marked.
 */
export function ThroughputChart({ points, prior, bucketS, fromMs, toMs, tz, priorLabel = 'Previous period', plotClassName = 'h-60 xl:h-80' }: ThroughputChartProps) {
  const theme = useTheme((s) => s.theme)
  const colors = SERIES[theme === 'dark' ? 'dark' : 'light']
  const uid = useId().replace(/:/g, '')
  const [table, setTable] = useState(false)
  const rows = useMemo(() => chartRows(points, bucketS, toMs, prior), [points, bucketS, toMs, prior])
  const gaps = useMemo(() => stateSpans(rows, ['gap', 'unknown', 'expired']), [rows])
  const open = rows.find((r) => r.open)
  // Edge buckets only partly inside the window: their rate is over the covered seconds, and they
  // are shaded so a short first bucket is not read as a full one.
  const edges = useMemo(() => rows.filter((r) => r.partial && !r.open && r.state === 'data'), [rows])
  const ticks = useMemo(() => timeTicks(fromMs, toMs, tz), [fromMs, toMs, tz])
  const span = toMs - fromMs
  const summary = useMemo(() => summarizeSeries(points, bucketS), [points, bucketS])
  // The scale follows complete buckets: a bucket still filling, or an edge bucket of a few seconds,
  // divides by those seconds and would stretch the axis to a spike that never happened; such a
  // bucket is clipped at the top (its tooltip keeps the value).
  const scaleMax = useMemo(() => maxForScale([points, prior], bucketS), [points, prior, bucketS])
  const yDomain: [number, number | 'auto'] = scaleMax != null && scaleMax > 0 ? [0, scaleMax * 1.08] : [0, 'auto']
  const isolated = useMemo(() => isolatedRows(rows, 'bps'), [rows])
  const isolatedPrior = useMemo(() => isolatedRows(rows, 'prior_bps'), [rows])

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text2" aria-hidden>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-0.5 w-4 rounded" style={{ background: colors.current }} />
          Throughput, bit/s ({formatInterval(bucketS)} buckets)
        </span>
        {prior && (
          <span className="inline-flex items-center gap-1.5">
            <span className="w-4 border-t-2 border-dashed" style={{ borderColor: colors.prior }} />
            {priorLabel}
          </span>
        )}
        {gaps.some((g) => g.state === 'gap') && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm border border-border" style={{ background: `repeating-linear-gradient(45deg, ${colors.hatch}66 0 2px, transparent 2px 5px)` }} />
            No records received
          </span>
        )}
        {gaps.some((g) => g.state !== 'gap') && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm border border-dashed border-border" />
            Not tracked / expired
          </span>
        )}
        {open && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm border border-border bg-surface3/70" />
            Bucket still filling
          </span>
        )}
        {edges.length > 0 && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-4 rounded-sm border border-border bg-surface3/40" />
            Partial bucket at the window edge
          </span>
        )}
      </div>
      <p className="sr-only">
        Throughput from {formatDateTime(fromMs, tz)} to {formatDateTime(toMs, tz)}: peak {formatBps(summary.peakBps)}, average{' '}
        {formatBps(summary.avgBps)}, {summary.gaps} bucket{summary.gaps === 1 ? '' : 's'} without received records.
      </p>
      <div className={plotClassName} aria-hidden>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id={`nf-fill-${uid}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={colors.current} stopOpacity={0.28} />
                <stop offset="100%" stopColor={colors.current} stopOpacity={0.03} />
              </linearGradient>
              <pattern id={`nf-gap-${uid}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                <line x1="0" y1="0" x2="0" y2="6" stroke={colors.hatch} strokeWidth="2" strokeOpacity="0.45" />
              </pattern>
            </defs>
            <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.7} vertical={false} />
            {gaps.map((g) => (
              <ReferenceArea
                key={`${g.state}-${g.from}`}
                x1={g.from}
                x2={g.to}
                fill={g.state === 'gap' ? `url(#nf-gap-${uid})` : 'rgb(var(--surface3))'}
                fillOpacity={g.state === 'gap' ? 1 : 0.35}
                strokeOpacity={0}
                ifOverflow="hidden"
              />
            ))}
            {open && <ReferenceArea x1={open.x} x2={open.xEnd} fill="rgb(var(--surface3))" fillOpacity={0.55} strokeOpacity={0} ifOverflow="hidden" />}
            {edges.map((e) => (
              <ReferenceArea key={`edge-${e.x}`} x1={Math.max(e.x, fromMs)} x2={e.xEnd} fill="rgb(var(--surface3))" fillOpacity={0.3} strokeOpacity={0} ifOverflow="hidden" />
            ))}
            <XAxis
              dataKey="x"
              type="number"
              scale="time"
              domain={[fromMs, toMs]}
              ticks={ticks}
              tickFormatter={(v: number) => formatTick(v, span, tz)}
              tick={{ fontSize: chartFont(11), fill: 'rgb(var(--text2))' }}
              axisLine={{ stroke: 'rgb(var(--border-strong))' }}
              tickLine={false}
              allowDataOverflow
            />
            <YAxis
              domain={yDomain}
              allowDataOverflow
              tickFormatter={(v: number) => formatBpsAxis(v)}
              tick={{ fontSize: chartFont(11), fill: 'rgb(var(--text2))' }}
              axisLine={false}
              tickLine={false}
              width={44}
            />
            <Tooltip
              cursor={{ stroke: 'rgb(var(--border-strong))', strokeWidth: 1 }}
              content={<ChartTooltip tz={tz} bucketS={bucketS} priorLabel={priorLabel} colors={colors} />}
            />
            <Area
              dataKey="bps"
              type="linear"
              stroke={colors.current}
              strokeWidth={2}
              fill={`url(#nf-fill-${uid})`}
              connectNulls={false}
              isAnimationActive={false}
              dot={(p: { cx?: number; cy?: number; index?: number }) =>
                p.index != null && isolated[p.index] && p.cx != null && p.cy != null
                  ? <circle key={`d-${p.index}`} cx={p.cx} cy={p.cy} r={3} fill={colors.current} />
                  : <g key={`d-${p.index}`} />}
              activeDot={{ r: 4, stroke: 'rgb(var(--surface))', strokeWidth: 2 }}
            />
            {prior && (
              <Line
                dataKey="prior_bps"
                type="linear"
                stroke={colors.prior}
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={(p: { cx?: number; cy?: number; index?: number }) =>
                  p.index != null && isolatedPrior[p.index] && p.cx != null && p.cy != null
                    ? <circle key={`p-${p.index}`} cx={p.cx} cy={p.cy} r={3} fill={colors.prior} />
                    : <g key={`p-${p.index}`} />}
                connectNulls={false}
                isAnimationActive={false}
                activeDot={{ r: 4, stroke: 'rgb(var(--surface))', strokeWidth: 2 }}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text2">
        <span>
          Peak <b className="font-semibold text-text">{formatBps(summary.peakBps)}</b>
          {summary.peakExcluded > 0 && (
            <span title="A bucket still filling, or an edge bucket covering less than half a bucket, divides its bytes by a few seconds; it is left out of the peak and the scale">
              {' '}(complete buckets; {summary.peakExcluded} short bucket{summary.peakExcluded === 1 ? '' : 's'} left out)
            </span>
          )}
          {summary.peakFromShort && <span> (from a partial bucket)</span>} · average{' '}
          <b className="font-semibold text-text">{formatBps(summary.avgBps)}</b>
          {summary.gaps > 0 && <> · {summary.gaps} bucket{summary.gaps === 1 ? '' : 's'} without records</>}
        </span>
        <button
          type="button"
          onClick={() => setTable((v) => !v)}
          aria-expanded={table}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-medium text-text2 hover:bg-surface2 hover:text-text"
        >
          <Table className="h-3.5 w-3.5" aria-hidden />
          {table ? 'Hide data table' : 'Show data table'}
        </button>
      </div>
      {table && <SeriesTable rows={rows} tz={tz} withPrior={!!prior} priorLabel={priorLabel} />}
    </div>
  )
}

const STATE_NOTE: Record<ChartRow['state'], string> = {
  data: '',
  idle: 'Records received, none matched',
  gap: 'No records received (collection gap)',
  unknown: 'Before delivery tracking began',
  expired: 'Older than the data kept',
  prior_only: 'Previous period only (its last bucket has no counterpart in this window)',
}

function ChartTooltip({
  active,
  payload,
  tz,
  bucketS,
  priorLabel,
  colors,
}: {
  active?: boolean
  payload?: { payload: ChartRow }[]
  tz: string
  bucketS: number
  priorLabel: string
  colors: { current: string; prior: string }
}) {
  if (!active || !payload?.length) return null
  const r = payload[0].payload
  const note = STATE_NOTE[r.state]
  return (
    <div className="min-w-[180px] rounded-md border border-border bg-surface px-2.5 py-2 text-xs shadow-elevated">
      <div className="text-text2">
        {formatDateTime(r.x, tz, { zone: 'none' })} · {formatInterval(bucketS)}
      </div>
      <div className="mt-1 flex items-center gap-1.5">
        <span className="h-0.5 w-3 rounded" style={{ background: colors.current }} />
        <b className="text-sm font-semibold tabular-nums text-text">{r.bps == null ? '—' : formatBps(r.bps)}</b>
        {r.bytes != null && <span className="text-text2">{formatBytes(r.bytes)}</span>}
      </div>
      {note && <div className="mt-0.5 text-text2">{note}</div>}
      {r.partial && !r.open && r.state === 'data' && <div className="mt-0.5 text-text2">Partial bucket at the window edge; rate over its covered seconds</div>}
      {r.open && <div className="mt-0.5 text-text2">Bucket still filling; flows are reported when they end</div>}
      {r.prior_ts != null && (
        <div className="mt-1.5 border-t border-border pt-1.5">
          <div className="text-text2">
            {priorLabel}: {formatDateTime(r.prior_ts, tz, { zone: 'none' })}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5">
            <span className="w-3 border-t-2 border-dashed" style={{ borderColor: colors.prior }} />
            <b className="font-semibold tabular-nums text-text">{r.prior_bps == null ? '—' : formatBps(r.prior_bps)}</b>
            {r.prior_state && STATE_NOTE[r.prior_state] && <span className="text-text2">{STATE_NOTE[r.prior_state]}</span>}
          </div>
        </div>
      )}
    </div>
  )
}

function SeriesTable({ rows, tz, withPrior, priorLabel }: { rows: ChartRow[]; tz: string; withPrior: boolean; priorLabel: string }) {
  return (
    <div className="max-h-64 overflow-auto rounded-md border border-border">
      <table className="w-full text-left text-xs">
        <caption className="sr-only">Throughput by bucket</caption>
        <thead className="sticky top-0 bg-surface2 text-text2">
          <tr>
            <th scope="col" className="px-2 py-1 font-medium">Bucket start ({tz})</th>
            <th scope="col" className="px-2 py-1 text-right font-medium">Rate</th>
            <th scope="col" className="px-2 py-1 text-right font-medium">Bytes</th>
            {withPrior && <th scope="col" className="px-2 py-1 text-right font-medium">{priorLabel}</th>}
            <th scope="col" className="px-2 py-1 font-medium">Note</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.state === 'prior_only' ? 'p' : 'c'}-${r.x}`} className="border-t border-border">
              <td className="whitespace-nowrap px-2 py-0.5 tabular-nums">
                {r.state === 'prior_only' && r.prior_ts != null ? `${formatDateTime(r.prior_ts, tz, { zone: 'none' })} (previous)` : formatDateTime(r.x, tz, { zone: 'none' })}
              </td>
              <td className="px-2 py-0.5 text-right tabular-nums">{r.bps == null ? '—' : formatBps(r.bps)}</td>
              <td className="px-2 py-0.5 text-right tabular-nums">{r.bytes == null ? '—' : formatBytes(r.bytes)}</td>
              {withPrior && <td className="px-2 py-0.5 text-right tabular-nums">{r.prior_bps == null ? '—' : formatBps(r.prior_bps)}</td>}
              <td className="px-2 py-0.5 text-text2">{r.open ? 'Still filling' : STATE_NOTE[r.state] || (r.partial ? 'Partial' : '')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
