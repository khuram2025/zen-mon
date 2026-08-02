import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bell,
  CheckCircle2,
  ChevronRight,
  Gauge,
  Layers,
  Loader2,
  Network,
  Search,
  X,
  XCircle,
} from 'lucide-react'
import {
  Area, AreaChart, CartesianGrid, Legend, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import {
  apiErrorMessage, cn, formatBps, formatBpsAxis, formatBytes,
  timeAxisTickFormatter, timeTooltipLabelFormatter,
} from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { toast } from '@/components/ui/Toast'

type LinkRow = {
  device_id: string
  hostname: string
  device_ip: string
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_speed: number | null
  configured_speed_bps: number | null
  effective_speed_bps: number
  oper_status: string | null
  in_bps: number
  out_bps: number
  total_bps: number
  util_pct: number | null
  peak_util_pct: number | null
  has_netflow: boolean
}

type LinkKey = { device_id: string; if_index: number }

const SORT_LABELS: Record<string, string> = {
  util: 'utilization',
  traffic: 'total traffic',
  in: 'inbound',
  out: 'outbound',
  name: 'name',
}

/** Keep the previous page of results on screen while the next one loads. */
const keepPrev = <T,>(prev: T | undefined) => prev

/** Search is a server-side filter, so every keystroke would otherwise be its
 *  own fleet-wide query. Hold off until typing pauses. */
function useDebounced<T>(value: T, delay = 350): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(id)
  }, [value, delay])
  return debounced
}

function utilTone(pct: number | null | undefined): string {
  if (pct == null) return 'text-muted'
  if (pct >= 90) return 'text-danger'
  if (pct >= 75) return 'text-warning'
  if (pct >= 50) return 'text-info'
  return 'text-success'
}

function utilBarColor(pct: number): string {
  if (pct >= 90) return 'bg-danger'
  if (pct >= 75) return 'bg-warning'
  if (pct >= 50) return 'bg-info'
  return 'bg-success'
}

function UtilBar({ pct, className }: { pct: number; className?: string }) {
  const w = Math.min(100, Math.max(pct > 0 ? 2 : 0, pct))
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface2">
        <div className={cn('h-full rounded-full transition-all', utilBarColor(pct))} style={{ width: `${w}%` }} />
      </div>
      <span className={cn('w-11 text-right font-mono text-[11px] tabular-nums', utilTone(pct))}>
        {pct.toFixed(0)}%
      </span>
    </div>
  )
}

function KpiCard({
  label, value, sub, tone,
}: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
        <div className={cn('mt-1 text-2xl font-bold tabular-nums', tone)}>{value}</div>
        {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
      </CardContent>
    </Card>
  )
}

