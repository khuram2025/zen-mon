/* ZenPlus — Network Operations Overview
 *
 * The home dashboard, rebuilt around REAL telemetry only:
 *   · device fleet + availability   → /devices/*, /reports/data/executive
 *   · alerting                      → /alerts/stats, /alerts, /alerts/device-counts
 *   · live traffic                  → /netflow/overview, /netflow/timeseries
 *   · who/what is talking           → /netflow/top-talkers, /netflow/protocols
 *   · busiest interfaces            → /netflow/interfaces
 *   · service checks                → /service-checks/summary
 *   · fleet CPU/MEM/temperature     → /devices/current-metrics
 * Nothing on this page is synthesized — every figure traces to a collector.
 */

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  Clock,
  Cpu,
  Flame,
  Gauge,
  Globe,
  HeartPulse,
  Network,
  Radio,
  Server,
  Shield,
  Waves,
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
import { RingGauge } from '@/components/dashboard/RingGauge'
import { Sparkline } from '@/components/dashboard/Sparkline'

/* ── Types ──────────────────────────────────────────────────────────────── */

type DeviceSummary = { total: number; up: number; down: number; degraded: number; unknown: number; maintenance: number }

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  location: string | null
  group_id: string | null
  status: 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance'
  last_rtt_ms: number | null
}

type AlertRow = {
  id: string
  device_hostname: string
  severity: 'critical' | 'warning' | 'info' | string
  message: string
  triggered_at: string
}

type NetflowOverview = {
  bytes: number; packets: number; flows: number; exporters: number
  src_hosts: number; dst_hosts: number; current_bps: number
  top_protocol?: { protocol: number; name: string; bytes: number } | null
}

type NetflowPoint = { ts: number; bps: number; bytes: number; packets: number; flows: number }

type TopTalker = {
  ip: string; bytes: number; flows: number
  src_bytes: number; dst_bytes: number
  ports?: Array<{ port: number; service: string; application: string }>
  protocols?: Array<{ protocol: number; name: string }>
}

type ProtocolRow = {
  protocol: number; name: string; bytes: number; flows: number
  ports?: Array<{ port: number; service: string; application: string; bytes: number }>
}

type NetflowInterface = {
  exporter_ip: string; ifindex: number
  display_name: string; if_alias: string | null; if_speed: number | null
  device_hostname: string | null
  in_bytes: number; out_bytes: number; bytes: number
}

type ExecutiveData = {
  kpis: { availability_pct: number | null; availability_delta_pct: number | null; devices_monitored: number; sla_target_pct: number }
  availability_trend: Array<{ ts: string; availability_pct: number | null }>
}

type CurrentMetrics = {
  devices: Record<string, { cpu?: number; memory?: number; uptime?: number; [k: string]: number | string | undefined }>
}

type ServiceSummary = { total: number; up: number; down: number; warning: number; degraded: number; unknown: number }

type DeviceAlertCounts = { devices: Record<string, { active: number; critical: number; warning: number }> }

/* ── Range selector ─────────────────────────────────────────────────────── */

type RangeKey = '1h' | '6h' | '24h' | '7d'
const RANGES: Array<{ key: RangeKey; label: string; minutes: number; hours: number; days: number }> = [
  { key: '1h', label: '1H', minutes: 60, hours: 1, days: 1 },
  { key: '6h', label: '6H', minutes: 360, hours: 6, days: 1 },
  { key: '24h', label: '24H', minutes: 1440, hours: 24, days: 1 },
  { key: '7d', label: '7D', minutes: 10080, hours: 168, days: 7 },
]

/* ── Page ───────────────────────────────────────────────────────────────── */

