/**
 * Availability Command Center — NOC-grade uptime dashboard with live SSE,
 * time-window analytics, and theme-aware glass UI for wall displays.
 */

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertOctagon,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  Clock,
  HeartPulse,
  Layers,
  Loader2,
  MapPin,
  Maximize2,
  Minimize2,
  Radio,
  Server,
  Shield,
  Sparkles,
  Target,
  TrendingUp,
  Zap,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import { RingGauge } from '@/components/dashboard/RingGauge'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { useExecutiveReport, useTechnicalReport, useBusinessReport } from '@/hooks/useReports'
import { useSSE } from '@/hooks/useSSE'
import type { ServerMonitoringOverview } from '@/types/servers'

/* ── Types ──────────────────────────────────────────────────────────────── */

type DeviceSummary = {
  total: number
  up: number
  down: number
  degraded: number
  unknown: number
  maintenance: number
}

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  location: string | null
  group_id: string | null
  status: 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance'
}

type ServiceSummary = {
  total: number
  up: number
  down: number
  warning: number
  degraded: number
  unknown: number
}

type AvailSegment = {
  key: string
  label: string
  pct: number
  up: number
  total: number
}

type RingColor = 'success' | 'warning' | 'danger' | 'info' | 'primary' | 'accent'

type FleetDomain = 'devices' | 'services' | 'servers'

const FLEET_DOMAINS: Array<{ key: FleetDomain; label: string; icon: typeof Server }> = [
  { key: 'devices', label: 'Devices', icon: Server },
  { key: 'services', label: 'Services', icon: Shield },
  { key: 'servers', label: 'Servers', icon: Activity },
]

type TileAccent = {
  bar: string
  bg: string
  glow: string
  ring: RingColor
}

/* ── Theme tokens ───────────────────────────────────────────────────────── */

const TILE_ACCENTS: TileAccent[] = [
  { bar: 'bg-emerald-500', bg: 'from-emerald-500/10 to-teal-500/5', glow: 'group-hover:shadow-emerald-500/15', ring: 'success' },
  { bar: 'bg-sky-500', bg: 'from-sky-500/10 to-blue-500/5', glow: 'group-hover:shadow-sky-500/15', ring: 'info' },
  { bar: 'bg-violet-500', bg: 'from-violet-500/10 to-purple-500/5', glow: 'group-hover:shadow-violet-500/15', ring: 'accent' },
  { bar: 'bg-amber-500', bg: 'from-amber-500/10 to-orange-500/5', glow: 'group-hover:shadow-amber-500/15', ring: 'warning' },
  { bar: 'bg-rose-500', bg: 'from-rose-500/10 to-pink-500/5', glow: 'group-hover:shadow-rose-500/15', ring: 'danger' },
  { bar: 'bg-cyan-500', bg: 'from-cyan-500/10 to-teal-500/5', glow: 'group-hover:shadow-cyan-500/15', ring: 'info' },
]

const KPI_THEMES = {
  devices: {
    icon: 'from-emerald-500 to-teal-600',
    card: 'border-emerald-500/25 bg-gradient-to-br from-emerald-500/10 via-surface to-surface dark:from-emerald-500/15',
    bar: 'from-emerald-400 to-teal-500',
  },
  services: {
    icon: 'from-violet-500 to-purple-600',
    card: 'border-violet-500/25 bg-gradient-to-br from-violet-500/10 via-surface to-surface dark:from-violet-500/15',
    bar: 'from-violet-400 to-purple-500',
  },
  servers: {
    icon: 'from-sky-500 to-blue-600',
    card: 'border-sky-500/25 bg-gradient-to-br from-sky-500/10 via-surface to-surface dark:from-sky-500/15',
    bar: 'from-sky-400 to-blue-500',
  },
  alerts: {
    icon: 'from-rose-500 to-red-600',
    card: 'border-rose-500/25 bg-gradient-to-br from-rose-500/10 via-surface to-surface dark:from-rose-500/15',
    bar: 'from-rose-400 to-red-500',
  },
} as const

const keepPrev = <T,>(prev: T | undefined) => prev

/* ── Helpers ────────────────────────────────────────────────────────────── */

function availColor(pct: number): RingColor {
  if (pct >= 99.9) return 'success'
  if (pct >= 99) return 'warning'
  if (pct >= 95) return 'warning'
  return 'danger'
}

function availText(pct: number) {
  if (pct >= 99.9) return 'text-success'
  if (pct >= 99) return 'text-warning'
  if (pct >= 95) return 'text-warning'
  return 'text-danger'
}

function availBarGradient(pct: number) {
  if (pct >= 99.9) return 'from-emerald-400 to-teal-500'
  if (pct >= 99) return 'from-amber-400 to-orange-500'
  if (pct >= 95) return 'from-amber-400 to-orange-500'
  return 'from-rose-400 to-red-500'
}

