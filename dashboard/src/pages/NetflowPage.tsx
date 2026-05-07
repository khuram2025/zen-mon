import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  Activity,
  AlertTriangle,
  ArrowDownUp,
  ArrowLeft,
  Bell,
  ChevronRight,
  Cable,
  Database,
  Gauge,
  Layers,
  Loader2,
  Network,
  RadioTower,
  Router,
  Search,
  Server,
  Shield,
  ShieldAlert,
  Signal,
  Smartphone,
  Wifi,
  WifiOff,
  X,
  Zap,
} from 'lucide-react'
import { api } from '@/lib/api'
import { formatBps, formatBytes, relativeTime, timeAxisTickFormatter, timeTooltipLabelFormatter } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'

type Overview = {
  bytes: number
  packets: number
  flows: number
  exporters: number
  src_hosts: number
  dst_hosts: number
  last_seen: string | null
  current_bps: number
  top_protocol: { protocol: number; name: string; bytes: number } | null
}

type SeriesPoint = { ts: number; bps: number; bytes: number; packets: number; flows: number }
type Talker = { ip: string; bytes: number; packets: number; flows: number }
type Conversation = { src: string; dst: string; protocol_name: string; dst_port: number; service: string; bytes: number; packets: number; flows: number }
type Protocol = { protocol: number; name: string; bytes: number; packets: number; flows: number }
type Application = { name: string; bytes: number; packets: number; flows: number }
type Exporter = { exporter_ip: string; bytes: number; packets: number; flows: number; last_seen: string }
type DeviceStatus = { latency_ms: number; packet_loss_pct: number; uptime_pct: number; flows: number; packets: number; exporters: number; last_seen: string | null }
type Heatmap = { max_bytes: number; cells: { dow: number; hour: number; bytes: number; flows: number }[] }
type Interface = {
  exporter_ip: string
  ifindex: number
  if_name?: string | null
  if_alias?: string | null
  if_speed?: number | null
  device_hostname?: string | null
  in_bytes: number
  out_bytes: number
  bytes: number
  in_packets: number
  out_packets: number
  packets: number
  in_flows: number
  out_flows: number
  flows: number
}

type DscpClass = { dscp: number; label: string; bytes: number; packets: number; flows: number }
type TcpFlagSummary = { total_tcp: number; syn_only: number; ack_only: number; rst: number; fin: number; psh: number; urg: number; no_flags: number }
type NetClass = { name: string; bytes: number; packets: number; flows: number }

const TALKER_COLORS = ['#22d3ee', '#a78bfa', '#f472b6', '#34d399', '#facc15', '#fb923c']
const APP_COLORS = ['#22d3ee', '#34d399', '#facc15', '#fb7185', '#a78bfa', '#f59e0b', '#10b981', '#ef4444']
const PROTO_COLORS = ['#22d3ee', '#a78bfa', '#facc15', '#fb7185', '#34d399', '#fb923c']
const CONV_COLORS = ['#22d3ee', '#34d399', '#facc15', '#a78bfa', '#fb7185', '#fb923c']

export function NetflowDevicePage() {
  const { ip = '' } = useParams<{ ip: string }>()
  return <NetflowPage exporter={ip} />
}

