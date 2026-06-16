import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  ArrowUpDown,
  CheckCircle2,
  ChevronRight,
  Gauge,
  Loader2,
  Network,
  Pencil,
  Search,
  TrendingUp,
  XCircle,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage, formatBps } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { toast } from '@/components/ui/Toast'

type SortKey = 'name' | 'status' | 'speed' | 'in' | 'out' | 'util' | 'errors'
type SortDir = 'asc' | 'desc'

const SPEED_PRESETS_MBPS = [
  { label: '10 Mbps', mbps: 10 },
  { label: '100 Mbps', mbps: 100 },
  { label: '1 Gbps', mbps: 1000 },
  { label: '10 Gbps', mbps: 10000 },
  { label: '40 Gbps', mbps: 40000 },
  { label: '100 Gbps', mbps: 100000 },
] as const

function mbpsToBps(mbps: number): number {
  return Math.round(mbps * 1_000_000)
}

function effectiveSpeed(iface: { configured_speed_bps?: number | null; if_speed?: number | null }): number {
  const manual = iface.configured_speed_bps
  if (manual != null && manual > 0) return Number(manual)
  return Number(iface.if_speed) || 0
}

interface Iface {
  id: number
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_type: number | null
  if_speed: number | null
  configured_speed_bps: number | null
  mac_address: string | null
  admin_status: string | null
  oper_status: string | null
  monitored: boolean
  last_seen: string | null
}

interface MetricPoint { ts: number; in_bps: number; out_bps: number }

interface Row extends Iface {
  inBps: number
  outBps: number
  total: number
  util: number
  speed: number
  snmpSpeed: number
  hasManualSpeed: boolean
  series: MetricPoint[]
}

