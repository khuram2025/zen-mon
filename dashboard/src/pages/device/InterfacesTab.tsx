import { Fragment, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, CheckCircle2, ChevronRight, CircleDashed, CircleSlash,
  Gauge, HelpCircle, Info, Loader2, MinusCircle, Pencil, Search, XCircle,
} from 'lucide-react'
import { DeviceWidgetGrid } from '@/components/devices/DeviceWidgetGrid'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { Legend, Panel, SERIES_COLORS, Sparkline, TimeSeriesPanel } from './charts'
import { formatAbsolute, formatBitRate, formatBitRateAxis, formatLinkSpeed, formatRelative, formatUsagePct, MISSING } from './format'
import { UsageBar } from './KpiTile'
import {
  classifyInterface, compareNullable, discardSum, effectiveSpeed, errorSum, interfaceDescription, interfaceName,
  interfaceTypeGroup, matchesSavedFilter, matchesSearch, parseSavedFilter, SAVED_FILTERS, sparkDomain, STATUS_ORDER,
  TYPE_GROUPS, typeGroupLabel, uplinkSpeed, utilisationPct,
  type ErrorTotals, type InterfaceRecord, type InterfaceStatus, type InterfaceTypeGroup, type SavedFilter,
} from './interfaceModel'
import type { DeviceRange, DeviceRecord } from './types'

/* Port-level troubleshooting inside the device shell. The shell owns the time
 * range; rates, errors and charts follow `range.hours`. Widget ids and the
 * "interfaces" layout section are unchanged so saved layouts survive. */

type SortKey = 'name' | 'status' | 'speed' | 'in' | 'out' | 'util' | 'errors' | 'discards'
type SortDir = 'asc' | 'desc'

type MetricPoint = { ts: number; in_bps: number; out_bps: number }

type Row = InterfaceRecord & {
  name: string
  description: string | null
  status: InterfaceStatus
  group: InterfaceTypeGroup
  speed: number | null
  snmpSpeed: number | null
  hasManualSpeed: boolean
  inBps: number | null
  outBps: number | null
  util: number | null
  totals: ErrorTotals | null
  errors: number | null
  discards: number | null
  series: MetricPoint[]
  throughput: Array<number | null>
}

const SPEED_PRESETS_MBPS = [
  { label: '10 Mbps', mbps: 10 },
  { label: '100 Mbps', mbps: 100 },
  { label: '1 Gbps', mbps: 1000 },
  { label: '10 Gbps', mbps: 10000 },
  { label: '40 Gbps', mbps: 40000 },
  { label: '100 Gbps', mbps: 100000 },
] as const

const COLUMNS = 12

function mbpsToBps(mbps: number): number {
  return Math.round(mbps * 1_000_000)
}

function count(n: number | null): string {
  return n == null ? MISSING : n.toLocaleString()
}