export function NetflowPage({ exporter }: { exporter?: string } = {}) {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [searchParams, setSearchParams] = useSearchParams()
  const ifaceParam = searchParams.get('iface')
  const iface = ifaceParam !== null && ifaceParam !== '' ? Number(ifaceParam) : null
  const hours = range.hours
  const [search, setSearch] = useState('')
  const isDeviceView = !!exporter
  const isIfaceFiltered = iface !== null && Number.isFinite(iface)

  const setIfaceFilter = (val: number | null) => {
    const next = new URLSearchParams(searchParams)
    if (val === null) next.delete('iface')
    else next.set('iface', String(val))
    setSearchParams(next, { replace: true })
  }

  const compareEnabled = searchParams.get('compare') === '1'
  const toggleCompare = () => {
    const next = new URLSearchParams(searchParams)
    if (compareEnabled) next.delete('compare')
    else next.set('compare', '1')
    setSearchParams(next, { replace: true })
  }

  // ─── Click-to-filter URL state for every drill-down dimension ───
  const filterTalker = searchParams.get('talker') || ''
  const filterProtocol = searchParams.get('protocol') || ''
  const filterDscp = searchParams.get('dscp') || ''
  const filterApp = searchParams.get('app') || ''
  const filterNetClass = searchParams.get('netclass') || ''
  const filterTcpFlag = searchParams.get('tcpflag') || ''
  const filterHour = searchParams.get('hour') || ''
  const filterDow = searchParams.get('dow') || ''

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams)
    if (!value) next.delete(key)
    else next.set(key, value)
    setSearchParams(next, { replace: true })
  }
  const setParams = (entries: [string, string | null][]) => {
    const next = new URLSearchParams(searchParams)
    for (const [k, v] of entries) {
      if (!v) next.delete(k)
      else next.set(k, v)
    }
    setSearchParams(next, { replace: true })
  }
  const clearAllFilters = () => {
    const next = new URLSearchParams(searchParams)
    for (const k of ['talker', 'protocol', 'dscp', 'app', 'netclass', 'tcpflag', 'hour', 'dow', 'iface']) next.delete(k)
    setSearchParams(next, { replace: true })
  }
  const anyExtraFilter = !!(filterTalker || filterProtocol || filterDscp || filterApp || filterNetClass || filterTcpFlag || filterHour || filterDow || isIfaceFiltered)

  // Build prior-window QS by shifting back exactly one window length.
  const priorQS = useMemo(() => {
    const params = new URLSearchParams({ hours: String(hours) })
    const fromMs = Date.parse(range.fromISO)
    const toMs = Date.parse(range.toISO)
    const span = toMs - fromMs
    params.set('from', new Date(fromMs - span).toISOString())
    params.set('to', new Date(toMs - span).toISOString())
    if (exporter) params.set('exporter', exporter)
    if (isIfaceFiltered) params.set('iface', String(iface))
    return params.toString()
  }, [hours, range.fromISO, range.toISO, exporter, iface, isIfaceFiltered])

  // Build the shared time-range query string. Custom ranges send absolute from/to
  // so the data anchors to the picked window rather than "now − hours".
  const rangeQS = useMemo(() => {
    const params = new URLSearchParams({ hours: String(hours) })
    if (isCustom) {
      params.set('from', range.fromISO)
      params.set('to', range.toISO)
    }
    if (exporter) params.set('exporter', exporter)
    if (isIfaceFiltered) params.set('iface', String(iface))
    if (filterTalker) params.set('talker', filterTalker)
    if (filterProtocol) params.set('protocol', filterProtocol)
    if (filterDscp) params.set('dscp', filterDscp)
    if (filterApp) params.set('app', filterApp)
    if (filterNetClass) params.set('netclass', filterNetClass)
    if (filterTcpFlag) params.set('tcpflag', filterTcpFlag)
    if (filterHour) params.set('hour', filterHour)
    if (filterDow) params.set('dow', filterDow)
    return params.toString()
  }, [hours, isCustom, range.fromISO, range.toISO, exporter, iface, isIfaceFiltered, filterTalker, filterProtocol, filterDscp, filterApp, filterNetClass, filterTcpFlag, filterHour, filterDow])

  // Build the QS for the Top Interfaces list itself — this should NOT itself be filtered by iface,
  // otherwise picking one interface would cause the list to collapse to that single item.
  const interfacesQS = useMemo(() => {
    const params = new URLSearchParams({ hours: String(hours), limit: '5' })
    if (isCustom) {
      params.set('from', range.fromISO)
      params.set('to', range.toISO)
    }
    if (exporter) params.set('exporter', exporter)
    return params.toString()
  }, [hours, isCustom, range.fromISO, range.toISO, exporter])

  // Cache key includes every filter dim so a refetch is a fresh entry.
  const rangeKey = `${isCustom ? `c:${range.fromISO}|${range.toISO}` : `p:${hours}h`}|${exporter || 'all'}|${iface ?? 'all'}|${filterTalker}|${filterProtocol}|${filterDscp}|${filterApp}|${filterNetClass}|${filterTcpFlag}|${filterHour}|${filterDow}`

  const overview = useQuery<Overview>({
    queryKey: ['netflow', 'overview', rangeKey],
    queryFn: async () => (await api.get(`/netflow/overview?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const timeseries = useQuery<SeriesPoint[]>({
    queryKey: ['netflow', 'timeseries', rangeKey],
    queryFn: async () => (await api.get(`/netflow/timeseries?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const timeseriesPrior = useQuery<SeriesPoint[]>({
    queryKey: ['netflow', 'timeseries-prior', priorQS],
    queryFn: async () => (await api.get(`/netflow/timeseries?${priorQS}`)).data,
    enabled: compareEnabled,
    refetchInterval: false,
  })
  const talkers = useQuery<Talker[]>({
    queryKey: ['netflow', 'talkers', rangeKey],
    queryFn: async () => (await api.get(`/netflow/top-talkers?${rangeQS}&limit=6`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const conversations = useQuery<Conversation[]>({
    queryKey: ['netflow', 'conversations', rangeKey],
    queryFn: async () => (await api.get(`/netflow/top-conversations?${rangeQS}&limit=10`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const protocols = useQuery<Protocol[]>({
    queryKey: ['netflow', 'protocols', rangeKey],
    queryFn: async () => (await api.get(`/netflow/protocols?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const applications = useQuery<Application[]>({
    queryKey: ['netflow', 'applications', rangeKey],
    queryFn: async () => (await api.get(`/netflow/applications?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const exporters = useQuery<Exporter[]>({
    queryKey: ['netflow', 'exporters', rangeKey],
    queryFn: async () => (await api.get(`/netflow/exporters?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 15_000,
  })
  const deviceStatus = useQuery<DeviceStatus>({
    queryKey: ['netflow', 'device-status', rangeKey],
    queryFn: async () => (await api.get(`/netflow/device-status?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 30_000,
  })
  const heatmap = useQuery<Heatmap>({
    queryKey: ['netflow', 'heatmap', rangeKey],
    queryFn: async () => (await api.get(`/netflow/heatmap?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 60_000,
  })
  const interfaces = useQuery<Interface[]>({
    queryKey: ['netflow', 'interfaces', `${isCustom ? `c:${range.fromISO}|${range.toISO}` : `p:${hours}h`}|${exporter || 'all'}`],
    queryFn: async () => (await api.get(`/netflow/interfaces?${interfacesQS}`)).data,
    refetchInterval: isCustom ? false : 60_000,
  })
  const dscp = useQuery<DscpClass[]>({
    queryKey: ['netflow', 'dscp', rangeKey],
    queryFn: async () => (await api.get(`/netflow/dscp?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 30_000,
  })
  const tcpFlags = useQuery<TcpFlagSummary>({
    queryKey: ['netflow', 'tcp-flags', rangeKey],
    queryFn: async () => (await api.get(`/netflow/tcp-flags?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 30_000,
  })
  const netClasses = useQuery<NetClass[]>({
    queryKey: ['netflow', 'network-classes', rangeKey],
    queryFn: async () => (await api.get(`/netflow/network-classes?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : 60_000,
  })

  const loading = overview.isLoading || timeseries.isLoading
  const totalBytes = overview.data?.bytes || 0
  const series = timeseries.data || []

  // Merge current + prior into a single dataset keyed by current ts; prior values are
  // shifted forward by the window length so they line up on the same x-axis.
  const mergedSeries = useMemo(() => {
    if (!compareEnabled || !timeseriesPrior.data?.length) return series.map((p) => ({ ...p }))
    const fromMs = Date.parse(range.fromISO)
    const toMs = Date.parse(range.toISO)
    const span = toMs - fromMs
    const priorByTs = new Map<number, number>()
    for (const p of timeseriesPrior.data) priorByTs.set(p.ts + span, p.bps)
    return series.map((p) => ({ ...p, prior_bps: priorByTs.get(p.ts) ?? null }))
  }, [series, timeseriesPrior.data, compareEnabled, range.fromISO, range.toISO])

  const compareDelta = useMemo(() => {
    if (!compareEnabled || !timeseriesPrior.data?.length || !series.length) return null
    const cur = series.reduce((a, b) => a + b.bytes, 0)
    const prior = timeseriesPrior.data.reduce((a, b) => a + b.bytes, 0)
    if (prior === 0) return null
    return ((cur - prior) / prior) * 100
  }, [series, timeseriesPrior.data, compareEnabled])
  const protocolData = protocols.data || []
  const applicationData = applications.data || []
  const conversationData = conversations.data || []
  const talkerData = talkers.data || []
  const exporterData = exporters.data || []

  const filteredConversations = useMemo(() => {
    if (!search.trim()) return conversationData
    const q = search.trim().toLowerCase()
    return conversationData.filter(
      (c) =>
        c.src.includes(q) ||
        c.dst.includes(q) ||
        c.service.toLowerCase().includes(q) ||
        c.protocol_name.toLowerCase().includes(q),
    )
  }, [conversationData, search])

  const health = useMemo(() => {
    if (!overview.data?.last_seen) return { label: 'waiting', variant: 'outline' as const }
    const age = Date.now() - new Date(overview.data.last_seen).getTime()
    if (age < 2 * 60_000) return { label: 'receiving', variant: 'success' as const }
    if (age < 15 * 60_000) return { label: 'stale', variant: 'warning' as const }
    return { label: 'offline', variant: 'danger' as const }
  }, [overview.data?.last_seen])

  // Build alerts from real signals (exporter staleness, traffic spikes, RST loss).
  const alerts = useMemo(() => buildAlerts(overview.data, exporterData, deviceStatus.data, applicationData), [overview.data, exporterData, deviceStatus.data, applicationData])

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          {isDeviceView && (
            <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
              <Link to="/netflow" className="inline-flex items-center gap-1 hover:text-text">
                <ArrowLeft className="h-3 w-3" />
                NetFlow
              </Link>
              <ChevronRight className="h-3 w-3" />
              <span>Device</span>
              <ChevronRight className="h-3 w-3" />
              <span className="font-mono text-text">{exporter}</span>
            </div>
          )}
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Network className="h-5 w-5 text-primary" />
            {isDeviceView ? <>Device Deep Dive — <span className="font-mono text-primary">{exporter}</span></> : 'NetFlow Monitoring Dashboard'}
          </h1>
          <p className="text-xs text-muted">
            {isDeviceView
              ? `All flow data is filtered to exporter ${exporter}.`
              : 'Live flow telemetry: throughput, devices, applications, sessions.'}
          </p>
          {anyExtraFilter && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {isIfaceFiltered && (
                <FilterChip icon={Cable} label="ifIndex" value={String(iface)} onClear={() => setIfaceFilter(null)} />
              )}
              {filterTalker && <FilterChip label="Talker" value={filterTalker} onClear={() => setParam('talker', null)} mono />}
              {filterProtocol && <FilterChip label="Protocol" value={filterProtocol} onClear={() => setParam('protocol', null)} />}
              {filterApp && <FilterChip label="App" value={filterApp} onClear={() => setParam('app', null)} />}
              {filterDscp && <FilterChip label="DSCP" value={filterDscp} onClear={() => setParam('dscp', null)} />}
              {filterNetClass && <FilterChip label="Net" value={filterNetClass} onClear={() => setParam('netclass', null)} />}
              {filterTcpFlag && <FilterChip label="TCP flag" value={filterTcpFlag} onClear={() => setParam('tcpflag', null)} />}
              {(filterHour || filterDow) && (
                <FilterChip
                  label="When"
                  value={`${filterDow ? ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][Number(filterDow) - 1] || '' : ''} ${filterHour ? `${filterHour}:00` : ''}`.trim()}
                  onClear={() => setParams([['hour', null], ['dow', null]])}
                />
              )}
              <button
                onClick={clearAllFilters}
                className="rounded-full border border-border bg-surface2/40 px-2 py-0.5 text-[11px] text-muted hover:text-text"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className="relative hidden md:block">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search devices, applications, interfaces..."
              className="h-8 w-80 rounded-md border border-border bg-surface2/60 pl-7 pr-3 text-xs placeholder:text-muted focus:border-primary focus:outline-none"
            />
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
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <KpiCard
          icon={ArrowDownUp}
          tone="cyan"
          label="Total Traffic"
          value={formatBytes(totalBytes)}
          sub={`${(overview.data?.packets || 0).toLocaleString()} packets`}
        />
        <KpiCard
          icon={Wifi}
          tone="violet"
          label="Active Flows"
          value={(overview.data?.flows || 0).toLocaleString()}
          sub={`${formatBps(overview.data?.current_bps || 0)} current rate`}
        />
        <KpiCard
          icon={RadioTower}
          tone="amber"
          label="Top Devices"
          value={(overview.data?.exporters || 0).toLocaleString()}
          sub={<Badge variant={health.variant}>{health.label}</Badge>}
        />
        <KpiCard
          icon={Layers}
          tone="emerald"
          label="Top Applications"
          value={applicationData.length.toLocaleString()}
          sub={applicationData[0]?.name || '—'}
        />
        <KpiCard
          icon={Smartphone}
          tone="pink"
          label="Active Sessions"
          value={`${(overview.data?.src_hosts || 0).toLocaleString()} / ${(overview.data?.dst_hosts || 0).toLocaleString()}`}
          sub="src / dst hosts"
        />
      </div>

      {/* Throughput + Traffic Heatmap + Device Status */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.9fr)_minmax(280px,360px)]">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Traffic Throughput</CardTitle>
              <p className="text-xs text-muted">{range.label}{compareEnabled ? ' · vs prior period' : ''}</p>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-cyan-400" />Inbound</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-violet-400" />Outbound</span>
              {compareEnabled && <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-amber-400 opacity-70" />Prior</span>}
              <button
                onClick={toggleCompare}
                className={`rounded border px-2 py-0.5 text-[10px] font-semibold transition-colors ${compareEnabled ? 'border-amber-400/60 bg-amber-400/10 text-amber-300' : 'border-border bg-surface2/40 text-muted hover:text-text'}`}
                title="Compare to previous period of same length"
              >
                Compare ↗
              </button>
              {compareDelta !== null && (
                <span className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${compareDelta >= 0 ? 'border-cyan-400/40 bg-cyan-500/10 text-cyan-300' : 'border-emerald-400/40 bg-emerald-500/10 text-emerald-300'}`}>
                  {compareDelta >= 0 ? '+' : ''}{compareDelta.toFixed(1)}%
                </span>
              )}
              {loading && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
            </div>
          </CardHeader>
          <CardContent>
            <div className="mb-3 flex items-baseline gap-2">
              <ThroughputDisplay bps={overview.data?.current_bps || 0} />
              <span className="text-[11px] uppercase tracking-wider text-muted">current rate</span>
            </div>
            <div className="h-[220px]">
              {series.length === 0 ? (
                <EmptyState />
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={mergedSeries} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                    <defs>
                      <linearGradient id="netflowIn" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#22d3ee" stopOpacity={0.55} />
                        <stop offset="95%" stopColor="#22d3ee" stopOpacity={0.02} />
                      </linearGradient>
                      <linearGradient id="netflowOut" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#a78bfa" stopOpacity={0.45} />
                        <stop offset="95%" stopColor="#a78bfa" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} vertical={false} />
                    <XAxis dataKey="ts" tickFormatter={timeAxisTickFormatter(hours)} tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                    <YAxis tickFormatter={formatBps} tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} width={72} />
                    <Tooltip
                      labelFormatter={timeTooltipLabelFormatter}
                      formatter={(value: any, name: string) => [formatBps(Number(value)), name === 'prior_bps' ? 'Prior' : 'Rate']}
                      contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8 }}
                    />
                    {compareEnabled && (
                      <Area type="monotone" dataKey="prior_bps" stroke="#fbbf24" strokeWidth={1.5} strokeDasharray="4 3" fill="none" connectNulls />
                    )}
                    <Area type="monotone" dataKey="bps" stroke="#22d3ee" strokeWidth={2} fill="url(#netflowIn)" />
                    <Area type="monotone" dataKey="packets" stroke="#a78bfa" strokeWidth={1.4} fill="url(#netflowOut)" />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </CardContent>
        </Card>

        <TrafficHeatmap
          data={heatmap.data}
          activeHour={filterHour ? Number(filterHour) : null}
          activeDow={filterDow ? Number(filterDow) : null}
          onSelect={(dow, hour) => {
            const sameCell = filterHour === String(hour) && filterDow === String(dow)
            setParams([['hour', sameCell ? null : String(hour)], ['dow', sameCell ? null : String(dow)]])
          }}
        />
        <DeviceStatusCard status={deviceStatus.data} exporters={exporterData} deviceView={isDeviceView} />
      </div>

      {/* Donuts row */}
      <div className="grid grid-cols-1 items-stretch gap-4 md:grid-cols-2 xl:grid-cols-4">
        <DonutCard
          title="Top Talkers"
          subtitle="By bytes — click to filter"
          data={talkerData.map((t) => ({ name: t.ip, value: t.bytes }))}
          colors={TALKER_COLORS}
          labelMode="ip"
          activeName={filterTalker || null}
          onSelect={(name) => setParam('talker', filterTalker === name ? null : name)}
        />
        <DonutCard
          title="Top Applications"
          subtitle="By bytes — click to filter"
          data={applicationData.slice(0, 6).map((a) => ({ name: a.name, value: a.bytes }))}
          colors={APP_COLORS}
          activeName={filterApp || null}
          onSelect={(name) => setParam('app', filterApp === name ? null : name)}
        />
        <DonutCard
          title="Protocol Distribution"
          subtitle="Traffic split — click to filter"
          data={protocolData.slice(0, 6).map((p) => ({ name: p.name, value: p.bytes, _proto: p.protocol }))}
          colors={PROTO_COLORS}
          activeName={filterProtocol ? (protocolData.find((p) => String(p.protocol) === filterProtocol)?.name ?? null) : null}
          onSelect={(name) => {
            const p = protocolData.find((x) => x.name === name)
            if (!p) return
            setParam('protocol', String(p.protocol) === filterProtocol ? null : String(p.protocol))
          }}
        />
        <DonutCard
          title="Top Conversations"
          subtitle="src → dst pairs"
          data={conversationData.slice(0, 6).map((c) => ({ name: `${c.src} → ${c.dst}`, value: c.bytes }))}
          colors={CONV_COLORS}
          compactNames
        />
      </div>

      {/* Left: Recent Flow Records + Security & QoS row | Right: Top Interfaces + Alerts */}
      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(320px,0.7fr)]">
        <div className="flex flex-col gap-4">
          <ConversationTable conversations={filteredConversations} lastSeen={overview.data?.last_seen} label={range.label} />
          <div className="grid grid-cols-1 items-stretch gap-4 md:grid-cols-3">
            <DscpCard
              data={dscp.data || []}
              activeDscp={filterDscp ? Number(filterDscp) : null}
              onSelect={(d) => setParam('dscp', String(d) === filterDscp ? null : String(d))}
            />
            <TcpFlagsCard
              data={tcpFlags.data}
              activeFlag={filterTcpFlag || null}
              onSelect={(f) => setParam('tcpflag', f === filterTcpFlag ? null : f)}
            />
            <NetworkClassesCard
              data={netClasses.data || []}
              activeClass={filterNetClass || null}
              onSelect={(n) => setParam('netclass', n === filterNetClass ? null : n)}
            />
          </div>
        </div>
        <div className="flex flex-col gap-4">
          <TopInterfacesCard
            interfaces={interfaces.data || []}
            loading={interfaces.isLoading}
            activeIface={iface}
            onSelect={setIfaceFilter}
            showExporter={!isDeviceView}
            compact
          />
          <AlertsPanel alerts={alerts} />
        </div>
      </div>

      {/* Quick links */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Link to="/netflow/forensics" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-3 py-1.5 text-muted hover:border-primary/40 hover:bg-primary/5 hover:text-text">
          <Search className="h-3.5 w-3.5" />
          Forensics search
        </Link>
        <Link to="/netflow/anomalies" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-3 py-1.5 text-muted hover:border-primary/40 hover:bg-primary/5 hover:text-text">
          <ShieldAlert className="h-3.5 w-3.5" />
          Anomaly Detection
        </Link>
        <Link to="/netflow/capacity" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-3 py-1.5 text-muted hover:border-primary/40 hover:bg-primary/5 hover:text-text">
          <Gauge className="h-3.5 w-3.5" />
          Capacity Planning
        </Link>
        <Link to="/netflow/saved-views" className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-3 py-1.5 text-muted hover:border-primary/40 hover:bg-primary/5 hover:text-text">
          <Layers className="h-3.5 w-3.5" />
          Saved Views
        </Link>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// KPI Cards
// ─────────────────────────────────────────────────────────────────

function KpiCard({
  icon: Icon,
  label,
  value,
  sub,
  tone,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string
  sub?: React.ReactNode
  tone: 'cyan' | 'violet' | 'amber' | 'emerald' | 'pink'
}) {
  const ringMap: Record<typeof tone, string> = {
    cyan: 'bg-cyan-500/15 text-cyan-300 ring-cyan-400/30 shadow-[0_0_22px_-4px_rgba(34,211,238,0.55)]',
    violet: 'bg-violet-500/15 text-violet-300 ring-violet-400/30 shadow-[0_0_22px_-4px_rgba(167,139,250,0.55)]',
    amber: 'bg-amber-500/15 text-amber-300 ring-amber-400/30 shadow-[0_0_22px_-4px_rgba(251,191,36,0.55)]',
    emerald: 'bg-emerald-500/15 text-emerald-300 ring-emerald-400/30 shadow-[0_0_22px_-4px_rgba(52,211,153,0.55)]',
    pink: 'bg-pink-500/15 text-pink-300 ring-pink-400/30 shadow-[0_0_22px_-4px_rgba(244,114,182,0.55)]',
  }
  return (
    <Card className="relative overflow-hidden">
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full ring-1 ${ringMap[tone]}`}>
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
          <div className="mt-0.5 truncate text-2xl font-semibold leading-tight text-text">{value}</div>
          <div className="mt-0.5 truncate text-[10px] text-muted">{sub}</div>
        </div>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────────────────────────────────────────────
// Donut card (Top Talkers / Apps / Protocols / Conversations)
// ─────────────────────────────────────────────────────────────────

function DonutCard({
  title,
  subtitle,
  data,
  colors,
  labelMode,
  compactNames,
  onSelect,
  activeName,
}: {
  title: string
  subtitle: string
  data: { name: string; value: number }[]
  colors: string[]
  labelMode?: 'ip'
  compactNames?: boolean
  onSelect?: (name: string) => void
  activeName?: string | null
}) {
  const total = data.reduce((acc, d) => acc + (d.value || 0), 0)
  return (
    <Card className="flex h-full flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{title}</CardTitle>
        <p className="text-[11px] text-muted">{subtitle}</p>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col pb-4">
        <div className="flex items-center gap-3">
          <div className="relative h-[120px] w-[120px] shrink-0">
            {data.length === 0 ? (
              <EmptyState compact />
            ) : (
              <>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={data}
                      dataKey="value"
                      nameKey="name"
                      innerRadius={38}
                      outerRadius={56}
                      paddingAngle={2}
                      stroke="rgb(var(--surface))"
                      strokeWidth={2}
                      onClick={onSelect ? (slice: any) => onSelect(slice?.name) : undefined}
                      cursor={onSelect ? 'pointer' : 'default'}
                    >
                      {data.map((d, i) => (
                        <Cell
                          key={i}
                          fill={colors[i % colors.length]}
                          opacity={activeName && activeName !== d.name ? 0.3 : 1}
                        />
                      ))}
                    </Pie>
                    <Tooltip
                      formatter={(value: any) => formatBytes(Number(value))}
                      contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8 }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                  <div className="text-[8px] uppercase tracking-wider text-muted">Total</div>
                  <div className="text-[11px] font-semibold leading-tight">{formatBytes(total)}</div>
                </div>
              </>
            )}
          </div>
          <div className="min-w-0 flex-1 space-y-1 text-[11px]">
            {data.slice(0, 5).map((d, i) => {
              const active = activeName === d.name
              const Row = onSelect ? 'button' : 'div'
              return (
                <Row
                  key={`${d.name}-${i}`}
                  {...(onSelect ? { onClick: () => onSelect(d.name), type: 'button' as const } : {})}
                  className={`flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-left transition-colors ${onSelect ? 'cursor-pointer hover:bg-primary/5' : ''} ${active ? 'bg-primary/10 ring-1 ring-primary/40' : ''}`}
                >
                  <span className="flex min-w-0 items-center gap-1.5 truncate">
                    <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: colors[i % colors.length] }} />
                    <span className={`truncate ${labelMode === 'ip' || compactNames ? 'font-mono text-[10px]' : ''}`}>{d.name}</span>
                  </span>
                  <span className="shrink-0 text-muted">{total ? ((d.value / total) * 100).toFixed(1) : '0.0'}%</span>
                </Row>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────────────────────────────────────────────
// Flow Map (world map with arcs + exporter list)
// ─────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────
// Alerts panel
// ─────────────────────────────────────────────────────────────────

type Alert = {
  id: string
  title: string
  body: string
  severity: 'critical' | 'warning' | 'info' | 'success'
  icon: React.ComponentType<{ className?: string }>
  ts?: string
}

function buildAlerts(overview?: Overview, exporters: Exporter[] = [], device?: DeviceStatus, applications: Application[] = []): Alert[] {
  const list: Alert[] = []
  const now = Date.now()
  for (const e of exporters) {
    const age = now - new Date(e.last_seen).getTime()
    if (age >= 15 * 60_000) {
      list.push({
        id: `down-${e.exporter_ip}`,
        title: 'Device Offline',
        body: `${e.exporter_ip} stopped exporting flows`,
        severity: 'critical',
        icon: WifiOff,
        ts: e.last_seen,
      })
    } else if (age >= 2 * 60_000) {
      list.push({
        id: `stale-${e.exporter_ip}`,
        title: 'Stale Exporter',
        body: `${e.exporter_ip} last seen ${relativeTime(e.last_seen)}`,
        severity: 'warning',
        icon: AlertTriangle,
        ts: e.last_seen,
      })
    }
  }
  if (overview && overview.current_bps > 0) {
    if (overview.current_bps > 100_000_000) {
      list.push({
        id: 'high-bw',
        title: 'High Bandwidth Usage',
        body: `${formatBps(overview.current_bps)} aggregate rate`,
        severity: 'warning',
        icon: Zap,
      })
    } else {
      list.push({
        id: 'throughput-ok',
        title: 'Throughput Healthy',
        body: `${formatBps(overview.current_bps)} aggregate rate`,
        severity: 'success',
        icon: Activity,
      })
    }
  }
  if (device && device.packet_loss_pct >= 5) {
    list.push({
      id: 'loss',
      title: 'Elevated Packet Loss',
      body: `${device.packet_loss_pct.toFixed(2)}% RST/abort flows`,
      severity: 'critical',
      icon: ShieldAlert,
    })
  }
  const suspicious = applications.find((a) => a.name === 'System Services')
  if (suspicious && suspicious.bytes > 0) {
    list.push({
      id: 'unauth',
      title: 'Privileged Service Traffic',
      body: `${formatBytes(suspicious.bytes)} on system ports`,
      severity: 'info',
      icon: Shield,
    })
  }
  if (overview?.flows && overview.flows > 1_000_000) {
    list.push({
      id: 'flows-high',
      title: 'Large Flow Volume',
      body: `${overview.flows.toLocaleString()} flows in window`,
      severity: 'info',
      icon: Signal,
    })
  }
  return list.slice(0, 6)
}

function AlertsPanel({ alerts }: { alerts: Alert[] }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <Bell className="h-4 w-4 text-warning" />
          <div>
            <CardTitle>Alerts / Incidents</CardTitle>
            <p className="text-xs text-muted">Live signals from the collector</p>
          </div>
        </div>
        <Badge variant={alerts.some((a) => a.severity === 'critical') ? 'danger' : alerts.length ? 'warning' : 'success'}>{alerts.length}</Badge>
      </CardHeader>
      <CardContent className="space-y-2">
        {alerts.length === 0 && (
          <div className="rounded-md border border-dashed border-border bg-surface2/30 p-4 text-center text-xs text-muted">
            No active incidents.
          </div>
        )}
        {alerts.map((a) => {
          const palette =
            a.severity === 'critical'
              ? { dot: 'bg-rose-500 shadow-[0_0_10px_rgba(244,63,94,0.7)]', icon: 'bg-rose-500/15 text-rose-300', side: 'border-l-rose-500/70' }
              : a.severity === 'warning'
              ? { dot: 'bg-amber-400 shadow-[0_0_10px_rgba(251,191,36,0.7)]', icon: 'bg-amber-400/15 text-amber-300', side: 'border-l-amber-400/70' }
              : a.severity === 'success'
              ? { dot: 'bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.6)]', icon: 'bg-emerald-400/15 text-emerald-300', side: 'border-l-emerald-400/70' }
              : { dot: 'bg-cyan-400 shadow-[0_0_10px_rgba(34,211,238,0.6)]', icon: 'bg-cyan-400/15 text-cyan-300', side: 'border-l-cyan-400/70' }
          const Icon = a.icon
          return (
            <div key={a.id} className={`flex gap-2.5 rounded-md border border-border border-l-2 bg-surface2/40 p-2.5 ${palette.side}`}>
              <div className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${palette.icon}`}>
                <Icon className="h-3.5 w-3.5" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className={`h-1.5 w-1.5 rounded-full ${palette.dot}`} />
                  <span className="text-xs font-medium text-text">{a.title}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-muted">{a.body}</div>
                {a.ts && <div className="mt-0.5 text-[10px] text-muted">{relativeTime(a.ts)}</div>}
              </div>
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}

// ─────────────────────────────────────────────────────────────────
// Device Status (Latency / Packet Loss / Uptime)
// ─────────────────────────────────────────────────────────────────

function DeviceStatusCard({ status, exporters, deviceView }: { status?: DeviceStatus; exporters: Exporter[]; deviceView?: boolean }) {
  const latency = status?.latency_ms ?? 0
  const loss = status?.packet_loss_pct ?? 0
  return (
    <Card className="shrink-0">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Device Status</CardTitle>
        <p className="text-[11px] text-muted">Health from flow telemetry</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <MiniStat icon={Gauge} label="Latency" value={`${latency} ms`} tone="cyan" />
          <MiniStat icon={Database} label="Packet Loss" value={`${loss.toFixed(2)}%`} tone={loss >= 5 ? 'rose' : 'emerald'} />
        </div>
        <div className="rounded-md border border-border bg-surface2/30 p-2.5">
          <div className="mb-1.5 flex items-center justify-between text-[11px]">
            <span className="font-medium text-text">Exporting Devices</span>
            <span className="text-muted">{exporters.length}</span>
          </div>
          <div className="max-h-[180px] space-y-1.5 overflow-auto pr-1">
            {exporters.length === 0 && (
              <div className="rounded border border-dashed border-border bg-surface/40 p-2 text-center text-[10px] text-muted">
                No exporters in range
              </div>
            )}
            {exporters.map((e) => {
              const age = Date.now() - new Date(e.last_seen).getTime()
              const tone =
                age < 2 * 60_000
                  ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.7)]'
                  : age < 15 * 60_000
                  ? 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.7)]'
                  : 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.7)]'
              const Row = deviceView ? 'div' : Link
              const rowProps = deviceView
                ? { className: 'flex items-center gap-2 rounded border border-border/60 bg-surface/50 px-2 py-1.5' }
                : { to: `/netflow/devices/${encodeURIComponent(e.exporter_ip)}`, className: 'flex items-center gap-2 rounded border border-border/60 bg-surface/50 px-2 py-1.5 transition-colors hover:border-primary/60 hover:bg-primary/5' }
              return (
                <Row key={e.exporter_ip} {...(rowProps as any)}>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone}`} />
                  <span className="truncate font-mono text-[10px]">{e.exporter_ip}</span>
                  <span className="ml-auto shrink-0 text-[9px] text-muted" title={e.last_seen}>
                    {relativeTime(e.last_seen)}
                  </span>
                  {!deviceView && <ChevronRight className="h-3 w-3 shrink-0 text-muted" />}
                </Row>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function MiniStat({ icon: Icon, label, value, tone }: { icon: React.ComponentType<{ className?: string }>; label: string; value: string; tone: 'cyan' | 'emerald' | 'rose' }) {
  const toneMap = { cyan: 'text-cyan-400 bg-cyan-500/10', emerald: 'text-emerald-400 bg-emerald-500/10', rose: 'text-rose-400 bg-rose-500/10' }
  return (
    <div className="rounded-md border border-border bg-surface2/30 p-2.5">
      <div className="flex items-center justify-between">
        <div className={`flex h-6 w-6 items-center justify-center rounded ${toneMap[tone]}`}>
          <Icon className="h-3 w-3" />
        </div>
      </div>
      <div className="mt-1.5 text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="text-sm font-semibold">{value}</div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────
// Recent flow records (table)
// ─────────────────────────────────────────────────────────────────

type SortKey = 'src' | 'dst' | 'service' | 'protocol_name' | 'bytes' | 'packets'

function ConversationTable({ conversations, lastSeen, label }: { conversations: Conversation[]; lastSeen?: string | null; label?: string }) {
  const [sortKey, setSortKey] = useState<SortKey>('bytes')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  const sorted = useMemo(() => {
    const out = [...conversations]
    out.sort((a, b) => {
      const av = a[sortKey] as string | number
      const bv = b[sortKey] as string | number
      const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))
      return sortDir === 'asc' ? cmp : -cmp
    })
    return out
  }, [conversations, sortKey, sortDir])

  const toggle = (k: SortKey) => {
    if (k === sortKey) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(k); setSortDir(k === 'src' || k === 'dst' || k === 'service' || k === 'protocol_name' ? 'asc' : 'desc') }
  }
  const arrow = (k: SortKey) => (sortKey === k ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '')

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Recent Flow Records</CardTitle>
          <p className="text-xs text-muted">Highest-volume endpoint pairs in {label || 'selected range'}</p>
        </div>
        <div className="text-xs text-muted">Last flow {relativeTime(lastSeen)}</div>
      </CardHeader>
      <CardContent>
        <Table>
          <THead>
            <Tr>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('src')}>Source{arrow('src')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('dst')}>Destination{arrow('dst')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('service')}>Application{arrow('service')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('protocol_name')}>Protocol{arrow('protocol_name')}</button></Th>
              <Th className="text-right"><button className="ml-auto flex items-center gap-1 hover:text-text" onClick={() => toggle('bytes')}>Bytes{arrow('bytes')}</button></Th>
              <Th className="text-right"><button className="ml-auto flex items-center gap-1 hover:text-text" onClick={() => toggle('packets')}>Packets{arrow('packets')}</button></Th>
              <Th>Status</Th>
            </Tr>
          </THead>
          <TBody>
            {sorted.length === 0 && (
              <Tr><Td colSpan={7} className="py-8 text-center text-xs text-muted">No flow conversations match.</Td></Tr>
            )}
            {sorted.map((c) => (
              <Tr key={`${c.src}-${c.dst}-${c.protocol_name}-${c.dst_port}`}>
                <Td className="font-mono text-xs">{c.src}</Td>
                <Td className="font-mono text-xs">{c.dst}</Td>
                <Td>{c.service}</Td>
                <Td><Badge variant="outline">{c.protocol_name}</Badge></Td>
                <Td className="text-right text-sm">{formatBytes(c.bytes)}</Td>
                <Td className="text-right text-sm">{c.packets.toLocaleString()}</Td>
                <Td><span className="inline-flex items-center gap-1.5 text-xs text-success"><span className="h-1.5 w-1.5 rounded-full bg-success" />Allowed</span></Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}

// ─────────────────────────────────────────────────────────────────
// Traffic Heatmap (24h × 7d)
// ─────────────────────────────────────────────────────────────────

function TrafficHeatmap({ data, activeHour, activeDow, onSelect }: { data?: Heatmap; activeHour?: number | null; activeDow?: number | null; onSelect?: (dow: number, hour: number) => void }) {
  const cells = data?.cells || []
  const max = Math.max(1, data?.max_bytes || 0)
  const map = new Map<string, number>()
  for (const c of cells) map.set(`${c.dow}-${c.hour}`, c.bytes)
  // ClickHouse toDayOfWeek -> 1=Mon..7=Sun
  const dows = [1, 2, 3, 4, 5, 6, 7]
  const dowLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
  const hours = Array.from({ length: 24 }, (_, i) => i)
  const colorFor = (v: number) => {
    if (v <= 0) return 'rgba(148,163,184,0.07)'
    const t = Math.min(1, Math.sqrt(v / max))
    // warm gradient: green -> yellow -> orange -> red (proper "heat")
    const hue = 150 - 150 * t
    const sat = 75 + 15 * t
    const light = 55 - 15 * t
    return `hsl(${hue.toFixed(0)} ${sat.toFixed(0)}% ${light.toFixed(0)}%)`
  }
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Traffic Heatmap</CardTitle>
        <p className="text-[11px] text-muted">Bytes by hour over the last 7 days</p>
      </CardHeader>
      <CardContent>
        {cells.length === 0 ? (
          <EmptyState compact />
        ) : (
          <div className="space-y-1.5">
            {dows.map((dow, di) => (
              <div key={dow} className="flex items-center gap-1.5">
                <span className="w-7 text-[10px] uppercase tracking-wider text-muted">{dowLabels[di]}</span>
                <div className="grid flex-1 grid-cols-24 gap-[3px]" style={{ gridTemplateColumns: 'repeat(24, minmax(0, 1fr))' }}>
                  {hours.map((h) => {
                    const v = map.get(`${dow}-${h}`) || 0
                    const active = activeHour === h && activeDow === dow
                    const Cell = onSelect ? 'button' : 'div'
                    return (
                      <Cell
                        key={h}
                        {...(onSelect ? { type: 'button' as const, onClick: () => onSelect(dow, h) } : {})}
                        title={`${dowLabels[di]} ${h}:00 — ${formatBytes(v)}${onSelect ? ' (click to filter)' : ''}`}
                        className={`aspect-square rounded-sm ${onSelect ? 'cursor-pointer hover:ring-2 hover:ring-primary/60' : ''} ${active ? 'ring-2 ring-primary' : ''}`}
                        style={{ background: colorFor(v) }}
                      />
                    )
                  })}
                </div>
              </div>
            ))}
            <div className="flex items-center justify-between pt-2 text-[10px] text-muted">
              <span>0h</span>
              <div className="flex items-center gap-1">
                <span>Less</span>
                <div className="flex gap-0.5">
                  {[0.1, 0.3, 0.55, 0.8, 1].map((t, i) => (
                    <span key={i} className="h-2 w-3 rounded-sm" style={{ background: colorFor(t * max) }} />
                  ))}
                </div>
                <span>More</span>
              </div>
              <span>23h</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function formatBitsPerSecond(bps: number): string {
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let v = bps; let i = 0
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

const DSCP_COLORS = ['#22d3ee', '#34d399', '#facc15', '#fb7185', '#a78bfa', '#fb923c', '#06b6d4', '#10b981', '#ef4444']

function DscpCard({ data, activeDscp, onSelect }: { data: DscpClass[]; activeDscp?: number | null; onSelect?: (dscp: number) => void }) {
  const top = data.slice(0, 6)
  const total = top.reduce((a, b) => a + b.bytes, 0)
  const max = Math.max(1, ...top.map((d) => d.bytes))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Shield className="h-4 w-4 text-cyan-400" />
        <div>
          <CardTitle className="text-sm">DSCP / QoS Classes</CardTitle>
          <p className="text-[11px] text-muted">Click a class to filter the page</p>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {top.length === 0 && <EmptyState compact />}
        {top.map((d, i) => {
          const active = activeDscp === d.dscp
          return (
            <button
              key={d.dscp}
              type="button"
              onClick={() => onSelect?.(d.dscp)}
              className={`block w-full space-y-0.5 rounded px-1 py-1 text-left transition-colors hover:bg-primary/5 ${active ? 'bg-primary/10 ring-1 ring-primary/40' : ''}`}
            >
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="flex min-w-0 items-center gap-1.5 truncate">
                  <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: DSCP_COLORS[i % DSCP_COLORS.length] }} />
                  <span className="truncate font-medium">{d.label}</span>
                </span>
                <span className="shrink-0 text-muted">{formatBytes(d.bytes)} · {total ? ((d.bytes / total) * 100).toFixed(1) : 0}%</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-surface">
                <div className="h-full rounded-full" style={{ width: `${(d.bytes / max) * 100}%`, background: DSCP_COLORS[i % DSCP_COLORS.length] }} />
              </div>
            </button>
          )
        })}
      </CardContent>
    </Card>
  )
}

function TcpFlagsCard({ data, activeFlag, onSelect }: { data?: TcpFlagSummary; activeFlag?: string | null; onSelect?: (flag: string) => void }) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">TCP Flags</CardTitle>
          <p className="text-[11px] text-muted">Loading…</p>
        </CardHeader>
        <CardContent><EmptyState compact /></CardContent>
      </Card>
    )
  }
  const total = Math.max(1, data.total_tcp)
  const synRatio = data.syn_only / total
  const ackRatio = data.ack_only / total
  // Heuristic: high SYN-only ratio suggests scan or SYN flood; high RST suggests probes.
  const scanWarn = synRatio > 0.4 && data.syn_only > 1000
  const tiles: { key: string; label: string; value: number; tone: string }[] = [
    { key: 'syn_only', label: 'SYN-only (scan/SYN-flood)', value: data.syn_only, tone: scanWarn ? 'border-rose-500/50 bg-rose-500/10 text-rose-300' : 'border-cyan-400/30 bg-cyan-500/10 text-cyan-300' },
    { key: 'ack_only', label: 'ACK-only (mid-session)', value: data.ack_only, tone: 'border-violet-400/30 bg-violet-500/10 text-violet-300' },
    { key: 'rst', label: 'RST', value: data.rst, tone: 'border-amber-400/30 bg-amber-500/10 text-amber-300' },
    { key: 'fin', label: 'FIN', value: data.fin, tone: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300' },
    { key: 'psh', label: 'PSH', value: data.psh, tone: 'border-indigo-400/30 bg-indigo-500/10 text-indigo-300' },
    { key: 'no_flags', label: 'No flags (anomaly)', value: data.no_flags, tone: data.no_flags > total * 0.05 ? 'border-rose-500/50 bg-rose-500/10 text-rose-300' : 'border-border bg-surface2/40 text-muted' },
  ]
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <ShieldAlert className={`h-4 w-4 ${scanWarn ? 'text-rose-400' : 'text-amber-400'}`} />
        <div>
          <CardTitle className="text-sm">TCP Flags</CardTitle>
          <p className="text-[11px] text-muted">{data.total_tcp.toLocaleString()} TCP flows · {(synRatio * 100).toFixed(1)}% SYN-only</p>
        </div>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-2">
        {tiles.map((t) => {
          const active = activeFlag === t.key
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => onSelect?.(t.key)}
              className={`rounded-md border p-2 text-left transition-transform hover:scale-[1.02] ${t.tone} ${active ? 'ring-2 ring-primary/60' : ''}`}
            >
              <div className="text-[10px] uppercase tracking-wider opacity-70">{t.label}</div>
              <div className="mt-0.5 text-sm font-semibold">{t.value.toLocaleString()}</div>
              <div className="text-[9px] opacity-70">{((t.value / total) * 100).toFixed(2)}%</div>
            </button>
          )
        })}
      </CardContent>
    </Card>
  )
}

const NETCLASS_COLORS: Record<string, string> = {
  'Private 10/8': '#22d3ee',
  'Private 172.16/12': '#34d399',
  'Private 192.168/16': '#facc15',
  'Public': '#fb7185',
  'Multicast': '#a78bfa',
  'Loopback': '#94a3b8',
  'Link-local': '#fb923c',
  'CGNAT': '#06b6d4',
}

function NetworkClassesCard({ data, activeClass, onSelect }: { data: NetClass[]; activeClass?: string | null; onSelect?: (name: string) => void }) {
  const total = Math.max(1, data.reduce((a, b) => a + b.bytes, 0))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <Network className="h-4 w-4 text-emerald-400" />
        <div>
          <CardTitle className="text-sm">Network Class Mix</CardTitle>
          <p className="text-[11px] text-muted">Private vs public traffic distribution</p>
        </div>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <EmptyState compact />
        ) : (
          <>
            {/* Stacked bar */}
            <div className="flex h-2 overflow-hidden rounded-full bg-surface">
              {data.map((d) => (
                <div
                  key={d.name}
                  title={`${d.name}: ${formatBytes(d.bytes)} (${((d.bytes / total) * 100).toFixed(1)}%)`}
                  style={{ width: `${(d.bytes / total) * 100}%`, background: NETCLASS_COLORS[d.name] || '#64748b' }}
                />
              ))}
            </div>
            <div className="mt-3 space-y-1 text-[11px]">
              {data.map((d) => {
                const active = activeClass === d.name
                return (
                  <button
                    key={d.name}
                    type="button"
                    onClick={() => onSelect?.(d.name)}
                    className={`flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-left transition-colors hover:bg-primary/5 ${active ? 'bg-primary/10 ring-1 ring-primary/40' : ''}`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-sm" style={{ background: NETCLASS_COLORS[d.name] || '#64748b' }} />
                      <span>{d.name}</span>
                    </span>
                    <span className="text-muted">{formatBytes(d.bytes)} · {((d.bytes / total) * 100).toFixed(1)}%</span>
                  </button>
                )
              })}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function TopInterfacesCard({
  interfaces,
  loading,
  activeIface,
  onSelect,
  showExporter,
  compact = false,
}: {
  interfaces: Interface[]
  loading: boolean
  activeIface: number | null
  onSelect: (val: number | null) => void
  showExporter: boolean
  compact?: boolean
}) {
  const max = Math.max(1, ...interfaces.map((i) => i.bytes))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div className="flex items-center gap-2">
          <Cable className="h-4 w-4 text-primary" />
          <div>
            <CardTitle>Top Interfaces</CardTitle>
            <p className="text-xs text-muted">
              {showExporter ? 'Highest-traffic interfaces across all exporters' : 'Highest-traffic interfaces on this device'} · click a row to filter
            </p>
          </div>
        </div>
        {loading && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
      </CardHeader>
      <CardContent>
        {interfaces.length === 0 ? (
          <EmptyState compact />
        ) : (
          <div className={compact ? 'flex flex-col gap-1.5' : 'grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-5'}>
            {interfaces.map((it, idx) => {
              const active = activeIface === it.ifindex
              const widthPct = Math.max(4, (it.bytes / max) * 100)
              if (compact) {
                // Single-row condensed list item: index | name | thin bar | bytes
                return (
                  <button
                    key={`${it.exporter_ip}-${it.ifindex}-${idx}`}
                    onClick={() => onSelect(active ? null : it.ifindex)}
                    className={`group relative flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors ${
                      active
                        ? 'border-primary/60 bg-primary/10 ring-1 ring-primary/40'
                        : 'border-border bg-surface2/40 hover:border-primary/40 hover:bg-primary/5'
                    }`}
                    title={it.if_alias || ''}
                  >
                    <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold ${active ? 'bg-primary text-black' : 'bg-primary/15 text-primary'}`}>
                      {idx + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-xs font-semibold">{it.if_name || `if ${it.ifindex}`}</span>
                        <span className="shrink-0 text-[10px] font-medium text-muted">{formatBytes(it.bytes)}</span>
                      </div>
                      <div className="mt-1 h-1 overflow-hidden rounded-full bg-surface">
                        <div className="h-full rounded-full bg-primary" style={{ width: `${widthPct}%` }} />
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-1 text-[9px] text-muted">
                        <span className="truncate font-mono">
                          {it.device_hostname || it.exporter_ip}
                          {it.if_alias ? ` · ${it.if_alias}` : ''}
                        </span>
                        <span className="shrink-0">
                          {it.if_speed && it.if_speed > 0 ? formatBitsPerSecond(it.if_speed) : `${it.flows.toLocaleString()} flows`}
                        </span>
                      </div>
                    </div>
                  </button>
                )
              }
              const inPct = it.bytes ? (it.in_bytes / it.bytes) * 100 : 0
              return (
                <button
                  key={`${it.exporter_ip}-${it.ifindex}-${idx}`}
                  onClick={() => onSelect(active ? null : it.ifindex)}
                  className={`group flex flex-col items-stretch rounded-md border p-3 text-left transition-colors ${
                    active
                      ? 'border-primary/60 bg-primary/10 ring-1 ring-primary/40'
                      : 'border-border bg-surface2/40 hover:border-primary/40 hover:bg-primary/5'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${active ? 'bg-primary text-black' : 'bg-primary/15 text-primary'}`}>
                        {idx + 1}
                      </span>
                      <span className="truncate text-sm font-semibold" title={it.if_alias || ''}>
                        {it.if_name || `if ${it.ifindex}`}
                      </span>
                    </div>
                    <span className="shrink-0 text-[11px] font-medium text-muted">{formatBytes(it.bytes)}</span>
                  </div>
                  {it.if_alias && (
                    <div className="mt-1 truncate text-[10px] text-muted" title={it.if_alias}>{it.if_alias}</div>
                  )}
                  {showExporter && (
                    <div className="mt-1 truncate font-mono text-[10px] text-muted">
                      {it.device_hostname ? `${it.device_hostname} · ` : ''}{it.exporter_ip} · ifIndex {it.ifindex}
                    </div>
                  )}
                  {it.if_speed && it.if_speed > 0 && (
                    <div className="mt-1 flex items-center justify-between text-[10px]">
                      <span className="text-muted">Capacity</span>
                      <span className="text-muted">{formatBitsPerSecond(it.if_speed)}</span>
                    </div>
                  )}
                  <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface">
                    <div className="h-full rounded-full bg-primary" style={{ width: `${widthPct}%` }} />
                  </div>
                  <div className="mt-2 flex items-center justify-between text-[10px] text-muted">
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-cyan-400" />
                      In {formatBytes(it.in_bytes)} ({inPct.toFixed(0)}%)
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-violet-400" />
                      Out {formatBytes(it.out_bytes)}
                    </span>
                  </div>
                  <div className="mt-1 text-[10px] text-muted">
                    {it.flows.toLocaleString()} flows · {it.packets.toLocaleString()} pkts
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function FilterChip({ icon: Icon, label, value, onClear, mono }: { icon?: any; label: string; value: string; onClear: () => void; mono?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
      {Icon && <Icon className="h-3 w-3" />}
      <span className="text-muted">{label}:</span>
      <span className={mono ? 'font-mono' : ''}>{value}</span>
      <button onClick={onClear} className="ml-0.5 rounded-full p-0.5 hover:bg-primary/20" aria-label={`Clear ${label} filter`}>
        <X className="h-3 w-3" />
      </button>
    </span>
  )
}

function ThroughputDisplay({ bps }: { bps: number }) {
  // Split formatted "1.23 Gbps" into number + unit so the number can be huge.
  const formatted = formatBps(bps)
  const m = formatted.match(/^([\d.,]+)\s*(.*)$/)
  const num = m?.[1] || formatted
  const unit = m?.[2] || ''
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-4xl font-semibold leading-none tracking-tight text-text">{num}</span>
      {unit && <span className="text-base font-medium text-muted">{unit}</span>}
    </div>
  )
}

function EmptyState({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`flex ${compact ? 'min-h-32' : 'h-full'} items-center justify-center rounded-md border border-dashed border-border bg-surface2/30 text-center text-xs text-muted`}>
      No NetFlow records collected for this range.
    </div>
  )
}

// Re-export for explicit imports elsewhere if needed.
export { Router, Server }
