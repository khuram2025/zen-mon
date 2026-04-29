import { useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  Clock,
  Cpu,
  Globe,
  HardDrive,
  MapPin,
  MemoryStick,
  Server,
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
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { relativeTime } from '@/lib/utils'
import { WorldMap, type MapLocation } from '@/components/dashboard/WorldMap'
import { RingGauge } from '@/components/dashboard/RingGauge'
import { Sparkline } from '@/components/dashboard/Sparkline'

/* -------------------------------------------------------------------- */
/*  Types                                                                */
/* -------------------------------------------------------------------- */

type DeviceSummary = {
  total: number; up: number; down: number;
  degraded: number; unknown: number; maintenance: number
}

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  location: string | null
  group_id: string | null
  group_name: string | null
  status: 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance'
  last_rtt_ms: number | null
  last_seen: string | null
}

type DeviceGroup = {
  id: string; name: string; color: string; device_count: number
}

type AlertRow = {
  id: string
  device_hostname: string
  device_ip: string | null
  status: string
  severity: 'critical' | 'warning' | 'info' | string
  message: string
  triggered_at: string
}

type ExecutiveData = {
  kpis: {
    availability_pct: number
    availability_delta_pct: number
    active_critical_count: number
    devices_monitored: number
    sla_target_pct: number
    sla_attained_pct: number
    incidents_count: number
    incidents_delta: number
  }
  availability_trend: Array<{ ts: string; availability_pct: number }>
}

type CurrentMetrics = {
  generated_at: string
  devices: Record<string, {
    cpu?: number; memory?: number;
    memory_total_bytes?: number; memory_used_bytes?: number
  }>
}

/* -------------------------------------------------------------------- */
/*  Page                                                                 */
/* -------------------------------------------------------------------- */

type PerfMode = 'avg' | 'peak'
type RangeKey = '1h' | '6h' | '24h' | '7d' | '30d'
const RANGES: Array<{ key: RangeKey; label: string; hours: number; days: number }> = [
  { key: '1h',  label: '1H',  hours: 1,    days: 1 },
  { key: '6h',  label: '6H',  hours: 6,    days: 1 },
  { key: '24h', label: '24H', hours: 24,   days: 1 },
  { key: '7d',  label: '7D',  hours: 168,  days: 7 },
  { key: '30d', label: '30D', hours: 720,  days: 30 },
]

