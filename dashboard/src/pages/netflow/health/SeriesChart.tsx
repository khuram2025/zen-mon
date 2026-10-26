import { useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Table as TableIcon, BarChart3 } from 'lucide-react'
import { useTheme } from '@/stores/theme'
import { formatCount, formatCountExact, formatDateTime, formatInterval, zonedDate } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { fillSeries, type FilledPoint, type SeriesPoint } from './model'
import { chartFont } from '@/lib/chartTheme'

// Single series, validated against both chart surfaces (dataviz validator: light #2563eb, dark #3d8ee6).
const SERIES_COLOR = { light: '#2563eb', dark: '#3d8ee6' }

/**
 * Records stored per bucket from the collector statistics. Buckets without a statistics row are
 * gaps (the collector wrote nothing), never zeros; a table view carries every value.
 */
export function SeriesChart({ series, from, to, granularityS, title }: {
  series: SeriesPoint[] | null | undefined
  from: string
  to: string
  granularityS: number
  title: string
}) {
  const theme = useTheme((s) => s.theme)
  const tz = displayTimeZone()
  const [asTable, setAsTable] = useState(false)
  const points = useMemo(() => fillSeries(series, from, to, granularityS), [series, from, to, granularityS])
  const data = useMemo(() => points.map((p) => ({ ...p, value: p.missing ? null : p.records_written ?? 0 })), [points])
  const missing = points.filter((p) => p.missing).length
  const sameDay = points.length > 0 && zonedDate(points[0].t, tz) === zonedDate(points[points.length - 1].t, tz)
  const tick = (t: number) => formatDateTime(t, tz, { zone: 'none', seconds: false, timeOnly: sameDay })
  const color = theme === 'dark' ? SERIES_COLOR.dark : SERIES_COLOR.light
  return (
    <figure className="space-y-2">
      <figcaption className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="font-medium text-text">
          {title} <span className="font-normal text-text2">· {formatInterval(granularityS)} buckets, {formatDateTime(from, tz, { seconds: false })} – {formatDateTime(to, tz, { seconds: false, timeOnly: sameDay })}</span>
        </span>
        <button
          type="button"
          onClick={() => setAsTable((v) => !v)}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs font-medium text-text2 hover:bg-surface2 hover:text-text"
          aria-pressed={asTable}
        >
          {asTable ? <BarChart3 className="h-3.5 w-3.5" aria-hidden /> : <TableIcon className="h-3.5 w-3.5" aria-hidden />}
          {asTable ? 'Show chart' : 'Show table'}
        </button>
      </figcaption>
      {asTable ? (
        <div className="max-h-72 overflow-auto rounded-md border border-border">
          <Table>
            <THead>
              <Tr>
                <Th>Bucket start</Th>
                <Th className="text-right">Datagrams</Th>
                <Th className="text-right">Decoded</Th>
                <Th className="text-right">Stored</Th>
                <Th className="text-right">Dropped</Th>
                <Th className="text-right">Waiting for template</Th>
              </Tr>
            </THead>
            <TBody>
              {[...points].reverse().map((p) => (
                <Tr key={p.t}>
                  <Td className="whitespace-nowrap text-xs tabular-nums">{formatDateTime(p.t, tz, { seconds: false })}</Td>
                  {p.missing ? (
                    <Td colSpan={5} className="text-xs text-text2">No statistics recorded</Td>
                  ) : (
                    <>
                      <Td className="text-right text-xs tabular-nums">{formatCountExact(p.datagrams)}</Td>
                      <Td className="text-right text-xs tabular-nums">{formatCountExact(p.records_decoded)}</Td>
                      <Td className="text-right text-xs tabular-nums">{formatCountExact(p.records_written)}</Td>
                      <Td className="text-right text-xs tabular-nums">{formatCountExact(p.dropped)}</Td>
                      <Td className="text-right text-xs tabular-nums">{formatCountExact(p.waiting)}</Td>
                    </>
                  )}
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      ) : (
        <div className="h-48 w-full" role="img" aria-label={`${title}; ${missing} of ${points.length} buckets have no statistics. Use Show table for the values.`}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barCategoryGap={1}>
              <CartesianGrid vertical={false} stroke="rgb(var(--border))" strokeWidth={1} />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={[points[0]?.t ?? 'auto', (points[points.length - 1]?.t ?? 0) + granularityS * 1000]}
                tickFormatter={tick}
                tick={{ fontSize: chartFont(11), fill: 'rgb(var(--text2))' }}
                stroke="rgb(var(--border))"
                minTickGap={40}
              />
              <YAxis
                width={48}
                tickFormatter={(v) => formatCount(v)}
                tick={{ fontSize: chartFont(11), fill: 'rgb(var(--text2))' }}
                stroke="rgb(var(--border))"
                allowDecimals={false}
              />
              <Tooltip
                cursor={{ fill: 'rgb(var(--surface2))' }}
                content={({ active, payload }) => {
                  const p = active && payload?.[0]?.payload as (FilledPoint & { value: number | null }) | undefined
                  if (!p) return null
                  return (
                    <div className="rounded-md border border-border bg-surface px-2.5 py-2 text-xs text-text shadow-elevated">
                      <div className="font-semibold">{formatDateTime(p.t, tz, { seconds: false })}</div>
                      {p.missing ? (
                        <div className="text-text2">No statistics recorded for this bucket</div>
                      ) : (
                        <dl className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5 tabular-nums">
                          <dt className="text-text2">Stored</dt><dd className="text-right">{formatCountExact(p.records_written)}</dd>
                          <dt className="text-text2">Decoded</dt><dd className="text-right">{formatCountExact(p.records_decoded)}</dd>
                          <dt className="text-text2">Datagrams</dt><dd className="text-right">{formatCountExact(p.datagrams)}</dd>
                          <dt className="text-text2">Dropped</dt><dd className="text-right">{formatCountExact(p.dropped)}</dd>
                          <dt className="text-text2">Waiting for template</dt><dd className="text-right">{formatCountExact(p.waiting)}</dd>
                          <dt className="text-text2">Sequence gaps</dt><dd className="text-right">{formatCountExact(p.sequence_gaps)}</dd>
                        </dl>
                      )}
                    </div>
                  )
                }}
              />
              <Bar dataKey="value" fill={color} radius={[4, 4, 0, 0]} maxBarSize={24} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
      {missing > 0 && (
        <p className="text-xs text-text2">
          {missing === points.length
            ? 'No collector statistics were recorded in this period.'
            : `${missing} of ${points.length} buckets have no statistics (collector stopped, or not yet recording); they are shown as gaps, not zero.`}
        </p>
      )}
    </figure>
  )
}