export function LinkUtilizationPage() {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'all' | 'up' | 'down'>('all')
  const [minUtil, setMinUtil] = useState<string>('')
  const [sort, setSort] = useState('util')
  const [selected, setSelected] = useState<LinkKey | null>(null)
  const [alertOpen, setAlertOpen] = useState(false)

  const debouncedSearch = useDebounced(search)

  const qs = useMemo(() => {
    const p = new URLSearchParams({
      hours: String(range.hours),
      limit: '200',
      sort,
    })
    if (debouncedSearch.trim()) p.set('search', debouncedSearch.trim())
    if (status !== 'all') p.set('status', status)
    if (minUtil) p.set('min_util', minUtil)
    return p.toString()
  }, [range.hours, debouncedSearch, status, minUtil, sort])

  const { data, isLoading, isFetching } = useQuery<{
    items: LinkRow[]
    summary: {
      total: number
      high_util: number
      warning_util: number
      with_netflow: number
      avg_util: number | null
      /** How many of `total` are actually in `items` (server caps at 200). */
      returned?: number
    }
  }>({
    queryKey: ['link-utilization', qs],
    queryFn: async () => (await api.get(`/link-utilization?${qs}`)).data,
    refetchInterval: 30_000,
    // Changing a filter or sort should not blank the table behind a spinner.
    placeholderData: keepPrev,
  })

  const items = data?.items || []
  const summary = data?.summary

  const selectedRow = useMemo(
    () => items.find((i) => i.device_id === selected?.device_id && i.if_index === selected?.if_index),
    [items, selected],
  )

  // Drop a selection that the current filters exclude, so the layout doesn't
  // hold a column open for a panel that can no longer render.
  useEffect(() => {
    if (selected && data && !isFetching && !selectedRow) setSelected(null)
  }, [selected, data, isFetching, selectedRow])

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Gauge className="h-6 w-6 text-primary" />
            Link Utilization
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Fleet-wide interface bandwidth ranked by SNMP utilization. Select a link for traffic charts,
            NetFlow breakdown, and usage alerts.
          </p>
        </div>
        <TimeRangePicker
          rangeIdx={rangeIdx}
          isCustom={isCustom}
          customFrom={range.fromISO}
          customTo={range.toISO}
          onPreset={setPreset}
          onCustom={setCustom}
        />
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
        <KpiCard label="Monitored links" value={String(summary?.total ?? '—')} sub={range.label} />
        <KpiCard
          label="High util (≥80%)"
          value={String(summary?.high_util ?? '—')}
          tone={(summary?.high_util || 0) > 0 ? 'text-danger' : undefined}
        />
        <KpiCard
          label="Elevated (50–79%)"
          value={String(summary?.warning_util ?? '—')}
          tone={(summary?.warning_util || 0) > 0 ? 'text-warning' : undefined}
        />
        <KpiCard label="Fleet avg util" value={summary?.avg_util != null ? `${summary.avg_util}%` : '—'} />
        <KpiCard
          label="NetFlow enabled"
          value={String(summary?.with_netflow ?? '—')}
          sub="interfaces with flow data"
          tone="text-info"
        />
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 pt-4">
          <div className="relative min-w-[200px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-9 pl-8"
              placeholder="Search device, interface, alias, IP…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <Select value={status} onValueChange={(v) => setStatus(v as typeof status)}>
            <SelectTrigger className="h-9 w-[130px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All status</SelectItem>
              <SelectItem value="up">Up only</SelectItem>
              <SelectItem value="down">Down only</SelectItem>
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={setSort}>
            <SelectTrigger className="h-9 w-[150px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="util">Sort: Utilization</SelectItem>
              <SelectItem value="traffic">Sort: Total traffic</SelectItem>
              <SelectItem value="in">Sort: Inbound</SelectItem>
              <SelectItem value="out">Sort: Outbound</SelectItem>
              <SelectItem value="name">Sort: Name</SelectItem>
            </SelectContent>
          </Select>
          <Select value={minUtil || 'any'} onValueChange={(v) => setMinUtil(v === 'any' ? '' : v)}>
            <SelectTrigger className="h-9 w-[140px]"><SelectValue placeholder="Min util" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="any">Any util</SelectItem>
              <SelectItem value="50">≥ 50%</SelectItem>
              <SelectItem value="75">≥ 75%</SelectItem>
              <SelectItem value="90">≥ 90%</SelectItem>
            </SelectContent>
          </Select>
          {isFetching && !isLoading && (
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-5">
        {/* Link table */}
        {/* Width must follow the same condition as the panel below, or a
            filtered-out selection leaves 3/5 of the row empty. */}
        <Card className={cn('overflow-hidden', selectedRow ? 'xl:col-span-2' : 'xl:col-span-5')}>
          <CardContent className="p-0">
            <div className="border-b border-border/60 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                <Network className="h-4 w-4 text-primary" />
                Interface links
                {/* The server caps the page — say so rather than let the table
                    look like the complete set. */}
                {summary && summary.returned != null && summary.returned < summary.total && (
                  <span className="text-xs font-normal text-muted">
                    · showing top {summary.returned.toLocaleString()} of {summary.total.toLocaleString()} by {SORT_LABELS[sort] ?? sort}
                  </span>
                )}
                <span className="text-xs font-normal text-muted">· click a row for drill-down</span>
              </div>
            </div>
            <div className="max-h-[calc(100vh-22rem)] overflow-auto">
              <Table>
                <THead className="sticky top-0 z-10 bg-surface2/95 backdrop-blur">
                  <Tr>
                    <Th className="pl-4">Link</Th>
                    <Th>Status</Th>
                    <Th className="text-right">In</Th>
                    <Th className="text-right">Out</Th>
                    <Th className="min-w-[140px]">Utilization</Th>
                    <Th className="pr-4">Flow</Th>
                  </Tr>
                </THead>
                <TBody>
                  {isLoading && (
                    <Tr><Td colSpan={6} className="py-12 text-center text-sm text-muted">
                      <Loader2 className="mx-auto mb-2 h-5 w-5 animate-spin" /> Loading links…
                    </Td></Tr>
                  )}
                  {!isLoading && items.length === 0 && (
                    <Tr><Td colSpan={6} className="py-12 text-center text-sm text-muted">
                      No interfaces match — ensure SNMP polling is active and interfaces are monitored.
                    </Td></Tr>
                  )}
                  {items.map((row) => {
                    const isSel = selected?.device_id === row.device_id && selected?.if_index === row.if_index
                    const isUp = row.oper_status === 'up'
                    const label = row.if_alias || row.if_name || row.if_descr || `if${row.if_index}`
                    return (
                      <Tr
                        key={`${row.device_id}-${row.if_index}`}
                        onClick={() => setSelected({ device_id: row.device_id, if_index: row.if_index })}
                        className={cn(
                          'cursor-pointer transition-colors',
                          isSel ? 'bg-primary/10' : 'hover:bg-surface2/40',
                          !isUp && 'opacity-70',
                        )}
                      >
                        <Td className="py-2.5 pl-4">
                          <div className="flex items-center gap-1.5">
                            <ChevronRight className={cn('h-3.5 w-3.5 text-muted transition-transform', isSel && 'rotate-90 text-primary')} />
                            <div>
                              <div className="text-sm font-medium">{label}</div>
                              <div className="text-[11px] text-muted">
                                <Link
                                  to={`/devices/${row.device_id}`}
                                  className="hover:text-primary"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  {row.hostname}
                                </Link>
                                {row.effective_speed_bps > 0 && (
                                  <span className="ml-1.5">· {formatBps(row.effective_speed_bps)}</span>
                                )}
                              </div>
                            </div>
                          </div>
                        </Td>
                        <Td>
                          {isUp ? (
                            <Badge variant="success" className="gap-1"><CheckCircle2 className="h-3 w-3" /> Up</Badge>
                          ) : (
                            <Badge variant="danger" className="gap-1"><XCircle className="h-3 w-3" /> Down</Badge>
                          )}
                        </Td>
                        <Td className="text-right font-mono text-xs tabular-nums">
                          <span className="inline-flex items-center gap-0.5 text-success">
                            <ArrowDown className="h-3 w-3" />{row.in_bps > 0 ? formatBps(row.in_bps) : '—'}
                          </span>
                        </Td>
                        <Td className="text-right font-mono text-xs tabular-nums">
                          <span className="inline-flex items-center gap-0.5 text-info">
                            <ArrowUp className="h-3 w-3" />{row.out_bps > 0 ? formatBps(row.out_bps) : '—'}
                          </span>
                        </Td>
                        <Td>
                          {row.util_pct != null ? (
                            <UtilBar pct={row.util_pct} />
                          ) : (
                            <span className="text-xs text-muted">—</span>
                          )}
                        </Td>
                        <Td className="pr-4">
                          {row.has_netflow ? (
                            <Badge variant="info" className="text-[10px]">NetFlow</Badge>
                          ) : (
                            <span className="text-xs text-muted">SNMP</span>
                          )}
                        </Td>
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            </div>
          </CardContent>
        </Card>

        {/* Detail panel */}
        {selected && selectedRow && (
          <div className="xl:col-span-3">
            <LinkDetailPanel
              key={`${selected.device_id}-${selected.if_index}`}
              link={selectedRow}
              hours={range.hours}
              rangeLabel={range.label}
              onClose={() => setSelected(null)}
              onAlert={() => setAlertOpen(true)}
            />
          </div>
        )}
      </div>

      {selectedRow && (
        <LinkAlertDialog
          open={alertOpen}
          onOpenChange={setAlertOpen}
          link={selectedRow}
        />
      )}
    </div>
  )
}

function LinkDetailPanel({
  link, hours, rangeLabel, onClose, onAlert,
}: {
  link: LinkRow
  hours: number
  rangeLabel: string
  onClose: () => void
  onAlert: () => void
}) {
  const label = link.if_alias || link.if_name || link.if_descr || `if${link.if_index}`

  const { data, isLoading } = useQuery<{
    traffic: { ts: number; in_bps: number; out_bps: number }[]
    summary: Record<string, number>
    netflow: {
      has_flows: boolean
      timeseries: { ts: number; in_bps: number; out_bps: number; bytes: number }[]
      top_talkers: { ip: string; bytes: number; packets: number; flows: number }[]
      protocols: { protocol: number; bytes: number }[]
    }
  }>({
    queryKey: ['link-utilization', 'detail', link.device_id, link.if_index, hours],
    queryFn: async () =>
      (await api.get(`/link-utilization/${link.device_id}/${link.if_index}?hours=${hours}`)).data,
    refetchInterval: 30_000,
  })

  const traffic = data?.traffic || []
  const nf = data?.netflow
  const sum = data?.summary || {}
  const speed = link.effective_speed_bps
  // HH:mm alone repeats itself once the window spans more than a day.
  const tickFormatter = useMemo(() => timeAxisTickFormatter(hours), [hours])

  const chartData = useMemo(() => {
    const nfMap = new Map((nf?.timeseries || []).map((p) => [p.ts, p]))
    return traffic.map((p) => {
      const nfPt = nfMap.get(p.ts)
      const utilIn = speed > 0 ? (p.in_bps / speed) * 100 : 0
      const utilOut = speed > 0 ? (p.out_bps / speed) * 100 : 0
      return {
        ...p,
        util_in: utilIn,
        util_out: utilOut,
        nf_in: nfPt?.in_bps,
        nf_out: nfPt?.out_bps,
      }
    })
  }, [traffic, nf, speed])

  /* Throughput above the reported link speed means ifSpeed is wrong, not that
     the link is 686% loaded — some ports report a stale 10 Mbps after
     renegotiating. Give the axis a rounded ceiling so the 50/80% guide lines
     stay readable, and say what the reader is looking at. */
  const peakUtil = useMemo(
    () => chartData.reduce((m, p) => Math.max(m, p.util_in || 0, p.util_out || 0), 0),
    [chartData],
  )
  const overCapacity = peakUtil > 100
  const utilAxisMax = overCapacity ? Math.ceil(peakUtil / 50) * 50 : 100

  const PROTO_NAMES: Record<number, string> = {
    1: 'ICMP', 6: 'TCP', 17: 'UDP', 47: 'GRE', 50: 'ESP',
  }

  return (
    <Card className="sticky top-4">
      <CardContent className="space-y-4 pt-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold">{label}</h2>
              {link.has_netflow && <Badge variant="info">NetFlow</Badge>}
              {link.oper_status === 'up' ? (
                <Badge variant="success">Up</Badge>
              ) : (
                <Badge variant="danger">Down</Badge>
              )}
            </div>
            <p className="mt-0.5 text-sm text-muted">
              <Link to={`/devices/${link.device_id}`} className="hover:text-primary">{link.hostname}</Link>
              {' · '}{link.device_ip.replace(/\/\d+$/, '')}
              {speed > 0 && <> · {formatBps(speed)} capacity</>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="outline" onClick={onAlert}>
              <Bell className="h-3.5 w-3.5" /> Create alert
            </Button>
            <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close">
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Mini stats */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <MiniStat label="Current util" value={sum.util_pct != null ? `${sum.util_pct.toFixed(1)}%` : '—'} tone={utilTone(sum.util_pct)} />
          <MiniStat label="Peak util" value={sum.peak_util_pct != null ? `${sum.peak_util_pct.toFixed(1)}%` : '—'} tone={utilTone(sum.peak_util_pct)} />
          <MiniStat label="In max" value={sum.in_max_bps ? formatBps(sum.in_max_bps) : '—'} />
          <MiniStat label="Out max" value={sum.out_max_bps ? formatBps(sum.out_max_bps) : '—'} />
        </div>

        {/* Bandwidth chart */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-medium">Bandwidth · {rangeLabel}</h3>
            <span className="text-[11px] text-muted">SNMP polling</span>
          </div>
          <div className="h-56 rounded-lg border border-border/60 bg-surface2/20 p-2">
            {isLoading ? (
              <div className="flex h-full items-center justify-center text-sm text-muted">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : chartData.length === 0 ? (
              <div className="flex h-full items-center justify-center text-sm text-muted">No traffic in this window</div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                  <defs>
                    <linearGradient id="linkIn" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="linkOut" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} strokeDasharray="3 3" vertical={false} />
                  <XAxis
                    dataKey="ts"
                    tickFormatter={tickFormatter}
                    tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                    axisLine={false}
                    tickLine={false}
                    minTickGap={40}
                  />
                  <YAxis
                    width={52}
                    tickFormatter={(v) => formatBpsAxis(Number(v))}
                    tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                    axisLine={false}
                    tickLine={false}
                  />
                  {speed > 0 && (
                    <ReferenceLine y={speed} stroke="rgb(var(--warning))" strokeDasharray="4 4" label={{ value: '100%', fontSize: 10, fill: 'rgb(var(--warning))' }} />
                  )}
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={timeTooltipLabelFormatter}
                    formatter={(v: number, name: string) => [formatBps(v), name]}
                  />
                  <Legend verticalAlign="top" align="right" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  <Area type="monotone" dataKey="in_bps" name="Inbound" stroke="rgb(var(--success))" fill="url(#linkIn)" strokeWidth={2} dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="out_bps" name="Outbound" stroke="rgb(var(--info))" fill="url(#linkOut)" strokeWidth={2} dot={false} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>

        {/* Utilization % chart */}
        {speed > 0 && chartData.length > 0 && (
          <div>
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium">Utilization %</h3>
              {overCapacity && (
                <span
                  className="text-[11px] text-warning"
                  title="Throughput exceeds the interface speed reported over SNMP, so the percentage is not meaningful. Set a manual speed override for this interface to correct it."
                >
                  Exceeds reported {formatBps(speed)} capacity — link speed looks misreported
                </span>
              )}
            </div>
            <div className="h-36 rounded-lg border border-border/60 bg-surface2/20 p-2">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="ts" hide />
                  <YAxis width={44} domain={[0, utilAxisMax]} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} />
                  <ReferenceLine y={80} stroke="rgb(var(--danger))" strokeDasharray="3 3" strokeOpacity={0.6} />
                  <ReferenceLine y={50} stroke="rgb(var(--warning))" strokeDasharray="3 3" strokeOpacity={0.5} />
                  <Tooltip
                    contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12 }}
                    labelFormatter={timeTooltipLabelFormatter}
                    formatter={(v: number, name: string) => [`${v.toFixed(1)}%`, name]}
                  />
                  <Area type="monotone" dataKey="util_in" name="In util" stroke="rgb(var(--success))" fill="rgb(var(--success))" fillOpacity={0.15} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  <Area type="monotone" dataKey="util_out" name="Out util" stroke="rgb(var(--info))" fill="rgb(var(--info))" fillOpacity={0.15} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* NetFlow section */}
        {link.has_netflow && (
          <div className="rounded-lg border border-info/20 bg-info/5 p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="flex items-center gap-2 text-sm font-semibold">
                <Layers className="h-4 w-4 text-info" />
                NetFlow overlay
              </h3>
              <Link
                to={`/netflow?exporter=${encodeURIComponent(link.device_ip)}&iface=${link.if_index}`}
                className="text-xs text-info hover:underline"
              >
                Open in NetFlow explorer →
              </Link>
            </div>

            {!nf?.has_flows ? (
              <p className="text-xs text-muted">No flow records for this interface in {rangeLabel}.</p>
            ) : (
              <div className="space-y-4">
                <div className="h-32 rounded-md border border-border/40 bg-surface/60 p-2">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={nf.timeseries} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                      <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.3} vertical={false} />
                      <XAxis dataKey="ts" hide />
                      <YAxis width={44} tickFormatter={(v) => formatBpsAxis(Number(v))} tick={{ fontSize: 9, fill: 'rgb(var(--muted))' }} />
                      <Tooltip
                        contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 11 }}
                        labelFormatter={timeTooltipLabelFormatter}
                        formatter={(v: number, n: string) => [formatBps(v), n === 'in_bps' ? 'Flow in' : 'Flow out']}
                      />
                      <Area type="monotone" dataKey="in_bps" stroke="rgb(var(--success))" fill="rgb(var(--success))" fillOpacity={0.2} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                      <Area type="monotone" dataKey="out_bps" stroke="rgb(var(--info))" fill="rgb(var(--info))" fillOpacity={0.2} strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Top talkers</h4>
                    <div className="space-y-1">
                      {(nf.top_talkers || []).slice(0, 6).map((t) => (
                        <div key={t.ip} className="flex items-center justify-between text-xs">
                          <span className="font-mono">{t.ip}</span>
                          <span className="text-muted">{formatBytes(t.bytes)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Protocols</h4>
                    <div className="space-y-1">
                      {(nf.protocols || []).map((p) => (
                        <div key={p.protocol} className="flex items-center justify-between text-xs">
                          <span>{PROTO_NAMES[p.protocol] || `Proto ${p.protocol}`}</span>
                          <span className="text-muted">{formatBytes(p.bytes)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap gap-2 border-t border-border/50 pt-3">
          <Button size="sm" variant="outline" asChild>
            <Link to={`/devices/${link.device_id}/interfaces`}>
              <Activity className="h-3.5 w-3.5" /> All interfaces
            </Link>
          </Button>
          {link.has_netflow && (
            <Button size="sm" variant="outline" asChild>
              <Link to={`/netflow/devices/${encodeURIComponent(link.device_ip)}`}>
                <Network className="h-3.5 w-3.5" /> NetFlow device
              </Link>
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function MiniStat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-md border border-border/50 bg-surface2/30 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('text-sm font-semibold tabular-nums', tone)}>{value}</div>
    </div>
  )
}

function LinkAlertDialog({
  open, onOpenChange, link,
}: { open: boolean; onOpenChange: (v: boolean) => void; link: LinkRow }) {
  const qc = useQueryClient()
  const label = link.if_alias || link.if_name || `if${link.if_index}`
  const [name, setName] = useState('')
  const [metric, setMetric] = useState('if_util_pct')
  const [threshold, setThreshold] = useState('80')
  const [severity, setSeverity] = useState('warning')
  const [minutes, setMinutes] = useState('5')

  const create = useMutation({
    mutationFn: async () => (await api.post('/alert-rules', {
      name: name || `${label} high utilization`,
      metric,
      operator: '>',
      threshold: Number(threshold),
      severity,
      min_duration: Math.round(Number(minutes) * 60),
      device_id: link.device_id,
      target: link.if_name || String(link.if_index),
      enabled: true,
      notify_channels: [],
    })).data,
    onSuccess: () => {
      toast.success('Alert rule created')
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-warning" />
            Usage alert for {label}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <p className="text-xs text-muted">
            Alert on <strong>{link.hostname}</strong> · interface <strong>{label}</strong>
          </p>
          <div>
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={`${label} high utilization`} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Metric</Label>
              <Select value={metric} onValueChange={setMetric}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="if_util_pct">Utilization %</SelectItem>
                  <SelectItem value="if_in_bps">Inbound bps</SelectItem>
                  <SelectItem value="if_out_bps">Outbound bps</SelectItem>
                  <SelectItem value="if_errors">Errors (window)</SelectItem>
                  <SelectItem value="if_discards">Discards (window)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Severity</Label>
              <Select value={severity} onValueChange={setSeverity}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="warning">Warning</SelectItem>
                  <SelectItem value="critical">Critical</SelectItem>
                  <SelectItem value="info">Info</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Threshold {metric === 'if_util_pct' ? '%' : metric.includes('bps') ? '(bps)' : ''}</Label>
              <Input type="number" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
            </div>
            <div>
              <Label>Sustained (min)</Label>
              <Input type="number" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            {create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Create rule'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
