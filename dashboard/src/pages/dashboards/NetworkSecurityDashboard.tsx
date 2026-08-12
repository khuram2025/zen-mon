/**
 * Network & Security team dashboard.
 *
 * One screen answering the team's standing questions:
 *   is the network up · what is on fire · what is the traffic doing ·
 *   which links are hot · are WAN paths healthy · anything hostile inside ·
 *   are configs backed up.
 *
 * Every figure is real telemetry:
 *   /devices/*, /alerts/*, /netflow/*, /link-utilization, /netpath/summary,
 *   /traps/stats, /udt/summary, /ncm/overview, /reports/data/executive
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  Cable,
  FileCode,
  Gauge,
  Globe,
  HeartPulse,
  Inbox,
  Radio,
  Router as RouterIcon,
  ScanSearch,
  ShieldAlert,
  Waves,
  Waypoints,
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
import { Card } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { relativeTime } from '@/lib/utils'
import { Sparkline } from '@/components/dashboard/Sparkline'
import {
  CATEGORICAL,
  Empty,
  KpiCard,
  LiveClock,
  PctBar,
  RangeKey,
  RANGES,
  RangePills,
  SectionHeader,
  SERIES,
  ShareBar,
  TeamHeader,
  chartTooltipStyle,
  fmtBps,
  fmtBytes,
  fmtCount,
} from './shared'

/* ── Types (server response shapes) ─────────────────────────────────────── */

type DeviceSummary = { total: number; up: number; down: number; degraded: number; unknown: number; maintenance: number }

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  status: 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance'
}

type AlertRow = {
  id: string
  device_id: string | null
  server_id: string | null
  service_check_id: string | null
  device_hostname: string | null
  severity: string
  message: string
  triggered_at: string
}

type NetflowOverview = {
  bytes: number; packets: number; flows: number; exporters: number
  src_hosts: number; dst_hosts: number; current_bps: number
  top_protocol?: { protocol: number; name: string; bytes: number } | null
}

type NetflowPoint = { ts: number; bps: number; flows: number }

type TopTalker = {
  ip: string; bytes: number; flows: number; src_bytes: number; dst_bytes: number
  ports?: Array<{ port: number; service: string; application: string }>
}

type ProtocolRow = { protocol: number; name: string; bytes: number; flows: number }

type Anomaly = {
  id: string
  kind: string
  severity: 'critical' | 'warning' | string
  title: string
  description: string
  metric: number
  metric_label: string
}

type LinkItem = {
  device_id: string
  if_index: number
  hostname: string | null
  if_name: string | null
  if_alias: string | null
  oper_status: string
  util_pct: number | null
  peak_util_pct: number | null
  in_bps: number
  out_bps: number
  total_bps: number
  issues?: string[]
  health?: string
}

type LinkResponse = {
  items: LinkItem[]
  summary: {
    total: number
    high_util: number
    warning_util: number
    avg_util: number | null
    unhealthy: number
  }
}

type NetPathSummary = {
  total_probes: number
  enabled: number
  ok: number
  degraded: number
  unreachable: number
  path_changes_24h: number
  recent_events: Array<{
    id: number
    probe_id: string
    probe_name: string
    event_type: string
    severity: string
    details: Record<string, unknown> | null
    created_at: string
  }>
}

type TrapStats = { total: number; info: number; warning: number; critical: number }

type UdtSummary = {
  total_endpoints: number
  active_endpoints: number
  new_24h: number
  rogue: number
  watched: number
  logins_24h: number
}

type NcmOverview = { total_devices: number; backed_up: number; enrolled: number }

type ExecutiveData = {
  kpis: { availability_pct: number | null; availability_delta_pct: number | null; sla_target_pct: number }
  availability_trend: Array<{ ts: string; availability_pct: number | null }>
}

type DeviceAlertCounts = { devices: Record<string, { active: number; critical: number; warning: number }> }

/* ── Page ───────────────────────────────────────────────────────────────── */

