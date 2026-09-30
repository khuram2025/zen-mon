import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  AlertTriangle,
  ArrowDownUp,
  ArrowLeft,
  ChevronRight,
  Cable,
  Gauge,
  Layers,
  Loader2,
  Network,
  RadioTower,
  Router,
  Search,
  Server,
  ShieldAlert,
  Smartphone,
  Wifi,
} from 'lucide-react'
import { api } from '@/lib/api'
import { formatBps, formatBytes, timeAxisTickFormatter, timeTooltipLabelFormatter } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { TIME_RANGE_OPTIONS, TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import type { Overview, SeriesPoint, Talker, Endpoint, Conversation, Protocol, Application, Exporter, DeviceStatus, Heatmap, ExporterDevice, Interface, Country, DscpClass, TcpFlagSummary, NetClass } from './types'
import { HEATMAP_HOURS, TALKER_COLORS, APP_COLORS, PROTO_COLORS, CONV_COLORS, pollMs, interfaceLabel, buildAlerts } from './model'
import { ExporterDeviceDialog } from './components/ExporterDeviceDialog'
import { KpiCard } from './components/KpiCard'
import { DonutCard } from './components/DonutCard'
import { AlertsPanel } from './components/AlertsPanel'
import { DeviceStatusCard } from './components/DeviceStatusCard'
import { ConversationTable } from './components/ConversationTable'
import { TrafficHeatmap } from './components/TrafficHeatmap'
import { DscpCard } from './components/DscpCard'
import { TcpFlagsCard } from './components/TcpFlagsCard'
import { NetworkClassesCard } from './components/NetworkClassesCard'
import { TopInterfacesCard } from './components/TopInterfacesCard'
import { CountriesCard } from './components/CountriesCard'
import { FilterChip } from './components/FilterChip'
import { ThroughputDisplay } from './components/ThroughputDisplay'
import { EmptyState } from './components/EmptyState'

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

  // Exporters whose flows cannot be resolved to interface names, because the
  // address they export from is not any device's management IP and no mapping
  // has been set. Their interfaces can only render as raw "ifIndex N".
  const [mapOpen, setMapOpen] = useState(false)
  const exporterDevices = useQuery<ExporterDevice[]>({
    queryKey: ['netflow', 'exporter-devices'],
    queryFn: async () => (await api.get('/netflow/exporter-devices')).data,
    staleTime: 60_000,
  })
  const unmappedExporters = useMemo(
    () => (exporterDevices.data || []).filter((e) => !e.source),
    [exporterDevices.data],
  )
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
            unmappedExporters={unmappedExporters}
            onMapExporters={() => setMapOpen(true)}
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

      <ExporterDeviceDialog
        open={mapOpen}
        onOpenChange={setMapOpen}
        exporters={exporterDevices.data || []}
      />
    </div>
  )
}

// Re-export for explicit imports elsewhere if needed.
export { Router, Server }
