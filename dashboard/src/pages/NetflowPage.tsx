import { Fragment, useEffect, useMemo, useState } from 'react'
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
import { apiErrorMessage, formatBps, formatBytes, relativeTime, timeAxisTickFormatter, timeTooltipLabelFormatter } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { TIME_RANGE_OPTIONS, TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'

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
type Endpoint = { ip: string; bytes: number; packets: number; flows: number; src_bytes: number; dst_bytes: number; src_flows: number; dst_flows: number }
type NetflowInterfaceRef = {
  exporter_ip: string
  ifindex: number
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  if_speed?: number | null
  device_hostname?: string | null
  display_name?: string | null
}
type Conversation = {
  src: string
  dst: string
  protocol: number
  protocol_name: string
  dst_port: number
  service: string
  application?: string
  port_class?: string
  src_ports?: number[]
  bytes: number
  packets: number
  flows: number
  exporters?: { ip: string; hostname?: string | null }[]
  input_snmp?: number[]
  output_snmp?: number[]
  input_interfaces?: NetflowInterfaceRef[]
  output_interfaces?: NetflowInterfaceRef[]
  first_seen?: string | null
  last_seen?: string | null
  received_at?: string | null
  avg_duration_ms?: number
  tcp_flags?: number
  avg_bytes?: number
  avg_packets?: number
}
type Protocol = { protocol: number; name: string; bytes: number; packets: number; flows: number }
type Application = { name: string; bytes: number; packets: number; flows: number }
type Exporter = { exporter_ip: string; bytes: number; packets: number; flows: number; last_seen: string }
type DeviceStatus = { latency_ms: number; packet_loss_pct: number; uptime_pct: number; flows: number; packets: number; exporters: number; last_seen: string | null }
type Heatmap = { max_bytes: number; cells: { dow: number; hour: number; bytes: number; flows: number }[] }
type Interface = {
  exporter_ip: string
  ifindex: number
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  if_speed?: number | null
  device_hostname?: string | null
  display_name?: string | null
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

type Country = { country: string | null; country_name: string | null; bytes: number; packets: number; flows: number }
type DscpClass = { dscp: number; label: string; bytes: number; packets: number; flows: number }
type TcpFlagSummary = { total_tcp: number; syn_only: number; ack_only: number; rst: number; fin: number; psh: number; urg: number; no_flags: number }
type NetClass = { name: string; bytes: number; packets: number; flows: number }

// The traffic heatmap is always a 7-day hour×day grid. Any "When" (hour/dow)
// filter picked from it therefore needs a page range at least this wide to
// match anything — see the onSelect handler and the range guard below.
const HEATMAP_HOURS = 168

const TALKER_COLORS = ['#22d3ee', '#a78bfa', '#f472b6', '#34d399', '#facc15', '#fb923c']
const APP_COLORS = ['#22d3ee', '#34d399', '#facc15', '#fb7185', '#a78bfa', '#f59e0b', '#10b981', '#ef4444']
const PROTO_COLORS = ['#22d3ee', '#a78bfa', '#facc15', '#fb7185', '#34d399', '#fb923c']
const CONV_COLORS = ['#22d3ee', '#34d399', '#facc15', '#a78bfa', '#fb7185', '#fb923c']

export function NetflowDevicePage() {
  const { ip = '' } = useParams<{ ip: string }>()
  return <NetflowPage exporter={ip} />
}

const NETFLOW_SECTION_META: Record<string, { title: string; description: string; endpoint: string; icon: React.ComponentType<{ className?: string }> }> = {
  'top-talkers': {
    title: 'Top Talkers',
    description: 'Highest-volume IP addresses by total bytes in the selected window.',
    endpoint: 'top-talkers',
    icon: RadioTower,
  },
  conversations: {
    title: 'Conversations',
    description: 'Highest-volume source to destination pairs with protocol and service context.',
    endpoint: 'top-conversations',
    icon: ArrowDownUp,
  },
  'top-endpoints': {
    title: 'Top Endpoints',
    description: 'Endpoint IPs ranked by total source and destination traffic contribution.',
    endpoint: 'top-endpoints',
    icon: Smartphone,
  },
  applications: {
    title: 'Top Applications',
    description: 'Application buckets inferred from destination service ports.',
    endpoint: 'applications',
    icon: Layers,
  },
  protocols: {
    title: 'Protocol Distribution',
    description: 'Traffic split by IP protocol.',
    endpoint: 'protocols',
    icon: Database,
  },
}

export function NetflowSectionPage() {
  const { section = 'top-talkers' } = useParams<{ section: string }>()
  const meta = NETFLOW_SECTION_META[section] || NETFLOW_SECTION_META['top-talkers']
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [searchParams] = useSearchParams()

  const qs = useMemo(() => {
    const params = new URLSearchParams({ hours: String(range.hours), limit: '50' })
    if (isCustom) {
      params.set('from', range.fromISO)
      params.set('to', range.toISO)
    }
    for (const key of ['exporter', 'iface', 'talker', 'protocol', 'dscp', 'app', 'netclass', 'tcpflag', 'hour', 'dow']) {
      const value = searchParams.get(key)
      if (value) params.set(key, value)
    }
    return params.toString()
  }, [range.hours, range.fromISO, range.toISO, isCustom, searchParams])

  const { data = [], isLoading, error } = useQuery<any[]>({
    queryKey: ['netflow', 'section', section, qs],
    queryFn: async () => (await api.get(`/netflow/${meta.endpoint}?${qs}`)).data,
    refetchInterval: isCustom ? false : pollMs(range.hours),
  })

  const Icon = meta.icon

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
            <Link to="/netflow" className="inline-flex items-center gap-1 hover:text-text">
              <ArrowLeft className="h-3 w-3" />
              NetFlow
            </Link>
            <ChevronRight className="h-3 w-3" />
            <span>{meta.title}</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Icon className="h-5 w-5 text-primary" />
            {meta.title}
          </h1>
          <p className="text-xs text-muted">{meta.description}</p>
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

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>{meta.title}</CardTitle>
            <p className="text-xs text-muted">{range.label} · {data.length.toLocaleString()} rows</p>
          </div>
          {isLoading && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
        </CardHeader>
        <CardContent>
          {error ? (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
              Failed to load {meta.title.toLowerCase()}: {apiErrorMessage(error)}
            </div>
          ) : (
            <NetflowSectionTable section={section} rows={data} searchParams={searchParams} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// Auto-refresh cadence scaled to the window: multi-day windows run multi-second
// ClickHouse scans — re-polling those every 15s just burns the database.
function pollMs(hours: number, floor = 15_000): number | false {
  const ms = hours <= 6 ? 15_000 : hours <= 48 ? 60_000 : 300_000
  return Math.max(ms, floor)
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

  // A shared link like ?hour=19&dow=7 carries no range, so it would fall back
  // to the 1h default and render an empty page: a weekday+hour filter only
  // matches over a multi-day window. Give those links the heatmap's own 7-day
  // window. An explicit ?range= is always respected, so this never fights a
  // range the user chose.
  useEffect(() => {
    if ((filterHour || filterDow) && !searchParams.get('range')) {
      const next = new URLSearchParams(searchParams)
      next.set('range', '7d')
      setSearchParams(next, { replace: true })
    }
  }, [filterHour, filterDow, searchParams, setSearchParams])

  // A "When" filter that the current range cannot possibly satisfy: surfaced
  // in the UI instead of silently rendering zeros everywhere.
  const whenFilterOutOfRange = !!(filterHour || filterDow) && !isCustom && hours < HEATMAP_HOURS
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
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const timeseries = useQuery<SeriesPoint[]>({
    queryKey: ['netflow', 'timeseries', rangeKey],
    queryFn: async () => (await api.get(`/netflow/timeseries?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const timeseriesPrior = useQuery<SeriesPoint[]>({
    queryKey: ['netflow', 'timeseries-prior', priorQS],
    queryFn: async () => (await api.get(`/netflow/timeseries?${priorQS}`)).data,
    enabled: compareEnabled,
    refetchInterval: false,
  })
  const talkers = useQuery<Talker[]>({
    queryKey: ['netflow', 'talkers', rangeKey],
    queryFn: async () => (await api.get(`/netflow/top-talkers?${rangeQS}&limit=6&by=src`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const endpoints = useQuery<Endpoint[]>({
    queryKey: ['netflow', 'endpoints', rangeKey],
    queryFn: async () => (await api.get(`/netflow/top-endpoints?${rangeQS}&limit=6`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const conversations = useQuery<Conversation[]>({
    queryKey: ['netflow', 'conversations', rangeKey],
    queryFn: async () => (await api.get(`/netflow/top-conversations?${rangeQS}&limit=10`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const protocols = useQuery<Protocol[]>({
    queryKey: ['netflow', 'protocols', rangeKey],
    queryFn: async () => (await api.get(`/netflow/protocols?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const applications = useQuery<Application[]>({
    queryKey: ['netflow', 'applications', rangeKey],
    queryFn: async () => (await api.get(`/netflow/applications?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const exporters = useQuery<Exporter[]>({
    queryKey: ['netflow', 'exporters', rangeKey],
    queryFn: async () => (await api.get(`/netflow/exporters?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours),
  })
  const deviceStatus = useQuery<DeviceStatus>({
    queryKey: ['netflow', 'device-status', rangeKey],
    queryFn: async () => (await api.get(`/netflow/device-status?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 30_000),
  })
  // The heatmap is a 7-day hour×day picker — pin it to a week (or the custom
  // window) instead of the page range, otherwise a 1h range leaves it empty.
  const heatmapQS = useMemo(() => {
    const params = new URLSearchParams(rangeQS)
    if (!isCustom) params.set('hours', String(HEATMAP_HOURS))
    return params.toString()
  }, [rangeQS, isCustom])
  const heatmap = useQuery<Heatmap>({
    queryKey: ['netflow', 'heatmap', `${rangeKey}|7d`],
    queryFn: async () => (await api.get(`/netflow/heatmap?${heatmapQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 60_000),
  })
  const interfaces = useQuery<Interface[]>({
    queryKey: ['netflow', 'interfaces', `${isCustom ? `c:${range.fromISO}|${range.toISO}` : `p:${hours}h`}|${exporter || 'all'}`],
    queryFn: async () => (await api.get(`/netflow/interfaces?${interfacesQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 60_000),
  })
  const dscp = useQuery<DscpClass[]>({
    queryKey: ['netflow', 'dscp', rangeKey],
    queryFn: async () => (await api.get(`/netflow/dscp?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 30_000),
  })
  const tcpFlags = useQuery<TcpFlagSummary>({
    queryKey: ['netflow', 'tcp-flags', rangeKey],
    queryFn: async () => (await api.get(`/netflow/tcp-flags?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 30_000),
  })
  const netClasses = useQuery<NetClass[]>({
    queryKey: ['netflow', 'network-classes', rangeKey],
    queryFn: async () => (await api.get(`/netflow/network-classes?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 60_000),
  })
  const countries = useQuery<Country[]>({
    queryKey: ['netflow', 'countries', rangeKey],
    queryFn: async () => (await api.get(`/netflow/countries?${rangeQS}`)).data,
    refetchInterval: isCustom ? false : pollMs(hours, 60_000),
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
  const endpointData = endpoints.data || []
  const exporterData = exporters.data || []

  const sectionLink = (sectionName: string) => {
    const params = new URLSearchParams(searchParams)
    if (!params.get('range')) params.set('range', '1h')
    if (exporter) params.set('exporter', exporter)
    return `/netflow/${sectionName}?${params.toString()}`
  }

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
  const activeIfaceLabel = useMemo(() => {
    if (!isIfaceFiltered) return null
    const match = (interfaces.data || []).find((it) => it.ifindex === iface)
    return match ? interfaceLabel(match) : `ifIndex ${iface}`
  }, [interfaces.data, iface, isIfaceFiltered])

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
                <FilterChip icon={Cable} label="Interface" value={activeIfaceLabel || String(iface)} onClear={() => setIfaceFilter(null)} />
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
          {whenFilterOutOfRange && (
            <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] text-warning">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              <span>
                The <b>When</b> filter picks one hour of one weekday, which the <b>{range.label}</b> range cannot
                contain — that is why the panels below are empty.
              </span>
              <button
                onClick={() => setPreset(TIME_RANGE_OPTIONS.findIndex((r) => r.key === '7d'))}
                className="rounded-full border border-warning/40 px-2 py-0.5 font-medium hover:bg-warning/20"
              >
                Switch to 7d
              </button>
              <button
                onClick={() => setParams([['hour', null], ['dow', null]])}
                className="rounded-full border border-warning/40 px-2 py-0.5 font-medium hover:bg-warning/20"
              >
                Clear When
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
              placeholder="Filter flow records (IP, service, protocol)…"
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
          label="Flows in Window"
          value={(overview.data?.flows || 0).toLocaleString()}
          sub={`${formatBps(overview.data?.current_bps || 0)} current rate`}
        />
        <KpiCard
          icon={RadioTower}
          tone="amber"
          label="Exporters"
          value={(overview.data?.exporters || 0).toLocaleString()}
          sub={<Badge variant={health.variant}>{health.label}</Badge>}
        />
        <KpiCard
          icon={Layers}
          tone="emerald"
          label="Applications"
          value={applicationData.length.toLocaleString()}
          sub={applicationData[0] ? `top: ${applicationData[0].name}` : '—'}
        />
        <KpiCard
          icon={Smartphone}
          tone="pink"
          label="Unique Hosts"
          value={`${(overview.data?.src_hosts || 0).toLocaleString()} / ${(overview.data?.dst_hosts || 0).toLocaleString()}`}
          sub="sources / destinations"
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
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-cyan-400" />Throughput</span>
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
                    </defs>
                    <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.4} vertical={false} />
                    <XAxis dataKey="ts" tickFormatter={timeAxisTickFormatter(hours)} tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                    <YAxis tickFormatter={formatBps} tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} width={72} />
                    <Tooltip
                      labelFormatter={timeTooltipLabelFormatter}
                      formatter={(value: any, name: string) => [formatBps(Number(value)), name === 'prior_bps' ? 'Prior' : 'Throughput']}
                      contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8 }}
                    />
                    {compareEnabled && (
                      <Area type="monotone" dataKey="prior_bps" stroke="#fbbf24" strokeWidth={1.5} strokeDasharray="4 3" fill="none" connectNulls />
                    )}
                    <Area type="monotone" dataKey="bps" stroke="#22d3ee" strokeWidth={2} fill="url(#netflowIn)" />
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
            const entries: [string, string | null][] = [
              ['hour', sameCell ? null : String(hour)],
              ['dow', sameCell ? null : String(dow)],
            ]
            // The heatmap always covers 7 days, but the page range does not.
            // Picking "Sun 19:00" while the page is on 1h asks for flows that
            // are both in the last hour and on a Sunday evening — empty unless
            // it happens to be Sunday 19:xx right now. Widen to the window the
            // user is actually looking at; a custom range is left alone.
            if (!sameCell && !isCustom && hours < HEATMAP_HOURS) entries.push(['range', '7d'])
            setParams(entries)
          }}
        />
        <DeviceStatusCard status={deviceStatus.data} exporters={exporterData} deviceView={isDeviceView} />
      </div>

      {/* Donuts row */}
      <div className="grid grid-cols-1 items-stretch gap-4 md:grid-cols-2 xl:grid-cols-5">
        <DonutCard
          title="Top Talkers"
          subtitle="By bytes sent — click to filter"
          data={talkerData.map((t: any) => ({ name: t.ip, value: t.src_bytes ?? t.bytes }))}
          colors={TALKER_COLORS}
          labelMode="ip"
          activeName={filterTalker || null}
          onSelect={(name) => setParam('talker', filterTalker === name ? null : name)}
          viewAllTo={sectionLink('top-talkers')}
        />
        <DonutCard
          title="Top Endpoints"
          subtitle="Src + dst contribution"
          data={endpointData.map((e) => ({ name: e.ip, value: e.bytes }))}
          colors={TALKER_COLORS}
          labelMode="ip"
          activeName={filterTalker || null}
          onSelect={(name) => setParam('talker', filterTalker === name ? null : name)}
          viewAllTo={sectionLink('top-endpoints')}
        />
        <DonutCard
          title="Top Applications"
          subtitle="By bytes — click to filter"
          data={applicationData.slice(0, 6).map((a) => ({ name: a.name, value: a.bytes }))}
          colors={APP_COLORS}
          activeName={filterApp || null}
          onSelect={(name) => setParam('app', filterApp === name ? null : name)}
          viewAllTo={sectionLink('applications')}
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
          viewAllTo={sectionLink('protocols')}
        />
        <DonutCard
          title="Top Conversations"
          subtitle="src → dst pairs"
          data={conversationData.slice(0, 6).map((c) => ({ name: `${c.src} → ${c.dst}`, value: c.bytes }))}
          colors={CONV_COLORS}
          compactNames
          viewAllTo={sectionLink('conversations')}
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
          <CountriesCard data={countries.data || []} />
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

function NetflowSectionTable({ section, rows, searchParams }: { section: string; rows: any[]; searchParams: URLSearchParams }) {
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const withParams = (path: string, updates: Record<string, string | null>) => {
    const params = new URLSearchParams(searchParams)
    if (!params.get('range')) params.set('range', '1h')
    for (const [key, value] of Object.entries(updates)) {
      if (value == null) params.delete(key)
      else params.set(key, value)
    }
    return `${path}?${params.toString()}`
  }

  if (rows.length === 0) {
    return <EmptyState />
  }

  if (section === 'conversations') {
    return (
      <Table>
        <THead>
          <Tr>
            <Th>Source</Th>
            <Th>Destination</Th>
            <Th>Application</Th>
            <Th>Protocol</Th>
            <Th className="text-right">Bytes</Th>
            <Th className="text-right">Packets</Th>
            <Th className="text-right">Flows</Th>
            <Th></Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const rowId = `${row.src}-${row.dst}-${row.protocol}-${row.dst_port}`
            const expanded = expandedRow === rowId
            return (
              <Fragment key={rowId}>
                <Tr className={expanded ? 'bg-primary/5' : undefined}>
                  <Td className="font-mono text-xs">{row.src}</Td>
                  <Td className="font-mono text-xs">{row.dst}</Td>
                  <Td>{row.service}</Td>
                  <Td><Badge variant="outline">{row.protocol_name}</Badge></Td>
                  <Td className="text-right">{formatBytes(row.bytes)}</Td>
                  <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                  <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                  <Td className="text-right">
                    <button
                      type="button"
                      className="text-xs font-medium text-primary hover:underline"
                      onClick={() => setExpandedRow(expanded ? null : rowId)}
                    >
                      {expanded ? 'Hide' : 'Details'}
                    </button>
                  </Td>
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={8} className="bg-surface2/20 p-4">
                      <ConversationDetailPanel
                        row={row}
                        forensicsTo={withParams('/netflow/forensics', { src: row.src, dst: row.dst, proto: String(row.protocol), dst_port: String(row.dst_port) })}
                      />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    )
  }

  if (section === 'applications') {
    return (
      <Table>
        <THead>
          <Tr>
            <Th>Application</Th>
            <Th className="text-right">Bytes</Th>
            <Th className="text-right">Packets</Th>
            <Th className="text-right">Flows</Th>
            <Th></Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const rowId = `app-${row.name}`
            const expanded = expandedRow === rowId
            return (
              <Fragment key={rowId}>
                <Tr className={expanded ? 'bg-primary/5' : undefined}>
                  <Td className="font-medium">{row.name}</Td>
                  <Td className="text-right">{formatBytes(row.bytes)}</Td>
                  <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                  <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                  <Td className="text-right">
                    <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => setExpandedRow(expanded ? null : rowId)}>
                      {expanded ? 'Hide' : 'Details'}
                    </button>
                  </Td>
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={5} className="bg-surface2/20 p-4">
                      <ApplicationDetailPanel row={row} filterTo={withParams('/netflow', { app: row.name })} />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    )
  }

  if (section === 'protocols') {
    return (
      <Table>
        <THead>
          <Tr>
            <Th>Protocol</Th>
            <Th>Number</Th>
            <Th className="text-right">Bytes</Th>
            <Th className="text-right">Packets</Th>
            <Th className="text-right">Flows</Th>
            <Th></Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const rowId = `proto-${row.protocol}`
            const expanded = expandedRow === rowId
            return (
              <Fragment key={rowId}>
                <Tr className={expanded ? 'bg-primary/5' : undefined}>
                  <Td className="font-medium">{row.name}</Td>
                  <Td>{row.protocol}</Td>
                  <Td className="text-right">{formatBytes(row.bytes)}</Td>
                  <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                  <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                  <Td className="text-right">
                    <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => setExpandedRow(expanded ? null : rowId)}>
                      {expanded ? 'Hide' : 'Details'}
                    </button>
                  </Td>
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={6} className="bg-surface2/20 p-4">
                      <ProtocolDetailPanel row={row} filterTo={withParams('/netflow', { protocol: String(row.protocol) })} />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    )
  }

  const isEndpoints = section === 'top-endpoints'
  return (
    <Table>
      <THead>
        <Tr>
          <Th>IP Address</Th>
          <Th className="text-right">Bytes</Th>
          <Th className="text-right">Packets</Th>
          <Th className="text-right">Flows</Th>
          {isEndpoints && <Th className="text-right">As Source</Th>}
          {isEndpoints && <Th className="text-right">As Destination</Th>}
          <Th></Th>
        </Tr>
      </THead>
      <TBody>
        {rows.map((row) => {
          const rowId = `ip-${row.ip}`
          const expanded = expandedRow === rowId
          const colSpan = isEndpoints ? 7 : 5
          return (
            <Fragment key={rowId}>
              <Tr className={expanded ? 'bg-primary/5' : undefined}>
                <Td className="font-mono text-xs">{row.ip}</Td>
                <Td className="text-right">{formatBytes(row.bytes)}</Td>
                <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                {isEndpoints && <Td className="text-right">{formatBytes(row.src_bytes || 0)} · {Number(row.src_flows || 0).toLocaleString()}</Td>}
                {isEndpoints && <Td className="text-right">{formatBytes(row.dst_bytes || 0)} · {Number(row.dst_flows || 0).toLocaleString()}</Td>}
                <Td className="text-right">
                  <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => setExpandedRow(expanded ? null : rowId)}>
                    {expanded ? 'Hide' : 'Details'}
                  </button>
                </Td>
              </Tr>
              {expanded && (
                <Tr className="hover:bg-transparent">
                  <Td colSpan={colSpan} className="bg-surface2/20 p-4">
                    <IpTrafficDetailPanel
                      row={row}
                      mode={isEndpoints ? 'endpoint' : 'talker'}
                      filterTo={withParams('/netflow', { talker: row.ip })}
                      forensicsTo={withParams('/netflow/forensics', { talker: row.ip })}
                    />
                  </Td>
                </Tr>
              )}
            </Fragment>
          )
        })}
      </TBody>
    </Table>
  )
}

function IpTrafficDetailPanel({ row, mode, filterTo, forensicsTo }: { row: any; mode: 'talker' | 'endpoint'; filterTo: string; forensicsTo: string }) {
  const srcBytes = Number(row.src_bytes || 0)
  const dstBytes = Number(row.dst_bytes || 0)
  const totalBytes = Math.max(1, Number(row.bytes || 0))
  const avgBytes = row.flows ? Number(row.bytes || 0) / Number(row.flows) : 0
  const firstSeen = row.first_seen ? new Date(row.first_seen).toLocaleString() : '—'
  const lastSeen = row.last_seen ? new Date(row.last_seen).toLocaleString() : '—'

  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-text">{row.ip}</span>
            <Badge variant="outline">{mode === 'endpoint' ? 'Endpoint' : 'Talker'}</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">first seen {firstSeen} · last seen {lastSeen}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link to={filterTo} className="inline-flex items-center justify-center rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted hover:text-text">
            Filter dashboard
          </Link>
          <Link to={forensicsTo} className="inline-flex items-center justify-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15">
            Open raw flows
            <ChevronRight className="h-3 w-3" />
          </Link>
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <DetailMetric label="Total Traffic" value={formatBytes(row.bytes || 0)} sub={`${Number(row.flows || 0).toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={Number(row.packets || 0).toLocaleString()} sub={`${avgBytes ? formatBytes(avgBytes) : '0 B'} avg / flow`} />
        <DetailMetric label="As Source" value={formatBytes(srcBytes)} sub={`${Number(row.src_flows || 0).toLocaleString()} source flows`} />
        <DetailMetric label="As Destination" value={formatBytes(dstBytes)} sub={`${Number(row.dst_flows || 0).toLocaleString()} destination flows`} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(260px,0.8fr)]">
        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center justify-between text-[11px]">
            <span className="font-semibold uppercase tracking-wider text-muted">Direction Split</span>
            <span className="text-muted">{formatBytes(totalBytes)} total</span>
          </div>
          <TrafficShapeBar label="Source bytes" value={srcBytes} max={totalBytes} display={`${((srcBytes / totalBytes) * 100).toFixed(0)}%`} color="bg-cyan-400" />
          <div className="mt-2">
            <TrafficShapeBar label="Destination bytes" value={dstBytes} max={totalBytes} display={`${((dstBytes / totalBytes) * 100).toFixed(0)}%`} color="bg-violet-400" />
          </div>
        </div>
        <DetailListPanel title="Observed Context">
          <PillList label="Protocols" values={(row.protocols || []).map((p: any) => p.name || `Protocol ${p.protocol}`)} empty="No protocol detail" />
          <PillList label="Ports" values={(row.ports || []).map((p: any) => p.service || String(p.port))} empty="No destination ports" />
          <PillList label="Exporters" values={(row.exporters || []).map((e: any) => e.hostname || e.ip)} empty="No exporter detail" mono />
        </DetailListPanel>
      </div>
    </div>
  )
}

function ApplicationDetailPanel({ row, filterTo }: { row: any; filterTo: string }) {
  const avgBytes = row.flows ? Number(row.bytes || 0) / Number(row.flows) : 0
  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Layers className="h-4 w-4 text-primary" />
            <span className="text-sm font-semibold text-text">{row.name}</span>
            <Badge variant="outline">Application bucket</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">Application is inferred from destination service ports.</div>
        </div>
        <Link to={filterTo} className="inline-flex shrink-0 items-center justify-center rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15">
          Filter dashboard
        </Link>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <DetailMetric label="Traffic" value={formatBytes(row.bytes || 0)} sub={`${Number(row.flows || 0).toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={Number(row.packets || 0).toLocaleString()} sub={`${avgBytes ? formatBytes(avgBytes) : '0 B'} avg / flow`} />
        <DetailMetric label="Services" value={String((row.ports || []).length)} sub="top destination ports" />
      </div>
      <div className="mt-4">
        <PortBreakdown ports={row.ports || []} totalBytes={Number(row.bytes || 0)} />
      </div>
    </div>
  )
}

function ProtocolDetailPanel({ row, filterTo }: { row: any; filterTo: string }) {
  const avgPackets = row.flows ? Number(row.packets || 0) / Number(row.flows) : 0
  const firstSeen = row.first_seen ? new Date(row.first_seen).toLocaleString() : '—'
  const lastSeen = row.last_seen ? new Date(row.last_seen).toLocaleString() : '—'
  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Network className="h-4 w-4 text-primary" />
            <span className="text-sm font-semibold text-text">{row.name}</span>
            <Badge variant="outline">IP protocol {row.protocol}</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">first seen {firstSeen} · last seen {lastSeen}</div>
        </div>
        <Link to={filterTo} className="inline-flex shrink-0 items-center justify-center rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15">
          Filter dashboard
        </Link>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <DetailMetric label="Traffic" value={formatBytes(row.bytes || 0)} sub={`${Number(row.flows || 0).toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={Number(row.packets || 0).toLocaleString()} sub={`${avgPackets.toFixed(1)} avg / flow`} />
        <DetailMetric label="Ports Seen" value={String((row.ports || []).length)} sub={row.protocol === 6 || row.protocol === 17 ? 'destination services' : 'non TCP/UDP may have none'} />
      </div>
      <div className="mt-4">
        <PortBreakdown ports={row.ports || []} totalBytes={Number(row.bytes || 0)} />
      </div>
    </div>
  )
}

function ConversationDetailPanel({ row, forensicsTo }: { row: Conversation; forensicsTo: string }) {
  const exporters = row.exporters || []
  const firstSeen = row.first_seen ? new Date(row.first_seen).toLocaleString() : '—'
  const lastSeen = row.last_seen ? new Date(row.last_seen).toLocaleString() : '—'
  const avgBytes = row.avg_bytes || (row.flows ? row.bytes / row.flows : 0)
  const avgPackets = row.avg_packets || (row.flows ? row.packets / row.flows : 0)
  const tcpFlags = describeTcpFlags(row.tcp_flags || 0)
  const application = row.application || inferApplication(row.dst_port)
  const portClass = row.port_class || describePortClass(row.dst_port)
  const sourcePorts = (row.src_ports || []).slice(0, 10)
  const inputIfs = normalizeInterfaceRefs(row.input_interfaces, row.input_snmp).slice(0, 8)
  const outputIfs = normalizeInterfaceRefs(row.output_interfaces, row.output_snmp).slice(0, 8)
  const durationMs = Math.max(0, row.avg_duration_ms || 0)

  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-text">{row.src}</span>
            <ArrowDownUp className="h-4 w-4 text-primary" />
            <span className="font-mono text-sm font-semibold text-text">{row.dst}</span>
            <Badge variant="outline">{row.protocol_name}</Badge>
            <Badge variant="outline">{row.service}</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">
            Port {row.dst_port} · first seen {firstSeen} · last seen {lastSeen}
          </div>
        </div>
        <Link
          to={forensicsTo}
          className="inline-flex shrink-0 items-center justify-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15"
        >
          Open raw flows
          <ChevronRight className="h-3 w-3" />
        </Link>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <DetailMetric label="Total Traffic" value={formatBytes(row.bytes)} sub={`${row.flows.toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={row.packets.toLocaleString()} sub={`${avgPackets.toFixed(1)} avg / flow`} />
        <DetailMetric label="Avg Flow Size" value={formatBytes(avgBytes)} sub={`${durationMs.toFixed(durationMs >= 100 ? 0 : 1)} ms avg duration`} />
        <DetailMetric label="TCP Flags" value={tcpFlags.primary} sub={tcpFlags.detail} />
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
            <Layers className="h-3.5 w-3.5 text-primary" />
            Application
          </div>
          <div className="text-base font-semibold text-text">{application}</div>
          <div className="mt-1 text-[11px] text-muted">
            {application === 'Other'
              ? 'No known application bucket matched this destination port.'
              : 'Inferred from the destination service port.'}
          </div>
        </div>

        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
            <Cable className="h-3.5 w-3.5 text-primary" />
            Ports Used
          </div>
          <div className="grid grid-cols-2 gap-2">
            <PortBadge label="Source" value={sourcePorts.length ? sourcePorts.join(', ') : 'Not reported'} />
            <PortBadge label="Destination" value={`${row.dst_port}`} />
          </div>
          <div className="mt-2 text-[11px] text-muted">{portClass}</div>
        </div>

        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
            <Network className="h-3.5 w-3.5 text-primary" />
            Protocol / Service
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Badge variant="outline">{row.protocol_name}</Badge>
            <Badge variant="outline">{row.service}</Badge>
            <Badge variant="outline">IP proto {row.protocol}</Badge>
          </div>
          <div className="mt-2 text-[11px] text-muted">
            {row.protocol_name === 'TCP' || row.protocol_name === 'UDP'
              ? `${row.protocol_name} conversation to destination port ${row.dst_port}.`
              : `${row.protocol_name} traffic does not normally expose TCP/UDP application ports.`}
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.8fr)]">
        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center justify-between text-[11px]">
            <span className="font-semibold uppercase tracking-wider text-muted">Traffic Shape</span>
            <span className="text-muted">{formatBytes(row.bytes)} total</span>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <TrafficShapeBar label="Bytes / flow" value={avgBytes} max={Math.max(avgBytes, row.bytes / Math.max(1, row.flows))} display={formatBytes(avgBytes)} color="bg-cyan-400" />
            <TrafficShapeBar label="Packets / flow" value={avgPackets} max={Math.max(1, avgPackets)} display={avgPackets.toFixed(1)} color="bg-violet-400" />
            <TrafficShapeBar label="Duration" value={durationMs} max={Math.max(1, durationMs)} display={`${durationMs.toFixed(durationMs >= 100 ? 0 : 1)} ms`} color="bg-emerald-400" />
          </div>
        </div>

        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Exporting Device</div>
          {exporters.length === 0 ? (
            <div className="text-xs text-muted">Exporter not reported for this group.</div>
          ) : (
            <div className="space-y-1.5">
              {exporters.map((exporter) => (
                <Link
                  key={exporter.ip}
                  to={`/netflow/devices/${encodeURIComponent(exporter.ip)}`}
                  className="flex items-center gap-2 rounded border border-border/70 bg-surface px-2 py-1.5 hover:border-primary/50 hover:bg-primary/5"
                >
                  <Router className="h-3.5 w-3.5 text-primary" />
                  <span className="min-w-0 flex-1 truncate text-xs font-medium">{exporter.hostname || exporter.ip}</span>
                  <span className="font-mono text-[10px] text-muted">{exporter.ip}</span>
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <InterfaceChipGroup title="Input Interfaces" values={inputIfs} empty="No input interface reported" />
        <InterfaceChipGroup title="Output Interfaces" values={outputIfs} empty="No output interface reported" />
      </div>
    </div>
  )
}

function DetailMetric({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums text-text">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted">{sub}</div>
    </div>
  )
}

function DetailListPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</div>
      <div className="space-y-3">{children}</div>
    </div>
  )
}

function PillList({ label, values, empty, mono = false }: { label: string; values: string[]; empty: string; mono?: boolean }) {
  const clean = Array.from(new Set(values.filter(Boolean))).slice(0, 8)
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-muted">{label}</div>
      {clean.length ? (
        <div className="flex flex-wrap gap-1.5">
          {clean.map((value) => (
            <span key={value} className={`rounded border border-border bg-surface px-2 py-1 text-[11px] text-text ${mono ? 'font-mono' : ''}`}>
              {value}
            </span>
          ))}
        </div>
      ) : (
        <div className="text-xs text-muted">{empty}</div>
      )}
    </div>
  )
}

function PortBreakdown({ ports, totalBytes }: { ports: any[]; totalBytes: number }) {
  const clean = ports.slice(0, 8)
  if (!clean.length) {
    return <div className="rounded-md border border-dashed border-border bg-surface2/30 p-5 text-center text-xs text-muted">No TCP/UDP destination port detail for this item.</div>
  }
  const max = Math.max(1, ...clean.map((p) => Number(p.bytes || 0)), totalBytes || 0)
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="mb-3 flex items-center justify-between text-[11px]">
        <span className="font-semibold uppercase tracking-wider text-muted">Top Services / Ports</span>
        <span className="text-muted">{formatBytes(totalBytes || clean.reduce((sum, p) => sum + Number(p.bytes || 0), 0))} total</span>
      </div>
      <div className="space-y-2">
        {clean.map((port) => {
          const bytes = Number(port.bytes || 0)
          return (
            <div key={`${port.port}-${port.service}`}>
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="flex min-w-0 items-center gap-1.5">
                  <Badge variant="outline">{port.service || `Port ${port.port}`}</Badge>
                  <span className="truncate text-muted">{port.application || describePortClass(Number(port.port || 0))}</span>
                </span>
                <span className="shrink-0 font-medium text-text">{formatBytes(bytes)} · {Number(port.flows || 0).toLocaleString()} flows</span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface">
                <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(4, (bytes / max) * 100)}%` }} />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PortBadge({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-border bg-surface px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-0.5 truncate font-mono text-xs font-semibold text-text">{value}</div>
    </div>
  )
}

function TrafficShapeBar({ label, value, max, display, color }: { label: string; value: number; max: number; display: string; color: string }) {
  const width = Math.max(6, Math.min(100, (value / Math.max(1, max)) * 100))
  return (
    <div>
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span className="text-muted">{label}</span>
        <span className="font-medium text-text">{display}</span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-surface">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${width}%` }} />
      </div>
    </div>
  )
}

function InterfaceChipGroup({ title, values, empty }: { title: string; values: NetflowInterfaceRef[]; empty: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted">{title}</div>
      {values.length ? (
        <div className="flex flex-wrap gap-1.5">
          {values.map((value) => (
            <span key={`${value.exporter_ip}-${value.ifindex}`} className="rounded border border-border bg-surface px-2 py-1 text-[11px] text-text">
              <span className="font-medium">{interfaceLabel(value)}</span>
              <span className="ml-1 font-mono text-muted">#{value.ifindex}</span>
            </span>
          ))}
        </div>
      ) : (
        <div className="text-xs text-muted">{empty}</div>
      )}
    </div>
  )
}

function normalizeInterfaceRefs(resolved?: NetflowInterfaceRef[], indexes?: number[]): NetflowInterfaceRef[] {
  if (resolved?.length) return dedupeInterfaceRefs(resolved)
  return dedupeInterfaceRefs((indexes || []).filter(Boolean).map((ifindex) => ({ exporter_ip: '', ifindex })))
}

function dedupeInterfaceRefs(values: NetflowInterfaceRef[]): NetflowInterfaceRef[] {
  const seen = new Set<string>()
  const out: NetflowInterfaceRef[] = []
  for (const value of values) {
    const key = `${value.exporter_ip || 'unknown'}-${value.ifindex}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(value)
  }
  return out
}

function interfaceLabel(value: Pick<NetflowInterfaceRef, 'display_name' | 'if_name' | 'if_descr' | 'if_alias' | 'ifindex'>): string {
  return value.display_name || value.if_name || value.if_descr || value.if_alias || `ifIndex ${value.ifindex}`
}

function describeTcpFlags(flags: number): { primary: string; detail: string } {
  if (!flags) return { primary: 'None', detail: 'No TCP control flags' }
  const names: [number, string][] = [
    [1, 'FIN'],
    [2, 'SYN'],
    [4, 'RST'],
    [8, 'PSH'],
    [16, 'ACK'],
    [32, 'URG'],
  ]
  const found = names.filter(([bit]) => (flags & bit) !== 0).map(([, name]) => name)
  return {
    primary: found.slice(0, 3).join(' + ') || 'TCP',
    detail: found.length > 3 ? `${found.length} flags: ${found.join(', ')}` : found.join(', ') || `mask ${flags}`,
  }
}

function inferApplication(port: number): string {
  if ([80, 443, 8080, 8443].includes(port)) return 'Web (HTTP/HTTPS)'
  if ([554, 1755, 1935, 5004, 5005, 8000, 8554, 5060, 5061].includes(port)) return 'Streaming Media'
  if (port === 53) return 'DNS'
  if ([25, 110, 143, 465, 587, 993, 995].includes(port)) return 'Email (SMTP/IMAP/POP)'
  if ([20, 21, 69, 115, 445, 2049].includes(port)) return 'File Transfer (FTP/SMB)'
  if ([22, 23, 3389, 5900, 5938].includes(port)) return 'Remote Access (SSH/RDP/Telnet)'
  if ([1433, 1521, 3306, 5432, 6379, 9042, 27017].includes(port)) return 'Database'
  if ([1719, 1720, 3478, 3479, 5060, 5061, 16384, 19302].includes(port)) return 'VoIP / Video Conf.'
  if ([123, 161, 162, 514, 6343].includes(port)) return 'Network Mgmt'
  if (port >= 1 && port <= 1023) return 'System Services'
  return 'Other'
}

function describePortClass(port: number): string {
  if (port === 0) return 'No TCP/UDP destination port was reported.'
  if (port >= 1 && port <= 1023) return 'Well-known system port.'
  if (port >= 1024 && port <= 49151) return 'Registered/user port. Application is inferred only if the port is in the known map.'
  return 'Dynamic/private port, commonly used by clients or custom services.'
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
  viewAllTo,
}: {
  title: string
  subtitle: string
  data: { name: string; value: number }[]
  colors: string[]
  labelMode?: 'ip'
  compactNames?: boolean
  onSelect?: (name: string) => void
  activeName?: string | null
  viewAllTo?: string
}) {
  const total = data.reduce((acc, d) => acc + (d.value || 0), 0)
  return (
    <Card className="flex h-full flex-col">
      <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
        <div>
          <CardTitle className="text-sm">{title}</CardTitle>
          <p className="text-[11px] text-muted">{subtitle}</p>
        </div>
        {viewAllTo && (
          <Link to={viewAllTo} className="shrink-0 text-[11px] font-medium text-primary hover:underline">
            View all
          </Link>
        )}
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
                  <span className="flex min-w-0 items-center gap-1.5 truncate" title={d.name}>
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
      id: 'rst-ratio',
      title: 'High TCP RST Ratio',
      body: `${device.packet_loss_pct.toFixed(2)}% of TCP flows carried RST (aborts/probes)`,
      severity: 'warning',
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
            <p className="text-xs text-muted">Derived from flow telemetry in this window</p>
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
  // These are flow-derived heuristics (avg flow duration, TCP RST ratio) —
  // NOT real latency / packet-loss measurements. Label them honestly.
  const flowDuration = status?.latency_ms ?? 0
  const rstRatio = status?.packet_loss_pct ?? 0
  return (
    <Card className="shrink-0">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Exporter Health</CardTitle>
        <p className="text-[11px] text-muted">Flow-derived signals (not ICMP/SNMP probes)</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <MiniStat icon={Gauge} label="Avg Flow Duration" value={`${flowDuration} ms`} tone="cyan" />
          <MiniStat icon={Database} label="TCP RST Ratio" value={`${rstRatio.toFixed(2)}%`} tone={rstRatio >= 5 ? 'rose' : 'emerald'} />
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
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('service')}>Service{arrow('service')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('protocol_name')}>Protocol{arrow('protocol_name')}</button></Th>
              <Th className="text-right"><button className="ml-auto flex items-center gap-1 hover:text-text" onClick={() => toggle('bytes')}>Bytes{arrow('bytes')}</button></Th>
              <Th className="text-right"><button className="ml-auto flex items-center gap-1 hover:text-text" onClick={() => toggle('packets')}>Packets{arrow('packets')}</button></Th>
              <Th className="text-right">Flows</Th>
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
                <Td className="text-right text-sm">{c.flows.toLocaleString()}</Td>
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
                        <span className="truncate text-xs font-semibold">{interfaceLabel(it)}</span>
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
                      <span className="truncate text-sm font-semibold" title={it.if_alias || it.if_descr || ''}>
                        {interfaceLabel(it)}
                      </span>
                    </div>
                    <span className="shrink-0 text-[11px] font-medium text-muted">{formatBytes(it.bytes)}</span>
                  </div>
                  {it.if_alias && (
                    <div className="mt-1 truncate text-[10px] text-muted" title={it.if_alias}>{it.if_alias}</div>
                  )}
                  {showExporter && (
                    <div className="mt-1 truncate font-mono text-[10px] text-muted">
                      {it.device_hostname ? `${it.device_hostname} · ` : ''}{it.exporter_ip} · index {it.ifindex}
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

function flagEmoji(cc: string | null): string {
  if (!cc || cc.length !== 2) return '🌐'
  const base = 0x1f1e6
  const a = 'A'.charCodeAt(0)
  return String.fromCodePoint(base + cc.charCodeAt(0) - a, base + cc.charCodeAt(1) - a)
}

function CountriesCard({ data }: { data: Country[] }) {
  // Endpoint returns [] when no GeoIP database is provisioned — hide entirely.
  if (data.length === 0) return null
  const top = data.slice(0, 6)
  const max = Math.max(1, ...top.map((c) => c.bytes))
  const total = Math.max(1, data.reduce((a, b) => a + b.bytes, 0))
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <RadioTower className="h-4 w-4 text-primary" />
        <div>
          <CardTitle className="text-sm">Top Countries</CardTitle>
          <p className="text-[11px] text-muted">GeoIP on top endpoint IPs · src + dst bytes</p>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {top.map((c) => (
          <div key={c.country || 'unknown'}>
            <div className="flex items-center justify-between gap-2 text-[11px]">
              <span className="flex min-w-0 items-center gap-1.5 truncate">
                <span>{flagEmoji(c.country)}</span>
                <span className="truncate">{c.country_name || 'Private / Unknown'}</span>
              </span>
              <span className="shrink-0 text-muted">{formatBytes(c.bytes)} · {((c.bytes / total) * 100).toFixed(1)}%</span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface">
              <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(3, (c.bytes / max) * 100)}%` }} />
            </div>
          </div>
        ))}
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