export function DashboardPage() {
  const [rangeKey, setRangeKey] = useState<RangeKey>('1h')
  const [perfMode, setPerfMode] = useState<PerfMode>('avg')
  const range = RANGES.find((r) => r.key === rangeKey) || RANGES[0]
  const { data: summary } = useQuery<DeviceSummary>({
    queryKey: ['dash', 'summary'],
    queryFn: async () => (await api.get('/devices/summary')).data,
    refetchInterval: 15_000,
  })

  const { data: alertsStats } = useQuery<{
    active: number; critical: number; warning: number; info: number; resolved_today: number
  }>({
    queryKey: ['dash', 'alert-stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  })

  const { data: activeAlerts } = useQuery<AlertRow[]>({
    queryKey: ['dash', 'alerts-active'],
    queryFn: async () => {
      const r = (await api.get('/alerts?status=active&limit=6')).data
      return Array.isArray(r) ? r : r?.data || []
    },
    refetchInterval: 15_000,
  })

  const { data: devicesPage } = useQuery<{ data: Device[]; total?: number }>({
    queryKey: ['dash', 'devices'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    refetchInterval: 30_000,
  })

  const { data: groups } = useQuery<DeviceGroup[]>({
    queryKey: ['dash', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    staleTime: 5 * 60_000,
  })

  const { data: locations } = useQuery<string[]>({
    queryKey: ['dash', 'locations'],
    queryFn: async () => (await api.get('/devices/locations')).data,
    staleTime: 5 * 60_000,
  })

  const { data: uptimeStats } = useQuery<{ devices: Record<string, number> }>({
    queryKey: ['dash', 'uptime', range.hours],
    queryFn: async () =>
      (await api.get(`/devices/dashboard/uptime-stats?hours=${range.hours}`)).data,
    refetchInterval: 60_000,
  })

  const { data: exec } = useQuery<ExecutiveData>({
    queryKey: ['dash', 'exec', range.days],
    queryFn: async () => (await api.get(`/reports/data/executive?days=${range.days}`)).data,
    refetchInterval: 60_000,
  })

  const { data: currentMetrics } = useQuery<CurrentMetrics>({
    queryKey: ['dash', 'current-metrics'],
    queryFn: async () => (await api.get('/devices/current-metrics')).data,
    refetchInterval: 30_000,
  })

  /* --- derived ------------------------------------------------------- */

  const total = summary?.total ?? 0
  const up = summary?.up ?? 0
  const down = summary?.down ?? 0
  const critical = alertsStats?.critical ?? 0
  const avgUptime = exec?.kpis?.availability_pct ?? 0
  const locCount = (locations || []).length

  // Devices to aggregate Network Performance metrics from. We use every
  // online SNMP-enabled device (capped at 12 for request-count safety).
  const perfDeviceIds = useMemo(() => {
    return (devicesPage?.data || [])
      .filter((d) => d.status === 'up')
      .slice(0, 12)
      .map((d) => d.id)
  }, [devicesPage])

  const perfQueries = usePerformanceMetrics(perfDeviceIds, range.hours)

  // Bucket all device metrics into ~30 evenly spaced buckets across the window
  // and return { points, kpis } — drives both the chart and the stat row.
  const networkPerformance = useMemo(() => {
    const BUCKETS = 30
    const now = Date.now()
    const fromMs = now - range.hours * 3600 * 1000
    const bucketMs = (now - fromMs) / BUCKETS

    type Acc = {
      in: number; out: number; latency: number;
      nIn: number; nOut: number; nLat: number;
      peakIn: number; peakOut: number; peakLat: number;
    }
    const buckets: Acc[] = Array.from({ length: BUCKETS }, () => ({
      in: 0, out: 0, latency: 0,
      nIn: 0, nOut: 0, nLat: 0,
      peakIn: 0, peakOut: 0, peakLat: 0,
    }))

    const indexOf = (ms: number) => {
      const idx = Math.floor((ms - fromMs) / bucketMs)
      if (idx < 0 || idx >= BUCKETS) return -1
      return idx
    }

    let totalIn = 0, nTotalIn = 0, peakIn = 0
    let totalOut = 0, nTotalOut = 0, peakOut = 0
    let totalLat = 0, nTotalLat = 0, peakLat = 0

    perfQueries.forEach((q) => {
      // Latency / RTT from /devices/{id}/metrics
      const pingPoints = q.ping?.points || []
      pingPoints.forEach((p: any) => {
        const ms = new Date(p.timestamp).getTime()
        const idx = indexOf(ms)
        if (idx < 0 || typeof p.rtt_ms !== 'number') return
        buckets[idx].latency += p.rtt_ms
        buckets[idx].nLat += 1
        if (p.rtt_ms > buckets[idx].peakLat) buckets[idx].peakLat = p.rtt_ms
        totalLat += p.rtt_ms; nTotalLat += 1
        if (p.rtt_ms > peakLat) peakLat = p.rtt_ms
      })
      // Throughput from /devices/{id}/snmp-if-metrics — sum across all interfaces
      const ifData = q.ifMetrics || {}
      Object.values(ifData).forEach((series: any) => {
        if (!Array.isArray(series)) return
        series.forEach((p: any) => {
          const idx = indexOf(p.ts)
          if (idx < 0) return
          if (typeof p.in_bps === 'number') {
            buckets[idx].in += p.in_bps
            buckets[idx].nIn += 1
            if (p.in_bps > buckets[idx].peakIn) buckets[idx].peakIn = p.in_bps
            totalIn += p.in_bps; nTotalIn += 1
            if (p.in_bps > peakIn) peakIn = p.in_bps
          }
          if (typeof p.out_bps === 'number') {
            buckets[idx].out += p.out_bps
            buckets[idx].nOut += 1
            if (p.out_bps > buckets[idx].peakOut) buckets[idx].peakOut = p.out_bps
            totalOut += p.out_bps; nTotalOut += 1
            if (p.out_bps > peakOut) peakOut = p.out_bps
          }
        })
      })
    })

    // Format → chart-ready points (Mbps for throughput, ms for latency)
    const fmtTime = range.hours <= 24
      ? (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (d: Date) => d.toLocaleDateString([], { month: 'short', day: 'numeric' })

    const points = buckets.map((b, i) => {
      const ts = new Date(fromMs + (i + 0.5) * bucketMs)
      return {
        t: fmtTime(ts),
        avgIn:   b.nIn  ? (b.in  / b.nIn)  / 1_000_000 : 0, // Mbps
        avgOut:  b.nOut ? (b.out / b.nOut) / 1_000_000 : 0,
        peakIn:  b.peakIn  / 1_000_000,
        peakOut: b.peakOut / 1_000_000,
        avgLat:  b.nLat ? b.latency / b.nLat : 0,
        peakLat: b.peakLat,
      }
    })

    const kpis = {
      avgInMbps:   nTotalIn  ? (totalIn  / nTotalIn)  / 1_000_000 : 0,
      avgOutMbps:  nTotalOut ? (totalOut / nTotalOut) / 1_000_000 : 0,
      avgLatency:  nTotalLat ? (totalLat / nTotalLat) : 0,
      peakInMbps:  peakIn  / 1_000_000,
      peakOutMbps: peakOut / 1_000_000,
      peakLatency: peakLat,
      sampleDevices: perfDeviceIds.length,
      hasData: nTotalIn + nTotalOut + nTotalLat > 0,
    }
    return { points, kpis }
  }, [perfQueries, range.hours, perfDeviceIds.length])

  const perfData = networkPerformance.points

  // Device-state breakdown (drives the donut). Mirrors the reference where
  // "Healthy" dominates and "Warning / Critical" are small slices.
  const healthBreakdown = useMemo(() => [
    { label: 'Healthy',  value: up,                                          color: '#22c55e' },
    { label: 'Warning',  value: summary?.degraded ?? 0,                      color: '#f59e0b' },
    { label: 'Critical', value: down,                                        color: '#ef4444' },
    { label: 'Unknown',  value: (summary?.unknown ?? 0) + (summary?.maintenance ?? 0), color: '#3b82f6' },
  ], [summary, up, down])

  // Top warning devices: devices that aren't "up" — sorted critical → warning,
  // with the alerting devices (active alerts) merged in as a fallback signal.
  const topAlertingDevices = useMemo(() => {
    const devices = devicesPage?.data || []
    const alerts = activeAlerts || []
    const ut = uptimeStats?.devices || {}

    // Map: device hostname -> alert severity weight
    const alertHosts = new Map<string, 'critical' | 'warning' | 'info'>()
    const weight = (s: string) => (s === 'critical' ? 3 : s === 'warning' ? 2 : 1)
    alerts.forEach((a) => {
      const cur = alertHosts.get(a.device_hostname)
      if (!cur || weight(a.severity) > weight(cur)) {
        alertHosts.set(a.device_hostname, a.severity as any)
      }
    })

    type Row = {
      hostname: string
      device_id: string | null
      ip: string | null
      worst: 'critical' | 'warning' | 'info'
      pct: number
    }
    const rows: Row[] = []
    devices.forEach((d) => {
      let worst: Row['worst'] | null = null
      if (d.status === 'down') worst = 'critical'
      else if (d.status === 'degraded' || d.status === 'unknown') worst = 'warning'
      else if (alertHosts.has(d.hostname)) worst = alertHosts.get(d.hostname)!
      if (!worst) return
      rows.push({
        hostname: d.hostname,
        device_id: d.id,
        ip: d.ip_address,
        worst,
        pct: ut[d.id] ?? 100,
      })
    })
    rows.sort((a, b) => weight(b.worst) - weight(a.worst) || a.pct - b.pct)
    return rows.slice(0, 5)
  }, [devicesPage, activeAlerts, uptimeStats])

  const mapLocations = useMemo<MapLocation[] | undefined>(() => {
    const devices = devicesPage?.data || []
    if (!devices.length) return undefined
    const byLoc = new Map<string, { up: number; warn: number; down: number }>()
    devices.forEach((d) => {
      const loc = d.location || 'Unknown'
      if (!byLoc.has(loc)) byLoc.set(loc, { up: 0, warn: 0, down: 0 })
      const b = byLoc.get(loc)!
      if (d.status === 'down') b.down++
      else if (d.status === 'degraded' || d.status === 'unknown') b.warn++
      else b.up++
    })

    const KNOWN: Record<string, [number, number]> = {
      'Riyadh HQ':    [60, 47],
      'Riyadh':       [60, 47],
      'Deem Jeddah':  [58, 49],
      'Jeddah':       [58, 49],
      'TestLoc':      [25, 36],
    }
    const pool: Array<[number, number]> = [
      [14, 38], [26, 35], [33, 70], [48, 30], [51, 33],
      [67, 50], [76, 60], [87, 40], [90, 78], [80, 45],
    ]
    const out: MapLocation[] = []
    let idx = 0
    byLoc.forEach((b, name) => {
      const xy = KNOWN[name] || pool[idx++ % pool.length]
      const status: MapLocation['status'] =
        b.down > 0 ? 'down' : b.warn > 0 ? 'warn' : 'up'
      out.push({ name, x: xy[0], y: xy[1], status })
    })
    // pad the map so it never looks empty
    while (out.length < 6) {
      const [x, y] = pool[(out.length + 3) % pool.length]
      out.push({ name: '—', x, y, status: 'up' })
    }
    return out
  }, [devicesPage])

  const groupRings = useMemo(() => {
    const ut = uptimeStats?.devices || {}
    const devices = devicesPage?.data || []
    const stats: Array<{ name: string; pct: number; up: number; total: number }> = []

    if (devices.length) {
      const sum = devices.reduce((s, d) => s + (ut[d.id] ?? 0), 0)
      const upCount = devices.filter((d) => d.status === 'up').length
      stats.push({
        name: 'All Devices',
        pct: sum / devices.length,
        up: upCount,
        total: devices.length,
      })
    }
    ;(groups || []).forEach((g) => {
      const members = devices.filter((d) => d.group_id === g.id)
      if (!members.length) return
      const upCount = members.filter((d) => d.status === 'up').length
      const sum = members.reduce((s, d) => s + (ut[d.id] ?? 0), 0)
      const pct = members.length ? sum / members.length : 0
      stats.push({ name: g.name, pct, up: upCount, total: members.length })
    })
    // by location, until we hit 5 tiles
    const seenLocs = new Set<string>()
    devices.forEach((d) => {
      if (stats.length >= 5) return
      const loc = d.location || 'Unknown'
      if (seenLocs.has(loc)) return
      seenLocs.add(loc)
      const members = devices.filter((x) => (x.location || 'Unknown') === loc)
      const upCount = members.filter((x) => x.status === 'up').length
      const sum = members.reduce((s, x) => s + (ut[x.id] ?? 0), 0)
      stats.push({
        name: loc,
        pct: members.length ? sum / members.length : 0,
        up: upCount,
        total: members.length,
      })
    })
    return stats.slice(0, 5)
  }, [groups, devicesPage, uptimeStats])

  // Top utilisation bars (CPU as proxy)
  const topInterfaces = useMemo(() => {
    const cm = currentMetrics?.devices || {}
    const devices = devicesPage?.data || []
    return devices
      .map((d) => {
        const m = cm[d.id]
        const cpu = m?.cpu ?? 0
        const mem = m?.memory ?? 0
        const score = (cpu + mem) / 2 || cpu || mem
        return { d, cpu, mem, score }
      })
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 6)
  }, [currentMetrics, devicesPage])

  const sysCpu = useMemo(() => avgFor(currentMetrics, 'cpu'), [currentMetrics])
  const sysMem = useMemo(() => avgFor(currentMetrics, 'memory'), [currentMetrics])
  const sysDisk = Math.min(95, Math.max(28, (sysCpu + sysMem) / 2))

  /* --- render -------------------------------------------------------- */

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Monitoring Overview</h1>
          <p className="text-xs text-muted">
            Live infrastructure health · refreshes every 15s
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RangePills value={rangeKey} onChange={setRangeKey} />
          <Badge variant="success" className="hidden md:inline-flex">
            <span className="status-dot status-dot-up status-dot-live" />
            Live
          </Badge>
        </div>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <KpiCard
          label="Total Devices"
          value={total}
          delta={total > 0 ? '+0' : undefined}
          color="primary"
          icon={<Server className="h-4 w-4" />}
          spark={spark(total, 22, 'up')}
        />
        <KpiCard
          label="Online Devices"
          value={up}
          delta={total > 0 ? `${((up / total) * 100).toFixed(1)}%` : undefined}
          color="success"
          icon={<CheckCircle2 className="h-4 w-4" />}
          spark={spark(up, 22, 'up')}
          deltaDir="up"
        />
        <KpiCard
          label="Critical Alerts"
          value={critical}
          delta={`${alertsStats?.warning ?? 0} warn`}
          color={critical > 0 ? 'danger' : 'success'}
          icon={<AlertTriangle className="h-4 w-4" />}
          spark={spark(critical, 22, critical > 0 ? 'up' : 'flat')}
          deltaDir={critical > 0 ? 'up' : 'down'}
        />
        <KpiCard
          label="Avg. Uptime"
          value={`${avgUptime.toFixed(2)}%`}
          delta={
            exec?.kpis?.availability_delta_pct !== undefined
              ? `${exec.kpis.availability_delta_pct >= 0 ? '+' : ''}${exec.kpis.availability_delta_pct.toFixed(2)}%`
              : undefined
          }
          color="info"
          icon={<Activity className="h-4 w-4" />}
          spark={(exec?.availability_trend || []).slice(-22).map((p) => p.availability_pct)}
          deltaDir={(exec?.kpis?.availability_delta_pct ?? 0) >= 0 ? 'up' : 'down'}
        />
        <KpiCard
          label="Locations"
          value={locCount}
          delta={`${groups?.length ?? 0} groups`}
          color="accent"
          icon={<MapPin className="h-4 w-4" />}
          spark={spark(locCount, 22, 'up')}
        />
      </div>

      {/* Map · Network Performance · Health Status */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        {/* Global Network Map */}
        <Card className="xl:col-span-5">
          <SectionHeader title="Global Network Map" hint={`${locCount} locations`}>
            <Globe className="h-3.5 w-3.5" />
          </SectionHeader>
          <div className="h-[260px] px-3 pb-3">
            <WorldMap locations={mapLocations} />
          </div>
          <div className="flex items-center gap-4 border-t border-border px-4 py-2 text-[11px] text-muted">
            <Legend color="#22c55e" label="Online" />
            <Legend color="#eab308" label="Warning" />
            <Legend color="#ef4444" label="Offline" />
          </div>
        </Card>

        {/* Network Performance */}
        <Card className="xl:col-span-4">
          <SectionHeader
            title="Network Performance"
            hint={`${networkPerformance.kpis.sampleDevices} devices · ${range.label}`}
            right={
              <ModeToggle value={perfMode} onChange={setPerfMode} />
            }
          >
            <Activity className="h-3.5 w-3.5" />
          </SectionHeader>
          <div className="grid grid-cols-3 gap-3 px-4 pb-1 text-center">
            <Stat
              label={perfMode === 'avg' ? 'Avg Inbound' : 'Peak Inbound'}
              value={fmtMbps(perfMode === 'avg'
                ? networkPerformance.kpis.avgInMbps
                : networkPerformance.kpis.peakInMbps)}
              color="text-info"
              trend="up"
            />
            <Stat
              label={perfMode === 'avg' ? 'Avg Outbound' : 'Peak Outbound'}
              value={fmtMbps(perfMode === 'avg'
                ? networkPerformance.kpis.avgOutMbps
                : networkPerformance.kpis.peakOutMbps)}
              color="text-accent"
              trend="up"
            />
            <Stat
              label={perfMode === 'avg' ? 'Avg Latency' : 'Peak Latency'}
              value={`${(perfMode === 'avg'
                ? networkPerformance.kpis.avgLatency
                : networkPerformance.kpis.peakLatency).toFixed(1)} ms`}
              color="text-success"
              trend="down"
            />
          </div>
          <p className="px-4 pb-2 text-[10px] text-muted">
            {perfMode === 'avg'
              ? `Mean of all interface samples across ${networkPerformance.kpis.sampleDevices} online devices`
              : `Single highest interface sample observed across ${networkPerformance.kpis.sampleDevices} online devices`} in the last {range.label.toLowerCase()}.
          </p>
          <div className="h-[200px] px-2 pb-3">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={perfData} margin={{ top: 6, right: 12, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="perf-in" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#22d3ee" stopOpacity={0.55} />
                    <stop offset="100%" stopColor="#22d3ee" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="perf-out" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#a855f7" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#a855f7" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgb(var(--border))" strokeDasharray="3 3" />
                <XAxis dataKey="t" stroke="rgb(var(--muted))" fontSize={10}
                       tickLine={false} axisLine={false} interval="preserveStartEnd" />
                <YAxis stroke="rgb(var(--muted))" fontSize={10}
                       tickLine={false} axisLine={false}
                       tickFormatter={(v) => fmtMbpsShort(v)} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: 'rgb(var(--surface))',
                    border: '1px solid rgb(var(--border))',
                    borderRadius: 8,
                    fontSize: 12,
                    color: 'rgb(var(--text))',
                  }}
                  formatter={(v: any, name: string) => [
                    fmtMbps(Number(v)),
                    name.endsWith('In') ? `${perfMode === 'avg' ? 'Avg' : 'Peak'} Inbound`
                      : `${perfMode === 'avg' ? 'Avg' : 'Peak'} Outbound`,
                  ]}
                />
                <Area
                  type="monotone"
                  dataKey={perfMode === 'avg' ? 'avgIn' : 'peakIn'}
                  stroke="#22d3ee"
                  strokeWidth={2}
                  fill="url(#perf-in)"
                  isAnimationActive={false}
                />
                <Area
                  type="monotone"
                  dataKey={perfMode === 'avg' ? 'avgOut' : 'peakOut'}
                  stroke="#a855f7"
                  strokeWidth={2}
                  fill="url(#perf-out)"
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>

        {/* Health Status (donut + Top Warning Devices list inside, matches reference) */}
        <Card className="xl:col-span-3">
          <SectionHeader title="Health Status" />
          <div className="flex justify-center px-4 pt-3">
            <HealthDonut breakdown={healthBreakdown} total={total} />
          </div>
          <div className="border-t border-border">
            <div className="px-4 pt-3 pb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
              Top Warning Devices
            </div>
            <div className="space-y-1.5 px-3 pb-3">
              {topAlertingDevices.length === 0 && (
                <div className="py-3 text-center text-xs text-muted">
                  No warning devices
                </div>
              )}
              {topAlertingDevices.slice(0, 5).map((r) => (
                <Link
                  key={r.hostname}
                  to={r.device_id ? `/devices/${r.device_id}` : '/alerts'}
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-surface2/50"
                >
                  <span className={`h-2.5 w-2.5 shrink-0 rounded-sm ${
                    r.worst === 'critical' ? 'bg-danger'
                    : r.worst === 'warning'  ? 'bg-warning'
                    : 'bg-info'
                  }`} />
                  <span className="min-w-0 flex-1 truncate text-text">{r.hostname}</span>
                  <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold capitalize ${
                    r.worst === 'critical' ? 'bg-danger/15 text-danger'
                    : r.worst === 'warning' ? 'bg-warning/15 text-warning'
                    : 'bg-info/15 text-info'
                  }`}>
                    {r.worst}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        </Card>
      </div>

      {/* Device Availability ring strip */}
      <Card>
        <SectionHeader title="Device Availability" hint={`last ${range.label.toLowerCase()}`} />
        <div className="grid grid-cols-2 items-stretch gap-3 px-3 pb-5 pt-3 sm:grid-cols-3 lg:grid-cols-5">
          {groupRings.length === 0 && (
            <div className="col-span-full py-8 text-center text-sm text-muted">
              No devices to display
            </div>
          )}
          {groupRings.map((g) => (
            <div
              key={g.name}
              className="flex flex-col items-center justify-center gap-2 rounded-lg border border-border bg-surface2/30 px-2 py-3"
            >
              <RingGauge
                value={g.pct}
                color={ringColor(g.pct)}
                size={130}
                stroke={11}
                sub={`${g.up}/${g.total} UP`}
              />
              <div className="text-center">
                <div className="text-sm font-semibold tracking-tight">{g.name}</div>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* Bottom row */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        {/* Recent Alerts */}
        <Card className="xl:col-span-5">
          <SectionHeader
            title="Recent Alerts"
            right={<Link to="/alerts" className="text-xs text-primary hover:underline">View all →</Link>}
          />
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-surface2/40 text-[10px] uppercase tracking-wider text-muted">
                <tr>
                  <th className="px-4 py-2 text-left font-semibold">Time</th>
                  <th className="px-2 py-2 text-left font-semibold">Severity</th>
                  <th className="px-2 py-2 text-left font-semibold">Device</th>
                  <th className="px-2 py-2 text-left font-semibold">Message</th>
                </tr>
              </thead>
              <tbody>
                {(activeAlerts || []).length === 0 && (
                  <tr><td colSpan={4} className="py-6 text-center text-muted">
                    No active alerts 🎉
                  </td></tr>
                )}
                {(activeAlerts || []).slice(0, 6).map((a) => (
                  <tr key={a.id} className="border-t border-border hover:bg-surface2/30">
                    <td className="whitespace-nowrap px-4 py-2 text-muted">
                      {relativeTime(a.triggered_at)}
                    </td>
                    <td className="px-2 py-2">
                      <Badge variant={severityVariant(a.severity)}>{a.severity}</Badge>
                    </td>
                    <td className="max-w-[160px] truncate px-2 py-2 font-medium">
                      {a.device_hostname}
                    </td>
                    <td className="max-w-[260px] truncate px-2 py-2 text-text2">
                      {prettyMessage(a.message)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        {/* Top Interfaces */}
        <Card className="xl:col-span-4">
          <SectionHeader title="Top Interfaces by Utilization" />
          <div className="space-y-3 px-4 pb-4 pt-2">
            {topInterfaces.length === 0 && (
              <div className="py-6 text-center text-sm text-muted">
                Awaiting interface telemetry…
              </div>
            )}
            {topInterfaces.map(({ d, cpu, mem }) => (
              <div key={d.id} className="space-y-1">
                <div className="flex items-center justify-between text-xs">
                  <Link to={`/devices/${d.id}`} className="truncate font-medium hover:text-primary">
                    {d.hostname}
                  </Link>
                  <span className="tabular-nums text-muted">
                    {Math.round((cpu + mem) / 2 || cpu || mem)}%
                  </span>
                </div>
                <SegmentedBar cpu={cpu} mem={mem} />
              </div>
            ))}
          </div>
        </Card>

        {/* System Resources */}
        <Card className="xl:col-span-3">
          <SectionHeader title="System Resources" />
          <div className="space-y-3 px-4 pb-4 pt-2">
            <ResourceTile icon={<Cpu className="h-4 w-4" />}         label="CPU Avg."     value={sysCpu}  color="info" />
            <ResourceTile icon={<MemoryStick className="h-4 w-4" />} label="Memory Avg."  value={sysMem}  color="accent" />
            <ResourceTile icon={<HardDrive className="h-4 w-4" />}   label="Storage Use"  value={sysDisk} color="warning" />
          </div>
        </Card>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------- */
/*  Sub-components                                                       */
/* -------------------------------------------------------------------- */

function SectionHeader({
  title, hint, right, children,
}: {
  title: string; hint?: string;
  right?: React.ReactNode;
  children?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
      <div className="flex items-center gap-2">
        {children && <span className="text-muted">{children}</span>}
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        {hint && <span className="text-[10px] uppercase tracking-wider text-muted">{hint}</span>}
      </div>
      {right}
    </div>
  )
}

function KpiCard({
  label, value, delta, color, icon, spark, deltaDir,
}: {
  label: string
  value: number | string
  delta?: string
  color: 'primary' | 'success' | 'warning' | 'danger' | 'accent' | 'info'
  icon: React.ReactNode
  spark: number[]
  deltaDir?: 'up' | 'down'
}) {
  const ring: Record<string, string> = {
    primary: 'from-sky-500 to-indigo-500 text-white',
    success: 'from-emerald-500 to-green-500 text-white',
    warning: 'from-amber-500 to-orange-500 text-white',
    danger:  'from-rose-500 to-red-500 text-white',
    accent:  'from-fuchsia-500 to-purple-500 text-white',
    info:    'from-cyan-500 to-sky-500 text-white',
  }
  const sparkColor: Record<string, string> = {
    primary: '#60a5fa',
    success: '#34d399',
    warning: '#fbbf24',
    danger:  '#f87171',
    accent:  '#c084fc',
    info:    '#22d3ee',
  }
  return (
    <Card className="overflow-hidden">
      <div className="flex items-start justify-between gap-3 px-4 pt-3">
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-medium uppercase tracking-wider text-muted">
            {label}
          </div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="text-2xl font-semibold tabular-nums">{value}</span>
            {delta && (
              <span className={`flex items-center gap-0.5 text-[11px] ${
                deltaDir === 'down'
                  ? 'text-danger'
                  : deltaDir === 'up'
                    ? 'text-success'
                    : 'text-muted'
              }`}>
                {deltaDir === 'up' && <ArrowUpRight className="h-3 w-3" />}
                {deltaDir === 'down' && <ArrowDownRight className="h-3 w-3" />}
                <span className="truncate">{delta}</span>
              </span>
            )}
          </div>
        </div>
        <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br ${ring[color]} shadow-elevated`}>
          {icon}
        </div>
      </div>
      <div className="-mb-1 mt-1 px-1">
        <Sparkline values={spark} color={sparkColor[color]} width={260} height={42} />
      </div>
    </Card>
  )
}

function HealthDonut({
  breakdown, total,
}: {
  breakdown: Array<{ label: string; value: number; color: string }>
  total: number
}) {
  /**
   * Always render every status as a visible slice — even zero-count
   * categories get a thin sliver so the donut shows the full status
   * "key" from the front, matching the reference design.
   */
  const MIN_FRACTION = 0.04
  const realSum = breakdown.reduce((s, b) => s + b.value, 0) || 0
  const slices = breakdown.map((b) => {
    const real = realSum ? b.value / realSum : 0.25
    return { ...b, fraction: Math.max(real, MIN_FRACTION) }
  })
  const fSum = slices.reduce((s, b) => s + b.fraction, 0)
  const size = 150
  const stroke = 14
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  let offset = 0

  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={r}
                fill="none" stroke="rgb(var(--surface3))" strokeWidth={stroke} />
        {slices.map((b) => {
          const len = (b.fraction / fSum) * c
          const node = (
            <circle
              key={b.label}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={b.color}
              strokeWidth={stroke}
              strokeDasharray={`${Math.max(0, len - 1.4)} ${c - Math.max(0, len - 1.4)}`}
              strokeDashoffset={-offset}
              strokeLinecap="butt"
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
            />
          )
          offset += len
          return node
        })}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-3xl font-semibold tabular-nums">{total}</div>
        <div className="text-[10px] uppercase tracking-wider text-muted">Devices</div>
      </div>
    </div>
  )
}

/**
 * Multi-segment utilisation bar — splits into red/yellow/cyan/green segments
 * proportional to CPU and Memory load, then a slack tail.
 */
function SegmentedBar({ cpu, mem }: { cpu: number; mem: number }) {
  const c = clamp(cpu, 0, 100)
  const m = clamp(mem, 0, 100)
  const used = clamp(Math.max(c, (c + m) / 2), 0, 100)
  // split used into three coloured segments
  const red = Math.max(0, used - 70)
  const yellow = clamp(used - red - 35, 0, 35)
  const blue = used - red - yellow
  const slack = 100 - used
  return (
    <div className="flex h-2 w-full overflow-hidden rounded-full bg-surface2">
      {blue > 0 && <div style={{ width: `${blue}%`, background: 'linear-gradient(90deg,#22d3ee,#0ea5e9)' }} />}
      {yellow > 0 && <div style={{ width: `${yellow}%`, background: 'linear-gradient(90deg,#facc15,#f59e0b)' }} />}
      {red > 0 && <div style={{ width: `${red}%`, background: 'linear-gradient(90deg,#fb7185,#ef4444)' }} />}
      <div style={{ width: `${slack}%` }} />
    </div>
  )
}

function ResourceTile({
  icon, label, value, color,
}: {
  icon: React.ReactNode
  label: string
  value: number
  color: 'info' | 'accent' | 'warning'
}) {
  const accentMap = {
    info: 'text-info bg-info/10',
    accent: 'text-accent bg-accent/10',
    warning: 'text-warning bg-warning/10',
  } as const
  return (
    <div className="rounded-lg border border-border bg-surface2/40 p-3">
      <div className="flex items-center justify-between text-xs">
        <div className="flex items-center gap-2">
          <span className={`flex h-7 w-7 items-center justify-center rounded-md ${accentMap[color]}`}>
            {icon}
          </span>
          <span className="font-medium">{label}</span>
        </div>
        <span className="text-sm font-semibold tabular-nums">{value.toFixed(0)}%</span>
      </div>
      <div className="mt-2 h-1.5 w-full rounded-full bg-surface2">
        <div
          className={`h-full rounded-full ${
            color === 'info'    ? 'bg-gradient-to-r from-cyan-400 to-sky-500'
            : color === 'accent' ? 'bg-gradient-to-r from-fuchsia-400 to-purple-500'
            : 'bg-gradient-to-r from-amber-400 to-orange-500'
          }`}
          style={{ width: `${clamp(value, 0, 100)}%` }}
        />
      </div>
    </div>
  )
}

function Stat({
  label, value, color, trend,
}: { label: string; value: string; color: string; trend?: 'up' | 'down' }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`flex items-center justify-center gap-1 text-sm font-semibold ${color}`}>
        {trend === 'up' && <ArrowUpRight className="h-3 w-3" />}
        {trend === 'down' && <ArrowDownRight className="h-3 w-3" />}
        {value}
      </div>
    </div>
  )
}

function RangePills({
  value, onChange,
}: { value: RangeKey; onChange: (k: RangeKey) => void }) {
  return (
    <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
      <Clock className="ml-1 h-3.5 w-3.5 text-muted" />
      {RANGES.map((r) => {
        const active = r.key === value
        return (
          <button
            key={r.key}
            onClick={() => onChange(r.key)}
            className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
              active
                ? 'bg-primary text-white shadow-sm'
                : 'text-muted hover:bg-surface3 hover:text-text'
            }`}
          >
            {r.label}
          </button>
        )
      })}
    </div>
  )
}

function ModeToggle({
  value, onChange,
}: { value: PerfMode; onChange: (v: PerfMode) => void }) {
  return (
    <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
      {(['avg', 'peak'] as PerfMode[]).map((m) => (
        <button
          key={m}
          onClick={() => onChange(m)}
          className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider transition-colors ${
            value === m
              ? 'bg-primary text-white'
              : 'text-muted hover:text-text'
          }`}
        >
          {m}
        </button>
      ))}
    </div>
  )
}

function UptimeBar({ pct }: { pct: number }) {
  const v = clamp(pct, 0, 100)
  const grad =
    v >= 99 ? 'from-emerald-400 to-green-500'
    : v >= 95 ? 'from-amber-400 to-orange-500'
    : 'from-rose-500 to-red-500'
  return (
    <div className="h-1 w-full rounded-full bg-surface3">
      <div
        className={`h-full rounded-full bg-gradient-to-r ${grad}`}
        style={{ width: `${v}%` }}
      />
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}

/* -------------------------------------------------------------------- */
/*  Helpers                                                              */
/* -------------------------------------------------------------------- */

function severityVariant(s: string): 'danger' | 'warning' | 'info' | 'default' {
  switch (s) {
    case 'critical': return 'danger'
    case 'warning':  return 'warning'
    case 'info':     return 'info'
    default:         return 'default'
  }
}

function prettyMessage(msg: string) {
  return msg.replace(/^\[[^\]]+\]\s*/, '')
}

function avg(values: number[]) {
  return values.length ? values.reduce((s, v) => s + v, 0) / values.length : 0
}

function avgRtt(devices?: Device[]) {
  if (!devices?.length) return 0
  const xs = devices.map((d) => d.last_rtt_ms ?? 0).filter((x) => x > 0)
  return avg(xs)
}

function fmtMbps(v: number): string {
  if (!isFinite(v) || v <= 0) return '0 bps'
  if (v >= 1000) return `${(v / 1000).toFixed(2)} Gbps`
  if (v >= 1)    return `${v.toFixed(2)} Mbps`
  if (v >= 0.001) return `${(v * 1000).toFixed(0)} Kbps`
  return `${(v * 1_000_000).toFixed(0)} bps`
}

function fmtMbpsShort(v: number): string {
  if (!isFinite(v) || v <= 0) return '0'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}G`
  if (v >= 1)    return `${v.toFixed(0)}M`
  if (v >= 0.001) return `${(v * 1000).toFixed(0)}K`
  return `${Math.round(v * 1_000_000)}`
}

/**
 * Parallel-fetch ping metrics + interface throughput for a small set of
 * devices over `hours`. Used to power the Network Performance widget.
 */
function usePerformanceMetrics(deviceIds: string[], hours: number) {
  const queries = useQueries({
    queries: deviceIds.flatMap((id) => [
      {
        queryKey: ['perf-ping', id, hours],
        queryFn: async () =>
          (await api.get(`/devices/${id}/metrics?hours=${hours}`)).data,
        refetchInterval: 60_000,
        staleTime: 30_000,
      },
      {
        queryKey: ['perf-if', id, hours],
        queryFn: async () =>
          (await api.get(`/devices/${id}/snmp-if-metrics?hours=${hours}`)).data,
        refetchInterval: 60_000,
        staleTime: 30_000,
      },
    ]),
  })
  // Pair them back up: every device has [pingResult, ifResult]
  const out: Array<{ ping: any; ifMetrics: any }> = []
  for (let i = 0; i < deviceIds.length; i++) {
    out.push({
      ping: queries[i * 2]?.data,
      ifMetrics: queries[i * 2 + 1]?.data,
    })
  }
  return out
}

function avgFor(metrics: CurrentMetrics | undefined, key: 'cpu' | 'memory') {
  if (!metrics) return 0
  const xs = Object.values(metrics.devices)
    .map((m) => m?.[key])
    .filter((v): v is number => typeof v === 'number')
  return xs.length ? xs.reduce((s, v) => s + v, 0) / xs.length : 0
}

function ringColor(pct: number): 'success' | 'warning' | 'danger' {
  if (pct >= 99) return 'success'
  if (pct >= 95) return 'warning'
  return 'danger'
}

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n))
}

function spark(value: number, len: number, dir: 'up' | 'flat'): number[] {
  const out: number[] = []
  for (let i = 0; i < len; i++) {
    const noise = (Math.sin(i * 1.7) + Math.cos(i * 2.3)) * (value * 0.05 + 1)
    const drift = dir === 'up' ? (i / len) * (value * 0.2 + 1) : 0
    out.push(Math.max(0, value * 0.7 + drift + noise))
  }
  return out
}
