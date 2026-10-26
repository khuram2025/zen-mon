/** Events tab — Windows Event Log summaries.
 *
 *  The agent reports per-interval event counts by channel (Application,
 *  System, …) and level. The tab summarises the window, charts the volume
 *  by level over time, breaks it down by channel, and lists the raw rows. */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertOctagon, AlertTriangle, BarChart3, CheckCircle2, Layers, ListTree, ScrollText, Search,
} from 'lucide-react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '@/lib/api'
import { cn, timeAxisTickFormatter, timeTooltipLabelFormatter } from '@/lib/utils'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import {
  ExportCsvButton, QueryError, TablePager, TableStateRow, cmp, sortIndicator, sortableTh, usePagedRows,
} from '@/components/servers/tables'
import type { ServerEventRow, ServerEventsResponse, ServerItem } from '@/types/servers'
import {
  C, EmptyState, Panel, Pill, SYNC_ID, Segmented, StatTile, fmtNum, type Tone,
} from '../ui'
import { chartFont } from '@/lib/chartTheme'

/* ── Constants ───────────────────────────────────────────────────── */

const EVENT_WINDOWS = [
  { value: '1', label: 'Last hour' },
  { value: '6', label: 'Last 6 hours' },
  { value: '24', label: 'Last 24 hours' },
  { value: '168', label: 'Last 7 days' },
] as const

/** The API's row cap; asking for the maximum keeps the chart honest over 7 days. */
const ROW_LIMIT = 1000

type LevelKey = 'critical' | 'error' | 'warning' | 'information' | 'verbose'
type LevelFilter = 'all' | LevelKey

const LEVELS: { key: LevelKey; label: string; color: string; tone: Tone }[] = [
  { key: 'critical', label: 'Critical', color: C.red, tone: 'danger' },
  // Between danger and warning, from theme tokens so both modes work.
  { key: 'error', label: 'Error', color: `color-mix(in srgb, ${C.red} 60%, ${C.orange})`, tone: 'danger' },
  { key: 'warning', label: 'Warning', color: C.orange, tone: 'warning' },
  { key: 'information', label: 'Information', color: C.sky, tone: 'info' },
  { key: 'verbose', label: 'Verbose', color: C.slate, tone: 'muted' },
]

/** Levels arrive lower-case from the store, but older agents sent "Info". */
function normLevel(lv: string): LevelKey {
  const l = (lv || '').toLowerCase()
  if (l === 'critical' || l === 'error' || l === 'warning' || l === 'verbose') return l
  return 'information'
}

function levelMeta(lv: string) {
  return LEVELS.find((l) => l.key === normLevel(lv)) ?? LEVELS[3]
}

/** Store timestamps are UTC; a naive value must not be read as local time. */
function parseUtc(ts: string): number {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(ts)
  return Date.parse(hasTz ? ts : `${ts.replace(' ', 'T')}Z`)
}

/** Bucket size that keeps the chart to roughly 40–75 bars. */
function bucketMs(hours: number): number {
  const candidates = [5, 15, 30, 60, 180, 360].map((m) => m * 60_000)
  return candidates.find((b) => (hours * 3_600_000) / b <= 75) ?? candidates[candidates.length - 1]
}

function fmtBucket(ms: number): string {
  const m = ms / 60_000
  return m >= 60 ? `${m / 60}h` : `${m} min`
}

type SortKey = 'timestamp' | 'log_name' | 'level' | 'count'

/* ── Tab ─────────────────────────────────────────────────────────── */