export function DeviceInterfacesPage() {
  const { id } = useParams<{ id: string }>()
  const deviceId = id || ''
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()

  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'up' | 'down'>('all')
  const [sortKey, setSortKey] = useState<SortKey>('util')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [speedDialog, setSpeedDialog] = useState<{ mode: 'single' | 'bulk'; rows: Row[] } | null>(null)

  const qc = useQueryClient()

  const { data: device } = useQuery<any>({
    queryKey: ['device', deviceId],
    queryFn: async () => (await api.get(`/devices/${deviceId}`)).data,
    enabled: !!deviceId,
  })
  const { data: ifs = [], isLoading: ifsLoading } = useQuery<Iface[]>({
    queryKey: ['device', deviceId, 'interfaces'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    enabled: !!deviceId,
    refetchInterval: 30_000,
  })
  const { data: ifMetrics = {} } = useQuery<Record<string, MetricPoint[]>>({
    queryKey: ['device', deviceId, 'if-metrics', range.hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-if-metrics?hours=${range.hours}`)).data,
    enabled: !!deviceId,
    refetchInterval: 30_000,
  })

  // Build the enriched per-interface row.
  const allRows: Row[] = useMemo(() => {
    return ifs.map((i) => {
      const series = ifMetrics[String(i.if_index)] || []
      const last = series[series.length - 1]
      const inBps = last?.in_bps || 0
      const outBps = last?.out_bps || 0
      const snmpSpeed = Number(i.if_speed) || 0
      const speed = effectiveSpeed(i)
      const hasManualSpeed = i.configured_speed_bps != null && i.configured_speed_bps > 0
      const util = speed > 0 ? Math.min(100, ((inBps + outBps) / speed) * 100) : 0
      return {
        ...i,
        inBps,
        outBps,
        total: inBps + outBps,
        util,
        speed,
        snmpSpeed,
        hasManualSpeed,
        series,
      }
    })
  }, [ifs, ifMetrics])

  const filtered = useMemo(() => {
    return allRows.filter((r) => {
      const name = (r.if_name || r.if_descr || '').toLowerCase()
      const alias = (r.if_alias || '').toLowerCase()
      if (search && !name.includes(search.toLowerCase()) && !alias.includes(search.toLowerCase())) return false
      if (filter === 'up' && r.oper_status !== 'up') return false
      if (filter === 'down' && r.oper_status === 'up') return false
      return true
    })
  }, [allRows, search, filter])

  const sorted = useMemo(() => {
    const dir = sortDir === 'asc' ? 1 : -1
    const sorted = [...filtered]
    sorted.sort((a, b) => {
      switch (sortKey) {
        case 'name':
          return dir * (a.if_name || '').localeCompare(b.if_name || '')
        case 'status':
          return dir * String(a.oper_status || '').localeCompare(String(b.oper_status || ''))
        case 'speed':
          return dir * (a.speed - b.speed)
        case 'in':
          return dir * (a.inBps - b.inBps)
        case 'out':
          return dir * (a.outBps - b.outBps)
        case 'util':
          return dir * (a.util - b.util)
        case 'errors':
          return 0
      }
    })
    return sorted
  }, [filtered, sortKey, sortDir])

  // Aggregate summary numbers (across all interfaces, not just filtered).
  const summary = useMemo(() => {
    const total = allRows.length
    const up = allRows.filter((r) => r.oper_status === 'up').length
    const down = total - up
    const totalIn = allRows.reduce((s, r) => s + r.inBps, 0)
    const totalOut = allRows.reduce((s, r) => s + r.outBps, 0)
    const totalCapacity = allRows.reduce((s, r) => s + r.speed, 0)
    const aggUtil = totalCapacity > 0 ? Math.min(100, ((totalIn + totalOut) / totalCapacity) * 100) : 0
    const top = [...allRows]
      .filter((r) => r.oper_status === 'up')
      .sort((a, b) => b.total - a.total)
      .slice(0, 5)
    const monitored = allRows.filter((r) => r.monitored).length
    return { total, up, down, totalIn, totalOut, aggUtil, totalCapacity, top, monitored }
  }, [allRows])

  function toggleSort(k: SortKey) {
    if (sortKey === k) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(k)
      setSortDir(k === 'name' || k === 'status' ? 'asc' : 'desc')
    }
  }

  const visibleIndexes = useMemo(() => sorted.map((r) => r.if_index), [sorted])
  const allVisibleSelected = visibleIndexes.length > 0 && visibleIndexes.every((idx) => selected.has(idx))
  const someSelected = selected.size > 0

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
      if (allVisibleSelected) {
        visibleIndexes.forEach((idx) => next.delete(idx))
      } else {
        visibleIndexes.forEach((idx) => next.add(idx))
      }
      return next
    })
  }

  const selectedRows = useMemo(
    () => allRows.filter((r) => selected.has(r.if_index)),
    [allRows, selected],
  )

  const saveSpeedMutation = useMutation({
    mutationFn: async ({
      ifIndexes,
      configured_speed_bps,
    }: {
      ifIndexes: number[]
      configured_speed_bps: number | null
    }) => {
      if (ifIndexes.length === 1) {
        return (
          await api.patch(`/devices/${deviceId}/interfaces/${ifIndexes[0]}`, {
            configured_speed_bps,
          })
        ).data
      }
      return (
        await api.post(`/devices/${deviceId}/interfaces/bulk-speed`, {
          if_indexes: ifIndexes,
          configured_speed_bps,
        })
      ).data
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

  return (
    <div className="space-y-4">
      {/* Page header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-start gap-2">
          <Link
            to={`/devices/${deviceId}`}
            className="mt-1 rounded-md p-1.5 text-muted hover:bg-surface2"
            aria-label="Back to device"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">
              Interfaces
              {device && <span className="ml-2 text-sm font-normal text-muted">· {device.hostname}</span>}
            </h1>
            <p className="text-[11px] text-muted">
              Live throughput, utilization and health for every monitored interface.
            </p>
          </div>
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

      {/* Main grid: table + sidebar */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        {/* Table column */}
        <div className="space-y-3">
          {/* Toolbar */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs">
              <span className="text-muted">{summary.total} interfaces</span>
              <Badge variant="success">{summary.up} up</Badge>
              {summary.down > 0 && <Badge variant="outline">{summary.down} down</Badge>}
            </div>
            <div className="flex items-center gap-2">
              {someSelected && (
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 text-xs"
                  onClick={() => setSpeedDialog({ mode: 'bulk', rows: selectedRows })}
                >
                  <Gauge className="h-3.5 w-3.5" />
                  Set speed ({selected.size})
                </Button>
              )}
              <div className="flex overflow-hidden rounded-md border border-border text-[11px]">
                {(['all', 'up', 'down'] as const).map((f) => (
                  <button
                    key={f}
                    onClick={() => setFilter(f)}
                    className={`px-2.5 py-1 capitalize transition ${
                      filter === f
                        ? 'bg-primary/10 text-primary font-medium'
                        : 'text-muted hover:bg-surface2'
                    }`}
                  >
                    {f}
                  </button>
                ))}
              </div>
              <div className="relative">
                <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted" />
                <Input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search name or description…"
                  className="h-8 w-56 pl-7 text-xs"
                />
              </div>
            </div>
          </div>

          <Card>
            <CardContent className="p-0">
              {ifsLoading ? (
                <div className="flex items-center justify-center py-12 text-muted">
                  <Loader2 className="h-5 w-5 animate-spin" />
                </div>
              ) : (
                <Table>
                  <THead className="bg-surface2/40">
                    <Tr>
                      <Th className="w-10">
                        <input
                          type="checkbox"
                          checked={allVisibleSelected}
                          onChange={toggleSelectAllVisible}
                          className="h-3.5 w-3.5 rounded border-border"
                          aria-label="Select all visible interfaces"
                        />
                      </Th>
                      <Th>
                        <SortHeader k="status" current={sortKey} dir={sortDir} onClick={toggleSort}>
                          Status
                        </SortHeader>
                      </Th>
                      <Th>
                        <SortHeader k="name" current={sortKey} dir={sortDir} onClick={toggleSort}>
                          Interface
                        </SortHeader>
                      </Th>
                      <Th>Description</Th>
                      <Th>
                        <SortHeader k="speed" current={sortKey} dir={sortDir} onClick={toggleSort}>
                          Speed
                        </SortHeader>
                      </Th>
                      <Th className="text-right">
                        <SortHeader k="in" current={sortKey} dir={sortDir} onClick={toggleSort}>
                          <ArrowDown className="inline h-3 w-3" /> In
                        </SortHeader>
                      </Th>
                      <Th className="text-right">
                        <SortHeader k="out" current={sortKey} dir={sortDir} onClick={toggleSort}>
                          <ArrowUp className="inline h-3 w-3" /> Out
                        </SortHeader>
                      </Th>
                      <Th className="w-44">
                        <SortHeader k="util" current={sortKey} dir={sortDir} onClick={toggleSort}>
                          Utilization
                        </SortHeader>
                      </Th>
                      <Th>MAC</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {sorted.map((r) => {
                      const isUp = r.oper_status === 'up'
                      const adminDown = r.admin_status && r.admin_status !== 'up'
                      const expanded = expandedIdx === r.if_index
                      return (
                        <>
                          <Tr
                            key={r.id}
                            onClick={() => setExpandedIdx(expanded ? null : r.if_index)}
                            className={`cursor-pointer transition-colors ${!isUp ? 'opacity-60' : ''} ${
                              expanded ? 'bg-primary/5' : 'hover:bg-surface2/40'
                            }`}
                          >
                            <Td onClick={(e) => e.stopPropagation()}>
                              <input
                                type="checkbox"
                                checked={selected.has(r.if_index)}
                                onChange={() => toggleSelect(r.if_index)}
                                className="h-3.5 w-3.5 rounded border-border"
                                aria-label={`Select ${r.if_name || r.if_descr}`}
                              />
                            </Td>
                            <Td>
                              <div className="flex items-center gap-1.5">
                                <ChevronRight
                                  className={`h-3 w-3 text-muted transition-transform ${expanded ? 'rotate-90' : ''}`}
                                />
                                {isUp ? (
                                  <CheckCircle2 className="h-3.5 w-3.5 text-success" />
                                ) : (
                                  <XCircle className="h-3.5 w-3.5 text-danger" />
                                )}
                                <span className="text-[11px] capitalize">
                                  {r.oper_status || 'unknown'}
                                  {adminDown && (
                                    <span className="ml-1 text-[10px] text-warning">
                                      (admin {r.admin_status})
                                    </span>
                                  )}
                                </span>
                              </div>
                            </Td>
                            <Td>
                              <span className="text-sm font-medium">
                                {r.if_name || r.if_descr || `if${r.if_index}`}
                              </span>
                            </Td>
                            <Td className="max-w-[220px] truncate text-xs text-muted">
                              {r.if_alias || r.if_descr || '—'}
                            </Td>
                            <Td className="text-xs" onClick={(e) => e.stopPropagation()}>
                              <div className="flex items-center gap-1.5">
                                <span>{r.speed > 0 ? formatBps(r.speed) : '—'}</span>
                                {r.hasManualSpeed && (
                                  <Badge variant="outline" className="text-[9px] px-1 py-0">
                                    manual
                                  </Badge>
                                )}
                                <button
                                  type="button"
                                  className="rounded p-0.5 text-muted hover:bg-surface2 hover:text-text"
                                  title="Set manual speed / bandwidth"
                                  onClick={() => setSpeedDialog({ mode: 'single', rows: [r] })}
                                >
                                  <Pencil className="h-3 w-3" />
                                </button>
                              </div>
                              {r.hasManualSpeed && r.snmpSpeed > 0 && r.snmpSpeed !== r.speed && (
                                <div className="text-[10px] text-muted">
                                  SNMP: {formatBps(r.snmpSpeed)}
                                </div>
                              )}
                            </Td>
                            <Td className="text-right font-mono text-xs">
                              {isUp && r.inBps > 0 ? formatBps(r.inBps) : '—'}
                            </Td>
                            <Td className="text-right font-mono text-xs">
                              {isUp && r.outBps > 0 ? formatBps(r.outBps) : '—'}
                            </Td>
                            <Td>
                              {isUp && r.speed > 0 ? (
                                <UtilBar pct={r.util} />
                              ) : (
                                <span className="text-xs text-muted">—</span>
                              )}
                            </Td>
                            <Td className="font-mono text-[10px] text-muted">{r.mac_address || '—'}</Td>
                          </Tr>
                          {expanded && (
                            <tr key={`${r.id}-detail`}>
                              <td colSpan={9} className="border-b border-primary/20 bg-surface2/20 p-0">
                                <InterfaceExpansion
                                  deviceId={deviceId}
                                  iface={r}
                                  hours={range.hours}
                                  rangeLabel={range.label}
                                />
                              </td>
                            </tr>
                          )}
                        </>
                      )
                    })}
                    {sorted.length === 0 && (
                      <Tr>
                        <Td colSpan={9} className="py-8 text-center text-xs text-muted">
                          {allRows.length === 0 ? 'No interfaces discovered yet' : 'No interfaces match your filter'}
                        </Td>
                      </Tr>
                    )}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Sidebar */}
        <div className="space-y-3">
          <SummaryCard
            icon={<Network className="h-4 w-4" />}
            title="Inventory"
            tint="primary"
          >
            <div className="grid grid-cols-3 gap-2 text-center">
              <Stat label="Total" value={String(summary.total)} />
              <Stat label="Up" value={String(summary.up)} tone="success" />
              <Stat label="Down" value={String(summary.down)} tone={summary.down > 0 ? 'danger' : 'muted'} />
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-center">
              <Stat label="Monitored" value={String(summary.monitored)} />
              <Stat label="Capacity" value={summary.totalCapacity > 0 ? formatBps(summary.totalCapacity) : '—'} />
            </div>
          </SummaryCard>

          <SummaryCard
            icon={<Activity className="h-4 w-4" />}
            title="Throughput (now)"
            tint="success"
          >
            <div className="space-y-2 text-xs">
              <ThroughputRow label="In" bps={summary.totalIn} icon={<ArrowDown className="h-3 w-3 text-success" />} />
              <ThroughputRow label="Out" bps={summary.totalOut} icon={<ArrowUp className="h-3 w-3 text-info" />} />
              <ThroughputRow
                label="Total"
                bps={summary.totalIn + summary.totalOut}
                icon={<TrendingUp className="h-3 w-3 text-primary" />}
              />
            </div>
            {summary.totalCapacity > 0 && (
              <div className="mt-3 border-t border-border/40 pt-2">
                <div className="mb-1 flex items-center justify-between text-[10px]">
                  <span className="uppercase tracking-wider text-muted">Aggregate utilization</span>
                  <span className="font-mono text-text">{summary.aggUtil.toFixed(2)}%</span>
                </div>
                <UtilBar pct={summary.aggUtil} />
              </div>
            )}
          </SummaryCard>

          <SummaryCard
            icon={<TrendingUp className="h-4 w-4" />}
            title="Top talkers"
            tint="info"
          >
            {summary.top.length === 0 ? (
              <div className="py-2 text-center text-[11px] text-muted">No active interfaces.</div>
            ) : (
              <div className="space-y-2">
                {summary.top.map((r) => (
                  <div key={r.id} className="text-xs">
                    <div className="flex items-center justify-between">
                      <span className="truncate font-medium">{r.if_name || `if${r.if_index}`}</span>
                      <span className="ml-2 font-mono text-[11px] text-text">{formatBps(r.total)}</span>
                    </div>
                    <UtilBar pct={r.util} compact />
                  </div>
                ))}
              </div>
            )}
          </SummaryCard>
        </div>
      </div>

      <SpeedDialog
        open={!!speedDialog}
        rows={speedDialog?.rows ?? []}
        mode={speedDialog?.mode ?? 'single'}
        saving={saveSpeedMutation.isPending}
        onClose={() => setSpeedDialog(null)}
        onSave={(bps) => {
          if (!speedDialog) return
          saveSpeedMutation.mutate({
            ifIndexes: speedDialog.rows.map((r) => r.if_index),
            configured_speed_bps: bps,
          })
        }}
      />
    </div>
  )
}

/* ── Sub-components ───────────────────────────────────────────────────── */

function SortHeader({
  k, current, dir, onClick, children,
}: {
  k: SortKey
  current: SortKey
  dir: SortDir
  onClick: (k: SortKey) => void
  children: React.ReactNode
}) {
  const active = current === k
  return (
    <button
      type="button"
      onClick={() => onClick(k)}
      className={`inline-flex items-center gap-1 ${active ? 'text-primary' : 'hover:text-text'}`}
    >
      {children}
      <ArrowUpDown className={`h-3 w-3 ${active ? 'opacity-100' : 'opacity-40'}`} />
      {active && <span className="text-[9px]">{dir === 'asc' ? '↑' : '↓'}</span>}
    </button>
  )
}

function UtilBar({ pct, compact }: { pct: number; compact?: boolean }) {
  const color =
    pct > 80 ? 'bg-danger' : pct > 50 ? 'bg-warning' : pct > 0 ? 'bg-primary' : 'bg-surface2'
  return (
    <div className="flex items-center gap-2">
      <div className={`flex-1 overflow-hidden rounded-full bg-surface2 ${compact ? 'h-1' : 'h-1.5'}`}>
        <div
          className={`h-full rounded-full ${color}`}
          style={{ width: `${Math.max(1, pct)}%` }}
        />
      </div>
      <span className="w-9 text-right font-mono text-[10px] text-muted">{pct.toFixed(0)}%</span>
    </div>
  )
}

function SummaryCard({
  icon, title, tint, children,
}: {
  icon: React.ReactNode
  title: string
  tint: 'primary' | 'success' | 'info' | 'warning'
  children: React.ReactNode
}) {
  const tintCls = {
    primary: 'bg-primary/10 text-primary',
    success: 'bg-success/10 text-success',
    info: 'bg-info/10 text-info',
    warning: 'bg-warning/10 text-warning',
  }[tint]
  return (
    <Card>
      <CardContent className="p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className={`flex h-6 w-6 items-center justify-center rounded ${tintCls}`}>
            {icon}
          </span>
          <h3 className="text-xs font-semibold uppercase tracking-wider">{title}</h3>
        </div>
        {children}
      </CardContent>
    </Card>
  )
}

function Stat({
  label, value, tone,
}: {
  label: string
  value: string
  tone?: 'success' | 'danger' | 'muted'
}) {
  const colorCls =
    tone === 'success' ? 'text-success' : tone === 'danger' ? 'text-danger' : tone === 'muted' ? 'text-muted' : 'text-text'
  return (
    <div className="rounded-md bg-surface2/30 px-2 py-1.5">
      <div className={`text-base font-semibold tabular-nums ${colorCls}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
    </div>
  )
}

function ThroughputRow({
  label, bps, icon,
}: {
  label: string
  bps: number
  icon: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="inline-flex items-center gap-1.5 text-muted">
        {icon}
        {label}
      </span>
      <span className="font-mono text-text">{bps > 0 ? formatBps(bps) : '0 bps'}</span>
    </div>
  )
}

/* ── Per-interface expansion: traffic chart + summary ────────────────── */

interface DetailResp {
  traffic: { ts: number; in_bps: number; out_bps: number }[]
  errors: { ts: number; in_errors: number; out_errors: number; in_discards: number; out_discards: number }[]
  summary: {
    in_avg_bps?: number
    in_max_bps?: number
    in_current_bps?: number
    out_avg_bps?: number
    out_max_bps?: number
    out_current_bps?: number
    total_errors?: number
    total_discards?: number
    samples?: number
  }
}

function InterfaceExpansion({
  deviceId, iface, hours, rangeLabel,
}: {
  deviceId: string
  iface: Row
  hours: number
  rangeLabel: string
}) {
  const { data, isLoading } = useQuery<DetailResp>({
    queryKey: ['device', deviceId, 'if-detail', iface.if_index, hours],
    queryFn: async () =>
      (await api.get(`/devices/${deviceId}/interfaces/${iface.if_index}/metrics?hours=${hours}`)).data,
    refetchInterval: 30_000,
  })

  const traffic = data?.traffic || []
  const summary = data?.summary || {}
  const errs = (data?.errors || []).reduce(
    (acc, e) => acc + (e.in_errors || 0) + (e.out_errors || 0),
    0,
  )
  const discs = (data?.errors || []).reduce(
    (acc, e) => acc + (e.in_discards || 0) + (e.out_discards || 0),
    0,
  )

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs">
          <span className="font-semibold">{iface.if_name || `if${iface.if_index}`}</span>
          {iface.if_alias && <span className="ml-2 text-muted">· {iface.if_alias}</span>}
          {iface.speed > 0 && (
            <span className="ml-2 text-muted">
              · {formatBps(iface.speed)}
              {iface.hasManualSpeed && ' (manual)'}
            </span>
          )}
          {iface.hasManualSpeed && iface.snmpSpeed > 0 && iface.snmpSpeed !== iface.speed && (
            <span className="ml-2 text-muted">· SNMP {formatBps(iface.snmpSpeed)}</span>
          )}
        </div>
        <span className="text-[11px] text-muted">{rangeLabel}</span>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-6">
        <Stat2 label="In avg" value={fmtBps(summary.in_avg_bps)} />
        <Stat2 label="In max" value={fmtBps(summary.in_max_bps)} />
        <Stat2 label="Out avg" value={fmtBps(summary.out_avg_bps)} />
        <Stat2 label="Out max" value={fmtBps(summary.out_max_bps)} />
        <Stat2 label="Errors" value={errs > 0 ? errs.toLocaleString() : '0'} tone={errs > 0 ? 'warn' : 'muted'} />
        <Stat2 label="Discards" value={discs > 0 ? discs.toLocaleString() : '0'} tone={discs > 0 ? 'warn' : 'muted'} />
      </div>

      {/* Traffic chart */}
      <div className="h-48 rounded-md border border-border bg-surface/40 p-2">
        {isLoading ? (
          <div className="flex h-full items-center justify-center text-xs text-muted">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            Loading…
          </div>
        ) : traffic.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-muted">
            No traffic data in this window.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={traffic} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id={`g-in-${iface.if_index}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.45} />
                  <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                </linearGradient>
                <linearGradient id={`g-out-${iface.if_index}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.45} />
                  <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
              <XAxis
                dataKey="ts"
                tickFormatter={(ts) => {
                  const d = new Date(ts)
                  return hours <= 24
                    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                    : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
                }}
                tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                axisLine={false}
                tickLine={false}
                minTickGap={40}
              />
              <YAxis
                tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                width={60}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) => formatBps(Number(v))}
              />
              <Tooltip
                contentStyle={{
                  background: 'rgb(var(--surface))',
                  border: '1px solid rgb(var(--border))',
                  fontSize: 11,
                }}
                labelFormatter={(ts) => new Date(Number(ts)).toLocaleString()}
                formatter={(v: number, name: string) => [formatBps(Number(v)), name]}
              />
              <Area
                type="monotone"
                name="In"
                dataKey="in_bps"
                stroke="rgb(var(--success))"
                strokeWidth={1.6}
                fill={`url(#g-in-${iface.if_index})`}
                isAnimationActive={false}
              />
              <Area
                type="monotone"
                name="Out"
                dataKey="out_bps"
                stroke="rgb(var(--info))"
                strokeWidth={1.6}
                fill={`url(#g-out-${iface.if_index})`}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}

function Stat2({
  label, value, tone,
}: {
  label: string
  value: string
  tone?: 'warn' | 'muted'
}) {
  const cls =
    tone === 'warn' ? 'text-warning' : tone === 'muted' ? 'text-muted' : 'text-text'
  return (
    <div className="rounded bg-surface2/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-xs ${cls}`}>{value}</div>
    </div>
  )
}

function fmtBps(v: number | undefined): string {
  if (v == null) return '—'
  if (v <= 0) return '0 bps'
  return formatBps(v)
}

function SpeedDialog({
  open,
  rows,
  mode,
  saving,
  onClose,
  onSave,
}: {
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

  // Reset form when dialog opens for a different selection
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

  const title =
    mode === 'single'
      ? `Set speed — ${single?.if_name || single?.if_descr || `if${single?.if_index}`}`
      : `Set speed — ${rows.length} interfaces`

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
            SNMP reported speed:{' '}
            <span className="font-mono text-text">
              {single.snmpSpeed > 0 ? formatBps(single.snmpSpeed) : 'unknown'}
            </span>
            {single.hasManualSpeed && (
              <>
                {' '}
                · current manual:{' '}
                <span className="font-mono text-text">{formatBps(single.configured_speed_bps!)}</span>
              </>
            )}
          </div>
        )}

        <div>
          <div className="mb-2 text-xs font-medium">Presets</div>
          <div className="flex flex-wrap gap-1.5">
            {SPEED_PRESETS_MBPS.map((p) => (
              <button
                key={p.mbps}
                type="button"
                onClick={() => {
                  setSelectedPreset(p.mbps)
                  setCustomMbps(String(p.mbps))
                }}
                className={`rounded-md border px-2.5 py-1 text-xs transition ${
                  selectedPreset === p.mbps
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'border-border hover:border-primary/40'
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-xs font-medium">Custom speed (Mbps)</label>
          <Input
            type="number"
            min={1}
            step={1}
            placeholder="e.g. 1000 for 1 Gbps"
            value={customMbps}
            onChange={(e) => {
              setCustomMbps(e.target.value)
              setSelectedPreset(null)
            }}
            className="h-9 text-sm"
          />
          {previewBps && (
            <p className="mt-1.5 text-[11px] text-muted">
              = <span className="font-mono text-text">{formatBps(previewBps)}</span> per direction (half-duplex util)
            </p>
          )}
        </div>

        <DialogFooter className="flex-wrap gap-2 sm:justify-between">
          <Button
            type="button"
            variant="ghost"
            disabled={saving || (mode === 'bulk' && rows.every((r) => !r.hasManualSpeed))}
            onClick={() => onSave(null)}
          >
            Use SNMP speed
          </Button>
          <div className="flex gap-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button
              type="button"
              disabled={saving || !previewBps}
              onClick={() => onSave(previewBps)}
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Apply'}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