function fmtPct(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v.toFixed(digits)}%`
}

function fmtMin(v: number | null | undefined) {
  if (v == null) return '—'
  if (v < 1) return `${(v * 60).toFixed(0)}s`
  if (v < 60) return `${v.toFixed(1)}m`
  return `${(v / 60).toFixed(1)}h`
}

function titleCase(s: string) {
  return s.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function buildSegments(
  devices: Device[],
  uptime: Record<string, number>,
  getKey: (d: Device) => string,
  labelOf: (key: string) => string,
): AvailSegment[] {
  const grouped = new Map<string, { score: number; up: number; total: number }>()
  for (const d of devices) {
    const key = getKey(d) || 'Unassigned'
    const cur = grouped.get(key) || { score: 0, up: 0, total: 0 }
    cur.total += 1
    if (d.status === 'up') cur.up += 1
    cur.score += uptime[d.id] ?? (d.status === 'up' ? 100 : d.status === 'degraded' ? 75 : 0)
    grouped.set(key, cur)
  }
  return Array.from(grouped.entries())
    .map(([key, v]) => ({
      key,
      label: labelOf(key),
      pct: v.total ? v.score / v.total : 0,
      up: v.up,
      total: v.total,
    }))
    .sort((a, b) => b.total - a.total)
}

/* ── Page ───────────────────────────────────────────────────────────────── */

export function AvailabilityPage() {
  const qc = useQueryClient()
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [noc, setNoc] = useState(false)
  const [fleetDomain, setFleetDomain] = useState<FleetDomain>('devices')
  const [clock, setClock] = useState(() => new Date())

  const liveInterval = 15_000
  const histInterval = 60_000

  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1_000)
    return () => clearInterval(id)
  }, [])

  useSSE('/api/v1/stream/status', {
    enabled: true,
    onMessage: () => qc.invalidateQueries({ queryKey: ['avail'] }),
  })

  useEffect(() => {
    if (!noc) return
    const aside = document.querySelector('aside') as HTMLElement | null
    const header = document.querySelector('header') as HTMLElement | null
    const prevAside = aside?.style.display ?? ''
    const prevHeader = header?.style.display ?? ''
    if (aside) aside.style.display = 'none'
    if (header) header.style.display = 'none'
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setNoc(false) }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      if (aside) aside.style.display = prevAside
      if (header) header.style.display = prevHeader
    }
  }, [noc])

  const { data: exec, isLoading: execLoading, isFetching: execFetching } = useExecutiveReport({
    fromISO: range.fromISO,
    toISO: range.toISO,
  })
  const { data: technical, isFetching: techFetching } = useTechnicalReport({
    fromISO: range.fromISO,
    toISO: range.toISO,
  })
  const { data: business, isFetching: bizFetching } = useBusinessReport({
    fromISO: range.fromISO,
    toISO: range.toISO,
  })

  const { data: summary, isLoading: summaryLoading } = useQuery<DeviceSummary>({
    queryKey: ['avail', 'summary'],
    queryFn: async () => (await api.get('/devices/summary')).data,
    refetchInterval: liveInterval,
    placeholderData: keepPrev,
  })

  const { data: devicesResp, isLoading: devicesLoading } = useQuery<{ data: Device[] }>({
    queryKey: ['avail', 'devices'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    refetchInterval: liveInterval,
    staleTime: 30_000,
    placeholderData: keepPrev,
  })
  const devices = devicesResp?.data || []

  const { data: groups } = useQuery<Array<{ id: string; name: string }>>({
    queryKey: ['avail', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    staleTime: 5 * 60_000,
    placeholderData: keepPrev,
  })

  const { data: uptime, isFetching: uptimeFetching } = useQuery<{
    devices: Record<string, number>
    failed_checks?: Record<string, number>
  }>({
    queryKey: ['avail', 'uptime', range.hours],
    queryFn: async () => (await api.get(`/devices/dashboard/uptime-stats?hours=${range.hours}`)).data,
    refetchInterval: histInterval,
    placeholderData: keepPrev,
  })

  const { data: services } = useQuery<ServiceSummary>({
    queryKey: ['avail', 'services'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: liveInterval,
    placeholderData: keepPrev,
  })

  const { data: servers } = useQuery<ServerMonitoringOverview>({
    queryKey: ['avail', 'servers'],
    queryFn: async () => (await api.get('/server-monitoring/overview')).data,
    refetchInterval: liveInterval,
    retry: 1,
    placeholderData: keepPrev,
  })

  const rangeFetching = execFetching || techFetching || bizFetching || uptimeFetching
  const initialLoading = execLoading && !exec
  const segmentsLoading = (devicesLoading && !devices.length) || (uptimeFetching && !uptime)

  const ut = uptime?.devices || {}
  const failedChecks = uptime?.failed_checks || {}
  const k = exec?.kpis
  const fleetPct = k?.availability_pct ?? 0
  const slaMet = k ? k.sla_attained_pct >= k.sla_target_pct : true

  const byLocation = useMemo(
    () => buildSegments(devices, ut, (d) => d.location || 'Unassigned', titleCase),
    [devices, ut],
  )
  const byType = useMemo(
    () => buildSegments(devices, ut, (d) => d.device_type || 'other', titleCase),
    [devices, ut],
  )
  const byGroup = useMemo(() => {
    const groupMap = new Map((groups || []).map((g) => [g.id, g.name]))
    return buildSegments(
      devices.filter((d) => d.group_id),
      ut,
      (d) => groupMap.get(d.group_id!) || 'Unknown Group',
      (key) => key,
    )
  }, [devices, groups, ut])

  const downDevices = useMemo(
    () => devices.filter((d) => d.status === 'down' || d.status === 'degraded').slice(0, 12),
    [devices],
  )

  /** Devices below 100% uptime — failed checks align with ping-sample uptime math. */
  const worstDevices = useMemo(() => {
    const sampleEst = Math.max(1, Math.round(range.hours * 12))
    return devices
      .map((d) => {
        const pct = ut[d.id]
        if (pct === undefined || pct >= 100) return null
        const failed =
          failedChecks[d.id] ??
          Math.max(1, Math.round(sampleEst * (100 - pct) / 100))
        return {
          device_id: d.id,
          hostname: d.hostname,
          ip: d.ip_address,
          availability_pct: pct,
          failed_checks: failed,
        }
      })
      .filter((row): row is NonNullable<typeof row> => row !== null)
      .sort((a, b) => a.availability_pct - b.availability_pct)
      .slice(0, 12)
  }, [devices, ut, failedChecks, range.hours])

  const serverTotal = servers?.total ?? 0
  const serverHealthy = servers?.status_counts?.healthy ?? 0
  const serverPct = serverTotal > 0 ? (serverHealthy / serverTotal) * 100 : 100

  const serviceAvail = business?.service_availability || []
  const avgServicePct = serviceAvail.length
    ? serviceAvail.reduce((s, r) => s + (r.availability_pct ?? 0), 0) / serviceAvail.length
    : services && services.total > 0 ? (services.up / services.total) * 100 : 100

  const trendData = exec?.availability_trend || []

  const serviceFailedChecks = useMemo(
    () => serviceAvail.reduce((s, r) => s + (r.checks_failed ?? 0), 0),
    [serviceAvail],
  )

  const activeFleet = useMemo(() => {
    const views = {
      devices: {
        title: 'Device Availability',
        pct: fleetPct,
        sub: `${summary?.up ?? 0}/${summary?.total ?? 0} UP`,
        ringLabel: 'Network Devices',
        gradient: 'from-emerald-600 via-teal-600 to-cyan-600 dark:from-emerald-300 dark:via-teal-300 dark:to-cyan-300',
        labelAccent: 'text-emerald-600 dark:text-emerald-400',
        metrics: [
          { icon: <Server className="h-3.5 w-3.5" />, label: 'Monitored', value: String(k?.devices_monitored ?? summary?.total ?? '—') },
          { icon: <Clock className="h-3.5 w-3.5" />, label: 'MTTR', value: fmtMin(k?.mttr_minutes) },
          { icon: <Zap className="h-3.5 w-3.5" />, label: 'Incidents', value: String(k?.incidents_count ?? '—') },
        ],
        showExecDelta: true,
      },
      services: {
        title: 'Service Availability',
        pct: avgServicePct,
        sub: `${services?.up ?? 0}/${services?.total ?? 0} UP`,
        ringLabel: 'Service Checks',
        gradient: 'from-violet-600 via-purple-600 to-fuchsia-600 dark:from-violet-300 dark:via-purple-300 dark:to-fuchsia-300',
        labelAccent: 'text-violet-600 dark:text-violet-400',
        metrics: [
          { icon: <Shield className="h-3.5 w-3.5" />, label: 'Checks', value: String(services?.total ?? '—') },
          { icon: <AlertOctagon className="h-3.5 w-3.5" />, label: 'Down', value: String(services?.down ?? 0) },
          { icon: <Target className="h-3.5 w-3.5" />, label: 'Failed', value: String(serviceFailedChecks) },
        ],
        showExecDelta: false,
      },
      servers: {
        title: 'Server Availability',
        pct: serverPct,
        sub: `${serverHealthy}/${serverTotal} healthy`,
        ringLabel: 'Server Fleet',
        gradient: 'from-sky-600 via-blue-600 to-indigo-600 dark:from-sky-300 dark:via-blue-300 dark:to-indigo-300',
        labelAccent: 'text-sky-600 dark:text-sky-400',
        metrics: [
          { icon: <Activity className="h-3.5 w-3.5" />, label: 'Total', value: String(serverTotal || '—') },
          { icon: <AlertOctagon className="h-3.5 w-3.5" />, label: 'Critical', value: String(servers?.status_counts?.critical ?? 0) },
          { icon: <Target className="h-3.5 w-3.5" />, label: 'Warning', value: String(servers?.status_counts?.warning ?? 0) },
        ],
        showExecDelta: false,
      },
    } as const
    return views[fleetDomain]
  }, [
    fleetDomain, fleetPct, avgServicePct, serverPct, summary, services, k,
    serverTotal, serverHealthy, servers, serviceFailedChecks,
  ])

  const heroPct = activeFleet.pct

  return (
    <div className={cn('relative', noc && 'fixed inset-0 z-40 overflow-y-auto p-4 md:p-6')}>
      {/* Ambient mesh — theme-aware */}
      <div
        aria-hidden
        className={cn(
          'pointer-events-none absolute inset-0 -z-10 overflow-hidden',
          noc && 'fixed',
        )}
      >
        <div className="absolute -left-32 -top-32 h-96 w-96 rounded-full bg-emerald-400/20 blur-3xl dark:bg-emerald-500/10" />
        <div className="absolute -right-24 top-1/4 h-80 w-80 rounded-full bg-violet-400/15 blur-3xl dark:bg-violet-500/10" />
        <div className="absolute bottom-0 left-1/3 h-72 w-72 rounded-full bg-sky-400/15 blur-3xl dark:bg-sky-500/10" />
      </div>

      <div className={cn('space-y-5 animate-fade-in', rangeFetching && 'opacity-[0.97]')}>
        {/* Header */}
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex items-start gap-4">
            <div className={cn(
              'relative flex shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-emerald-500 via-teal-500 to-cyan-500 p-[1px] shadow-lg shadow-emerald-500/20',
              noc ? 'h-14 w-14' : 'h-12 w-12',
            )}>
              <div className="flex h-full w-full items-center justify-center rounded-2xl bg-surface">
                <HeartPulse className={cn('text-emerald-600 dark:text-emerald-400', noc ? 'h-7 w-7' : 'h-6 w-6')} />
              </div>
              <span className="absolute -right-0.5 -top-0.5 flex h-3 w-3">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-emerald-500 ring-2 ring-surface" />
              </span>
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-3">
                <h1 className={cn('font-bold tracking-tight text-text', noc ? 'text-3xl' : 'text-2xl')}>
                  Availability Command Center
                </h1>
                <LiveBadge />
                {rangeFetching && (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2.5 py-0.5 text-[10px] font-semibold text-primary">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Updating {range.label.toLowerCase()}
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs text-muted">
                Real-time fleet health · {range.label} analytics · SSE push + 15s polling
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ClockDisplay date={clock} large={noc} />
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
            />
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setNoc((v) => !v)}
              title={noc ? 'Exit NOC fullscreen (Esc)' : 'NOC fullscreen for video wall'}
              className="border border-border bg-surface/80 backdrop-blur-sm"
            >
              {noc ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </Button>
          </div>
        </div>

        {/* Hero command strip */}
        <GlassPanel className="overflow-hidden p-0">
          <div className="pointer-events-none absolute inset-0 bg-gradient-to-br from-emerald-500/[0.07] via-transparent to-violet-500/[0.06]" />
          <div className="absolute right-4 top-4 z-20 md:right-6 md:top-6">
            <FleetDomainTabs value={fleetDomain} onChange={setFleetDomain} />
          </div>
          <div className={cn('relative z-10 grid gap-6 p-5 pt-14 md:grid-cols-12 md:p-7 md:pt-16', noc && 'md:p-9 md:pt-18')}>
            <div className="flex flex-col justify-center md:col-span-5">
              <div className={cn('flex items-center gap-2 text-[11px] font-bold uppercase tracking-[0.22em]', activeFleet.labelAccent)}>
                <Sparkles className="h-3.5 w-3.5" />
                {activeFleet.title}
              </div>
              {initialLoading && fleetDomain === 'devices' ? (
                <Skeleton className="mt-3 h-16 w-48" />
              ) : (
                <div className={cn(
                  'mt-2 bg-gradient-to-r bg-clip-text font-black tabular-nums tracking-tight text-transparent',
                  activeFleet.gradient,
                  noc ? 'text-7xl' : 'text-6xl',
                )}>
                  {fmtPct(heroPct)}
                </div>
              )}
              {k && fleetDomain === 'devices' && activeFleet.showExecDelta && (
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <span className={cn(
                    'inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs font-semibold',
                    (k.availability_delta_pct ?? 0) >= 0
                      ? 'bg-success/10 text-success'
                      : 'bg-danger/10 text-danger',
                  )}>
                    {(k.availability_delta_pct ?? 0) >= 0
                      ? <ArrowUpRight className="h-3.5 w-3.5" />
                      : <ArrowDownRight className="h-3.5 w-3.5" />}
                    {(k.availability_delta_pct ?? 0) >= 0 ? '+' : ''}{fmtPct(k.availability_delta_pct, 2)} vs prior window
                  </span>
                  <SlaChip met={slaMet} attained={k.sla_attained_pct} target={k.sla_target_pct} />
                </div>
              )}
              {fleetDomain === 'services' && (services?.down ?? 0) > 0 && (
                <div className="mt-3">
                  <span className="inline-flex items-center gap-1 rounded-lg bg-danger/10 px-2.5 py-1 text-xs font-semibold text-danger">
                    <AlertOctagon className="h-3.5 w-3.5" />
                    {services!.down} service{services!.down === 1 ? '' : 's'} down · {range.label}
                  </span>
                </div>
              )}
              {fleetDomain === 'servers' && serverTotal > 0 && (
                <div className="mt-3">
                  <span className="inline-flex items-center gap-1 rounded-lg bg-info/10 px-2.5 py-1 text-xs font-semibold text-info">
                    <Radio className="h-3.5 w-3.5" />
                    Live agent status · {range.label} uptime on devices
                  </span>
                </div>
              )}
              <div className="mt-5 grid grid-cols-3 gap-2">
                {activeFleet.metrics.map((m) => (
                  <MetricChip key={m.label} icon={m.icon} label={m.label} value={m.value} />
                ))}
              </div>
            </div>

            <div className="flex items-center justify-center md:col-span-3">
              <div className="relative">
                <div className={cn(
                  'absolute inset-0 rounded-full blur-2xl',
                  heroPct >= 99.9 ? 'bg-emerald-400/30 dark:bg-emerald-500/20' : heroPct >= 99 ? 'bg-amber-400/30' : 'bg-rose-400/30',
                  fleetDomain === 'services' && 'bg-violet-400/25 dark:bg-violet-500/15',
                  fleetDomain === 'servers' && 'bg-sky-400/25 dark:bg-sky-500/15',
                )} />
                <RingGauge
                  value={heroPct}
                  size={noc ? 190 : 160}
                  stroke={noc ? 15 : 13}
                  color={availColor(heroPct)}
                  sub={activeFleet.sub}
                  label={activeFleet.ringLabel}
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 md:col-span-4">
              <DomainKpi
                theme={KPI_THEMES.devices}
                label="Devices"
                value={summary ? `${summary.up}/${summary.total}` : '—'}
                pct={summary && summary.total ? (summary.up / summary.total) * 100 : 100}
                icon={<Server className="h-4 w-4" />}
                to="/devices"
                issue={summary?.down}
                loading={summaryLoading && !summary}
                large={noc}
              />
              <DomainKpi
                theme={KPI_THEMES.services}
                label="Services"
                value={services ? `${services.up}/${services.total}` : '—'}
                pct={avgServicePct}
                icon={<Shield className="h-4 w-4" />}
                to="/services"
                issue={services?.down}
                large={noc}
              />
              <DomainKpi
                theme={KPI_THEMES.servers}
                label="Servers"
                value={serverTotal ? `${serverHealthy}/${serverTotal}` : '—'}
                pct={serverPct}
                icon={<Activity className="h-4 w-4" />}
                to="/servers"
                issue={(servers?.status_counts?.critical ?? 0) + (servers?.status_counts?.warning ?? 0)}
                large={noc}
              />
              <DomainKpi
                theme={KPI_THEMES.alerts}
                label="Critical Alerts"
                value={String(k?.active_critical_count ?? 0)}
                pct={k?.active_critical_count ? 0 : 100}
                icon={<AlertOctagon className="h-4 w-4" />}
                to="/alerts"
                large={noc}
              />
            </div>
          </div>
        </GlassPanel>

        {/* Trend + live */}
        <div className="grid gap-4 lg:grid-cols-12">
          <GlassPanel className="lg:col-span-8">
            <PanelHeader
              icon={<TrendingUp className="h-4 w-4 text-emerald-500" />}
              title="Availability Trend"
              hint={range.label}
              accent="border-emerald-500/30"
            />
            <div className={cn('relative px-3 pb-4', noc ? 'h-[360px]' : 'h-[300px]')}>
              {initialLoading && !trendData.length ? (
                <ChartSkeleton />
              ) : trendData.length === 0 ? (
                <EmptyPanel icon={<TrendingUp className="h-8 w-8" />} text="No trend data for this window" />
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={trendData} margin={{ top: 16, right: 20, left: -4, bottom: 0 }}>
                    <defs>
                      <linearGradient id="availTrendFill2" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.45} />
                        <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="availTrendStroke" x1="0" y1="0" x2="1" y2="0">
                        <stop offset="0%" stopColor="#10b981" />
                        <stop offset="50%" stopColor="#06b6d4" />
                        <stop offset="100%" stopColor="#8b5cf6" />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border))" strokeOpacity={0.6} vertical={false} />
                    <XAxis
                      dataKey="ts"
                      tickFormatter={(v) => {
                        const d = new Date(v)
                        return range.hours <= 48
                          ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                          : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
                      }}
                      tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                      axisLine={false}
                      tickLine={false}
                      minTickGap={36}
                    />
                    <YAxis
                      domain={[(min: number) => Math.max(0, Math.floor(min - 2)), 100]}
                      tickFormatter={(v) => `${v}%`}
                      tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                      axisLine={false}
                      tickLine={false}
                      width={52}
                    />
                    <Tooltip content={<TrendTooltip sla={k?.sla_target_pct} />} />
                    {k && (
                      <ReferenceLine
                        y={k.sla_target_pct}
                        stroke="rgb(var(--success))"
                        strokeDasharray="5 5"
                        strokeOpacity={0.8}
                        label={{ value: `SLA ${k.sla_target_pct}%`, position: 'right', fill: 'rgb(var(--success))', fontSize: 10 }}
                      />
                    )}
                    <Area
                      type="monotone"
                      dataKey="availability_pct"
                      stroke="url(#availTrendStroke)"
                      strokeWidth={3}
                      fill="url(#availTrendFill2)"
                      isAnimationActive={false}
                      dot={false}
                      activeDot={{ r: 5, fill: '#10b981', stroke: '#fff', strokeWidth: 2 }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </GlassPanel>

          <GlassPanel className="lg:col-span-4">
            <PanelHeader
              icon={<Radio className="h-4 w-4 text-cyan-500" />}
              title="Live Fleet Status"
              hint="real-time"
              accent="border-cyan-500/30"
            />
            <div className="space-y-4 px-4 pb-5 pt-3">
              {summaryLoading && !summary ? (
                <div className="space-y-3">
                  <Skeleton className="h-4 w-full rounded-full" />
                  <div className="grid grid-cols-5 gap-2">
                    {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-12" />)}
                  </div>
                </div>
              ) : (
                <LiveStatusGrid summary={summary} large={noc} />
              )}
              <div className="rounded-xl border border-border/70 bg-surface2/40 p-3">
                <div className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-wider text-muted">
                  <Target className="h-3 w-3" />
                  Needs attention
                </div>
                {downDevices.length === 0 ? (
                  <div className="flex items-center justify-center gap-2 py-5 text-sm font-medium text-success">
                    <CheckCircle2 className="h-4 w-4" />
                    All devices reachable
                  </div>
                ) : (
                  <div className="max-h-[200px] space-y-1 overflow-y-auto">
                    {downDevices.map((d) => (
                      <Link
                        key={d.id}
                        to={`/devices/${d.id}`}
                        className="group flex items-center gap-2 rounded-lg border border-transparent px-2 py-2 transition hover:border-border hover:bg-surface/80"
                      >
                        <span className={cn(
                          'h-2.5 w-2.5 shrink-0 rounded-full shadow-sm',
                          d.status === 'down' ? 'bg-danger shadow-danger/40 animate-pulse' : 'bg-warning shadow-warning/40',
                        )} />
                        <span className="min-w-0 flex-1 truncate text-sm font-medium text-text">{d.hostname}</span>
                        <Badge variant={d.status === 'down' ? 'danger' : 'warning'} className="capitalize">
                          {d.status}
                        </Badge>
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </GlassPanel>
        </div>

        {/* Breakdown tiles */}
        <div className="grid gap-4 xl:grid-cols-3">
          <BreakdownPanel
            title="By Location"
            icon={<MapPin className="h-4 w-4" />}
            segments={byLocation}
            hint={range.label}
            loading={segmentsLoading && !byLocation.length}
            large={noc}
            accent="emerald"
          />
          <BreakdownPanel
            title="By Group"
            icon={<Layers className="h-4 w-4" />}
            segments={byGroup}
            hint={range.label}
            loading={segmentsLoading && !byGroup.length}
            large={noc}
            accent="violet"
          />
          <BreakdownPanel
            title="By Device Type"
            icon={<Server className="h-4 w-4" />}
            segments={byType}
            hint={range.label}
            loading={segmentsLoading && !byType.length}
            large={noc}
            accent="sky"
          />
        </div>

        {/* Data tables */}
        <div className="grid gap-4 lg:grid-cols-2">
          <DataTablePanel
            title="Site Health Matrix"
            icon={<MapPin className="h-4 w-4 text-emerald-500" />}
            hint={range.label}
            loading={initialLoading && !(exec?.location_summary?.length)}
            empty="No location data"
            columns={['Location', 'Devices', 'Down', 'Availability']}
            rows={(exec?.location_summary || []).map((row) => ({
              key: row.location,
              cells: [
                <span className="font-medium">{row.location}</span>,
                <span className="tabular-nums">{row.devices}</span>,
                <span className={cn('tabular-nums', row.down > 0 && 'font-bold text-danger')}>{row.down}</span>,
                <GradientBar pct={row.availability_pct} />,
              ],
            }))}
          />
          <DataTablePanel
            title="Service Availability"
            icon={<Shield className="h-4 w-4 text-violet-500" />}
            hint={range.label}
            loading={bizFetching && !serviceAvail.length}
            empty="No service checks configured"
            columns={['Service', 'Type', 'Status', 'Uptime']}
            rows={serviceAvail.slice(0, 14).map((svc) => ({
              key: svc.service_check_id,
              cells: [
                <span className="max-w-[160px] truncate font-medium" title={svc.name}>{svc.name}</span>,
                <span className="text-[10px] font-bold uppercase tracking-wider text-muted">{svc.type}</span>,
                <Badge variant={svc.status === 'up' ? 'success' : svc.status === 'down' ? 'danger' : 'warning'} className="capitalize">
                  {svc.status}
                </Badge>,
                <GradientBar pct={svc.availability_pct ?? 0} />,
              ],
            }))}
          />
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <DataTablePanel
            title="Lowest Availability Devices"
            icon={<Target className="h-4 w-4 text-amber-500" />}
            hint={range.label}
            loading={segmentsLoading && !worstDevices.length}
            empty="No device issues in this window"
            columns={['Device', 'IP', 'Failed checks', 'Uptime']}
            rows={worstDevices.map((d) => ({
              key: d.device_id,
              cells: [
                <Link to={`/devices/${d.device_id}`} className="font-medium text-primary hover:underline">{d.hostname}</Link>,
                <span className="font-mono text-xs text-muted">{d.ip}</span>,
                <span
                  className="tabular-nums"
                  title="Ping samples that did not respond in this window (same source as uptime %)"
                >
                  {d.failed_checks}
                </span>,
                <GradientBar pct={d.availability_pct} />,
              ],
            }))}
          />
          <DataTablePanel
            title="Recent Outage Timeline"
            icon={<Clock className="h-4 w-4 text-rose-500" />}
            hint={range.label}
            loading={initialLoading && !(exec?.outage_timeline?.length)}
            empty="No outages recorded — fleet is healthy"
            emptyTone="success"
            columns={['Started', 'Device', 'Duration']}
            rows={(exec?.outage_timeline || []).slice(0, 12).map((o, i) => ({
              key: `${o.device_id}-${i}`,
              cells: [
                <span className="text-xs text-muted">{o.started_at ? relativeTime(o.started_at) : '—'}</span>,
                <span className="font-medium">{o.hostname}</span>,
                <span className="tabular-nums font-semibold">{fmtMin(o.duration_minutes)}</span>,
              ],
            }))}
          />
        </div>
      </div>
    </div>
  )
}

/* ── UI building blocks ─────────────────────────────────────────────────── */

function GlassPanel({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn(
      'relative overflow-hidden rounded-2xl border border-border/80 bg-surface/90 shadow-sm backdrop-blur-md',
      'dark:border-border/60 dark:bg-surface/80 dark:shadow-black/20',
      className,
    )}>
      {children}
    </div>
  )
}

function PanelHeader({
  icon, title, hint, accent,
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  accent?: string
}) {
  return (
    <div className={cn(
      'flex items-center justify-between gap-2 border-b border-border/70 px-4 py-3',
      accent && `border-l-4 ${accent}`,
    )}>
      <div className="flex min-w-0 items-center gap-2.5">
        {icon}
        <h3 className="text-sm font-bold tracking-tight text-text">{title}</h3>
        {hint && (
          <span className="rounded-md bg-surface2 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
            {hint}
          </span>
        )}
      </div>
    </div>
  )
}

function FleetDomainTabs({
  value,
  onChange,
}: {
  value: FleetDomain
  onChange: (d: FleetDomain) => void
}) {
  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-surface/90 p-0.5 shadow-sm backdrop-blur-sm"
      role="group"
      aria-label="Availability domain"
    >
      {FLEET_DOMAINS.map(({ key, label, icon: Icon }) => {
        const active = value === key
        return (
          <button
            key={key}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(key)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-semibold transition-colors',
              active
                ? 'bg-primary text-black shadow-sm'
                : 'text-muted hover:bg-surface2 hover:text-text',
            )}
          >
            <Icon className="h-3 w-3" />
            {label}
          </button>
        )
      })}
    </div>
  )
}

function LiveBadge() {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-success/40 bg-success/10 px-3 py-1 text-[10px] font-bold uppercase tracking-[0.15em] text-success shadow-sm shadow-success/10">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
      </span>
      Live
    </span>
  )
}

function ClockDisplay({ date, large }: { date: Date; large?: boolean }) {
  return (
    <div className={cn(
      'rounded-xl border border-border bg-surface/90 px-3.5 py-2 font-mono tabular-nums text-text shadow-sm backdrop-blur-sm',
      large ? 'text-base' : 'text-sm',
    )}>
      {date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
      <span className="ml-2 text-muted">
        {date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}
      </span>
    </div>
  )
}

function SlaChip({ met, attained, target }: { met: boolean; attained: number; target: number }) {
  return (
    <span className={cn(
      'inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs font-semibold',
      met ? 'border-success/40 bg-success/10 text-success' : 'border-danger/40 bg-danger/10 text-danger',
    )}>
      <Target className="h-3 w-3" />
      SLA {fmtPct(attained, 2)} / {target}%
    </span>
  )
}

function MetricChip({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border/60 bg-surface2/50 px-3 py-2.5 backdrop-blur-sm">
      <div className="flex items-center gap-1.5 text-[9px] font-bold uppercase tracking-wider text-muted">
        {icon}
        {label}
      </div>
      <div className="mt-1 text-lg font-bold tabular-nums text-text">{value}</div>
    </div>
  )
}

function DomainKpi({
  theme, label, value, pct, icon, to, issue, loading, large,
}: {
  theme: (typeof KPI_THEMES)[keyof typeof KPI_THEMES]
  label: string
  value: string
  pct: number
  icon: React.ReactNode
  to: string
  issue?: number
  loading?: boolean
  large?: boolean
}) {
  return (
    <Link
      to={to}
      className={cn(
        'group relative overflow-hidden rounded-xl border p-3.5 transition-all duration-200',
        'hover:-translate-y-0.5 hover:shadow-lg dark:hover:shadow-black/30',
        theme.card,
        large && 'p-4',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[10px] font-bold uppercase tracking-wider text-muted">{label}</div>
          {loading ? (
            <Skeleton className="mt-2 h-7 w-16" />
          ) : (
            <div className={cn('mt-1 font-black tabular-nums text-text', large ? 'text-2xl' : 'text-xl')}>{value}</div>
          )}
          {issue != null && issue > 0 && (
            <div className="mt-1 text-[11px] font-bold text-danger">{issue} issue{issue === 1 ? '' : 's'}</div>
          )}
        </div>
        <span className={cn('flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br text-white shadow-md', theme.icon)}>
          {icon}
        </span>
      </div>
      <div className="mt-3">
        <GradientBar pct={pct} compact barClass={theme.bar} />
      </div>
    </Link>
  )
}

function LiveStatusGrid({ summary, large }: { summary?: DeviceSummary; large?: boolean }) {
  const rows = [
    { label: 'Up', value: summary?.up ?? 0, bar: 'bg-success', pill: 'text-success bg-success/10 border-success/30' },
    { label: 'Degraded', value: summary?.degraded ?? 0, bar: 'bg-warning', pill: 'text-warning bg-warning/10 border-warning/30' },
    { label: 'Down', value: summary?.down ?? 0, bar: 'bg-danger', pill: 'text-danger bg-danger/10 border-danger/30' },
    { label: 'Maint.', value: summary?.maintenance ?? 0, bar: 'bg-info', pill: 'text-info bg-info/10 border-info/30' },
    { label: 'Unknown', value: summary?.unknown ?? 0, bar: 'bg-muted', pill: 'text-muted bg-surface2 border-border' },
  ]
  const total = summary?.total ?? 0
  return (
    <div className="space-y-3">
      <div className={cn('flex h-3.5 w-full overflow-hidden rounded-full bg-surface2 ring-1 ring-border/50', large && 'h-4')}>
        {total > 0 && rows.map((r) => r.value > 0 && (
          <div
            key={r.label}
            className={cn('h-full transition-all duration-500', r.bar)}
            style={{ width: `${(r.value / total) * 100}%` }}
            title={`${r.label}: ${r.value}`}
          />
        ))}
      </div>
      <div className="grid grid-cols-5 gap-1.5">
        {rows.map((r) => (
          <div key={r.label} className={cn('rounded-lg border px-1 py-2 text-center', r.pill)}>
            <div className={cn('font-black tabular-nums', large ? 'text-xl' : 'text-lg')}>{r.value}</div>
            <div className="text-[8px] font-bold uppercase tracking-wider opacity-80">{r.label}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function BreakdownPanel({
  title, icon, segments, hint, loading, large, accent,
}: {
  title: string
  icon: React.ReactNode
  segments: AvailSegment[]
  hint?: string
  loading?: boolean
  large?: boolean
  accent: 'emerald' | 'violet' | 'sky'
}) {
  const accentBorder = {
    emerald: 'border-l-emerald-500',
    violet: 'border-l-violet-500',
    sky: 'border-l-sky-500',
  }[accent]

  return (
    <GlassPanel>
      <PanelHeader icon={icon} title={title} hint={hint} accent={accentBorder} />
      <div className={cn('grid gap-3 p-3', large ? 'grid-cols-2 xl:grid-cols-3' : 'grid-cols-2')}>
        {loading && (
          Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-28 rounded-xl" />
          ))
        )}
        {!loading && segments.length === 0 && (
          <EmptyPanel icon={icon} text="No data for this window" />
        )}
        {!loading && segments.slice(0, large ? 9 : 6).map((seg, i) => {
          const accentStyle = TILE_ACCENTS[i % TILE_ACCENTS.length]
          return (
            <div
              key={seg.key}
              className={cn(
                'group relative overflow-hidden rounded-xl border border-border/60 bg-gradient-to-br p-3 transition-all duration-200',
                'hover:-translate-y-0.5 hover:shadow-lg',
                accentStyle.bg,
                accentStyle.glow,
              )}
            >
              <div className={cn('absolute left-0 top-0 h-full w-1', accentStyle.bar)} />
              <div className="flex items-center gap-3 pl-2">
                <RingGauge
                  value={seg.pct}
                  size={large ? 72 : 64}
                  stroke={large ? 7 : 6}
                  color={accentStyle.ring}
                />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-bold text-text" title={seg.label}>{seg.label}</div>
                  <div className="text-[10px] font-medium text-muted">{seg.up}/{seg.total} up</div>
                  <div className={cn('mt-0.5 text-lg font-black tabular-nums', availText(seg.pct))}>
                    {seg.pct.toFixed(1)}%
                  </div>
                  <GradientBar pct={seg.pct} compact className="mt-2" />
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </GlassPanel>
  )
}

function DataTablePanel({
  title, icon, hint, columns, rows, loading, empty, emptyTone = 'muted',
}: {
  title: string
  icon: React.ReactNode
  hint?: string
  columns: string[]
  rows: Array<{ key: string; cells: React.ReactNode[] }>
  loading?: boolean
  empty: string
  emptyTone?: 'muted' | 'success'
}) {
  return (
    <GlassPanel>
      <PanelHeader icon={icon} title={title} hint={hint} />
      <div className="overflow-x-auto">
        {loading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
          </div>
        ) : rows.length === 0 ? (
          <div className={cn('px-4 py-10 text-center text-sm font-medium', emptyTone === 'success' ? 'text-success' : 'text-muted')}>
            {empty}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-surface2/40 text-left text-[10px] font-bold uppercase tracking-wider text-muted">
                {columns.map((col, i) => (
                  <th key={col} className={cn('px-4 py-2.5', i > 0 && 'text-right')}>{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr
                  key={row.key}
                  className={cn(
                    'border-b border-border/40 transition-colors hover:bg-surface2/50',
                    ri % 2 === 0 && 'bg-surface2/20',
                  )}
                >
                  {row.cells.map((cell, ci) => (
                    <td key={ci} className={cn('px-4 py-2.5', ci > 0 && 'text-right')}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </GlassPanel>
  )
}

function GradientBar({
  pct, compact, barClass, className,
}: {
  pct: number
  compact?: boolean
  barClass?: string
  className?: string
}) {
  const grad = barClass || availBarGradient(pct)
  return (
    <div className={cn('flex items-center gap-2', compact ? '' : 'justify-end', className)}>
      <div className={cn(compact ? 'h-2 flex-1' : 'h-2 w-20', 'overflow-hidden rounded-full bg-surface2 ring-1 ring-border/40')}>
        <div
          className={cn('h-full rounded-full bg-gradient-to-r transition-all duration-500', grad)}
          style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
        />
      </div>
      <span className={cn('shrink-0 text-xs font-bold tabular-nums', availText(pct), compact ? '' : 'min-w-[52px] text-right')}>
        {pct.toFixed(1)}%
      </span>
    </div>
  )
}

function TrendTooltip({ active, payload, label, sla }: any) {
  if (!active || !payload?.length) return null
  const v = payload[0]?.value as number
  return (
    <div className="rounded-xl border border-border bg-surface/95 px-3 py-2 text-xs shadow-xl backdrop-blur-md">
      <div className="font-medium text-muted">{new Date(label).toLocaleString()}</div>
      <div className="mt-1 text-base font-black tabular-nums text-success">{v?.toFixed(3)}%</div>
      {sla != null && (
        <div className="mt-0.5 text-[10px] text-muted">SLA target {sla}%</div>
      )}
    </div>
  )
}

function ChartSkeleton() {
  return (
    <div className="flex h-full flex-col justify-end gap-2 px-4 pb-2">
      <Skeleton className="h-[85%] w-full rounded-lg" />
      <Skeleton className="h-3 w-full" />
    </div>
  )
}

function EmptyPanel({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="col-span-full flex flex-col items-center justify-center gap-2 py-10 text-muted">
      <span className="opacity-40">{icon}</span>
      <span className="text-sm">{text}</span>
    </div>
  )
}