export function NetworkSecurityDashboard() {
  const [rangeKey, setRangeKey] = useState<RangeKey>('6h')
  const range = RANGES.find((r) => r.key === rangeKey) || RANGES[1]

  const summary = useQuery<DeviceSummary>({
    queryKey: ['netsec', 'summary'],
    queryFn: async () => (await api.get('/devices/summary')).data,
    refetchInterval: 15_000,
  }).data

  const devices = useQuery<{ data: Device[] }>({
    queryKey: ['netsec', 'devices'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    refetchInterval: 30_000,
  }).data?.data

  const alertStats = useQuery<{ active: number; critical: number; warning: number; resolved_today: number }>({
    queryKey: ['netsec', 'alert-stats'],
    queryFn: async () => (await api.get('/alerts/stats')).data,
    refetchInterval: 15_000,
  }).data

  const activeAlerts = useQuery<{ data: AlertRow[] }>({
    queryKey: ['netsec', 'alerts'],
    queryFn: async () => (await api.get('/alerts?status=active&limit=50')).data,
    refetchInterval: 15_000,
  }).data?.data

  const alertCounts = useQuery<DeviceAlertCounts>({
    queryKey: ['netsec', 'alert-counts'],
    queryFn: async () => (await api.get('/alerts/device-counts?hours=24')).data,
    refetchInterval: 60_000,
  }).data

  const exec = useQuery<ExecutiveData>({
    queryKey: ['netsec', 'exec', range.days],
    queryFn: async () => (await api.get(`/reports/data/executive?days=${range.days}`)).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const nfOverview = useQuery<NetflowOverview>({
    queryKey: ['netsec', 'nf-overview', range.minutes],
    queryFn: async () => (await api.get(`/netflow/overview?minutes=${range.minutes}`)).data,
    refetchInterval: 30_000,
    retry: 1,
  }).data

  const nfSeries = useQuery<NetflowPoint[]>({
    queryKey: ['netsec', 'nf-series', range.minutes],
    queryFn: async () => (await api.get(`/netflow/timeseries?minutes=${range.minutes}&points=48`)).data,
    refetchInterval: 30_000,
    retry: 1,
  }).data

  const talkers = useQuery<TopTalker[]>({
    queryKey: ['netsec', 'nf-talkers', range.minutes],
    queryFn: async () => (await api.get(`/netflow/top-talkers?minutes=${range.minutes}&limit=6`)).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const protocols = useQuery<ProtocolRow[]>({
    queryKey: ['netsec', 'nf-protocols', range.minutes],
    queryFn: async () => (await api.get(`/netflow/protocols?minutes=${range.minutes}`)).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const anomalies = useQuery<{ findings: Anomaly[] } | Anomaly[]>({
    queryKey: ['netsec', 'nf-anomalies', range.hours],
    queryFn: async () => (await api.get(`/netflow/anomalies?hours=${Math.max(1, range.hours)}`)).data,
    refetchInterval: 120_000,
    retry: 1,
  }).data

  const links = useQuery<LinkResponse>({
    queryKey: ['netsec', 'links', range.hours],
    queryFn: async () => (await api.get(`/link-utilization?hours=${Math.max(1, range.hours)}&limit=7&sort=util`)).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const netpath = useQuery<NetPathSummary>({
    queryKey: ['netsec', 'netpath'],
    queryFn: async () => (await api.get('/netpath/summary')).data,
    refetchInterval: 30_000,
    retry: 1,
  }).data

  const traps = useQuery<TrapStats>({
    queryKey: ['netsec', 'traps'],
    queryFn: async () => (await api.get('/traps/stats?hours=24')).data,
    refetchInterval: 60_000,
    retry: false,
  }).data

  const udt = useQuery<UdtSummary>({
    queryKey: ['netsec', 'udt'],
    queryFn: async () => (await api.get('/udt/summary')).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const ncm = useQuery<NcmOverview>({
    queryKey: ['netsec', 'ncm'],
    queryFn: async () => (await api.get('/ncm/overview')).data,
    refetchInterval: 5 * 60_000,
    retry: 1,
  }).data

  /* — derived — */

  const total = summary?.total ?? 0
  const up = summary?.up ?? 0

  const findings = useMemo<Anomaly[]>(() => {
    if (!anomalies) return []
    return Array.isArray(anomalies) ? anomalies : anomalies.findings || []
  }, [anomalies])
  const criticalFindings = findings.filter((f) => f.severity === 'critical').length

  // The network team's feed: device-attached alerts (plus netpath events via rules).
  const netAlerts = useMemo(
    () => (activeAlerts || []).filter((a) => a.device_id != null || (!a.server_id && !a.service_check_id)).slice(0, 8),
    [activeAlerts],
  )

  const trafficSpark = useMemo(() => (nfSeries || []).map((p) => p.bps), [nfSeries])
  const trafficStats = useMemo(() => {
    const vals = (nfSeries || []).map((p) => p.bps).sort((a, b) => a - b)
    if (!vals.length) return { avg: 0, peak: 0, p95: 0 }
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
    return (nfSeries || []).map((p) => ({ t: fmt(new Date(p.ts)), mbps: p.bps / 1_000_000 }))
  }, [nfSeries, range.minutes])

  const protoMix = useMemo(() => {
    const rows = (protocols || []).slice(0, 5)
    const sum = rows.reduce((s, r) => s + r.bytes, 0) || 1
    return rows.map((r, i) => ({ ...r, pct: (r.bytes / sum) * 100, color: CATEGORICAL[i % CATEGORICAL.length] }))
  }, [protocols])

  const problemDevices = useMemo(() => {
    const ac = alertCounts?.devices || {}
    return (devices || [])
      .map((d) => {
        const a = ac[d.id]
        const sev = d.status === 'down' ? 3 : d.status === 'degraded' ? 2 : (a?.active || 0) > 0 ? 1 : 0
        return { d, alerts: a?.active || 0, critical: a?.critical || 0, sev }
      })
      .filter((r) => r.sev > 0)
      .sort((a, b) => b.sev - a.sev || b.alerts - a.alerts)
      .slice(0, 6)
  }, [devices, alertCounts])

  const backupPct = ncm && ncm.total_devices > 0 ? (ncm.backed_up / ncm.total_devices) * 100 : null

  /* — render — */

  return (
    <div className="space-y-4 animate-fade-in">
      <TeamHeader
        title="Network & Security"
        subtitle={
          nfOverview
            ? <>{fmtCount(nfOverview.flows)} flows · {fmtCount(nfOverview.src_hosts)} active hosts · {nfOverview.exporters} exporters · refreshes every 15s</>
            : 'Fleet reachability, traffic, WAN paths and threat signals · refreshes every 15s'
        }
        right={<><LiveClock /><RangePills value={rangeKey} onChange={setRangeKey} /></>}
      />

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <KpiCard to="/devices" label="Devices Online" icon={<RouterIcon className="h-4 w-4" />} accent="success"
          value={<>{up}<span className="text-base font-medium text-muted">/{total}</span></>}
          sub={summary && (summary.down > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{summary.down} down</span>
            : summary.degraded > 0
              ? <span className="text-[10.5px] text-warning">{summary.degraded} degraded</span>
              : <span className="text-[10.5px] text-success">all reachable</span>)}
          foot={<DeviceStatusBar s={summary} />}
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
          foot={<Sparkline values={trafficSpark.slice(-30)} color={SERIES.traffic} width={250} height={30} />}
        />
        <KpiCard to="/availability" label="Availability" icon={<HeartPulse className="h-4 w-4" />}
          accent={(exec?.kpis?.availability_pct ?? 100) >= 99.5 ? 'success' : 'warning'}
          value={exec?.kpis?.availability_pct != null ? `${exec.kpis.availability_pct.toFixed(2)}%` : '—'}
          sub={exec?.kpis?.availability_delta_pct != null
            ? <span className={`flex items-center gap-0.5 text-[10.5px] ${exec.kpis.availability_delta_pct >= 0 ? 'text-success' : 'text-danger'}`}>
                {exec.kpis.availability_delta_pct >= 0 ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                {exec.kpis.availability_delta_pct >= 0 ? '+' : ''}{exec.kpis.availability_delta_pct.toFixed(2)}%
              </span>
            : <span className="text-[10.5px] text-muted">SLA {exec?.kpis?.sla_target_pct ?? 99.9}%</span>}
          foot={<Sparkline values={(exec?.availability_trend || []).flatMap((p) => (p.availability_pct == null ? [] : [p.availability_pct]))} color="#34d399" width={250} height={30} />}
        />
        <KpiCard to="/link-utilization" label="Link Health" icon={<Cable className="h-4 w-4" />}
          accent={(links?.summary?.high_util ?? 0) > 0 ? 'danger' : (links?.summary?.warning_util ?? 0) > 0 ? 'warning' : 'success'}
          value={links ? fmtCount(links.summary.high_util + links.summary.warning_util) : '—'}
          sub={<span className="text-[10.5px] text-muted">links ≥50% util · avg {links?.summary?.avg_util != null ? `${links.summary.avg_util}%` : '—'}</span>}
          foot={<div className="text-[10px] text-muted">{links?.summary?.unhealthy ?? 0} with errors/discards/flaps</div>}
        />
        <KpiCard to="/netpath" label="NetPath" icon={<Waypoints className="h-4 w-4" />}
          accent={(netpath?.unreachable ?? 0) > 0 ? 'danger' : (netpath?.degraded ?? 0) > 0 ? 'warning' : 'success'}
          value={netpath ? <>{netpath.ok}<span className="text-base font-medium text-muted">/{netpath.enabled}</span></> : '—'}
          sub={netpath && (netpath.unreachable > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{netpath.unreachable} unreachable</span>
            : netpath.degraded > 0
              ? <span className="text-[10.5px] text-warning">{netpath.degraded} degraded</span>
              : <span className="text-[10.5px] text-success">paths healthy</span>)}
          foot={<div className="text-[10px] text-muted">{netpath?.path_changes_24h ?? 0} route changes · 24h</div>}
        />
        <KpiCard to="/netflow/anomalies" label="Threat Signals" icon={<ShieldAlert className="h-4 w-4" />}
          accent={criticalFindings > 0 ? 'danger' : findings.length > 0 ? 'warning' : 'success'}
          value={fmtCount(findings.length)}
          sub={findings.length
            ? <span className="text-[10.5px] text-danger">{criticalFindings} critical</span>
            : <span className="text-[10.5px] text-success">no detections</span>}
          foot={<div className="text-[10px] text-muted">scan · sweep · exfil · beacon checks</div>}
        />
        <KpiCard to="/udt" label="Rogue Endpoints" icon={<ScanSearch className="h-4 w-4" />}
          accent={(udt?.rogue ?? 0) > 0 ? 'warning' : 'success'}
          value={fmtCount(udt?.rogue ?? 0)}
          sub={<span className="text-[10.5px] text-muted">{udt?.new_24h ?? 0} new endpoints · 24h</span>}
          foot={<div className="text-[10px] text-muted">{fmtCount(udt?.active_endpoints ?? 0)} active · {udt?.watched ?? 0} watched</div>}
        />
      </div>

      {/* Traffic + security findings */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        <Card className="xl:col-span-8">
          <SectionHeader icon={<Activity className="h-3.5 w-3.5" />} title="Network Traffic"
            hint={`NetFlow · ${range.label}${nfOverview ? ` · ${fmtBytes(nfOverview.bytes)} total` : ''}`}
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
          <div className="h-[220px] px-2 pb-2 pt-1">
            {!chartData.length ? (
              <div className="flex h-full flex-col items-center justify-center gap-1 text-center">
                <Waves className="h-6 w-6 text-muted/50" />
                <div className="text-xs text-muted">No flow records in this window</div>
                <div className="text-[10px] text-muted/70">Point a NetFlow/IPFIX exporter at this appliance to light this up</div>
              </div>
            ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={chartData} margin={{ top: 8, right: 14, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="netsec-traffic" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={SERIES.traffic} stopOpacity={0.4} />
                    <stop offset="100%" stopColor={SERIES.traffic} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.5} strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="t" stroke="rgb(var(--muted))" fontSize={10} tickLine={false} axisLine={false} interval="preserveStartEnd" minTickGap={42} />
                <YAxis stroke="rgb(var(--muted))" fontSize={10} tickLine={false} axisLine={false} width={44} tickFormatter={(v: number) => fmtMbpsShort(v)} />
                <Tooltip
                  contentStyle={chartTooltipStyle}
                  formatter={(v: number | string) => [fmtBps(Number(v) * 1_000_000), 'Throughput']}
                />
                <Area type="monotone" dataKey="mbps" stroke={SERIES.traffic} strokeWidth={2} fill="url(#netsec-traffic)" isAnimationActive={false} dot={false} activeDot={{ r: 3.5, fill: SERIES.traffic }} />
              </AreaChart>
            </ResponsiveContainer>
            )}
          </div>
          <div className="border-t border-border px-4 py-2.5">
            <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Protocol mix · {range.label}</div>
            <ShareBar parts={protoMix.map((p) => ({ label: p.name, value: p.bytes, color: p.color }))} height={10} />
            <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
              {protoMix.length === 0 && <span className="text-[10.5px] text-muted">No flow records in this window</span>}
              {protoMix.map((p) => (
                <span key={p.protocol} className="flex items-center gap-1.5 text-[10.5px] text-text2">
                  <span className="h-2 w-2 rounded-sm" style={{ background: p.color }} />
                  {p.name} <span className="font-mono text-muted">{p.pct.toFixed(0)}%</span>
                </span>
              ))}
            </div>
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<ShieldAlert className="h-3.5 w-3.5" />} title="Security Findings"
            hint={`flow detectors · ${range.label}`}
            right={<Link to="/netflow/anomalies" className="text-xs text-primary hover:underline">Anomalies →</Link>} />
          <div className="px-3 pb-3 pt-2">
            {!findings.length && <Empty text="No hostile patterns detected in this window 🎉" />}
            <div className="space-y-1">
              {findings.slice(0, 7).map((f) => (
                <div key={f.id} className="flex gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-surface2/50">
                  <div className={`mt-0.5 w-1 shrink-0 self-stretch rounded-full ${f.severity === 'critical' ? 'bg-danger' : 'bg-warning'}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-xs font-semibold text-text">{f.title}</span>
                      <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted">{fmtCount(f.metric)} {f.metric_label}</span>
                    </div>
                    <div className="line-clamp-2 text-[11px] text-text2" title={f.description}>{f.description}</div>
                  </div>
                  <Badge variant={f.severity === 'critical' ? 'danger' : 'warning'} className="self-center capitalize">{f.severity}</Badge>
                </div>
              ))}
            </div>
          </div>
        </Card>
      </div>

      {/* Links · Talkers · NetPath */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-12">
        <Card className="xl:col-span-4">
          <SectionHeader icon={<Gauge className="h-3.5 w-3.5" />} title="Hottest Links" hint={range.label}
            right={<Link to="/link-utilization" className="text-xs text-primary hover:underline">All links →</Link>} />
          <div className="space-y-2.5 px-4 pb-4 pt-3">
            {!links?.items?.length && <Empty text="No interface utilization samples yet" />}
            {(links?.items || []).map((l) => {
              const util = l.util_pct ?? 0
              return (
                <Link key={`${l.device_id}-${l.if_index}`} to={`/link-utilization/${l.device_id}/${l.if_index}`} className="block rounded-md px-1 py-0.5 transition hover:bg-surface2/40">
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="min-w-0 truncate">
                      <span className="font-mono font-medium text-text">{l.if_name || `if${l.if_index}`}</span>
                      <span className="ml-1.5 text-[10px] text-muted">{l.hostname}{l.if_alias ? ` · ${l.if_alias}` : ''}</span>
                    </span>
                    <span className={`shrink-0 font-mono text-[11px] font-semibold tabular-nums ${util >= 80 ? 'text-danger' : util >= 50 ? 'text-warning' : 'text-text2'}`}>
                      {util.toFixed(1)}%
                    </span>
                  </div>
                  <div className="mt-1"><PctBar value={util} warnAt={50} dangerAt={80} /></div>
                  <div className="mt-0.5 flex items-center justify-between text-[9.5px] text-muted">
                    <span>↓ {fmtBps(l.in_bps)} · ↑ {fmtBps(l.out_bps)}</span>
                    <span>peak {l.peak_util_pct != null ? `${l.peak_util_pct.toFixed(0)}%` : '—'}{l.issues?.length ? ` · ${l.issues.join(', ')}` : ''}</span>
                  </div>
                </Link>
              )
            })}
          </div>
        </Card>

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
                      {app && <span className="rounded bg-surface2 px-1 py-px font-sans text-[8.5px] text-muted">{app}</span>}
                    </span>
                    <span className="font-mono text-[11px] font-semibold tabular-nums text-text2">{fmtBytes(t.bytes)}</span>
                  </div>
                  <InOutBar tx={t.src_bytes} rx={t.dst_bytes} scale={max} />
                </div>
              )
            })}
            {!!talkers?.length && (
              <div className="flex items-center gap-3 pt-1 text-[9.5px] text-muted">
                <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded-full" style={{ background: SERIES.traffic }} /> received</span>
                <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded-full" style={{ background: CATEGORICAL[3] }} /> sent</span>
              </div>
            )}
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<Waypoints className="h-3.5 w-3.5" />} title="WAN Paths" hint="netpath probes"
            right={<Link to="/netpath" className="text-xs text-primary hover:underline">NetPath →</Link>} />
          <div className="px-4 pb-3 pt-3">
            <div className="grid grid-cols-3 gap-2 text-center">
              <PathStat label="Healthy" value={netpath?.ok ?? 0} tone="text-success" />
              <PathStat label="Degraded" value={netpath?.degraded ?? 0} tone="text-warning" />
              <PathStat label="Down" value={netpath?.unreachable ?? 0} tone="text-danger" />
            </div>
            <div className="mt-3 border-t border-border pt-2">
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Recent path events</div>
              {!netpath?.recent_events?.length && <Empty text="No route changes or outages recorded" />}
              <div className="space-y-1">
                {(netpath?.recent_events || []).slice(0, 6).map((e) => (
                  <Link key={e.id} to={`/netpath/probes/${e.probe_id}`} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-xs transition hover:bg-surface2/50">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${e.severity === 'critical' ? 'bg-danger' : e.severity === 'warning' ? 'bg-warning' : 'bg-info'}`} />
                    <span className="min-w-0 flex-1 truncate text-text2">
                      <span className="font-medium text-text">{e.probe_name}</span>
                      <span className="ml-1.5 text-[10px] text-muted">{prettyEvent(e.event_type)}</span>
                    </span>
                    <span className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(e.created_at)}</span>
                  </Link>
                ))}
              </div>
            </div>
          </div>
        </Card>
      </div>

      {/* Alerts · device health · posture */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-12">
        <Card className="xl:col-span-5">
          <SectionHeader icon={<AlertTriangle className="h-3.5 w-3.5" />} title="Network Alert Feed"
            right={<Link to="/alerts" className="text-xs text-primary hover:underline">View all →</Link>} />
          <div className="px-3 pb-3 pt-2">
            {!netAlerts.length && <Empty text="No active network alerts 🎉" />}
            <div className="space-y-0.5">
              {netAlerts.map((a) => (
                <Link to={`/alerts/${a.id}`} key={a.id} className="group flex gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-surface2/50">
                  <div className={`mt-0.5 w-1 shrink-0 self-stretch rounded-full ${a.severity === 'critical' ? 'bg-danger' : a.severity === 'warning' ? 'bg-warning' : 'bg-info'}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-xs font-semibold text-text">{a.device_hostname || 'network'}</span>
                      <span className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(a.triggered_at)}</span>
                    </div>
                    <div className="truncate text-[11px] text-text2" title={a.message}>{prettyMessage(a.message)}</div>
                  </div>
                  <Badge variant={a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info'} className="self-center capitalize">{a.severity}</Badge>
                </Link>
              ))}
            </div>
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<Radio className="h-3.5 w-3.5" />} title="Device Health" hint={`${total} monitored`} />
          <div className="px-4 pb-3 pt-3">
            <DeviceStatusBar s={summary} height="h-2.5" />
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10.5px] text-text2">
              <LegendSwatch color="bg-success" label={`Up ${summary?.up ?? 0}`} />
              <LegendSwatch color="bg-warning" label={`Degraded ${summary?.degraded ?? 0}`} />
              <LegendSwatch color="bg-danger" label={`Down ${summary?.down ?? 0}`} />
              <LegendSwatch color="bg-info" label={`Maint ${summary?.maintenance ?? 0}`} />
              <LegendSwatch color="bg-muted/60" label={`Unknown ${summary?.unknown ?? 0}`} />
            </div>
            <div className="mt-3 border-t border-border pt-2">
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Needs attention</div>
              {problemDevices.length === 0 && <Empty text="All devices healthy 🎉" />}
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
          </div>
        </Card>

        <Card className="xl:col-span-3">
          <SectionHeader icon={<FileCode className="h-3.5 w-3.5" />} title="Security Posture" hint="24h" />
          <div className="space-y-3 px-4 pb-4 pt-3">
            <PostureRow
              icon={<Inbox className="h-3.5 w-3.5" />}
              label="SNMP traps"
              to="/traps"
              value={traps ? fmtCount(traps.total) : '—'}
              detail={traps
                ? <><span className="text-danger">{traps.critical} crit</span> · <span className="text-warning">{traps.warning} warn</span></>
                : 'collector offline'}
            />
            <PostureRow
              icon={<ScanSearch className="h-3.5 w-3.5" />}
              label="New endpoints"
              to="/udt"
              value={fmtCount(udt?.new_24h ?? 0)}
              detail={<>{udt?.rogue ?? 0} unauthorized · {udt?.watched ?? 0} watched</>}
            />
            <PostureRow
              icon={<Activity className="h-3.5 w-3.5" />}
              label="AD logins"
              to="/udt/users"
              value={fmtCount(udt?.logins_24h ?? 0)}
              detail="correlated to switch ports"
            />
            <div className="border-t border-border pt-3">
              <div className="flex items-baseline justify-between text-xs">
                <span className="text-text2">Config backup coverage</span>
                <span className="font-mono text-[12px] font-semibold tabular-nums">
                  {backupPct != null ? `${backupPct.toFixed(0)}%` : '—'}
                </span>
              </div>
              <div className="mt-1.5"><PctBar value={backupPct ?? 0} warnAt={101} dangerAt={102} /></div>
              <div className="mt-1 flex items-center justify-between text-[10px] text-muted">
                <span>{ncm?.backed_up ?? 0}/{ncm?.total_devices ?? 0} devices backed up</span>
                <Link to="/ncm" className="text-primary hover:underline">NCM →</Link>
              </div>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}

/* ── Local building blocks ──────────────────────────────────────────────── */

function MiniStat({ label, value, tone }: { label: string; value: string; tone: string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-surface2/30 px-2 py-1.5">
      <div className="text-[9px] font-bold uppercase tracking-wider text-muted">{label}</div>
      <div className={`font-mono text-[13px] font-semibold tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}

function PathStat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-surface2/30 px-2 py-2">
      <div className={`text-xl font-bold tabular-nums ${tone}`}>{value}</div>
      <div className="text-[9px] font-bold uppercase tracking-wider text-muted">{label}</div>
    </div>
  )
}

function DeviceStatusBar({ s, height = 'h-1.5' }: { s?: DeviceSummary; height?: string }) {
  if (!s || !s.total) return <div className={`${height} rounded-full bg-surface2`} />
  const seg = (n: number) => `${(n / s.total) * 100}%`
  return (
    <div className={`flex ${height} w-full gap-px overflow-hidden rounded-full bg-surface2`}>
      <div style={{ width: seg(s.up) }} className="bg-success" />
      <div style={{ width: seg(s.degraded) }} className="bg-warning" />
      <div style={{ width: seg(s.down) }} className="bg-danger" />
      <div style={{ width: seg(s.maintenance) }} className="bg-info" />
      <div style={{ width: seg(s.unknown) }} className="bg-muted/60" />
    </div>
  )
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-2 w-2 rounded-sm ${color}`} />
      {label}
    </span>
  )
}

/** Two-direction byte bar scaled to the list max. */
function InOutBar({ tx, rx, scale }: { tx: number; rx: number; scale: number }) {
  const w = (v: number) => `${Math.max(0.5, (v / (scale || 1)) * 100)}%`
  return (
    <div className="mt-1 space-y-px">
      <div className="h-[5px] w-full overflow-hidden rounded-full bg-surface2/80">
        <div className="h-full rounded-full" style={{ width: w(rx), background: SERIES.traffic }} />
      </div>
      <div className="h-[5px] w-full overflow-hidden rounded-full bg-surface2/80">
        <div className="h-full rounded-full" style={{ width: w(tx), background: CATEGORICAL[3] }} />
      </div>
    </div>
  )
}

function PostureRow({ icon, label, value, detail, to }: {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
  detail: React.ReactNode
  to: string
}) {
  return (
    <Link to={to} className="flex items-center gap-2.5 rounded-md px-1 py-0.5 transition hover:bg-surface2/40">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface2 text-muted">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium text-text">{label}</div>
        <div className="truncate text-[10px] text-muted">{detail}</div>
      </div>
      <span className="shrink-0 font-mono text-sm font-bold tabular-nums text-text">{value}</span>
    </Link>
  )
}

function prettyMessage(msg: string) {
  return msg.replace(/^\[[^\]]+\]\s*/, '')
}

function prettyEvent(t: string) {
  return t.replace(/_/g, ' ')
}

function fmtMbpsShort(v: number): string {
  if (!isFinite(v) || v <= 0) return '0'
  if (v >= 1000) return `${(v / 1000).toFixed(1)}G`
  if (v >= 1) return `${v.toFixed(0)}M`
  return `${(v * 1000).toFixed(0)}K`
}
