import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { BarChart3 } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { cn } from '@/lib/utils'
import { CATEGORICAL, chartTooltipStyle } from '@/pages/dashboards/shared'
import type { ChangesFilters } from '@/pages/inventory/osHardwareChanges'
import type { ChangesResponse } from '@/pages/inventory/types'
import {
  CHART_DENSE_DAYS, CHART_SERIES, changesPageHref, chartDays, chartSummary, seriesTotals, withDay, type ChartDay,
} from './changesModel'
import { InventoryEmpty } from './InventoryEmpty'
import { fmtInt } from './model'
import { Panel } from './PanelCard'
import { ViewToggle, type ChartView } from './ViewToggle'

const th = 'px-2 py-1.5 text-left text-[10.5px] font-semibold uppercase tracking-wider text-muted'
const thRight = cn(th, 'text-right')

function DayTooltip({ active, payload }: { active?: boolean; payload?: { payload: ChartDay }[] }) {
  const d = active ? payload?.[0]?.payload : undefined
  if (!d) return null
  return (
    <div style={chartTooltipStyle} className="px-3 py-2 shadow-lg">
      <div className="mb-1 text-xs font-semibold">{d.full}{d.partialText && <span className="font-normal text-muted"> ({d.partialText})</span>}</div>
      <ul className="space-y-0.5 text-xs">
        {CHART_SERIES.map((s) => (
          <li key={s.key} className="flex items-center gap-2">
            <span aria-hidden className="h-2 w-2 rounded-sm" style={{ background: CATEGORICAL[s.color] }} />
            <span className="flex-1">{s.label}</span>
            <span className="tabular-nums font-medium">{fmtInt(d[s.key])}</span>
          </li>
        ))}
      </ul>
      <div className="mt-1 flex justify-between border-t border-border pt-1 text-xs"><span>All changes</span><span className="tabular-nums font-semibold">{fmtInt(d.total)}</span></div>
      <div className="mt-1 text-[10px] text-muted">Click to list this day</div>
    </div>
  )
}

/**
 * Changes per day as a stacked bar by category, with "View as table" (every day a link that opens that day's list).
 * The legend is a list of links (one per category, with the window's count). Clicking a bar filters the page to that
 * local day in the zone of the series; the table is the keyboard and screen-reader way to do the same.
 */
export function ChangesChart({
  series, since, filters, now, tz, pageSize, onDay,
}: {
  series: ChangesResponse['series']
  /** The window's start (`window.since` of the response): a day it starts in the middle of is marked partial. */
  since?: string | null
  filters: ChangesFilters
  now: number
  tz: string
  pageSize: number
  onDay: (day: string) => void
}) {
  const [view, setView] = useState<ChartView>('chart')
  const days = useMemo(() => chartDays(series, now, tz, since), [series, now, tz, since])
  const totals = useMemo(() => seriesTotals(series), [series])
  const summary = chartSummary(days)
  const dense = days.length > CHART_DENSE_DAYS
  return (
    <Panel
      title="Changes per day"
      icon={<BarChart3 className="h-4 w-4" />}
      hint="by category"
      right={totals.total > 0 ? <ViewToggle value={view} onChange={setView} label="Changes per day view" /> : undefined}
    >
      {totals.total === 0 ? (
        <InventoryEmpty kind="no-data" compact title="No changes on any day of this window">Every day of the window has zero changes. Widen the window to see older ones.</InventoryEmpty>
      ) : view === 'table' ? (
        <div className="max-h-72 overflow-auto">
          <table className="w-full text-xs" data-chart-table="">
            <caption className="sr-only">{summary}</caption>
            <thead className="sticky top-0 bg-surface">
              <tr>
                <th scope="col" className={th}>Day</th>
                {CHART_SERIES.map((s) => <th key={s.key} scope="col" className={thRight}>{s.label}</th>)}
                <th scope="col" className={thRight}>All</th>
              </tr>
            </thead>
            <tbody>
              {[...days].reverse().map((d) => (
                <tr key={d.day} className="border-t border-border/60">
                  <th scope="row" className="px-2 py-1 text-left font-normal">
                    {d.total > 0 ? (
                      <Link to={changesPageHref(withDay(filters, d.day, tz), { page_size: pageSize })} className="rounded-sm hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary" title="List the changes of this day">{d.full}</Link>
                    ) : d.full}
                    {d.partialText && <span className="text-muted"> ({d.partialText})</span>}
                  </th>
                  {CHART_SERIES.map((s) => <td key={s.key} className="px-2 py-1 text-right tabular-nums text-text2">{fmtInt(d[s.key])}</td>)}
                  <td className="px-2 py-1 text-right font-medium tabular-nums">{fmtInt(d.total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="space-y-3">
          <div role="img" aria-label={summary} className="h-[190px] w-full" data-chart="changes-per-day">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={days}
                margin={{ top: 6, right: 8, bottom: 0, left: 0 }}
                barCategoryGap={dense ? '10%' : '25%'}
                onClick={(state: { activePayload?: { payload: ChartDay }[] } | undefined) => {
                  const day = state?.activePayload?.[0]?.payload?.day
                  if (day) onDay(day)
                }}
                style={{ cursor: 'pointer' }}
              >
                <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                  tickLine={false}
                  axisLine={false}
                  interval={dense ? 'preserveStartEnd' : 0}
                  minTickGap={dense ? 24 : 4}
                />
                <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }} tickLine={false} axisLine={false} width={32} />
                <Tooltip content={<DayTooltip />} cursor={{ fill: 'rgb(var(--surface2))' }} isAnimationActive={false} />
                {CHART_SERIES.map((s) => (
                  <Bar key={s.key} dataKey={s.key} name={s.label} stackId="changes" fill={CATEGORICAL[s.color]} isAnimationActive={false} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          {days.some((d) => d.partial) && (
            <p className="text-[11px] text-muted" data-chart-partial="">* A partial day: the window starts part-way through the first day, and today is not over.</p>
          )}
          <ul role="list" aria-label="Categories" className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            {CHART_SERIES.map((s) => (
              <li key={s.key}>
                <Link
                  to={changesPageHref({ ...filters, category: s.key, action: undefined, label: undefined }, { page_size: pageSize })}
                  className="inline-flex items-center gap-1.5 rounded text-text2 outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary"
                  title={`Show only ${s.label} changes`}
                >
                  <span aria-hidden className="h-2.5 w-2.5 rounded-sm" style={{ background: CATEGORICAL[s.color] }} />
                  {s.label} <span className="tabular-nums text-muted">{fmtInt(totals[s.key])}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  )
}