export function EventsTab({ server }: { server: ServerItem }) {
  const serverId = server.id
  const [hours, setHours] = useState('24')
  const [level, setLevel] = useState<LevelFilter>('all')
  const [channel, setChannel] = useState('all')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'timestamp', dir: 'desc' })

  const fetchEvents = (lv: LevelFilter) => async (): Promise<ServerEventsResponse> =>
    (await api.get(`/servers/${serverId}/events`, {
      params: { hours: Number(hours), limit: ROW_LIMIT, ...(lv !== 'all' ? { level: lv } : {}) },
    })).data

  // The whole window drives the tiles and chart. When no level is picked the
  // table shares this query (identical key), so there is one request.
  const summaryQ = useQuery<ServerEventsResponse>({
    queryKey: ['servers', serverId, 'events', hours, 'all', ROW_LIMIT],
    queryFn: fetchEvents('all'),
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  })
  // Level is filtered server-side: filtering the most recent N rows
  // client-side could miss critical events that fell outside that slice.
  const tableQ = useQuery<ServerEventsResponse>({
    queryKey: ['servers', serverId, 'events', hours, level, ROW_LIMIT],
    queryFn: fetchEvents(level),
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  })

  const channels = summaryQ.data?.channels ?? tableQ.data?.channels ?? []
  const windowLabel = EVENT_WINDOWS.find((w) => w.value === hours)?.label ?? `Last ${hours}h`
  const rangeHours = Number(hours)

  /* Summary over the whole window (channel filter applies, level does not). */
  const summaryRows = useMemo(() => {
    const rows = summaryQ.data?.items ?? []
    return channel === 'all' ? rows : rows.filter((r) => r.log_name === channel)
  }, [summaryQ.data, channel])

  const totals = useMemo(() => {
    const t: Record<LevelKey, number> = { critical: 0, error: 0, warning: 0, information: 0, verbose: 0 }
    for (const r of summaryRows) t[normLevel(r.level)] += r.count || 0
    return t
  }, [summaryRows])
  const totalEvents = Object.values(totals).reduce((a, b) => a + b, 0)
  const problems = totals.critical + totals.error

  /* Chart rows: fixed buckets across the window, zero-filled. */
  const bucket = bucketMs(rangeHours)
  const chart = useMemo(() => {
    const end = Math.ceil(Date.now() / bucket) * bucket
    const start = end - rangeHours * 3_600_000
    const map = new Map<number, Record<string, number>>()
    for (let t = start; t < end; t += bucket) {
      map.set(t, { ts: t, critical: 0, error: 0, warning: 0, information: 0, verbose: 0 })
    }
    for (const r of summaryRows) {
      const ts = parseUtc(r.timestamp)
      if (Number.isNaN(ts)) continue
      const b = Math.floor(ts / bucket) * bucket
      const row = map.get(b)
      if (row) row[normLevel(r.level)] += r.count || 0
    }
    const rows = [...map.values()]
    const present = LEVELS.filter((l) => rows.some((r) => r[l.key] > 0))
    return { rows, present, start, end }
  }, [summaryRows, bucket, rangeHours])

  const peakBucket = useMemo(
    () => chart.rows.reduce((best, r) => {
      const sum = LEVELS.reduce((s, l) => s + (r[l.key] || 0), 0)
      return sum > best.sum ? { ts: r.ts, sum } : best
    }, { ts: 0, sum: 0 }),
    [chart.rows],
  )

  /* By channel. */
  const byChannel = useMemo(() => {
    const m = new Map<string, Record<LevelKey, number> & { total: number }>()
    for (const name of channels) {
      m.set(name, { critical: 0, error: 0, warning: 0, information: 0, verbose: 0, total: 0 })
    }
    for (const r of summaryQ.data?.items ?? []) {
      const e = m.get(r.log_name) ?? { critical: 0, error: 0, warning: 0, information: 0, verbose: 0, total: 0 }
      e[normLevel(r.level)] += r.count || 0
      e.total += r.count || 0
      m.set(r.log_name, e)
    }
    return [...m.entries()].map(([name, v]) => ({ name, ...v })).sort((a, b) => b.total - a.total)
  }, [summaryQ.data, channels])
  const maxChannel = Math.max(1, ...byChannel.map((c) => c.total))

  /* Table rows. */
  const tableRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let rows = tableQ.data?.items ?? []
    if (channel !== 'all') rows = rows.filter((r) => r.log_name === channel)
    if (q) rows = rows.filter((r) => r.log_name.toLowerCase().includes(q) || r.level.toLowerCase().includes(q))
    const dir = sort.dir === 'asc' ? 1 : -1
    const levelRank = (lv: string) => LEVELS.findIndex((l) => l.key === normLevel(lv))
    return [...rows].sort((a, b) => {
      const av = sort.key === 'timestamp' ? parseUtc(a.timestamp) : sort.key === 'level' ? levelRank(a.level) : a[sort.key]
      const bv = sort.key === 'timestamp' ? parseUtc(b.timestamp) : sort.key === 'level' ? levelRank(b.level) : b[sort.key]
      return cmp(av, bv) * dir
    })
  }, [tableQ.data, channel, search, sort])
  const pager = usePagedRows(tableRows, 50)

  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'log_name' ? 'asc' : 'desc' }))

  const truncated = Boolean(summaryQ.data?.truncated || tableQ.data?.truncated)
  const tick = timeAxisTickFormatter(rangeHours)

  const levelItems: { key: LevelFilter; label: string; count?: number; tone?: Tone }[] = [
    { key: 'all', label: 'All' },
    ...LEVELS.map((l) => ({
      key: l.key,
      label: l.label,
      count: totals[l.key],
      tone: l.tone,
    })),
  ]

  const noData = !summaryQ.isLoading && !summaryQ.isError && channels.length === 0 && (summaryQ.data?.items.length ?? 0) === 0

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-muted">
          Windows Event Log summaries · {windowLabel.toLowerCase()}
          {channel !== 'all' && <> · <span className="font-medium text-text2">{channel}</span></>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={channel} onValueChange={(v) => { setChannel(v); pager.reset() }}>
            <SelectTrigger className="h-8 w-[170px] text-sm" aria-label="Channel"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All channels</SelectItem>
              {channels.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={hours} onValueChange={(v) => { setHours(v); pager.reset() }}>
            <SelectTrigger className="h-8 w-[150px] text-sm" aria-label="Time window"><SelectValue /></SelectTrigger>
            <SelectContent>
              {EVENT_WINDOWS.map((w) => <SelectItem key={w.value} value={w.value}>{w.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          icon={ScrollText}
          label="Total events"
          value={summaryQ.isLoading ? '—' : totalEvents.toLocaleString()}
          sub={`${summaryRows.length.toLocaleString()} summary rows · ${windowLabel.toLowerCase()}`}
          spark={chart.rows.map((r) => LEVELS.reduce((s, l) => s + (r[l.key] || 0), 0))}
          sparkColor={C.sky}
        />
        <StatTile
          icon={AlertOctagon}
          label="Errors + critical"
          value={summaryQ.isLoading ? '—' : problems.toLocaleString()}
          tone={problems > 0 ? 'danger' : 'default'}
          sub={`${totals.critical.toLocaleString()} critical · ${totals.error.toLocaleString()} error`}
          spark={chart.rows.map((r) => (r.critical || 0) + (r.error || 0))}
          sparkColor={C.red}
        />
        <StatTile
          icon={AlertTriangle}
          label="Warnings"
          value={summaryQ.isLoading ? '—' : totals.warning.toLocaleString()}
          tone={totals.warning > 0 ? 'warning' : 'default'}
          sub={totalEvents ? `${((totals.warning / totalEvents) * 100).toFixed(0)}% of all events` : 'none in window'}
          spark={chart.rows.map((r) => r.warning || 0)}
          sparkColor={C.orange}
          onClick={() => { setLevel(level === 'warning' ? 'all' : 'warning'); pager.reset() }}
          active={level === 'warning'}
        />
        <StatTile
          icon={Layers}
          label="Channels checked"
          value={channels.length || '—'}
          sub={<span title={channels.join(', ')}>{channels.length ? channels.join(', ') : 'collector not reporting'}</span>}
        />
      </div>

      {noData ? (
        <Panel icon={ScrollText} title="Event log">
          <EmptyState
            icon={ScrollText}
            title="No event log data collected"
            hint={(
              <>
                Enable the event log collector in this server’s{' '}
                <Link to="/agent-policies" className="font-medium text-primary hover:underline">agent policy</Link>
                {' '}to collect Windows Event Log summaries.
              </>
            )}
          />
        </Panel>
      ) : (
        <>
          {/* Chart + channel breakdown */}
          <div className="grid gap-4 lg:grid-cols-3">
            <Panel
              className="lg:col-span-2"
              icon={BarChart3}
              title="Events over time"
              subtitle={`By level · ${fmtBucket(bucket)} buckets${peakBucket.sum > 0 ? ` · peak ${fmtNum(peakBucket.sum)} at ${timeTooltipLabelFormatter(peakBucket.ts)}` : ''}`}
            >
              {summaryQ.isError ? (
                <QueryError error={summaryQ.error} onRetry={() => summaryQ.refetch()} />
              ) : summaryQ.isLoading ? (
                <Skeleton className="h-[220px] w-full" />
              ) : (
                <EventsChart rows={chart.rows} present={chart.present} domain={[chart.start, chart.end]} tick={tick} bucket={bucket} />
              )}
            </Panel>

            <Panel icon={ListTree} title="By channel" subtitle="Events in window" flush>
              {summaryQ.isLoading ? (
                <div className="p-4"><Skeleton className="h-[200px] w-full" /></div>
              ) : byChannel.length === 0 ? (
                <EmptyState compact title="No channels reported" />
              ) : (
                <ul className="divide-y divide-border/60">
                  {byChannel.map((c) => {
                    const active = channel === c.name
                    return (
                      <li key={c.name}>
                        <button
                          type="button"
                          onClick={() => { setChannel(active ? 'all' : c.name); pager.reset() }}
                          className={cn(
                            'flex w-full flex-col gap-1.5 px-4 py-2.5 text-left transition-colors hover:bg-surface2/40',
                            active && 'bg-primary/5',
                          )}
                          title={active ? 'Show all channels' : `Filter to ${c.name}`}
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className={cn('truncate text-sm font-medium', active ? 'text-primary' : 'text-text')} title={c.name}>
                              {c.name}
                            </span>
                            <span className="shrink-0 text-xs tabular-nums text-muted">
                              {c.total === 0 ? (
                                <span className="inline-flex items-center gap-1 text-success">
                                  <CheckCircle2 className="h-3 w-3" /> clean
                                </span>
                              ) : (
                                <>
                                  {(c.critical + c.error) > 0 && (
                                    <span className="mr-2 font-medium text-danger">{fmtNum(c.critical + c.error)} err</span>
                                  )}
                                  <span className="font-semibold text-text">{c.total.toLocaleString()}</span>
                                </>
                              )}
                            </span>
                          </div>
                          <div className="flex h-1.5 overflow-hidden rounded-full bg-surface2" style={{ width: `${Math.max(4, (c.total / maxChannel) * 100)}%` }}>
                            {LEVELS.map((l) => c[l.key] > 0 && (
                              <div key={l.key} style={{ width: `${(c[l.key] / c.total) * 100}%`, background: l.color }} />
                            ))}
                          </div>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </Panel>
          </div>

          {/* Rows */}
          <Panel
            icon={ScrollText}
            title="Event log summaries"
            subtitle={
              truncated
                ? `Most recent ${ROW_LIMIT.toLocaleString()} rows only — narrow the window or filter by level to see older rows`
                : `${tableRows.length.toLocaleString()} rows · ${windowLabel.toLowerCase()}`
            }
            flush
            actions={(
              <ExportCsvButton
                rows={tableRows}
                columns={[
                  { header: 'Time', value: (e: ServerEventRow) => e.timestamp },
                  { header: 'Log', value: (e: ServerEventRow) => e.log_name },
                  { header: 'Level', value: (e: ServerEventRow) => e.level },
                  { header: 'Events', value: (e: ServerEventRow) => e.count },
                ]}
                filename={`${server.display_name}-events.csv`}
              />
            )}
          >
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-4 py-2.5">
              <Segmented
                size="sm"
                items={levelItems}
                value={level}
                onChange={(k) => { setLevel(k); pager.reset() }}
              />
              <div className="relative w-full sm:w-56">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                <Input
                  value={search}
                  onChange={(e) => { setSearch(e.target.value); pager.reset() }}
                  placeholder="Filter channel or level…"
                  className="h-8 pl-8 text-sm"
                />
              </div>
            </div>
            <Table className="text-sm">
              <THead className="bg-surface2/40">
                <Tr className="hover:bg-transparent">
                  <Th className={cn('h-9 pl-4', sortableTh)} onClick={() => toggleSort('timestamp')}>
                    Time{sortIndicator(sort.key === 'timestamp', sort.dir)}
                  </Th>
                  <Th className={cn('h-9', sortableTh)} onClick={() => toggleSort('log_name')}>
                    Channel{sortIndicator(sort.key === 'log_name', sort.dir)}
                  </Th>
                  <Th className={cn('h-9', sortableTh)} onClick={() => toggleSort('level')}>
                    Level{sortIndicator(sort.key === 'level', sort.dir)}
                  </Th>
                  <Th className={cn('h-9 pr-4 text-right', sortableTh)} onClick={() => toggleSort('count')}>
                    Events{sortIndicator(sort.key === 'count', sort.dir)}
                  </Th>
                </Tr>
              </THead>
              <TBody>
                {tableQ.isError ? (
                  <TableStateRow colSpan={4}><QueryError error={tableQ.error} onRetry={() => tableQ.refetch()} /></TableStateRow>
                ) : tableQ.isLoading ? (
                  <TableStateRow colSpan={4}><div className="py-6"><Skeleton className="h-20 w-full" /></div></TableStateRow>
                ) : tableRows.length === 0 ? (
                  <TableStateRow colSpan={4}>
                    {/* An idle host reports plenty of zero-count rows; those are
                        filtered out server-side, which used to look identical to
                        "the agent never collected anything". */}
                    <EmptyState
                      compact
                      icon={CheckCircle2}
                      title={`No ${level === 'all' ? '' : `${level} `}events${search ? ' matching the filter' : ''} in the ${windowLabel.toLowerCase()}`}
                      hint={channels.length ? `The agent checked ${channel === 'all' ? channels.join(', ') : channel} and found nothing matching.` : undefined}
                    />
                  </TableStateRow>
                ) : pager.pageRows.map((e, i) => {
                  const meta = levelMeta(e.level)
                  const ts = parseUtc(e.timestamp)
                  return (
                    <Tr key={`${e.timestamp}-${e.log_name}-${e.level}-${i}`} className="border-border/60 hover:bg-surface2/40">
                      <Td className="whitespace-nowrap py-2 pl-4 tabular-nums text-text2" title={e.timestamp}>
                        {Number.isNaN(ts) ? e.timestamp : new Date(ts).toLocaleString()}
                      </Td>
                      <Td className="max-w-[260px] truncate py-2 font-medium" title={e.log_name}>{e.log_name}</Td>
                      <Td className="py-2"><Pill tone={meta.tone}>{meta.label}</Pill></Td>
                      <Td className="py-2 pr-4 text-right font-semibold tabular-nums">{e.count.toLocaleString()}</Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
            <TablePager {...pager} noun="rows" />
          </Panel>
        </>
      )}
    </div>
  )
}

/* ── Chart ───────────────────────────────────────────────────────── */

function EventsChart({ rows, present, domain, tick, bucket }: {
  rows: Record<string, number>[]
  present: typeof LEVELS
  domain: [number, number]
  tick: (ts: number) => string
  bucket: number
}) {
  if (present.length === 0) {
    return (
      <EmptyState
        compact
        icon={CheckCircle2}
        title="No events in this window"
        hint="Every channel the agent checked came back empty."
      />
    )
  }
  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1">
        {present.map((l) => (
          <span key={l.key} className="inline-flex items-center gap-1.5 text-xs text-muted">
            <span className="h-2 w-2 rounded-sm" style={{ background: l.color }} />
            {l.label}
          </span>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={rows} syncId={SYNC_ID} margin={{ top: 6, right: 8, bottom: 0, left: 0 }} barCategoryGap="15%">
          <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="ts" type="number" scale="time"
            // Half a bucket either side so the edge bars are not clipped.
            domain={[domain[0] - bucket / 2, domain[1] - bucket / 2]}
            tickFormatter={tick} tick={{ fontSize: chartFont(11), fill: 'rgb(var(--muted))' }}
            tickLine={false} axisLine={false} minTickGap={48}
          />
          <YAxis
            allowDecimals={false}
            tickFormatter={(v: number) => fmtNum(v, 0)} tick={{ fontSize: chartFont(11), fill: 'rgb(var(--muted))' }}
            tickLine={false} axisLine={false} width={48}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: 'rgb(var(--surface))',
              border: '1px solid rgb(var(--border-strong))',
              borderRadius: 10,
              boxShadow: '0 10px 30px rgb(0 0 0 / 0.25)',
              color: 'rgb(var(--text))',
              fontSize: chartFont(12),
              padding: '8px 10px',
            }}
            itemStyle={{ paddingTop: 1, paddingBottom: 1 }}
            labelStyle={{ color: 'rgb(var(--muted))', marginBottom: 4 }}
            labelFormatter={timeTooltipLabelFormatter}
            cursor={{ fill: 'rgb(var(--surface2))', opacity: 0.6 }}
            formatter={(v: number | string, name: string) => [typeof v === 'number' ? v.toLocaleString() : v, name]}
          />
          {present.map((l, i) => (
            <Bar
              key={l.key} dataKey={l.key} name={l.label} stackId="events" fill={l.color}
              maxBarSize={22} isAnimationActive={false}
              radius={i === present.length - 1 ? [2, 2, 0, 0] : undefined}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