export function InterfacesTab({ device, range }: { device: DeviceRecord; range: DeviceRange }) {
  const deviceId = device.id
  const hours = Math.min(720, Math.max(1, Math.round(range.hours)))
  const [params, setParams] = useSearchParams()
  const savedFilter = parseSavedFilter(params.get('ifilter'))

  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState<InterfaceTypeGroup | 'all'>('all')
  const [sortKey, setSortKey] = useState<SortKey>('util')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [speedDialog, setSpeedDialog] = useState<{ mode: 'single' | 'bulk'; rows: Row[] } | null>(null)

  const qc = useQueryClient()

  const ifsQ = useQuery<InterfaceRecord[]>({
    queryKey: ['device', deviceId, 'interfaces'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })
  const metricsQ = useQuery<Record<string, MetricPoint[]>>({
    queryKey: ['device', deviceId, 'if-metrics', hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-if-metrics?hours=${hours}`)).data,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })
  const errorsQ = useQuery<Record<string, ErrorTotals>>({
    queryKey: ['device', deviceId, 'if-error-totals', hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces/error-totals?hours=${hours}`)).data,
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
    retry: 1,
  })
  // A failed query never reads as zero errors.
  const errorTotals = errorsQ.isError ? null : errorsQ.data ?? null

  // The interface endpoints count back from now; a custom window that ended
  // earlier is shown as the same length up to now, and says so.
  const windowTo = useMemo(() => serverNow(), [metricsQ.dataUpdatedAt, hours])
  const windowFrom = windowTo - hours * 3_600_000
  const endsInPast = Date.parse(range.toISO) < windowTo - 10 * 60_000

  const ifs = ifsQ.data ?? []
  const ifMetrics = metricsQ.data ?? {}

  const allRows: Row[] = useMemo(() => ifs.map((i) => {
    const series = ifMetrics[String(i.if_index)] || []
    const last = series[series.length - 1]
    const status = classifyInterface(i)
    const isUp = status.kind === 'up'
    const inBps = isUp && last ? Number(last.in_bps) : null
    const outBps = isUp && last ? Number(last.out_bps) : null
    const speed = effectiveSpeed(i)
    const snmpSpeed = Number(i.if_speed) > 0 ? Number(i.if_speed) : null
    const totals = errorTotals ? errorTotals[String(i.if_index)] ?? null : null
    return {
      ...i,
      name: interfaceName(i),
      description: interfaceDescription(i),
      status,
      group: interfaceTypeGroup(i.if_type),
      speed,
      snmpSpeed,
      hasManualSpeed: Number(i.configured_speed_bps) > 0,
      inBps,
      outBps,
      util: isUp ? utilisationPct(inBps, outBps, speed) : null,
      totals,
      errors: errorSum(totals),
      discards: discardSum(totals),
      series,
      throughput: series.map((p) => (p.in_bps == null || p.out_bps == null ? null : Number(p.in_bps) + Number(p.out_bps))),
    }
  }), [ifs, ifMetrics, errorTotals])

  const ctx = useMemo(() => ({ uplinkSpeed: uplinkSpeed(allRows), errorTotals }), [allRows, errorTotals])

  const filterCounts = useMemo(() => {
    const out = {} as Record<SavedFilter, number>
    for (const f of SAVED_FILTERS) out[f.id] = allRows.filter((r) => matchesSavedFilter(r, f.id, ctx)).length
    return out
  }, [allRows, ctx])

  const typeCounts = useMemo(() => {
    const out = new Map<InterfaceTypeGroup, number>()
    for (const r of allRows) out.set(r.group, (out.get(r.group) || 0) + 1)
    return out
  }, [allRows])

  const filtered = useMemo(() => allRows.filter((r) =>
    matchesSavedFilter(r, savedFilter, ctx)
    && (typeFilter === 'all' || r.group === typeFilter)
    && matchesSearch(r, search),
  ), [allRows, savedFilter, ctx, typeFilter, search])

  const sorted = useMemo(() => {
    const dir: 1 | -1 = sortDir === 'asc' ? 1 : -1
    return [...filtered].sort((a, b) => {
      switch (sortKey) {
        case 'name': return dir * a.name.localeCompare(b.name, undefined, { numeric: true })
        case 'status': return dir * (STATUS_ORDER[a.status.kind] - STATUS_ORDER[b.status.kind]) || a.name.localeCompare(b.name, undefined, { numeric: true })
        case 'speed': return compareNullable(a.speed, b.speed, dir)
        case 'in': return compareNullable(a.inBps, b.inBps, dir)
        case 'out': return compareNullable(a.outBps, b.outBps, dir)
        case 'util': return compareNullable(a.util, b.util, dir)
        case 'errors': return compareNullable(a.errors, b.errors, dir)
        case 'discards': return compareNullable(a.discards, b.discards, dir)
      }
    })
  }, [filtered, sortKey, sortDir])

  // Aggregates across all interfaces, not just the filtered view.
  const summary = useMemo(() => {
    const kinds = { up: 0, down: 0, admin_down: 0, unused: 0, not_present: 0, unmonitored_down: 0, other: 0 }
    for (const r of allRows) kinds[r.status.kind] += 1
    const live = allRows.filter((r) => r.inBps != null && r.outBps != null)
    const totalIn = live.length ? live.reduce((s, r) => s + (r.inBps ?? 0), 0) : null
    const totalOut = live.length ? live.reduce((s, r) => s + (r.outBps ?? 0), 0) : null
    const sized = live.filter((r) => r.speed)
    const capacity = allRows.filter((r) => r.status.kind === 'up').reduce((s, r) => s + (r.speed ?? 0), 0)
    const sizedCapacity = sized.reduce((s, r) => s + (r.speed ?? 0), 0)
    const aggUtil = sizedCapacity > 0
      ? Math.min(100, (Math.max(sized.reduce((s, r) => s + (r.inBps ?? 0), 0), sized.reduce((s, r) => s + (r.outBps ?? 0), 0)) / sizedCapacity) * 100)
      : null
    const top = [...live].sort((a, b) => ((b.inBps ?? 0) + (b.outBps ?? 0)) - ((a.inBps ?? 0) + (a.outBps ?? 0))).slice(0, 5)
    const monitored = allRows.filter((r) => r.monitored).length
    return { total: allRows.length, kinds, totalIn, totalOut, capacity, aggUtil, top, monitored }
  }, [allRows])

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    else {
      setSortKey(k)
      setSortDir(k === 'name' || k === 'status' ? 'asc' : 'desc')
    }
  }

  function setSavedFilter(f: SavedFilter) {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (f === 'all') next.delete('ifilter')
      else next.set('ifilter', f)
      return next
    }, { replace: true })
  }

  const visibleIndexes = useMemo(() => sorted.map((r) => r.if_index), [sorted])
  const allVisibleSelected = visibleIndexes.length > 0 && visibleIndexes.every((idx) => selected.has(idx))

  function toggleSelect(ifIndex: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(ifIndex)) next.delete(ifIndex)
      else next.add(ifIndex)
      return next
    })
  }

  function toggleSelectAllVisible() {
    setSelected((prev) => {
      const next = new Set(prev)
      if (allVisibleSelected) visibleIndexes.forEach((idx) => next.delete(idx))
      else visibleIndexes.forEach((idx) => next.add(idx))
      return next
    })
  }

  const selectedRows = useMemo(() => allRows.filter((r) => selected.has(r.if_index)), [allRows, selected])

  const saveSpeedMutation = useMutation({
    mutationFn: async ({ ifIndexes, configured_speed_bps }: { ifIndexes: number[]; configured_speed_bps: number | null }) => {
      if (ifIndexes.length === 1) {
        return (await api.patch(`/devices/${deviceId}/interfaces/${ifIndexes[0]}`, { configured_speed_bps })).data
      }
      return (await api.post(`/devices/${deviceId}/interfaces/bulk-speed`, { if_indexes: ifIndexes, configured_speed_bps })).data
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['device', deviceId, 'interfaces'] })
      setSpeedDialog(null)
      if (vars.ifIndexes.length > 1) setSelected(new Set())
      toast.success(
        vars.configured_speed_bps
          ? `Speed set on ${vars.ifIndexes.length} interface(s)`
          : `Manual speed cleared on ${vars.ifIndexes.length} interface(s)`,
      )
    },
    onError: (e) => toast.error('Failed to update speed', apiErrorMessage(e)),
  })

  const notes: ReactNode[] = []
  if (endsInPast) notes.push(<span key="window">Interface data covers the last {hours} h up to now; earlier windows are not kept per interface here.</span>)
  if (errorsQ.isError) notes.push(<span key="errors" className="inline-flex items-center gap-1"><AlertTriangle className="h-3.5 w-3.5 text-warning" aria-hidden />Error counts unavailable: the metrics store did not answer.</span>)
  if (metricsQ.isError) notes.push(<span key="rates" className="inline-flex items-center gap-1"><AlertTriangle className="h-3.5 w-3.5 text-warning" aria-hidden />Throughput unavailable: {apiErrorMessage(metricsQ.error)}</span>)

  const emptyText = ifsQ.isError
    ? `Could not load interfaces: ${apiErrorMessage(ifsQ.error)}`
    : allRows.length === 0
      ? 'No interfaces discovered yet. They appear after the first SNMP interface poll.'
      : savedFilter === 'errors' && !errorTotals
        ? 'Error counts are unavailable, so the Errors filter cannot be applied.'
        : 'No interfaces match these filters.'

  const presentGroups = TYPE_GROUPS.filter((g) => typeCounts.get(g.id))

  const tableWidget = (
    <div className="space-y-3">
      {/* Saved filters, search and bulk action */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Saved filters">
          {SAVED_FILTERS.map((f) => (
            <Chip key={f.id} active={savedFilter === f.id} onClick={() => setSavedFilter(f.id)}
              tone={f.id === 'unexpected_down' && filterCounts[f.id] > 0 ? 'crit' : undefined}>
              {f.label}
              <span className="tabular-nums text-muted">{f.id === 'errors' && !errorTotals ? MISSING : filterCounts[f.id]}</span>
            </Chip>
          ))}
          <FilterHelp />
        </div>
        <div className="flex items-center gap-2">
          {selected.size > 0 && (
            <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => setSpeedDialog({ mode: 'bulk', rows: selectedRows })}>
              <Gauge className="h-3.5 w-3.5" />
              Set speed ({selected.size})
            </Button>
          )}
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
            <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, description, ifIndex…"
              aria-label="Search interfaces" className="h-8 w-60 pl-7 text-sm" />
          </div>
        </div>
      </div>

      {presentGroups.length > 1 && (
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Interface type">
          <Chip active={typeFilter === 'all'} onClick={() => setTypeFilter('all')}>
            All types<span className="tabular-nums text-muted">{allRows.length}</span>
          </Chip>
          {presentGroups.map((g) => (
            <Chip key={g.id} active={typeFilter === g.id} onClick={() => setTypeFilter(typeFilter === g.id ? 'all' : g.id)}>
              {g.label}<span className="tabular-nums text-muted">{typeCounts.get(g.id)}</span>
            </Chip>
          ))}
        </div>
      )}

      {notes.length > 0 && <div className="flex flex-col gap-1 text-xs text-muted">{notes}</div>}

      <div className="rounded-lg border border-border">
        {ifsQ.isPending ? (
          <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />Loading interfaces…
          </div>
        ) : (
          <Table className="text-sm">
            <THead className="bg-surface2/40">
              <Tr>
                <Th className="w-10">
                  <input type="checkbox" checked={allVisibleSelected} onChange={toggleSelectAllVisible}
                    className="h-3.5 w-3.5 rounded border-border" aria-label="Select all visible interfaces" />
                </Th>
                <Th><SortHeader k="status" current={sortKey} dir={sortDir} onClick={toggleSort}>Status</SortHeader></Th>
                <Th><SortHeader k="name" current={sortKey} dir={sortDir} onClick={toggleSort}>Interface</SortHeader></Th>
                <Th>Description</Th>
                <Th><SortHeader k="speed" current={sortKey} dir={sortDir} onClick={toggleSort}>Speed</SortHeader></Th>
                <Th className="text-right">
                  <SortHeader k="in" current={sortKey} dir={sortDir} onClick={toggleSort}><ArrowDown className="inline h-3 w-3" aria-hidden />In</SortHeader>
                </Th>
                <Th className="text-right">
                  <SortHeader k="out" current={sortKey} dir={sortDir} onClick={toggleSort}><ArrowUp className="inline h-3 w-3" aria-hidden />Out</SortHeader>
                </Th>
                <Th className="w-36"><SortHeader k="util" current={sortKey} dir={sortDir} onClick={toggleSort}>Utilization</SortHeader></Th>
                <Th className="w-32">Traffic</Th>
                <Th className="text-right"><SortHeader k="errors" current={sortKey} dir={sortDir} onClick={toggleSort}>Errors</SortHeader></Th>
                <Th className="text-right"><SortHeader k="discards" current={sortKey} dir={sortDir} onClick={toggleSort}>Discards</SortHeader></Th>
                <Th className="w-8"><span className="sr-only">Expand</span></Th>
              </Tr>
            </THead>
            <TBody>
              {sorted.map((r) => {
                const expanded = expandedIdx === r.if_index
                const quiet = r.status.kind === 'admin_down' || r.status.kind === 'unused' || r.status.kind === 'not_present'
                return (
                  <Fragment key={r.id}>
                    <Tr onClick={() => setExpandedIdx(expanded ? null : r.if_index)}
                      aria-expanded={expanded}
                      className={cn('cursor-pointer transition-colors', quiet && 'text-muted', expanded ? 'bg-primary/5' : 'hover:bg-surface2/40')}>
                      <Td onClick={(e) => e.stopPropagation()}>
                        <input type="checkbox" checked={selected.has(r.if_index)} onChange={() => toggleSelect(r.if_index)}
                          className="h-3.5 w-3.5 rounded border-border" aria-label={`Select ${r.name}`} />
                      </Td>
                      <Td><StatusCell row={r} /></Td>
                      <Td>
                        <div className="font-medium text-text">{r.name}</div>
                        <div className="text-xs text-muted">{typeGroupLabel(r.group)}</div>
                      </Td>
                      <Td className="max-w-[220px] truncate text-muted">{r.description ?? MISSING}</Td>
                      <Td onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-1.5 whitespace-nowrap">
                          <span className="tabular-nums">{formatLinkSpeed(r.speed)}</span>
                          {r.hasManualSpeed && <Badge variant="outline" className="px-1 py-0 text-xs">manual</Badge>}
                          <button type="button" className="rounded p-0.5 text-muted hover:bg-surface2 hover:text-text"
                            aria-label={`Set manual speed for ${r.name}`} onClick={() => setSpeedDialog({ mode: 'single', rows: [r] })}>
                            <Pencil className="h-3 w-3" aria-hidden />
                          </button>
                        </div>
                        {r.hasManualSpeed && r.snmpSpeed != null && r.snmpSpeed !== r.speed && (
                          <div className="text-xs text-muted">SNMP {formatLinkSpeed(r.snmpSpeed)}</div>
                        )}
                      </Td>
                      <Td className="whitespace-nowrap text-right tabular-nums">{formatBitRate(r.inBps)}</Td>
                      <Td className="whitespace-nowrap text-right tabular-nums">{formatBitRate(r.outBps)}</Td>
                      <Td>
                        {r.util == null ? <span className="text-muted">{MISSING}</span> : (
                          <div className="flex items-center gap-2">
                            <div className="flex-1"><UsageBar pct={r.util} /></div>
                            <span className="w-10 text-right text-xs tabular-nums text-muted">{formatUsagePct(r.util)}</span>
                          </div>
                        )}
                      </Td>
                      <Td>
                        <Sparkline label={`${r.name} traffic`} values={r.throughput} domain={sparkDomain(r.throughput, r.speed)}
                          format={formatBitRate} className="w-28" />
                      </Td>
                      <Td className="text-right"><CounterCell value={r.errors} /></Td>
                      <Td className="text-right"><CounterCell value={r.discards} /></Td>
                      <Td><ChevronRight className={cn('h-3.5 w-3.5 text-muted transition-transform', expanded && 'rotate-90')} aria-hidden /></Td>
                    </Tr>
                    {expanded && (
                      <tr>
                        <td colSpan={COLUMNS} className="border-b border-primary/20 bg-surface2/20 p-0">
                          <InterfaceExpansion deviceId={deviceId} row={r} hours={hours} fromTs={windowFrom} toTs={windowTo}
                            errorsUnavailable={errorsQ.isError} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
              {sorted.length === 0 && (
                <Tr>
                  <Td colSpan={COLUMNS} className="py-8 text-center text-sm text-muted">{emptyText}</Td>
                </Tr>
              )}
            </TBody>
          </Table>
        )}
      </div>
    </div>
  )

  const k = summary.kinds
  return (
    <div className="space-y-4">
      <DeviceWidgetGrid deviceId={deviceId} vendor={device.vendor || undefined} deviceType={device.device_type} section="interfaces" widgets={[
        { id: 'interface-table', title: 'Interface table', width: 9, height: 21, minWidth: 4, minHeight: 4, content: tableWidget },
        { id: 'interface-inventory', title: 'Interface inventory', width: 3, height: 5, minWidth: 2, minHeight: 4, content: (
          <Panel title="Inventory" meta={`${summary.monitored.toLocaleString()} of ${summary.total.toLocaleString()} monitored`}>
            <div className="grid grid-cols-3 gap-2 text-center">
              <Stat label="Up" value={k.up} />
              <Stat label="Down" value={k.down} tone={k.down > 0 ? 'crit' : undefined} />
              <Stat label="Admin down" value={k.admin_down} />
              <Stat label="Unused" value={k.unused} />
              <Stat label="Not present" value={k.not_present} />
              <Stat label="Other" value={k.unmonitored_down + k.other} />
            </div>
          </Panel>
        ) },
        { id: 'interface-throughput', title: 'Throughput', width: 3, height: 5, minWidth: 2, minHeight: 4, content: (
          <Panel title="Throughput now" meta="Latest sample of each up interface">
            <dl className="space-y-1.5 text-sm">
              <ThroughputRow label="In" value={formatBitRate(summary.totalIn)} />
              <ThroughputRow label="Out" value={formatBitRate(summary.totalOut)} />
              <ThroughputRow label="Up capacity" value={formatLinkSpeed(summary.capacity || null)} />
            </dl>
            {summary.aggUtil != null && (
              <div className="mt-3 border-t border-border/40 pt-2">
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="text-muted">Aggregate utilization (busier direction)</span>
                  <span className="tabular-nums">{formatUsagePct(summary.aggUtil)}</span>
                </div>
                <UsageBar pct={summary.aggUtil} />
              </div>
            )}
          </Panel>
        ) },
        { id: 'interface-talkers', title: 'Top talkers', width: 3, height: 7, minWidth: 2, minHeight: 4, content: (
          <Panel title="Top talkers" meta="In + out, latest sample">
            {summary.top.length === 0 ? (
              <div className="py-2 text-center text-sm text-muted">
                {metricsQ.isError ? 'Throughput unavailable.' : 'No up interface has a traffic sample in this window.'}
              </div>
            ) : (
              <ul className="space-y-2">
                {summary.top.map((r) => (
                  <li key={r.id} className="text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium">{r.name}</span>
                      <span className="whitespace-nowrap tabular-nums">{formatBitRate((r.inBps ?? 0) + (r.outBps ?? 0))}</span>
                    </div>
                    <UsageBar pct={r.util} />
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        ) },
      ]} />

      <SpeedDialog
        open={!!speedDialog}
        rows={speedDialog?.rows ?? []}
        mode={speedDialog?.mode ?? 'single'}
        saving={saveSpeedMutation.isPending}
        onClose={() => setSpeedDialog(null)}
        onSave={(bps) => {
          if (!speedDialog) return
          saveSpeedMutation.mutate({ ifIndexes: speedDialog.rows.map((r) => r.if_index), configured_speed_bps: bps })
        }}
      />
    </div>
  )
}

/* ── Cells and small parts ──────────────────────────────────────────────── */

function StatusCell({ row }: { row: Row }) {
  const s = row.status
  const Icon = s.kind === 'up' ? CheckCircle2
    : s.kind === 'down' || s.kind === 'unmonitored_down' ? XCircle
      : s.kind === 'admin_down' ? MinusCircle
        : s.kind === 'unused' ? CircleDashed
          : s.kind === 'not_present' ? CircleSlash
            : HelpCircle
  const tone = s.tone === 'ok' ? 'text-success' : s.tone === 'crit' ? 'text-danger' : 'text-muted'
  return (
    <div className="flex items-start gap-1.5 whitespace-nowrap">
      <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', tone)} aria-hidden />
      <div>
        <div className={cn(s.tone === 'crit' && 'font-medium text-danger')}>{s.label}</div>
        {s.kind === 'unmonitored_down' && <div className="text-xs text-muted">not monitored</div>}
        {(s.kind === 'down' || s.kind === 'unmonitored_down') && row.last_up_at && (
          <div className="text-xs text-muted" title={formatAbsolute(row.last_up_at)}>last up {formatRelative(row.last_up_at)}</div>
        )}
      </div>
    </div>
  )
}

function CounterCell({ value }: { value: number | null }) {
  if (value == null) return <span className="text-muted">{MISSING}</span>
  if (value === 0) return <span className="tabular-nums text-muted">0</span>
  return (
    <span className="inline-flex items-center justify-end gap-1 font-medium tabular-nums text-warning">
      <AlertTriangle className="h-3 w-3" aria-hidden />{value.toLocaleString()}
    </span>
  )
}

function Chip({ active, onClick, children, tone }: { active: boolean; onClick: () => void; children: ReactNode; tone?: 'crit' }) {
  return (
    <button type="button" aria-pressed={active} onClick={onClick}
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs transition-colors',
        active ? 'border-primary bg-primary/10 font-medium text-primary' : 'border-border text-text hover:bg-surface2',
        tone === 'crit' && !active && 'border-danger/40',
      )}>
      {tone === 'crit' && <XCircle className="h-3 w-3 text-danger" aria-hidden />}
      {children}
    </button>
  )
}

function FilterHelp() {
  const statuses: Array<[string, string]> = [
    ['Up', 'Operationally up.'],
    ['Down (red)', 'Admin up, operationally down, monitored and seen up before: the actionable set, the same rule the Interface-down alert uses.'],
    ['Down · not monitored', 'Down, but not monitored, so it raises no alert.'],
    ['Admin down', 'Shut down by configuration.'],
    ['Unused', 'Never seen up: a spare port, not an outage.'],
    ['Not present', 'The module or transceiver for the port is missing.'],
  ]
  return (
    <Popover>
      <PopoverTrigger className="inline-flex h-7 items-center gap-1 rounded px-1 text-xs text-muted hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
        <Info className="h-3.5 w-3.5" aria-hidden />What do these mean?
      </PopoverTrigger>
      <PopoverContent>
        <h4 className="mb-1 text-sm font-semibold">Filters</h4>
        <dl className="space-y-1 text-xs">
          {SAVED_FILTERS.map((f) => (
            <div key={f.id}><dt className="inline font-medium">{f.label}: </dt><dd className="inline text-muted">{f.explanation}</dd></div>
          ))}
        </dl>
        <h4 className="mb-1 mt-3 text-sm font-semibold">Status</h4>
        <dl className="space-y-1 text-xs">
          {statuses.map(([k, v]) => (
            <div key={k}><dt className="inline font-medium">{k}: </dt><dd className="inline text-muted">{v}</dd></div>
          ))}
        </dl>
        <p className="mt-3 text-xs text-muted">Errors and discards are counted between consecutive polls in the selected window; counter wraps and device restarts never count.</p>
      </PopoverContent>
    </Popover>
  )
}

function SortHeader({ k, current, dir, onClick, children }: {
  k: SortKey; current: SortKey; dir: SortDir; onClick: (k: SortKey) => void; children: ReactNode
}) {
  const active = current === k
  return (
    <button type="button" onClick={() => onClick(k)}
      aria-sort={active ? (dir === 'asc' ? 'ascending' : 'descending') : undefined}
      className={cn('inline-flex items-center gap-1 uppercase', active ? 'text-primary' : 'hover:text-text')}>
      {children}
      {active ? (dir === 'asc' ? <ArrowUp className="h-3 w-3" aria-hidden /> : <ArrowDown className="h-3 w-3" aria-hidden />)
        : <ArrowUpDown className="h-3 w-3 opacity-40" aria-hidden />}
    </button>
  )
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: 'crit' }) {
  return (
    <div className="rounded-md bg-surface2/30 px-2 py-1.5">
      <div className={cn('text-base font-semibold tabular-nums', tone === 'crit' ? 'text-danger' : 'text-text')}>{value.toLocaleString()}</div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  )
}

function ThroughputRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  )
}

/* ── Per-interface expansion: identity, window stats, traffic chart ─────── */

type DetailResp = {
  traffic: { ts: number; in_bps: number; out_bps: number }[]
  errors: { ts: number; in_errors: number; out_errors: number; in_discards: number; out_discards: number }[]
  summary: {
    in_avg_bps?: number
    in_max_bps?: number
    out_avg_bps?: number
    out_max_bps?: number
    samples?: number
  }
}

const IN_COLOR = SERIES_COLORS.primary
const OUT_COLOR = SERIES_COLORS.info

function InterfaceExpansion({ deviceId, row, hours, fromTs, toTs, errorsUnavailable }: {
  deviceId: string; row: Row; hours: number; fromTs: number; toTs: number; errorsUnavailable: boolean
}) {
  const { data, isPending, isError, error } = useQuery<DetailResp>({
    queryKey: ['device', deviceId, 'if-detail', row.if_index, hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces/${row.if_index}/metrics?hours=${hours}`)).data,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  })

  const traffic = data?.traffic || []
  const summary = data?.summary || {}
  const series = useMemo(() => [
    { key: 'in', name: 'In', color: IN_COLOR, points: traffic.map((p) => ({ ts: Number(p.ts), value: p.in_bps == null ? null : Number(p.in_bps) })) },
    { key: 'out', name: 'Out', color: OUT_COLOR, points: traffic.map((p) => ({ ts: Number(p.ts), value: p.out_bps == null ? null : Number(p.out_bps) })) },
  ], [traffic])

  const t = row.totals
  const facts: Array<[string, ReactNode]> = [
    ['ifIndex', row.if_index],
    ['Type', `${typeGroupLabel(row.group)}${row.if_type != null ? ` (ifType ${row.if_type})` : ''}`],
    ['MAC address', <span key="mac" className="font-mono">{row.mac_address || MISSING}</span>],
    ['Admin / oper', `${row.admin_status || MISSING} / ${row.oper_status || MISSING}`],
    ['Monitored', row.monitored ? 'Yes' : 'No'],
    ['Last seen up', row.last_up_at ? <span key="up" title={formatAbsolute(row.last_up_at)}>{formatRelative(row.last_up_at)}</span> : row.ever_up === false ? 'Never' : MISSING],
    ['Speed', `${formatLinkSpeed(row.speed)}${row.hasManualSpeed ? ' (manual)' : ''}`],
    ['SNMP speed', formatLinkSpeed(row.snmpSpeed)],
    ['ifDescr', row.if_descr || MISSING],
    ['Last polled', row.last_seen ? <span key="seen" title={formatAbsolute(row.last_seen)}>{formatRelative(row.last_seen)}</span> : MISSING],
  ]
  const stats: Array<[string, string]> = [
    ['In avg', formatBitRate(summary.in_avg_bps)],
    ['In max', formatBitRate(summary.in_max_bps)],
    ['Out avg', formatBitRate(summary.out_avg_bps)],
    ['Out max', formatBitRate(summary.out_max_bps)],
    ['Errors in / out', t ? `${t.in_errors.toLocaleString()} / ${t.out_errors.toLocaleString()}` : MISSING],
    ['Discards in / out', t ? `${t.in_discards.toLocaleString()} / ${t.out_discards.toLocaleString()}` : MISSING],
  ]

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="font-semibold">{row.name}</span>
        {row.description && <span className="text-muted">· {row.description}</span>}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-5">
        {facts.map(([label, value]) => (
          <div key={label} className="min-w-0">
            <dt className="text-muted">{label}</dt>
            <dd className="truncate text-sm">{value}</dd>
          </div>
        ))}
      </dl>

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-6">
        {stats.map(([label, value]) => (
          <div key={label} className="rounded bg-surface2/40 px-2 py-1.5">
            <dt className="text-xs text-muted">{label}</dt>
            <dd className="mt-0.5 text-sm tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      {errorsUnavailable && <p className="text-xs text-muted">Error counts unavailable: the metrics store did not answer.</p>}

      <div className="rounded-md border border-border bg-surface/40 p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs font-medium text-muted">Traffic, last {hours} h</span>
          <Legend items={[{ name: 'In', color: IN_COLOR }, { name: 'Out', color: OUT_COLOR }]} />
        </div>
        {isPending ? (
          <div className="flex h-48 items-center justify-center gap-2 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />Loading traffic…
          </div>
        ) : isError ? (
          <div className="flex h-48 items-center justify-center text-sm text-muted">Could not load traffic: {apiErrorMessage(error)}</div>
        ) : (
          <TimeSeriesPanel series={series} fromTs={fromTs} toTs={toTs} rangeHours={hours} domain="auto"
            format={formatBitRate} axisFormat={formatBitRateAxis} height={192}
            emptyText="No traffic samples for this interface in this window." />
        )}
      </div>
    </div>
  )
}

/* ── Manual speed dialog ────────────────────────────────────────────────── */

function SpeedDialog({ open, rows, mode, saving, onClose, onSave }: {
  open: boolean
  rows: Row[]
  mode: 'single' | 'bulk'
  saving: boolean
  onClose: () => void
  onSave: (bps: number | null) => void
}) {
  const single = rows[0]
  const initialMbps =
    mode === 'single' && single?.configured_speed_bps
      ? String(single.configured_speed_bps / 1_000_000)
      : mode === 'single' && single?.snmpSpeed
        ? String(single.snmpSpeed / 1_000_000)
        : ''

  const [customMbps, setCustomMbps] = useState(initialMbps)
  const [selectedPreset, setSelectedPreset] = useState<number | null>(null)

  // Reset the form when the dialog opens for a different selection.
  const rowKey = rows.map((r) => r.if_index).join(',')
  useEffect(() => {
    if (!open) return
    setCustomMbps(initialMbps)
    setSelectedPreset(null)
  }, [open, rowKey, initialMbps])

  const parsedMbps = parseFloat(customMbps)
  const previewBps =
    selectedPreset != null
      ? mbpsToBps(selectedPreset)
      : !isNaN(parsedMbps) && parsedMbps > 0
        ? mbpsToBps(parsedMbps)
        : null

  const title = mode === 'single' ? `Set speed — ${single ? single.name : ''}` : `Set speed — ${rows.length} interfaces`

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            Manual line rate used to calculate utilization. SNMP speed is unchanged and shown for reference.
          </DialogDescription>
        </DialogHeader>

        {mode === 'single' && single && (
          <div className="rounded-md border border-border bg-surface2/30 px-3 py-2 text-xs text-muted">
            SNMP reported speed: <span className="text-text">{single.snmpSpeed ? formatLinkSpeed(single.snmpSpeed) : 'unknown'}</span>
            {single.hasManualSpeed && (
              <> · current manual: <span className="text-text">{formatLinkSpeed(single.configured_speed_bps)}</span></>
            )}
          </div>
        )}

        <div>
          <div className="mb-2 text-xs font-medium">Presets</div>
          <div className="flex flex-wrap gap-1.5">
            {SPEED_PRESETS_MBPS.map((p) => (
              <button key={p.mbps} type="button"
                onClick={() => { setSelectedPreset(p.mbps); setCustomMbps(String(p.mbps)) }}
                className={cn('rounded-md border px-2.5 py-1 text-xs transition',
                  selectedPreset === p.mbps ? 'border-primary bg-primary/10 text-primary' : 'border-border hover:border-primary/40')}>
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium" htmlFor="iface-speed-mbps">Custom speed (Mbps)</label>
          <Input id="iface-speed-mbps" type="number" min={1} step={1} placeholder="e.g. 1000 for 1 Gbps" value={customMbps}
            onChange={(e) => { setCustomMbps(e.target.value); setSelectedPreset(null) }} className="h-9 text-sm" />
          {previewBps && (
            <p className="mt-1.5 text-xs text-muted">
              = <span className="text-text">{formatLinkSpeed(previewBps)}</span> per direction; utilization uses the busier direction
            </p>
          )}
        </div>

        <DialogFooter className="flex-wrap gap-2 sm:justify-between">
          <Button type="button" variant="ghost" disabled={saving || (mode === 'bulk' && rows.every((r) => !r.hasManualSpeed))}
            onClick={() => onSave(null)}>
            Use SNMP speed
          </Button>
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
            <Button type="button" disabled={saving || !previewBps} onClick={() => onSave(previewBps)}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Apply'}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