export function DashboardPage() {
  const [rangeKey, setRangeKey] = useState<RangeKey>('6h')
  const range = RANGES.find((r) => r.key === rangeKey) || RANGES[1]

  /* — queries (one request per source, no fan-out) — */
  const summary = useQuery<DeviceSummary>({
    queryKey: ['noc', 'summary'],
    queryFn: async () => (await api.get('/devices/summary')).data,
    refetchInterval: 15_000,
  }).data

  const alertStats = useQuery<{ active: number; critical: number; warning: number; info: number; resolved_today: number }>({
    queryKey: ['noc', 'alert-stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  }).data

  const activeAlerts = useQuery<AlertRow[]>({
    queryKey: ['noc', 'alerts'],
    queryFn: async () => {
      const r = (await api.get('/alerts?status=active&limit=8')).data
      return Array.isArray(r) ? r : r?.data || []
    },
    refetchInterval: 15_000,
  }).data

  const devices = useQuery<{ data: Device[] }>({
    queryKey: ['noc', 'devices'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    refetchInterval: 30_000,
  }).data?.data

  const groups = useQuery<Array<{ id: string; name: string }>>({
    queryKey: ['noc', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    staleTime: 5 * 60_000,
  }).data

  const uptime = useQuery<{ devices: Record<string, number> }>({
    queryKey: ['noc', 'uptime', range.hours],
    queryFn: async () => (await api.get(`/devices/dashboard/uptime-stats?hours=${range.hours}`)).data,
    refetchInterval: 60_000,
  }).data

  const exec = useQuery<ExecutiveData>({
    queryKey: ['noc', 'exec', range.days],
    queryFn: async () => (await api.get(`/reports/data/executive?days=${range.days}`)).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const metrics = useQuery<CurrentMetrics>({
    queryKey: ['noc', 'current-metrics'],
    queryFn: async () => (await api.get('/devices/current-metrics')).data,
    refetchInterval: 30_000,
  }).data

  const nfOverview = useQuery<NetflowOverview>({
    queryKey: ['noc', 'nf-overview', range.minutes],
    queryFn: async () => (await api.get(`/netflow/overview?minutes=${range.minutes}`)).data,
    refetchInterval: 30_000,
  }).data

  const nfSeries = useQuery<NetflowPoint[]>({
    queryKey: ['noc', 'nf-series', range.minutes],
    queryFn: async () => (await api.get(`/netflow/timeseries?minutes=${range.minutes}&points=48`)).data,
    refetchInterval: 30_000,
  }).data

  const talkers = useQuery<TopTalker[]>({
    queryKey: ['noc', 'nf-talkers', range.minutes],
    queryFn: async () => (await api.get(`/netflow/top-talkers?minutes=${range.minutes}&limit=6`)).data,
    refetchInterval: 60_000,
  }).data

  const protocols = useQuery<ProtocolRow[]>({
    queryKey: ['noc', 'nf-protocols', range.minutes],
    queryFn: async () => (await api.get(`/netflow/protocols?minutes=${range.minutes}`)).data,
    refetchInterval: 60_000,
  }).data

  const nfIfaces = useQuery<NetflowInterface[]>({
    queryKey: ['noc', 'nf-ifaces', range.minutes],
    queryFn: async () => (await api.get(`/netflow/interfaces?minutes=${range.minutes}&limit=6`)).data,
    refetchInterval: 60_000,
  }).data

  const services = useQuery<ServiceSummary>({
    queryKey: ['noc', 'services'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: 30_000,
  }).data

  const alertCounts = useQuery<DeviceAlertCounts>({
    queryKey: ['noc', 'alert-counts'],
    queryFn: async () => (await api.get('/alerts/device-counts?hours=24')).data,
    refetchInterval: 60_000,
  }).data

  /* — derived — */

  const total = summary?.total ?? 0
  const up = summary?.up ?? 0
  const trafficSpark = useMemo(() => (nfSeries || []).map((p) => p.bps), [nfSeries])

  const trafficStats = useMemo(() => {
    const pts = nfSeries || []
    if (!pts.length) return { avg: 0, peak: 0, p95: 0 }
    const vals = pts.map((p) => p.bps).sort((a, b) => a - b)
    return {
      avg: vals.reduce((s, v) => s + v, 0) / vals.length,
      peak: vals[vals.length - 1],
      p95: vals[Math.floor(vals.length * 0.95)] ?? vals[vals.length - 1],
    }
  }, [nfSeries])

  const chartData = useMemo(() => {
    const fmt = range.minutes <= 1440
      ? (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : (d: Date) => d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    return (nfSeries || []).map((p) => ({ t: fmt(new Date(p.ts)), mbps: p.bps / 1_000_000, flows: p.flows }))
  }, [nfSeries, range.minutes])

  // Problem devices: down/degraded first, then most-alerting (real alert counts).
  const problemDevices = useMemo(() => {
    const ac = alertCounts?.devices || {}
    const rows = (devices || [])
      .map((d) => {
        const a = ac[d.id]
        const sev = d.status === 'down' ? 3 : d.status === 'degraded' ? 2 : (a?.active || 0) > 0 ? 1 : 0
        return { d, alerts: a?.active || 0, critical: a?.critical || 0, sev }
      })
      .filter((r) => r.sev > 0)
      .sort((a, b) => b.sev - a.sev || b.alerts - a.alerts)
    return rows.slice(0, 5)
  }, [devices, alertCounts])

  const protoMix = useMemo(() => {
    const rows = (protocols || []).slice(0, 5)
    const sum = rows.reduce((s, r) => s + r.bytes, 0) || 1
    const colors = ['#22d3ee', '#a78bfa', '#f59e0b', '#34d399', '#f472b6']
    return rows.map((r, i) => ({ ...r, pct: (r.bytes / sum) * 100, color: colors[i % colors.length] }))
  }, [protocols])

  const topApps = useMemo(() => {
    const agg = new Map<string, number>()
    for (const p of protocols || []) {
      for (const port of p.ports || []) {
        if (!port.application) continue
        agg.set(port.application, (agg.get(port.application) || 0) + (port.bytes || 0))
      }
    }
    return [...agg.entries()].filter(([, b]) => b > 0).sort((a, b) => b[1] - a[1]).slice(0, 5)
  }, [protocols])

  const availRings = useMemo(() => {
    const ut = uptime?.devices || {}
    const ds = devices || []
    const ring = (name: string, members: Device[]) => ({
      name,
      pct: members.length ? members.reduce((s, d) => s + (ut[d.id] ?? 0), 0) / members.length : 0,
      up: members.filter((d) => d.status === 'up').length,
      total: members.length,
    })
    const out = ds.length ? [ring('All Devices', ds)] : []
    for (const g of groups || []) {
      const members = ds.filter((d) => d.group_id === g.id)
      if (members.length) out.push(ring(g.name, members))
    }
    if (out.length < 4) {
      const seen = new Set<string>()
      for (const d of ds) {
        if (out.length >= 4) break
        const loc = d.location || 'Unknown'
        if (seen.has(loc)) continue
        seen.add(loc)
        out.push(ring(loc, ds.filter((x) => (x.location || 'Unknown') === loc)))
      }
    }
    return out.slice(0, 4)
  }, [devices, groups, uptime])

  const fleet = useMemo(() => {
    const dm = metrics?.devices || {}
    const byId = new Map((devices || []).map((d) => [d.id, d]))
    const cpus: number[] = []
    const mems: number[] = []
    const hot: Array<{ d: Device; cpu: number; mem: number }> = []
    for (const [id, m] of Object.entries(dm)) {
      const cpu = typeof m.cpu === 'number' ? m.cpu : null
      const mem = typeof m.memory === 'number' ? m.memory : null
      if (cpu != null) cpus.push(cpu)
      if (mem != null) mems.push(mem)
      const d = byId.get(id)
      if (d && (cpu != null || mem != null)) hot.push({ d, cpu: cpu ?? 0, mem: mem ?? 0 })
    }
    hot.sort((a, b) => Math.max(b.cpu, b.mem) - Math.max(a.cpu, a.mem))
    return {
      avgCpu: cpus.length ? cpus.reduce((s, v) => s + v, 0) / cpus.length : 0,
      avgMem: mems.length ? mems.reduce((s, v) => s + v, 0) / mems.length : 0,
      sampled: cpus.length,
      hottest: hot.slice(0, 4),
    }
  }, [metrics, devices])

  /* — render — */

  return (
    <div className="space-y-4 animate-fade-in">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-2xl font-semibold tracking-tight">Network Operations</h1>
            <span className="flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[10px] font-bold tracking-widest text-success">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
              </span>
              LIVE
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted">
            {nfOverview ? <>{fmtCount(nfOverview.flows)} flows · {fmtCount(nfOverview.src_hosts)} active hosts · {nfOverview.exporters} exporters</> : 'Live infrastructure & traffic overview'}
            {' · '}refreshes every 15s
          </p>
        </div>
        <div className="flex items-center gap-2">
          <LiveClock />
          <RangePills value={rangeKey} onChange={setRangeKey} />
        </div>
      </div>

      {/* KPI strip — every figure is real */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <KpiCard to="/devices" label="Devices Online" icon={<Server className="h-4 w-4" />} accent="success"
          value={<>{up}<span className="text-base font-medium text-muted">/{total}</span></>}
          sub={summary ? <StatusDots s={summary} /> : null}
          foot={<StatusBar s={summary} />}
        />
        <KpiCard to="/alerts" label="Active Alerts" icon={<AlertTriangle className="h-4 w-4" />}
          accent={(alertStats?.critical ?? 0) > 0 ? 'danger' : (alertStats?.active ?? 0) > 0 ? 'warning' : 'success'}
          value={fmtCount(alertStats?.active ?? 0)}
          sub={<span className="flex items-center gap-2 text-[10.5px]">
            <span className="text-danger">{alertStats?.critical ?? 0} crit</span>
            <span className="text-warning">{alertStats?.warning ?? 0} warn</span>
          </span>}
          foot={<div className="text-[10px] text-muted">{alertStats?.resolved_today ?? 0} resolved today</div>}
        />
        <KpiCard to="/netflow" label="Traffic Now" icon={<Waves className="h-4 w-4" />} accent="info"
          value={fmtBps(nfOverview?.current_bps ?? 0)}
          sub={<span className="text-[10.5px] text-muted">peak {fmtBps(trafficStats.peak)}</span>}
          foot={<Sparkline values={trafficSpark.slice(-30)} color="#22d3ee" width={250} height={30} />}
        />
        <KpiCard to="/availability" label="Availability" icon={<HeartPulse className="h-4 w-4" />}
          accent={(exec?.kpis?.availability_pct ?? 100) >= 99.5 ? 'success' : 'warning'}
          value={exec?.kpis?.availability_pct != null ? `${exec.kpis.availability_pct.toFixed(2)}%` : '—'}
          sub={exec && (() => {
            // delta is null when there is no prior window to compare against (fresh install)
            const delta = exec.kpis.availability_delta_pct
            if (delta == null) {
              return <span className="text-[10.5px] text-muted">SLA {exec.kpis.sla_target_pct}%</span>
            }
            return (
              <span className={`flex items-center gap-0.5 text-[10.5px] ${delta >= 0 ? 'text-success' : 'text-danger'}`}>
                {delta >= 0 ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                {delta >= 0 ? '+' : ''}{delta.toFixed(2)}% · SLA {exec.kpis.sla_target_pct}%
              </span>
            )
          })()}
          foot={<Sparkline values={(exec?.availability_trend || []).flatMap((p) => (p.availability_pct == null ? [] : [p.availability_pct]))} color="#34d399" width={250} height={30} />}
        />
        <KpiCard to="/services" label="Services" icon={<Shield className="h-4 w-4" />}
          accent={(services?.down ?? 0) > 0 ? 'danger' : 'success'}
          value={services ? <>{services.up}<span className="text-base font-medium text-muted">/{services.total}</span></> : '—'}
          sub={services && services.down > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{services.down} down</span>
            : <span className="text-[10.5px] text-success">all passing</span>}
          foot={<div className="text-[10px] text-muted">HTTP · TCP · DNS probes</div>}
        />
        <KpiCard to="/devices" label="Fleet Load" icon={<Cpu className="h-4 w-4" />} accent="accent"
          value={`${fleet.avgCpu.toFixed(0)}%`}
          sub={<span className="text-[10.5px] text-muted">mem {fleet.avgMem.toFixed(0)}% · {fleet.sampled} devices</span>}
          foot={<DualBar a={fleet.avgCpu} b={fleet.avgMem} />}
        />
      </div>

      {/* Traffic chart + health */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        <Card className="xl:col-span-8">
          <SectionHeader icon={<Activity className="h-3.5 w-3.5" />} title="Network Traffic"
            hint={`NetFlow · ${range.label} · ${nfOverview ? fmtBytes(nfOverview.bytes) + ' total' : ''}`}
            right={nfOverview?.top_protocol && (
              <span className="rounded-md bg-surface2/80 px-2 py-0.5 font-mono text-[10px] text-text2">
                top: {nfOverview.top_protocol.name} · {fmtBytes(nfOverview.top_protocol.bytes)}
              </span>
            )}
          />
          <div className="grid grid-cols-4 gap-2 px-4 pt-3 text-center">
            <MiniStat label="Current" value={fmtBps(nfOverview?.current_bps ?? 0)} tone="text-info" />
            <MiniStat label="Average" value={fmtBps(trafficStats.avg)} tone="text-text" />
            <MiniStat label="95th pct" value={fmtBps(trafficStats.p95)} tone="text-accent" />
            <MiniStat label="Peak" value={fmtBps(trafficStats.peak)} tone="text-warning" />
          </div>
          <div className="h-[230px] px-2 pb-2 pt-1">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 8, right: 14, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="noc-traffic" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#22d3ee" stopOpacity={0.5} />
                    <stop offset="60%" stopColor="#0ea5e9" stopOpacity={0.12} />
                    <stop offset="100%" stopColor="#0ea5e9" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.5} strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="t" stroke="rgb(var(--muted))" fontSize={10} tickLine={false} axisLine={false} interval="preserveStartEnd" minTickGap={42} />
                <YAxis stroke="rgb(var(--muted))" fontSize={10} tickLine={false} axisLine={false} width={44} tickFormatter={(v) => fmtMbpsShort(v)} />
                <Tooltip
                  contentStyle={{ backgroundColor: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 8, fontSize: 12, color: 'rgb(var(--text))' }}
                  formatter={(v: number | string, name: string) => name === 'mbps' ? [fmtBps(Number(v) * 1_000_000), 'Throughput'] : [fmtCount(Number(v)), 'Flows']}
                />
                <Area type="monotone" dataKey="mbps" stroke="#22d3ee" strokeWidth={2.2} fill="url(#noc-traffic)" isAnimationActive={false} dot={false} activeDot={{ r: 3.5, fill: '#22d3ee' }} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<Gauge className="h-3.5 w-3.5" />} title="Device Health" hint={`${total} monitored`} />
          <div className="flex items-center gap-4 px-5 pt-4">
            <HealthDonut s={summary} />
            <div className="flex-1 space-y-1.5">
              <HealthRow color="bg-success" label="Up" value={summary?.up ?? 0} total={total} />
              <HealthRow color="bg-warning" label="Degraded" value={summary?.degraded ?? 0} total={total} />
              <HealthRow color="bg-danger" label="Down" value={summary?.down ?? 0} total={total} />
              <HealthRow color="bg-info" label="Maintenance" value={summary?.maintenance ?? 0} total={total} />
              <HealthRow color="bg-muted" label="Unknown" value={summary?.unknown ?? 0} total={total} />
            </div>
          </div>
          <div className="mt-3 border-t border-border px-4 pb-3 pt-2">
            <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Needs attention</div>
            {problemDevices.length === 0 && (
              <div className="py-3 text-center text-xs text-muted">All devices healthy 🎉</div>
            )}
            <div className="space-y-1">
              {problemDevices.map(({ d, alerts, critical }) => (
                <Link key={d.id} to={`/devices/${d.id}`} className="flex items-center gap-2 rounded-md px-2 py-1 text-xs transition hover:bg-surface2/60">
                  <span className={`h-2 w-2 shrink-0 rounded-full ${d.status === 'down' ? 'bg-danger' : d.status === 'degraded' ? 'bg-warning' : critical > 0 ? 'bg-danger' : 'bg-warning'}`} />
                  <span className="min-w-0 flex-1 truncate font-medium text-text">{d.hostname}</span>
                  {alerts > 0 && <span className={`rounded px-1 font-mono text-[10px] font-bold ${critical > 0 ? 'bg-danger/15 text-danger' : 'bg-warning/15 text-warning'}`}>{alerts}</span>}
                  <span className="shrink-0 text-[10px] capitalize text-muted">{d.status}</span>
                </Link>
              ))}
            </div>
          </div>
        </Card>
      </div>

      {/* Talkers · Interfaces · Protocols */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-12">
        <Card className="xl:col-span-4">
          <SectionHeader icon={<Globe className="h-3.5 w-3.5" />} title="Top Talkers" hint={range.label}
            right={<Link to="/netflow" className="text-xs text-primary hover:underline">NetFlow →</Link>} />
          <div className="space-y-2.5 px-4 pb-4 pt-3">
            {!talkers?.length && <Empty text="No flow records in this window" />}
            {(talkers || []).map((t, i) => {
              const max = talkers![0].bytes || 1
              const app = t.ports?.[0]?.application || t.ports?.[0]?.service
              return (
                <div key={t.ip}>
                  <div className="flex items-baseline justify-between text-xs">
                    <span className="flex items-center gap-1.5 font-mono font-medium text-text">
                      <span className="w-3 text-[9px] text-muted">{i + 1}</span>{t.ip}
                      {app && <span className="rounded bg-surface2 px-1 py-px text-[8.5px] font-sans text-muted">{app}</span>}
                    </span>
                    <span className="font-mono text-[11px] font-semibold tabular-nums text-text2">{fmtBytes(t.bytes)}</span>
                  </div>
                  <InOutBar tx={t.src_bytes} rx={t.dst_bytes} scale={max} />
                </div>
              )
            })}
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<Network className="h-3.5 w-3.5" />} title="Busiest Interfaces" hint={range.label} />
          <div className="space-y-2.5 px-4 pb-4 pt-3">
            {!nfIfaces?.length && <Empty text="Awaiting interface telemetry" />}
            {(nfIfaces || []).map((f) => {
              const max = nfIfaces![0].bytes || 1
              return (
                <div key={`${f.exporter_ip}-${f.ifindex}`}>
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="min-w-0 truncate">
                      <span className="font-mono font-medium text-text">{f.display_name}</span>
                      <span className="ml-1.5 text-[10px] text-muted">{f.device_hostname || f.exporter_ip}{f.if_alias ? ` · ${f.if_alias}` : ''}</span>
                    </span>
                    <span className="shrink-0 font-mono text-[11px] font-semibold tabular-nums text-text2">{fmtBytes(f.bytes)}</span>
                  </div>
                  <InOutBar tx={f.out_bytes} rx={f.in_bytes} scale={max} />
                </div>
              )
            })}
            {!!nfIfaces?.length && (
              <div className="flex items-center gap-3 pt-1 text-[9.5px] text-muted">
                <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded-full bg-sky-400" /> inbound</span>
                <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded-full bg-fuchsia-400" /> outbound</span>
              </div>
            )}
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<Radio className="h-3.5 w-3.5" />} title="Protocols & Applications" hint={range.label} />
          <div className="px-4 pb-4 pt-3">
            {/* stacked share bar */}
            <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface2">
              {protoMix.map((p) => (
                <div key={p.protocol} title={`${p.name} ${p.pct.toFixed(1)}%`} style={{ width: `${Math.max(1.5, p.pct)}%`, background: p.color }} />
              ))}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
              {protoMix.map((p) => (
                <span key={p.protocol} className="flex items-center gap-1.5 text-[10.5px] text-text2">
                  <span className="h-2 w-2 rounded-sm" style={{ background: p.color }} />
                  {p.name} <span className="font-mono text-muted">{p.pct.toFixed(0)}%</span>
                </span>
              ))}
            </div>
            <div className="mt-3 border-t border-border pt-2">
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Top applications</div>
              <div className="space-y-1.5">
                {!topApps.length && <Empty text="No classified application traffic" />}
                {topApps.map(([app, bytes]) => {
                  const max = topApps[0][1] || 1
                  return (
                    <div key={app} className="flex items-center gap-2 text-xs">
                      <span className="w-32 truncate text-text2" title={app}>{app}</span>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
                        <div className="h-full rounded-full bg-gradient-to-r from-indigo-400 to-violet-500" style={{ width: `${Math.max(3, (bytes / max) * 100)}%` }} />
                      </div>
                      <span className="w-16 text-right font-mono text-[10.5px] tabular-nums text-muted">{fmtBytes(bytes)}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* Alert feed · availability · fleet resources */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-12">
        <Card className="xl:col-span-5">
          <SectionHeader icon={<AlertTriangle className="h-3.5 w-3.5" />} title="Live Alert Feed"
            right={<Link to="/alerts" className="text-xs text-primary hover:underline">View all →</Link>} />
          <div className="px-3 pb-3 pt-2">
            {!activeAlerts?.length && <Empty text="No active alerts 🎉" />}
            <div className="space-y-0.5">
              {(activeAlerts || []).map((a) => (
                <div key={a.id} className="group flex gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-surface2/50">
                  <div className={`mt-0.5 w-1 shrink-0 self-stretch rounded-full ${a.severity === 'critical' ? 'bg-danger' : a.severity === 'warning' ? 'bg-warning' : 'bg-info'}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-xs font-semibold text-text">{a.device_hostname}</span>
                      <span className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(a.triggered_at)}</span>
                    </div>
                    <div className="truncate text-[11px] text-text2" title={a.message}>{prettyMessage(a.message)}</div>
                  </div>
                  <Badge variant={a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info'} className="self-center capitalize">{a.severity}</Badge>
                </div>
              ))}
            </div>
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<HeartPulse className="h-3.5 w-3.5" />} title="Availability" hint={`last ${range.label.toLowerCase()}`} />
          <div className="grid grid-cols-2 gap-2 px-3 pb-4 pt-3">
            {availRings.map((g) => (
              <div key={g.name} className="flex flex-col items-center gap-1.5 rounded-lg border border-border/70 bg-surface2/30 px-2 py-3">
                <RingGauge value={g.pct} color={g.pct >= 99 ? 'success' : g.pct >= 95 ? 'warning' : 'danger'} size={96} stroke={9} sub={`${g.up}/${g.total} UP`} />
                <div className="max-w-full truncate text-center text-xs font-semibold" title={g.name}>{g.name}</div>
              </div>
            ))}
          </div>
        </Card>

        <Card className="xl:col-span-3">
          <SectionHeader icon={<Flame className="h-3.5 w-3.5" />} title="Fleet Resources" hint={`${fleet.sampled} sampled`} />
          <div className="space-y-2 px-4 pb-4 pt-3">
            <ResourceRow label="CPU average" value={fleet.avgCpu} />
            <ResourceRow label="Memory average" value={fleet.avgMem} />
            <div className="border-t border-border pt-2">
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Hottest devices</div>
              <div className="space-y-1.5">
                {!fleet.hottest.length && <Empty text="Awaiting SNMP samples" />}
                {fleet.hottest.map(({ d, cpu, mem }) => (
                  <Link key={d.id} to={`/devices/${d.id}`} className="flex items-center gap-2 rounded-md px-1 py-1 text-xs transition hover:bg-surface2/50">
                    <span className="min-w-0 flex-1 truncate text-text2">{d.hostname}</span>
                    <span className="shrink-0 font-mono text-[10px] tabular-nums">
                      <span className={cpu >= 80 ? 'text-danger' : cpu >= 60 ? 'text-warning' : 'text-muted'}>C {cpu.toFixed(0)}%</span>
                      <span className="mx-1 text-muted">·</span>
                      <span className={mem >= 80 ? 'text-danger' : mem >= 60 ? 'text-warning' : 'text-muted'}>M {mem.toFixed(0)}%</span>
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}

/* ── Building blocks ────────────────────────────────────────────────────── */

function SectionHeader({ icon, title, hint, right }: { icon?: React.ReactNode; title: string; hint?: string; right?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
      <div className="flex min-w-0 items-center gap-2">
        {icon && <span className="text-muted">{icon}</span>}
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        {hint && <span className="truncate text-[10px] uppercase tracking-wider text-muted">{hint}</span>}
      </div>
      {right}
    </div>
  )
}

const KPI_ACCENT: Record<string, string> = {
  success: 'from-emerald-500/80 to-green-500/80',
  danger: 'from-rose-500/80 to-red-500/80',
  warning: 'from-amber-500/80 to-orange-500/80',
  info: 'from-cyan-500/80 to-sky-500/80',
  accent: 'from-fuchsia-500/80 to-purple-500/80',
}

function KpiCard({ to, label, icon, accent, value, sub, foot }: {
  to: string
  label: string
  icon: React.ReactNode
  accent: keyof typeof KPI_ACCENT
  value: React.ReactNode
  sub?: React.ReactNode
  foot?: React.ReactNode
}) {
  return (
    <Link to={to} className="group">
      <Card className="relative h-full overflow-hidden transition group-hover:border-primary/40">
        <span className={`absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r ${KPI_ACCENT[accent]}`} />
        <div className="flex items-start justify-between gap-2 px-4 pt-3">
          <div className="min-w-0">
            <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted">{label}</div>
            <div className="mt-1 text-2xl font-bold tabular-nums leading-none text-text">{value}</div>
            <div className="mt-1 min-h-[14px]">{sub}</div>
          </div>
          <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-white shadow ${KPI_ACCENT[accent]}`}>
            {icon}
          </span>
        </div>
        <div className="px-4 pb-3 pt-1.5">{foot}</div>
      </Card>
    </Link>
  )
}

/** Fleet status as a single proportional bar (up/degraded/down/maint/unknown). */
function StatusBar({ s }: { s?: DeviceSummary }) {
  if (!s || !s.total) return <div className="h-1.5 rounded-full bg-surface2" />
  const seg = (n: number) => `${(n / s.total) * 100}%`
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <div style={{ width: seg(s.up) }} className="bg-success" />
      <div style={{ width: seg(s.degraded) }} className="bg-warning" />
      <div style={{ width: seg(s.down) }} className="bg-danger" />
      <div style={{ width: seg(s.maintenance) }} className="bg-info" />
      <div style={{ width: seg(s.unknown) }} className="bg-muted/60" />
    </div>
  )
}

function StatusDots({ s }: { s: DeviceSummary }) {
  const parts = [
    s.down > 0 && <span key="d" className="text-danger">{s.down} down</span>,
    s.degraded > 0 && <span key="g" className="text-warning">{s.degraded} degraded</span>,
    s.unknown > 0 && <span key="u" className="text-muted">{s.unknown} unknown</span>,
  ].filter(Boolean)
  return <span className="flex items-center gap-2 text-[10.5px]">{parts.length ? parts : <span className="text-success">all reachable</span>}</span>
}

function DualBar({ a, b }: { a: number; b: number }) {
  return (
    <div className="space-y-1">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface2">
        <div className="h-full rounded-full bg-gradient-to-r from-fuchsia-400 to-purple-500" style={{ width: `${Math.min(100, Math.max(2, a))}%` }} />
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface2">
        <div className="h-full rounded-full bg-gradient-to-r from-cyan-400 to-sky-500" style={{ width: `${Math.min(100, Math.max(2, b))}%` }} />
      </div>
    </div>
  )
}

function MiniStat({ label, value, tone }: { label: string; value: string; tone: string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-surface2/30 px-2 py-1.5">
      <div className="text-[9px] font-bold uppercase tracking-wider text-muted">{label}</div>
      <div className={`font-mono text-[13px] font-semibold tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}

function HealthDonut({ s }: { s?: DeviceSummary }) {
  const total = s?.total || 0
  const slices = [
    { v: s?.up ?? 0, c: '#22c55e' },
    { v: s?.degraded ?? 0, c: '#f59e0b' },
    { v: s?.down ?? 0, c: '#ef4444' },
    { v: s?.maintenance ?? 0, c: '#3b82f6' },
    { v: s?.unknown ?? 0, c: '#6b7280' },
  ].filter((x) => x.v > 0)
  const sum = slices.reduce((acc, x) => acc + x.v, 0) || 1
  const size = 132, stroke = 13
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  let offset = 0
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgb(var(--surface3))" strokeWidth={stroke} />
        {slices.map((x, i) => {
          const len = (x.v / sum) * c
          const el = (
            <circle key={i} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={x.c} strokeWidth={stroke}
              strokeDasharray={`${Math.max(0, len - 1.5)} ${c - Math.max(0, len - 1.5)}`} strokeDashoffset={-offset}
              transform={`rotate(-90 ${size / 2} ${size / 2})`} strokeLinecap="butt" />
          )
          offset += len
          return el
        })}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-2xl font-bold tabular-nums">{total}</div>
        <div className="text-[9px] uppercase tracking-widest text-muted">devices</div>
      </div>
    </div>
  )
}

function HealthRow({ color, label, value, total }: { color: string; label: string; value: number; total: number }) {
  const pct = total ? (value / total) * 100 : 0
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} />
      <span className="w-20 text-text2">{label}</span>
      <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface2">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.max(value > 0 ? 3 : 0, pct)}%` }} />
      </div>
      <span className="w-6 text-right font-mono text-[11px] font-semibold tabular-nums">{value}</span>
    </div>
  )
}

/** Two-direction byte bar: outbound (fuchsia) vs inbound (sky), scaled to the list max. */
function InOutBar({ tx, rx, scale }: { tx: number; rx: number; scale: number }) {
  const w = (v: number) => `${Math.max(0.5, (v / (scale || 1)) * 100)}%`
  return (
    <div className="mt-1 space-y-px">
      <div className="h-[5px] w-full overflow-hidden rounded-full bg-surface2/80">
        <div className="h-full rounded-full bg-gradient-to-r from-sky-400 to-cyan-500" style={{ width: w(rx) }} />
      </div>
      <div className="h-[5px] w-full overflow-hidden rounded-full bg-surface2/80">
        <div className="h-full rounded-full bg-gradient-to-r from-fuchsia-400 to-purple-500" style={{ width: w(tx) }} />
      </div>
    </div>
  )
}

function ResourceRow({ label, value }: { label: string; value: number }) {
  const v = Math.max(0, Math.min(100, value))
  const tone = v >= 80 ? 'from-rose-400 to-red-500' : v >= 60 ? 'from-amber-400 to-orange-500' : 'from-emerald-400 to-green-500'
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-text2">{label}</span>
        <span className="font-mono text-[12px] font-semibold tabular-nums">{v.toFixed(1)}%</span>
      </div>
      <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-surface2">
        <div className={`h-full rounded-full bg-gradient-to-r ${tone} transition-all duration-700`} style={{ width: `${Math.max(2, v)}%` }} />
      </div>
    </div>
  )
}

function RangePills({ value, onChange }: { value: RangeKey; onChange: (k: RangeKey) => void }) {
  return (
    <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
      <Clock className="ml-1 h-3.5 w-3.5 text-muted" />
      {RANGES.map((r) => (
        <button key={r.key} onClick={() => onChange(r.key)}
          className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${r.key === value ? 'bg-primary text-white shadow-sm' : 'text-muted hover:bg-surface3 hover:text-text'}`}>
          {r.label}
        </button>
      ))}
    </div>
  )
}

function LiveClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return (
    <span className="hidden rounded-md border border-border bg-surface2/60 px-2.5 py-1 font-mono text-xs font-semibold tabular-nums text-text2 md:inline-block">
      {now.toLocaleTimeString([], { hour12: false })}
    </span>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="py-4 text-center text-xs text-muted">{text}</div>
}

/* ── Formatters ─────────────────────────────────────────────────────────── */

function prettyMessage(msg: string) {
  return msg.replace(/^\[[^\]]+\]\s*/, '')
}

function fmtBps(bps: number): string {
  if (!isFinite(bps) || bps <= 0) return '0 bps'
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let i = 0, v = bps
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`
}

function fmtBytes(b: number): string {
  if (!isFinite(b) || b <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0, v = b
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

function fmtCount(n: number): string {
  if (!isFinite(n) || n <= 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}K`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return `${Math.round(n)}`
}

function fmtMbpsShort(v: number): string {
  if (!isFinite(v) || v <= 0) return '0'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}G`
  if (v >= 1) return `${v.toFixed(0)}M`
  return `${(v * 1000).toFixed(0)}K`
}
