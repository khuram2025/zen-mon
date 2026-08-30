import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  Bell,
  BellOff,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock,
  Download,
  FileText,
  Gauge,
  Globe,
  HelpCircle,
  Info,
  LineChart,
  Loader2,
  LockKeyhole,
  MoreHorizontal,
  Network,
  Pause,
  Pencil,
  Play,
  Plug,
  Radar,
  Route,
  Settings as SettingsIcon,
  ShieldCheck,
  SlidersHorizontal,
  Terminal,
  Timer,
  Trash2,
  TrendingUp,
  Wrench,
  XCircle,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import {
  apiErrorMessage,
  axisRightPad,
  cn,
  relativeTime,
  timeAxisTickFormatter,
  timeTicks,
  timeTooltipLabelFormatter,
} from '@/lib/utils'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { TimeRangePicker, rangePhrase, useTimeRange } from '@/components/TimeRangePicker'
import {
  BAND_FILL,
  BAND_TEXT,
  PulseDot,
  UptimeBars,
  pulseStatusOf,
  uptimeBand,
  type UptimeBand,
} from '@/components/services/uptime'
import type { ServiceCheck, ServiceMetricPoint, ServiceMetricResponse } from '@/types'

const UP_COLOR = 'rgb(var(--success))'
const DOWN_COLOR = 'rgb(var(--danger))'
const WARN_COLOR = 'rgb(var(--warning))'

const TABS = [
  { key: 'overview', label: 'Overview', Icon: LineChart },
  { key: 'uptime', label: 'Uptime', Icon: CalendarDays },
  { key: 'incidents', label: 'Incidents & alerts', Icon: AlertTriangle },
  { key: 'config', label: 'Configuration', Icon: SlidersHorizontal },
] as const

type TabKey = (typeof TABS)[number]['key']

const statusMeta: Record<string, { label: string; tone: 'success' | 'danger' | 'warning' | 'outline'; Icon: typeof CheckCircle2 }> = {
  up: { label: 'Healthy', tone: 'success', Icon: CheckCircle2 },
  down: { label: 'Down', tone: 'danger', Icon: XCircle },
  degraded: { label: 'Degraded', tone: 'warning', Icon: AlertTriangle },
  warning: { label: 'Warning', tone: 'warning', Icon: AlertTriangle },
  unknown: { label: 'Unknown', tone: 'outline', Icon: HelpCircle },
}

const typeMeta: Record<string, { label: string; Icon: typeof Globe }> = {
  http: { label: 'HTTP', Icon: Globe },
  tcp: { label: 'TCP', Icon: Plug },
  tls: { label: 'TLS', Icon: ShieldCheck },
  icmp: { label: 'ICMP', Icon: Radar },
  dns: { label: 'DNS', Icon: Network },
}

type StatusHistoryEvent = {
  timestamp: string
  old_status: string | null
  new_status: string
  reason?: string | null
  duration_sec?: number | null
}

type ServiceAlert = {
  id: string
  status: 'active' | 'acknowledged' | 'resolved'
  severity: string
  message: string
  triggered_at: string
  acknowledged_at?: string | null
  resolved_at?: string | null
}

type RelatedResponse = {
  parent: ServiceCheck | null
  children: ServiceCheck[]
  same_device: ServiceCheck[]
  same_host: ServiceCheck[]
  same_group: ServiceCheck[]
}

type ProbeEvidenceStep = {
  index: number
  name: string
  status: 'up' | 'down'
  status_code: number | null
  response_time_ms: number
  content_matched: boolean | null
  error: string
  diagnosis: string | null
  response_url: string | null
  response_reason?: string | null
  content_type: string | null
  response_size_bytes: number | null
  redirect_count: number
  authentication_challenges?: string[]
}

type ProbeEvidenceResult = {
  status: 'up' | 'down' | 'unknown'
  response_time_ms: number
  error: string
  diagnosis: string | null
  details?: {
    status_code?: number | null
    authentication?: string
    steps?: ProbeEvidenceStep[]
  }
}

type ActivityEvent = {
  id: string
  timestamp: string
  kind: 'alert' | 'status'
  severity: 'critical' | 'warning' | 'info' | 'success'
  title: string
  subtitle?: string
  href?: string
}

type SlaStats = {
  uptime_pct: number | null
  sample_count: number
  incident_count: number
  total_downtime_sec: number
  longest_incident_sec: number
  mttr_sec: number | null
  mtbf_sec: number | null
  avg_response_ms: number | null
  p95_response_ms: number | null
  max_response_ms: number | null
  error_rate_pct: number | null
  uptime_streak_sec: number | null
}

type HourlyUptime = { ts: string; uptime_pct: number | null; sample_count: number }

type Outage = {
  start: number
  end: number | null
  kind: 'down' | 'warn'
  reason?: string
  clippedStart: boolean
}

/* ─── Formatters ────────────────────────────────────────────────────────── */

function formatDur(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec <= 0) return '—'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${Math.floor(sec % 60)}s`
  return `${Math.floor(sec)}s`
}

function formatMs(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  if (n >= 1000) return `${(n / 1000).toFixed(1)} s`
  return `${n.toFixed(n < 10 ? 1 : 0)} ms`
}

function pct(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return '—'
  return `${n.toFixed(digits)}%`
}

function statusOf(s: string) {
  return statusMeta[s] || statusMeta.unknown
}

function normalizeSeverity(s: string): ActivityEvent['severity'] {
  const v = (s || '').toLowerCase()
  if (v === 'critical' || v === 'down') return 'critical'
  if (v === 'warning' || v === 'degraded') return 'warning'
  if (v === 'success' || v === 'up' || v === 'ok') return 'success'
  return 'info'
}

function cleanAlertMessage(msg: string): string {
  return (msg || 'Alert').replace(/^\[ZenPlus\s+[A-Z]+\]\s*/, '')
}

/* ─── Page ──────────────────────────────────────────────────────────────── */

export function ServiceCheckDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [searchParams, setSearchParams] = useSearchParams()

  const tab = (TABS.find((t) => t.key === searchParams.get('tab'))?.key ?? 'overview') as TabKey
  const setTab = (next: TabKey) => {
    const p = new URLSearchParams(searchParams)
    if (next === 'overview') p.delete('tab')
    else p.set('tab', next)
    setSearchParams(p, { replace: true })
  }

  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [hsOpen, setHsOpen] = useState(false)
  const [maintOpen, setMaintOpen] = useState(false)
  const [eventsOpen, setEventsOpen] = useState(false)
  const [hsConfig, setHsConfig] = useState<HealthScoreConfig>(() => loadHealthScoreConfig())
  const [manualProbe, setManualProbe] = useState<{ result: ProbeEvidenceResult; observedAt: string } | null>(null)
  const [nowTick, setNowTick] = useState(() => Date.now())

  useEffect(() => {
    const h = setInterval(() => setNowTick(Date.now()), 1000)
    return () => clearInterval(h)
  }, [])

  const { data: check, isLoading, isError } = useQuery<ServiceCheck>({
    queryKey: ['service-check', id],
    queryFn: async () => (await api.get(`/service-checks/${id}`)).data,
    enabled: !!id,
    refetchInterval: 15_000,
  })

  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const granularity = range.hours <= 24 ? 'raw' : 'auto'

  const metricsQ = useQuery<ServiceMetricResponse>({
    queryKey: ['service-check-metrics', id, range.fromISO, range.toISO, granularity],
    queryFn: async () =>
      (await api.get(
        `/service-checks/${id}/metrics?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}&granularity=${granularity}`,
      )).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const points: ServiceMetricPoint[] = metricsQ.data?.points || []

  const { data: statusHistory = [] } = useQuery<StatusHistoryEvent[]>({
    queryKey: ['service-status-history', id, range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(
        `/service-checks/${id}/status-history?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}&limit=500`,
      )).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })

  const slaQ = useQuery<SlaStats>({
    // Send explicit bounds: `hours` alone always ends at now, so a custom historical range
    // would silently be scored against the last N hours instead.
    queryKey: ['service-sla', id, range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(
        `/service-checks/${id}/sla?hours=${range.hours}&from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}`,
      )).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const sla = slaQ.data

  // Fixed windows for the hero strip — always the last 24h/7d/30d ending now, independent
  // of the range picker, the way uptime products present a monitor.
  const sla24Q = useQuery<SlaStats>({
    queryKey: ['service-sla-fixed', id, 24],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?hours=24`)).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })
  const sla7dQ = useQuery<SlaStats>({
    queryKey: ['service-sla-fixed', id, 168],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?hours=168`)).data,
    enabled: !!id && !!check,
    refetchInterval: 120_000,
  })
  const sla30dQ = useQuery<SlaStats>({
    queryKey: ['service-sla-fixed', id, 720],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?hours=720`)).data,
    enabled: !!id && !!check,
    refetchInterval: 300_000,
  })

  const hourlyQ = useQuery<{ hours: HourlyUptime[] }>({
    queryKey: ['service-hourly-uptime', id],
    queryFn: async () => (await api.get(`/service-checks/${id}/hourly-uptime?days=30`)).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })
  const hourly = hourlyQ.data?.hours || []

  const { data: alertsResp } = useQuery<{ data: ServiceAlert[]; meta: { total: number } }>({
    queryKey: ['service-alerts', id],
    queryFn: async () => (await api.get(`/alerts?service_check_id=${id}&limit=50`)).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const alerts = useMemo(() => alertsResp?.data || [], [alertsResp])
  const activeAlerts = useMemo(() => alerts.filter((a) => a.status === 'active'), [alerts])

  // Whether any enabled alert rule can ever produce an alert for this check. When none
  // does, an empty Alerts panel is expected behaviour rather than a fault, and saying so
  // is the difference between a useful empty state and a confusing one.
  const { data: ruleCoverage } = useQuery<{ covered: boolean; count: number }>({
    queryKey: ['service-alert-rule-coverage', id, check?.group_id],
    queryFn: async () => {
      const raw = (await api.get('/alert-rules')).data
      const rules: any[] = Array.isArray(raw) ? raw : raw?.data || []
      const matches = rules.filter(
        (r) =>
          r?.enabled !== false &&
          (r?.service_check_id === id ||
            (!!check?.group_id && r?.service_check_group_id === check.group_id) ||
            r?.metric === 'service_status'),
      )
      return { covered: matches.length > 0, count: matches.length }
    },
    enabled: !!id && !!check,
    staleTime: 120_000,
    retry: false,
  })

  const { data: related, isError: relatedError } = useQuery<RelatedResponse>({
    queryKey: ['service-related', id, check?.device_id, check?.group_id, check?.target_host, check?.parent_check_id],
    queryFn: async () => {
      try {
        return (await api.get(`/service-checks/${id}/related`)).data
      } catch {
        return fetchRelatedFallback(check!)
      }
    },
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })

  // Widest-window fallback so a quiet 1h window still shows the service's real history.
  const wantsEventFallback = statusHistory.length === 0 && alerts.length === 0
  const wideFrom = useMemo(() => new Date(Date.now() - 720 * 3600_000).toISOString(), [])
  const { data: fbHistory = [] } = useQuery<StatusHistoryEvent[]>({
    queryKey: ['service-status-history-fallback', id],
    queryFn: async () =>
      (await api.get(
        `/service-checks/${id}/status-history?from=${encodeURIComponent(wideFrom)}&to=${encodeURIComponent(new Date().toISOString())}&limit=100`,
      )).data,
    enabled: !!id && !!check && wantsEventFallback,
    staleTime: 60_000,
  })

  const del = useMutation({
    mutationFn: async () => api.delete(`/service-checks/${id}`),
    onSuccess: () => {
      toast.success('Service check deleted')
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      navigate('/services')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const runNow = useMutation({
    mutationFn: async () => (await api.post(`/service-checks/${id}/test`, {})).data,
    onSuccess: (d: any) => {
      setManualProbe({ result: d as ProbeEvidenceResult, observedAt: new Date().toISOString() })
      toast.success('Probe complete', d?.status === 'up' ? `Up · ${Math.round(d.response_time_ms || 0)} ms` : `Down: ${d?.error || ''}`)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-check-metrics', id] })
      qc.invalidateQueries({ queryKey: ['service-sla', id] })
      qc.invalidateQueries({ queryKey: ['service-status-history', id] })
    },
    onError: (e: any) => toast.error('Probe failed to run', apiErrorMessage(e)),
  })

  const togglePause = useMutation({
    mutationFn: async () => (await api.put(`/service-checks/${id}`, { enabled: !check?.enabled })).data,
    onSuccess: () => {
      toast.success(check?.enabled ? 'Checks paused' : 'Checks resumed')
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Failed', apiErrorMessage(e)),
  })

  const startMaintenance = useMutation({
    mutationFn: async (durationHours: number) => {
      const now = new Date()
      const end = new Date(now.getTime() + durationHours * 3_600_000)
      return (
        await api.post('/service-check-maintenance', {
          scope_type: 'check',
          scope_check_id: id,
          starts_at: now.toISOString(),
          ends_at: end.toISOString(),
          reason: `Manual ${durationHours}h window from service detail`,
        })
      ).data
    },
    onSuccess: () => {
      toast.success('Maintenance window started')
      setMaintOpen(false)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Failed to start maintenance', apiErrorMessage(e)),
  })

  const startMaintenanceCustom = useMutation({
    mutationFn: async (args: { startsAtISO: string; endsAtISO: string }) =>
      (
        await api.post('/service-check-maintenance', {
          scope_type: 'check',
          scope_check_id: id,
          starts_at: args.startsAtISO,
          ends_at: args.endsAtISO,
          reason: 'Custom maintenance window from service detail',
        })
      ).data,
    onSuccess: (_d, args) => {
      const start = new Date(args.startsAtISO)
      const isFuture = start.getTime() > Date.now() + 60_000
      toast.success(
        isFuture ? 'Maintenance window scheduled' : 'Maintenance window started',
        `${start.toLocaleString()} → ${new Date(args.endsAtISO).toLocaleString()}`,
      )
      setMaintOpen(false)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Failed to start maintenance', apiErrorMessage(e)),
  })

  const endMaintenance = useMutation({
    mutationFn: async () => {
      const list: any[] = (await api.get('/service-check-maintenance')).data || []
      const now = Date.now()
      const active = list.filter(
        (m) =>
          m.scope_type === 'check' &&
          m.scope_check_id === id &&
          Date.parse(m.starts_at) <= now &&
          Date.parse(m.ends_at) >= now,
      )
      if (active.length === 0) throw new Error('No active maintenance window for this check')
      await Promise.all(active.map((m) => api.delete(`/service-check-maintenance/${m.id}`)))
      return active.length
    },
    onSuccess: (n: number) => {
      toast.success(n > 1 ? `Ended ${n} overlapping maintenance windows` : 'Maintenance window ended')
      setMaintOpen(false)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Failed to end maintenance', apiErrorMessage(e)),
  })

  const ackAllAlerts = useMutation({
    mutationFn: async () => {
      if (activeAlerts.length === 0) throw new Error('No active alerts to acknowledge')
      await Promise.all(activeAlerts.map((a) => api.post(`/alerts/${a.id}/acknowledge`)))
      return activeAlerts.length
    },
    onSuccess: (n: number) => {
      toast.success(`Acknowledged ${n} alert${n === 1 ? '' : 's'}`)
      qc.invalidateQueries({ queryKey: ['service-alerts', id] })
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => toast.info('Nothing to acknowledge', apiErrorMessage(e)),
  })

  const exportConfig = () => {
    if (!check) return
    const blob = new Blob([JSON.stringify({ ...check, _exported_at: new Date().toISOString() }, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${check.name.replace(/\s+/g, '-').toLowerCase()}-config.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    toast.success('Config exported')
  }

  const activityEvents = useMemo(
    () => buildActivityEvents(statusHistory, alerts, range.fromISO, range.toISO, check),
    [statusHistory, alerts, range.fromISO, range.toISO, check],
  )
  const fallbackEvents = useMemo(
    () => (wantsEventFallback ? buildActivityEvents(fbHistory, alerts, wideFrom, new Date().toISOString(), check) : []),
    [wantsEventFallback, fbHistory, alerts, wideFrom, check],
  )
  const derived = useMemo(
    () => (check ? deriveWindowStats(points, statusHistory, check, fromTs, toTs) : {
      uptime_pct: null, error_rate_pct: null, incident_count: 0, avg_ms: null, p95_ms: null, streak_sec: null,
    }),
    [points, statusHistory, check, fromTs, toTs],
  )

  const outages = useMemo(
    () => (check ? buildOutages(statusHistory, fromTs, toTs, check) : []),
    [statusHistory, fromTs, toTs, check],
  )
  const days = useMemo(() => buildDailyUptime(hourly, 30, check?.check_interval || 60), [hourly, check?.check_interval])

  if (isLoading) return <DetailSkeleton />

  if (isError || !check) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-24 text-center">
        <XCircle className="h-10 w-10 text-danger" />
        <h3 className="text-lg font-semibold">Service check not found</h3>
        <p className="max-w-sm text-sm text-muted">
          It may have been deleted, or the link points at an id that no longer exists.
        </p>
        <Button variant="outline" size="sm" asChild>
          <Link to="/services"><ArrowLeft className="h-4 w-4" /> Back to services</Link>
        </Button>
      </div>
    )
  }

  const lastCheckMs = check.last_check_at ? Date.parse(check.last_check_at) : null
  const intervalMs = (check.check_interval || 60) * 1000
  const nextPollMs = lastCheckMs ? lastCheckMs + intervalMs : null
  const secsToNext = nextPollMs ? Math.max(0, Math.floor((nextPollMs - nowTick) / 1000)) : null

  const uptimePct = sla?.uptime_pct ?? derived.uptime_pct
  const errorRatePct = sla?.error_rate_pct ?? derived.error_rate_pct
  const incidentCount = sla?.incident_count ?? derived.incident_count
  const avgMs = sla?.avg_response_ms ?? derived.avg_ms ?? check.last_response_ms
  const p95Ms = sla?.p95_response_ms ?? derived.p95_ms
  const streakSec = sla?.uptime_streak_sec ?? derived.streak_sec
  const downtimeSec = sla?.total_downtime_sec ?? 0

  const healthScore = computeHealthScore(
    { uptime_pct: uptimePct, error_rate_pct: errorRatePct, incident_count: incidentCount, p95_response_ms: p95Ms },
    hsConfig,
  )

  const latestPoint = latestProbePoint(points, check)

  // The poller updates Postgres and ClickHouse on separate paths, so a live last_check_at
  // with no stored samples means the metrics pipeline is broken, not that the service is idle.
  const metricsStale =
    !!lastCheckMs &&
    nowTick - lastCheckMs < Math.max(10 * 60_000, intervalMs * 5) &&
    points.length === 0 &&
    !metricsQ.isLoading &&
    check.enabled &&
    toTs - fromTs >= intervalMs * 3

  return (
    <div className="space-y-4 pb-10">
      <ServiceHeader
        check={check}
        secsToNext={secsToNext}
        rangePicker={(
          <TimeRangePicker
            rangeIdx={rangeIdx}
            isCustom={isCustom}
            customFrom={range.fromISO}
            customTo={range.toISO}
            onPreset={setPreset}
            onCustom={setCustom}
          />
        )}
        onEdit={() => setEditOpen(true)}
        onDelete={() => setDeleteOpen(true)}
        onRunProbe={() => runNow.mutate()}
        runPending={runNow.isPending}
        onPause={() => togglePause.mutate()}
        pausePending={togglePause.isPending}
        onMaintenance={() => {
          if (check.in_maintenance) endMaintenance.mutate()
          else setMaintOpen(true)
        }}
        onAck={() => ackAllAlerts.mutate()}
        activeAlertCount={activeAlerts.length}
        onLogs={() => navigate(`/services/${id}/incidents?filter=all`)}
        onExport={exportConfig}
      />

      {check.in_maintenance && (
        <StatusBanner
          tone="info"
          icon={Wrench}
          title="In maintenance"
          body="Probes still run. Alerts are suppressed and this window is excluded from SLA."
          actionLabel={endMaintenance.isPending ? 'Ending…' : 'End now'}
          onAction={() => endMaintenance.mutate()}
          actionDisabled={endMaintenance.isPending}
        />
      )}
      {!check.enabled && !check.in_maintenance && (
        <StatusBanner
          tone="warning"
          icon={Pause}
          title="Checks paused"
          body="The poller will not schedule this check until it is resumed."
          actionLabel={togglePause.isPending ? 'Resuming…' : 'Resume'}
          onAction={() => togglePause.mutate()}
          actionDisabled={togglePause.isPending}
        />
      )}
      {check.last_error && (check.status === 'down' || check.status === 'warning') && !check.in_maintenance && (
        <StatusBanner
          tone={check.status === 'down' ? 'danger' : 'warning'}
          icon={check.status === 'down' ? XCircle : AlertTriangle}
          title={check.status === 'down' ? 'Service is down' : 'Service is degraded'}
          body={check.last_error}
          meta={check.last_check_at ? `Last probe ${relativeTime(check.last_check_at)}` : undefined}
          mono
        />
      )}
      {metricsStale && (
        <StatusBanner
          tone="warning"
          icon={AlertCircle}
          title="Probing, but no samples are being stored"
          body={`The last probe ran ${relativeTime(check.last_check_at!)} yet no metrics were recorded for this window. Charts and SLA will stay empty until the metrics pipeline is restored.`}
        />
      )}

      <HeroStrip
        check={check}
        sla24={sla24Q.data}
        sla7d={sla7dQ.data}
        sla30d={sla30dQ.data}
        loading={sla24Q.isLoading && sla7dQ.isLoading}
        ongoingOutage={outages.find((o) => o.end == null) || null}
        nowTick={nowTick}
      />

      <ThirtyDayStrip
        days={days}
        onSelectDay={(d) => setCustom(new Date(d.start).toISOString(), new Date(Math.min(d.end, Date.now())).toISOString())}
      />

      <div
        role="tablist"
        aria-label="Service detail sections"
        className="sticky top-0 z-20 -mx-1 flex gap-1 overflow-x-auto border-b border-border bg-bg/95 px-1 backdrop-blur supports-[backdrop-filter]:bg-bg/80"
      >
        {TABS.map((t) => {
          const active = tab === t.key
          const count = t.key === 'incidents' ? outages.length + activeAlerts.length : 0
          return (
            <button
              key={t.key}
              role="tab"
              aria-selected={active}
              onClick={() => setTab(t.key)}
              className={cn(
                'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors',
                active ? 'border-primary text-primary' : 'border-transparent text-muted hover:border-border hover:text-text',
              )}
            >
              <t.Icon className="h-4 w-4" />
              {t.label}
              {count > 0 && (
                <span className="ml-0.5 rounded-full bg-surface3 px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-text2">
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {tab === 'overview' && (
        <div className="space-y-4">
          <AvailabilityTimeline
            points={points}
            statusHistory={statusHistory}
            check={check}
            rangeLabel={range.label}
            fromTs={fromTs}
            toTs={toTs}
          />

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.85fr)]">
            <PerformanceChart
              points={points}
              statusHistory={statusHistory}
              rangeLabel={range.label}
              rangeHours={range.hours}
              fromTs={fromTs}
              toTs={toTs}
              loading={metricsQ.isLoading}
              error={metricsQ.isError}
              onRetry={() => metricsQ.refetch()}
              stats={[
                { label: 'Avg', value: formatMs(avgMs) },
                { label: 'P95', value: formatMs(p95Ms) },
                { label: 'Error', value: pct(errorRatePct, 2) },
                { label: 'Incidents', value: String(incidentCount) },
              ]}
            />
            <div className="space-y-4">
              <LatestProbeCard check={check} latest={latestPoint} recent={points.slice(-6).reverse()} manualProbe={manualProbe} />
              <HealthScoreCard
                score={healthScore.score}
                tint={healthScore.tint}
                label={healthScore.label}
                factors={healthScore.factors}
                onViewDetails={() => setHsOpen(true)}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            <ActivityLogCard
              events={activityEvents.length > 0 ? activityEvents : fallbackEvents}
              rangeLabel={range.label}
              showingFallback={activityEvents.length === 0 && fallbackEvents.length > 0}
              onViewAll={() => setEventsOpen(true)}
              onWiden={() => setPreset(3)}
            />
            <RelatedServicesCard related={related} failed={relatedError} />
            <ConfigSummaryCard check={check} onEdit={() => setEditOpen(true)} onOpenConfig={() => setTab('config')} />
          </div>
        </div>
      )}

      {tab === 'uptime' && (
        <div className="space-y-4">
          <UptimeCalendar
            days={days}
            loading={hourlyQ.isLoading}
            error={hourlyQ.isError}
            onRetry={() => hourlyQ.refetch()}
            onSelectDay={(d) => setCustom(new Date(d.start).toISOString(), new Date(Math.min(d.end, Date.now())).toISOString())}
          />
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(300px,0.9fr)]">
            <DailyUptimeChart days={days} />
            <SlaSummaryCard sla={sla} rangeLabel={range.label} loading={slaQ.isLoading} error={slaQ.isError} onRetry={() => slaQ.refetch()} />
          </div>
        </div>
      )}

      {tab === 'incidents' && (
        <IncidentsTab
          outages={outages}
          alerts={alerts}
          rangeLabel={range.label}
          checkId={id}
          ruleCovered={ruleCoverage?.covered}
          onAck={() => ackAllAlerts.mutate()}
          ackDisabled={activeAlerts.length === 0 || ackAllAlerts.isPending}
          onWiden={() => setPreset(3)}
        />
      )}

      {tab === 'config' && <ConfigTab check={check} onEdit={() => setEditOpen(true)} onExport={exportConfig} />}

      <ServiceCheckFormDialog open={editOpen} onOpenChange={setEditOpen} check={check} />
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete ${check.name}?`}
        description="Removes the check and its metrics history."
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => del.mutate()}
      />
      <MaintenanceDialog
        open={maintOpen}
        onOpenChange={setMaintOpen}
        inMaintenance={!!check.in_maintenance}
        onStart={(hours) => startMaintenance.mutate(hours)}
        onStartCustom={(startsAtISO, endsAtISO) => startMaintenanceCustom.mutate({ startsAtISO, endsAtISO })}
        onEnd={() => endMaintenance.mutate()}
        starting={startMaintenance.isPending || startMaintenanceCustom.isPending}
        ending={endMaintenance.isPending}
      />
      <HealthScoreDetailsDialog
        open={hsOpen}
        onOpenChange={setHsOpen}
        score={healthScore.score}
        tint={healthScore.tint}
        label={healthScore.label}
        factors={healthScore.factors}
        config={hsConfig}
        onConfigChange={(next) => {
          setHsConfig(next)
          saveHealthScoreConfig(next)
        }}
      />
      <EventsDialog
        open={eventsOpen}
        onOpenChange={setEventsOpen}
        events={activityEvents.length > 0 ? activityEvents : fallbackEvents}
        rangeLabel={range.label}
        checkId={id}
      />
    </div>
  )
}

/* ─── Shell states ──────────────────────────────────────────────────────── */

function DetailSkeleton() {
  return (
    <div className="space-y-4" aria-label="Loading service check">
      <Skeleton className="h-[74px] w-full" />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[92px]" />)}
      </div>
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-24 w-full" />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.85fr)]">
        <Skeleton className="h-[280px]" />
        <Skeleton className="h-[280px]" />
      </div>
    </div>
  )
}

function PanelState({
  loading, error, empty, onRetry, loadingText, errorText, children,
}: {
  loading?: boolean
  error?: boolean
  empty?: boolean
  onRetry?: () => void
  loadingText?: string
  errorText?: string
  children: React.ReactNode
}) {
  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> {loadingText || 'Loading…'}
      </div>
    )
  }
  if (error) {
    return (
      <div className="py-8 text-center text-xs text-danger">
        {errorText || 'Could not load this data.'}
        {onRetry && (
          <button type="button" onClick={onRetry} className="ml-1.5 font-medium underline">Retry</button>
        )}
      </div>
    )
  }
  if (empty) return <>{children}</>
  return <>{children}</>
}

function EmptyState({ icon: Icon, title, description, action }: {
  icon: typeof Info
  title: string
  description: string
  action?: React.ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center px-4 py-10 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-primary/10 text-primary">
        <Icon className="h-5 w-5" />
      </span>
      <h3 className="mt-3 text-sm font-semibold text-text">{title}</h3>
      <p className="mt-1 max-w-md text-xs leading-relaxed text-muted">{description}</p>
      {action && <div className="mt-3">{action}</div>}
    </div>
  )
}

function SectionCard({
  title, subtitle, actions, children, className, bodyClassName,
}: {
  title: string
  subtitle?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <Card className={cn('flex flex-col overflow-hidden', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight text-text">{title}</h3>
          {subtitle && <div className="mt-0.5 text-[11px] text-muted">{subtitle}</div>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      <CardContent className={cn('flex-1 p-4', bodyClassName)}>{children}</CardContent>
    </Card>
  )
}

/* ─── Header ────────────────────────────────────────────────────────────── */

function ServiceHeader({
  check, secsToNext, rangePicker, onEdit, onDelete, onRunProbe, runPending,
  onPause, pausePending, onMaintenance, onAck, activeAlertCount, onLogs, onExport,
}: {
  check: ServiceCheck
  secsToNext: number | null
  rangePicker: React.ReactNode
  onEdit: () => void
  onDelete: () => void
  onRunProbe: () => void
  runPending: boolean
  onPause: () => void
  pausePending: boolean
  onMaintenance: () => void
  onAck: () => void
  activeAlertCount: number
  onLogs: () => void
  onExport: () => void
}) {
  const t = typeMeta[check.check_type] || typeMeta.http
  const target = check.target_url || `${check.target_host}${check.target_port ? `:${check.target_port}` : ''}`
  const nextLabel = !check.enabled || secsToNext == null
    ? '—'
    : `${String(Math.floor(secsToNext / 60)).padStart(2, '0')}:${String(secsToNext % 60).padStart(2, '0')}`
  const pulse = pulseStatusOf(check.status, check.enabled)
  const statusWord = !check.enabled
    ? 'Paused'
    : check.status === 'up' ? 'Up'
      : check.status === 'down' ? 'Down'
        : check.status === 'unknown' ? 'Pending'
          : 'Warning'
  const statusTone = !check.enabled
    ? 'text-muted'
    : check.status === 'up' ? 'text-success'
      : check.status === 'down' ? 'text-danger'
        : check.status === 'unknown' ? 'text-muted'
          : 'text-warning'

  return (
    <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <Link
          to="/services"
          className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border text-muted transition-colors hover:bg-surface2 hover:text-text"
          aria-label="Back to services"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <PulseDot status={pulse} size="lg" />
            <h1 className="truncate text-xl font-semibold tracking-tight">{check.name}</h1>
            <span className={cn('text-sm font-semibold', statusTone)}>{statusWord}</span>
            <span className="inline-flex items-center gap-1 rounded border border-border bg-surface2/60 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider text-muted">
              <t.Icon className="h-2.5 w-2.5" />{t.label}
            </span>
            {check.in_maintenance && <Badge variant="info"><Wrench className="h-3 w-3" />Maintenance</Badge>}
            {activeAlertCount > 0 && <Badge variant="danger"><Bell className="h-3 w-3" />{activeAlertCount} active</Badge>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11px] text-muted">
            <span className="inline-flex min-w-0 max-w-full items-center gap-1.5">
              <Globe className="h-3 w-3 shrink-0 text-primary" />
              <span className="truncate font-mono text-[11px] text-text2" title={target}>{target}</span>
            </span>
            <Dot />
            <span className="inline-flex items-center gap-1"><Timer className="h-3 w-3" />every {check.check_interval}s</span>
            <Dot />
            <span>checked {check.last_check_at ? relativeTime(check.last_check_at) : 'never'}</span>
            <Dot />
            <span className="tabular-nums">next in {nextLabel}</span>
            {check.device_id && (
              <>
                <Dot />
                <Link to={`/devices/${check.device_id}`} className="hover:text-primary hover:underline">
                  {check.device_hostname || 'Linked device'}
                </Link>
              </>
            )}
            {check.group_name && (
              <>
                <Dot />
                <span>{check.group_name}</span>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2">
        {rangePicker}
        <span className="hidden h-5 w-px bg-border sm:inline-block" />
        <Button size="sm" className="h-8" onClick={onRunProbe} disabled={runPending}>
          {runPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          Run probe
        </Button>
        <Button variant="outline" size="sm" className="h-8" onClick={onPause} disabled={pausePending}>
          {check.enabled ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
          {check.enabled ? 'Pause' : 'Resume'}
        </Button>
        <Button variant="outline" size="sm" className="h-8" onClick={onEdit}>
          <Pencil className="h-3.5 w-3.5" /> Edit
        </Button>
        <MoreMenu
          items={[
            { label: check.in_maintenance ? 'End maintenance' : 'Schedule maintenance', icon: Wrench, onSelect: onMaintenance },
            { label: `Acknowledge alerts${activeAlertCount > 0 ? ` (${activeAlertCount})` : ''}`, icon: Check, onSelect: onAck, disabled: activeAlertCount === 0 },
            { label: 'Incident log', icon: FileText, onSelect: onLogs },
            { label: 'Export config', icon: Download, onSelect: onExport },
            { label: 'Delete check', icon: Trash2, onSelect: onDelete, destructive: true },
          ]}
        />
      </div>
    </div>
  )
}

/* ─── Hero strip: fixed-window availability, uptime-product style ───────── */

function HeroStrip({ check, sla24, sla7d, sla30d, loading, ongoingOutage, nowTick }: {
  check: ServiceCheck
  sla24?: SlaStats
  sla7d?: SlaStats
  sla30d?: SlaStats
  loading: boolean
  ongoingOutage: Outage | null
  nowTick: number
}) {
  if (loading) {
    return (
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[86px]" />)}
      </div>
    )
  }

  const isDown = check.status === 'down' || check.status === 'warning' || check.status === 'degraded'
  const downForSec = ongoingOutage ? Math.max(0, (nowTick - ongoingOutage.start) / 1000) : null

  let statusCell: { label: string; value: string; tone: string; sub: string }
  if (!check.enabled) {
    statusCell = { label: 'Monitoring', value: 'Paused', tone: 'text-muted', sub: 'probes are not scheduled' }
  } else if (isDown) {
    statusCell = {
      label: check.status === 'down' ? 'Currently down for' : 'Degraded for',
      value: downForSec != null ? `${ongoingOutage?.clippedStart ? '≥ ' : ''}${formatDur(downForSec)}` : '—',
      tone: check.status === 'down' ? 'text-danger' : 'text-warning',
      sub: check.last_error ? check.last_error.slice(0, 60) : 'no error detail',
    }
  } else if (check.status === 'unknown') {
    statusCell = { label: 'Monitoring', value: 'Pending', tone: 'text-muted', sub: 'waiting for the first probe' }
  } else {
    statusCell = {
      label: 'Currently up for',
      value: formatDur(sla24?.uptime_streak_sec),
      tone: 'text-success',
      sub: sla24?.uptime_streak_sec
        ? `since ${new Date(nowTick - sla24.uptime_streak_sec * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`
        : 'no downtime recorded',
    }
  }

  const windowCell = (label: string, s?: SlaStats) => {
    const band = uptimeBand(s?.uptime_pct)
    const down = s?.total_downtime_sec || 0
    const inc = s?.incident_count || 0
    return {
      label,
      value: s?.uptime_pct != null ? `${s.uptime_pct.toFixed(s.uptime_pct >= 99.95 ? 2 : 2)}%` : '—',
      tone: BAND_TEXT[band],
      sub: s?.uptime_pct == null
        ? 'no data in this window'
        : down > 0
          ? `${formatDur(down)} down · ${inc} incident${inc === 1 ? '' : 's'}`
          : 'no downtime',
    }
  }

  const cells = [
    statusCell,
    windowCell('Last 24 hours', sla24),
    windowCell('Last 7 days', sla7d),
    windowCell('Last 30 days', sla30d),
  ]

  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      {cells.map((c, i) => (
        <div key={c.label} className="rounded-xl border border-border bg-surface px-4 py-3 transition-colors hover:border-border-strong">
          <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">
            {i === 0 && <PulseDot status={pulseStatusOf(check.status, check.enabled)} size="sm" />}
            {c.label}
          </div>
          <div className={cn('mt-1 text-[24px] font-bold leading-none tabular-nums', c.tone)}>{c.value}</div>
          <div className="mt-1 truncate text-[10.5px] text-muted" title={c.sub}>{c.sub}</div>
        </div>
      ))}
    </div>
  )
}

/* ─── 30-day bar strip ──────────────────────────────────────────────────── */

function ThirtyDayStrip({ days, onSelectDay }: {
  days: DayUptime[]
  onSelectDay: (d: DayUptime) => void
}) {
  const measured = days.filter((d) => d.uptimePct != null)
  const totalSamples = measured.reduce((s, d) => s + d.samples, 0)
  const overall = totalSamples > 0
    ? measured.reduce((s, d) => s + (d.uptimePct as number) * d.samples, 0) / totalSamples
    : null
  const totalDowntime = days.reduce((s, d) => s + d.downtimeSec, 0)

  const cells = days.map((d) => ({
    key: d.key,
    pct: d.uptimePct,
    title:
      d.uptimePct == null
        ? `${d.date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })} — not monitored`
        : `${d.date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })} — ${d.uptimePct.toFixed(2)}% up${d.downtimeSec > 0 ? ` · ${formatDur(d.downtimeSec)} down` : ''}`,
  }))

  return (
    <Card>
      <CardContent className="px-4 py-3">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-baseline gap-2">
            <h3 className="text-sm font-semibold tracking-tight">Last 30 days</h3>
            <span className="text-[11px] text-muted">click a day to zoom the page to it</span>
          </div>
          <div className="flex items-baseline gap-3 text-xs">
            {totalDowntime > 0 && <span className="text-muted">{formatDur(totalDowntime)} down</span>}
            {overall != null && (
              <span className={cn('font-mono font-semibold tabular-nums', BAND_TEXT[uptimeBand(overall)])}>
                {overall.toFixed(3)}%
              </span>
            )}
          </div>
        </div>
        <UptimeBars
          cells={cells}
          className="h-9"
          onSelect={(key) => {
            const d = days.find((x) => x.key === key)
            if (d) onSelectDay(d)
          }}
        />
        <div className="mt-1.5 flex justify-between text-[10px] text-muted">
          <span>30 days ago</span>
          <span>Today</span>
        </div>
      </CardContent>
    </Card>
  )
}

function Dot() {
  return <span aria-hidden className="text-border-strong">·</span>
}

function MoreMenu({ items }: {
  items: Array<{ label: string; icon: typeof Wrench; onSelect: () => void; disabled?: boolean; destructive?: boolean }>
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={ref} className="relative">
      <Button
        variant="outline"
        size="icon"
        className="h-8 w-8"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        onClick={() => setOpen((v) => !v)}
      >
        <MoreHorizontal className="h-4 w-4" />
      </Button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-1 w-56 overflow-hidden rounded-lg border border-border bg-surface p-1 shadow-elevated"
        >
          {items.map((it) => (
            <button
              key={it.label}
              role="menuitem"
              disabled={it.disabled}
              onClick={() => { setOpen(false); it.onSelect() }}
              className={cn(
                'flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs font-medium transition-colors',
                it.disabled
                  ? 'cursor-not-allowed text-muted/50'
                  : it.destructive
                    ? 'text-danger hover:bg-danger/10'
                    : 'text-text2 hover:bg-surface2 hover:text-text',
              )}
            >
              <it.icon className="h-3.5 w-3.5 shrink-0" />
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function StatusBanner({
  tone, icon: Icon, title, body, meta, actionLabel, onAction, actionDisabled, mono,
}: {
  tone: 'danger' | 'warning' | 'info'
  icon: typeof XCircle
  title: string
  body: string
  meta?: string
  actionLabel?: string
  onAction?: () => void
  actionDisabled?: boolean
  mono?: boolean
}) {
  const wrap = tone === 'danger'
    ? 'border-danger/40 bg-danger/10'
    : tone === 'warning'
      ? 'border-warning/40 bg-warning/10'
      : 'border-info/40 bg-info/10'
  const fg = tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-info'
  return (
    <div className={cn('flex items-start gap-3 rounded-lg border px-3 py-2.5', wrap)}>
      <Icon className={cn('mt-0.5 h-4 w-4 shrink-0', fg)} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className={cn('text-xs font-semibold uppercase tracking-wider', fg)}>{title}</span>
          {meta && <span className="text-[11px] text-muted">{meta}</span>}
        </div>
        <p className={cn('mt-0.5 break-words text-[12.5px] leading-relaxed', mono && 'font-mono')}>{body}</p>
      </div>
      {actionLabel && onAction && (
        <Button size="sm" className="shrink-0" onClick={onAction} disabled={actionDisabled}>{actionLabel}</Button>
      )}
    </div>
  )
}

/* ─── Availability timeline ─────────────────────────────────────────────── */

function AvailabilityTimeline({
  points, statusHistory, check, rangeLabel, fromTs, toTs,
}: {
  points: ServiceMetricPoint[]
  statusHistory: StatusHistoryEvent[]
  check: ServiceCheck
  rangeLabel: string
  fromTs: number
  toTs: number
}) {
  const buckets = useMemo(
    () => buildAvailabilityBuckets(points, statusHistory, check, fromTs, toTs),
    [points, statusHistory, check, fromTs, toTs],
  )
  const covered = buckets.filter((b) => b.state !== 'gap')
  const upCount = covered.filter((b) => b.state === 'up').length
  const downCount = covered.filter((b) => b.state === 'down' || b.state === 'warn').length
  const pctUp = covered.length ? (upCount / covered.length) * 100 : null
  const coverage = buckets.length ? (covered.length / buckets.length) * 100 : 0

  return (
    <SectionCard
      title="Availability timeline"
      subtitle={
        <span>
          {rangeLabel}
          {points.length > 0 && ` · ${points.length} probe${points.length === 1 ? '' : 's'}`}
          {covered.length > 0 && coverage < 95 && ` · ${coverage.toFixed(0)}% of the window has data`}
        </span>
      }
      actions={
        <div className="flex items-baseline gap-3 text-xs">
          {downCount > 0 && <span className="font-medium text-danger">{downCount} bad interval{downCount === 1 ? '' : 's'}</span>}
          {pctUp != null && (
            <span className={cn('font-mono font-semibold tabular-nums', BAND_TEXT[uptimeBand(pctUp)])}>{pctUp.toFixed(2)}% up</span>
          )}
        </div>
      }
      bodyClassName="p-4 pt-3"
    >
      {covered.length === 0 ? (
        <EmptyState
          icon={Activity}
          title="No availability data in this window"
          description={`Nothing was recorded for ${rangePhrase(rangeLabel)}. Widen the range, or check that the poller is storing probe results.`}
        />
      ) : (
        <>
          <div className="flex h-9 gap-[1px] overflow-hidden rounded-lg bg-surface2">
            {buckets.map((b, i) => (
              <div
                key={i}
                className="flex-1 transition-opacity hover:opacity-70"
                style={{
                  backgroundColor:
                    b.state === 'up' ? UP_COLOR
                      : b.state === 'down' ? DOWN_COLOR
                        : b.state === 'warn' ? WARN_COLOR
                          : 'transparent',
                }}
                title={
                  b.state === 'gap'
                    ? `${timeTooltipLabelFormatter(b.start)} — no data`
                    : `${timeTooltipLabelFormatter(b.start)} — ${b.state.toUpperCase()}${b.reason ? ` · ${b.reason}` : ''}`
                }
              />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <span className="text-[10px] tabular-nums text-muted">{timeTooltipLabelFormatter(fromTs)}</span>
            <div className="flex items-center gap-3 text-[10px] text-muted">
              <Swatch color={UP_COLOR} label="Up" />
              <Swatch color={WARN_COLOR} label="Warn" />
              <Swatch color={DOWN_COLOR} label="Down" />
              <span className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-surface2 ring-1 ring-inset ring-border" />No data
              </span>
            </div>
            <span className="text-[10px] tabular-nums text-muted">{timeTooltipLabelFormatter(toTs)}</span>
          </div>
        </>
      )}
    </SectionCard>
  )
}

function Swatch({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="h-2 w-2 rounded-sm" style={{ background: color }} />{label}
    </span>
  )
}

function buildAvailabilityBuckets(
  points: ServiceMetricPoint[],
  history: StatusHistoryEvent[],
  check: ServiceCheck,
  fromTs: number,
  toTs: number,
) {
  // Never make a bucket narrower than the polling interval: at 96 fixed buckets a 1h window
  // gives 37s slots while the probe runs every 60s, so two thirds of them render as "no data"
  // and a perfectly healthy service looks striped.
  const span = Math.max(1, toTs - fromTs)
  const intervalMs = Math.max(1, (check.check_interval || 60) * 1000)
  const count = Math.max(12, Math.min(96, Math.floor(span / Math.max(span / 96, intervalMs))))
  const width = span / count
  const slots: Array<{ start: number; end: number; up: number; down: number; warn: number; reason?: string; fromHistory?: 'up' | 'down' | 'warn' }> =
    Array.from({ length: count }, (_, i) => ({ start: fromTs + i * width, end: fromTs + (i + 1) * width, up: 0, down: 0, warn: 0 }))

  for (const p of points) {
    const ts = Date.parse(p.timestamp)
    if (!Number.isFinite(ts)) continue
    const i = Math.min(count - 1, Math.floor((ts - fromTs) / width))
    if (i < 0) continue
    if (p.is_up) slots[i].up++
    else slots[i].down++
  }

  const sorted = [...history].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
  let cursor = fromTs
  let state: 'up' | 'down' | 'warn' | null = null
  let reason: string | undefined
  for (const ev of sorted) {
    const t = Date.parse(ev.timestamp)
    if (!Number.isFinite(t)) continue
    if (state && t > cursor) paintHistory(slots, width, fromTs, count, cursor, Math.min(t, toTs), state, reason)
    state = ev.new_status === 'up' ? 'up' : ev.new_status === 'down' ? 'down' : 'warn'
    reason = ev.reason || undefined
    cursor = Math.max(t, fromTs)
  }
  if (state && cursor < toTs) paintHistory(slots, width, fromTs, count, cursor, toTs, state, reason)
  if (!sorted.length && check.last_check_at) {
    const live = check.status === 'up' ? 'up' : check.status === 'down' ? 'down' : 'warn'
    const last = Date.parse(check.last_check_at)
    if (Number.isFinite(last) && last >= fromTs && last <= toTs) {
      paintHistory(slots, width, fromTs, count, last, toTs, live, check.last_error || undefined)
    }
  }

  return slots.map((s) => {
    let st: 'up' | 'down' | 'warn' | 'gap' = 'gap'
    if (s.down > 0) st = 'down'
    else if (s.warn > 0) st = 'warn'
    else if (s.up > 0) st = 'up'
    else if (s.fromHistory) st = s.fromHistory
    return { start: s.start, end: s.end, state: st, reason: s.reason }
  })
}

function paintHistory(
  slots: Array<{ start: number; fromHistory?: 'up' | 'down' | 'warn'; reason?: string }>,
  width: number,
  fromTs: number,
  count: number,
  start: number,
  end: number,
  state: 'up' | 'down' | 'warn',
  reason?: string,
) {
  const a = Math.max(0, Math.floor((start - fromTs) / width))
  const b = Math.min(count - 1, Math.floor((end - 1 - fromTs) / width))
  for (let i = a; i <= b; i++) {
    if (!slots[i].fromHistory || state !== 'up') {
      slots[i].fromHistory = state
      if (reason) slots[i].reason = reason
    }
  }
}

/* ─── Performance chart ─────────────────────────────────────────────────── */

function PerformanceChart({
  points, statusHistory, rangeLabel, rangeHours, fromTs, toTs, loading, error, onRetry, stats,
}: {
  points: ServiceMetricPoint[]
  statusHistory: StatusHistoryEvent[]
  rangeLabel: string
  rangeHours: number
  fromTs: number
  toTs: number
  loading: boolean
  error: boolean
  onRetry: () => void
  stats?: Array<{ label: string; value: string }>
}) {
  const { p95Window, displayBucketMs, errBucketMs } = useMemo(() => {
    if (rangeHours <= 6) return { p95Window: 20, displayBucketMs: 0, errBucketMs: 5 * 60_000 }
    if (rangeHours <= 24) return { p95Window: 12, displayBucketMs: 0, errBucketMs: 15 * 60_000 }
    if (rangeHours <= 24 * 7) return { p95Window: 24, displayBucketMs: 30 * 60_000, errBucketMs: 2 * 3600_000 }
    return { p95Window: 12, displayBucketMs: 3600_000, errBucketMs: 6 * 3600_000 }
  }, [rangeHours])

  const merged = useMemo(() => {
    const base = points
      .map((p) => ({ ts: Date.parse(p.timestamp), ms: p.response_ms, up: p.is_up }))
      .filter((b) => Number.isFinite(b.ts))
    const p95s = rollingPercentile(base.map((b) => ({ ts: b.ts, ms: b.up ? b.ms : null })), 0.95, p95Window)
    const errBucket = new Map<number, { up: number; down: number }>()
    for (const p of points) {
      const k = Math.floor(Date.parse(p.timestamp) / errBucketMs) * errBucketMs
      const b = errBucket.get(k) || { up: 0, down: 0 }
      if (p.is_up) b.up++
      else b.down++
      errBucket.set(k, b)
    }
    const enriched = base.map((b, i) => {
      const bb = errBucket.get(Math.floor(b.ts / errBucketMs) * errBucketMs)
      return {
        ts: b.ts,
        avg: b.up ? b.ms : null,
        p95: p95s[i]?.p95 ?? null,
        err: bb ? (bb.down / Math.max(1, bb.up + bb.down)) * 100 : 0,
      }
    })
    if (displayBucketMs === 0 || enriched.length === 0) return enriched
    type Bin = { ts: number; avgSum: number; avgN: number; p95Max: number | null; errSum: number; errN: number }
    const bins = new Map<number, Bin>()
    for (const e of enriched) {
      const k = Math.floor(e.ts / displayBucketMs) * displayBucketMs
      let bin = bins.get(k)
      if (!bin) { bin = { ts: k, avgSum: 0, avgN: 0, p95Max: null, errSum: 0, errN: 0 }; bins.set(k, bin) }
      if (e.avg != null && Number.isFinite(e.avg)) { bin.avgSum += e.avg; bin.avgN++ }
      if (e.p95 != null && Number.isFinite(e.p95)) bin.p95Max = bin.p95Max == null ? e.p95 : Math.max(bin.p95Max, e.p95)
      bin.errSum += e.err; bin.errN++
    }
    return Array.from(bins.values()).sort((a, b) => a.ts - b.ts).map((b) => ({
      ts: b.ts,
      avg: b.avgN > 0 ? b.avgSum / b.avgN : null,
      p95: b.p95Max,
      err: b.errN > 0 ? b.errSum / b.errN : 0,
    }))
  }, [points, p95Window, errBucketMs, displayBucketMs])

  const bands = useMemo(() => statusBands(statusHistory, fromTs, toTs), [statusHistory, fromTs, toTs])
  const axisData = merged.length > 0 ? merged : [{ ts: fromTs, avg: null, p95: null, err: 0 }, { ts: toTs, avg: null, p95: null, err: 0 }]
  const tickFormatter = useMemo(() => timeAxisTickFormatter(rangeHours), [rangeHours])
  const ticks = useMemo(() => timeTicks(fromTs, toTs, rangeHours), [fromTs, toTs, rangeHours])
  const hasSeries = merged.some((m) => m.avg != null || m.p95 != null)
  const singlePoint = merged.filter((m) => m.avg != null).length === 1

  return (
    <SectionCard
      title="Response & outages"
      subtitle={
        <div className="flex flex-wrap items-center gap-3">
          <LegendDot color="rgb(var(--info))" label="Avg ms" />
          <LegendDot color="rgb(var(--accent))" label="P95 ms" />
          <LegendDot color="rgb(var(--warning))" label="Error %" />
          <LegendDot color="rgb(var(--danger) / 0.4)" label="Outage" />
        </div>
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          {stats?.map((s) => (
            <span key={s.label} className="inline-flex items-baseline gap-1 rounded-md border border-border bg-surface2/50 px-2 py-0.5">
              <span className="text-[9.5px] font-semibold uppercase tracking-wider text-muted">{s.label}</span>
              <span className="font-mono text-[11px] font-medium tabular-nums text-text">{s.value}</span>
            </span>
          ))}
          <span className="text-[11px] font-medium text-muted">{rangeLabel}</span>
        </div>
      }
      bodyClassName="flex flex-col p-4"
    >
      <PanelState loading={loading} error={error} onRetry={onRetry} loadingText="Loading probe samples…" errorText="Could not load probe metrics.">
        <div className="min-h-[236px] w-full flex-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={axisData} margin={{ top: 8, right: axisRightPad(rangeHours) + 18, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="svcRespG" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.4} />
                  <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="svcP95G" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="rgb(var(--accent))" stopOpacity={0.25} />
                  <stop offset="100%" stopColor="rgb(var(--accent))" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgb(var(--border) / 0.4)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="ts"
                type="number"
                domain={[fromTs, toTs]}
                scale="time"
                ticks={ticks}
                interval={0}
                tickFormatter={tickFormatter}
                tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                yAxisId="ms"
                tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                width={52}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`)}
              />
              <YAxis
                yAxisId="err"
                orientation="right"
                tick={{ fontSize: 10, fill: 'rgb(var(--warning))' }}
                width={36}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) => `${Number(v).toFixed(0)}%`}
                domain={[0, (dataMax: number) => Math.max(10, dataMax * 1.2)]}
              />
              <Tooltip
                contentStyle={{
                  background: 'rgb(var(--surface))',
                  border: '1px solid rgb(var(--border))',
                  borderRadius: 6,
                  fontSize: 11,
                  color: 'rgb(var(--text))',
                }}
                labelFormatter={timeTooltipLabelFormatter}
                formatter={(v: any, name: any) => {
                  if (v == null) return ['—', name]
                  if (name === 'Error Rate') return [`${Number(v).toFixed(2)}%`, name]
                  return [`${Number(v).toFixed(1)} ms`, name]
                }}
              />
              {bands.map((b, i) => (
                <ReferenceArea
                  key={i}
                  yAxisId="ms"
                  x1={Math.max(b.start, fromTs)}
                  x2={Math.min(b.end, toTs)}
                  stroke="none"
                  fill={b.kind === 'down' ? 'rgb(var(--danger))' : 'rgb(var(--warning))'}
                  fillOpacity={0.16}
                />
              ))}
              <Area yAxisId="err" type="stepAfter" dataKey="err" name="Error Rate" stroke="rgb(var(--warning))" fill="rgb(var(--warning) / 0.12)" strokeWidth={1.25} isAnimationActive={false} dot={false} />
              <Area yAxisId="ms" type="monotone" dataKey="avg" name="Avg Response" stroke="rgb(var(--info))" fill="url(#svcRespG)" strokeWidth={2} isAnimationActive={false} connectNulls={false} dot={singlePoint ? { r: 3 } : false} />
              <Area yAxisId="ms" type="monotone" dataKey="p95" name="P95" stroke="rgb(var(--accent))" fill="url(#svcP95G)" strokeWidth={1.5} isAnimationActive={false} connectNulls={false} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
        {!hasSeries && (
          <div className="mt-1 text-center text-[11px] text-muted">
            {bands.length > 0
              ? 'No response samples in this window — outage intervals are shaded from the status log.'
              : `No probe samples in ${rangePhrase(rangeLabel)}.`}
          </div>
        )}
      </PanelState>
    </SectionCard>
  )
}

function statusBands(history: StatusHistoryEvent[], fromTs: number, toTs: number) {
  const sorted = [...history].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
  const out: { start: number; end: number; kind: 'down' | 'warn' }[] = []
  let open: { start: number; kind: 'down' | 'warn' } | null = null
  for (const ev of sorted) {
    const t = Date.parse(ev.timestamp)
    const kind = ev.new_status === 'up' ? null : ev.new_status === 'down' ? 'down' : 'warn'
    if (kind && !open) open = { start: t, kind }
    else if (!kind && open) {
      out.push({ start: open.start, end: t, kind: open.kind })
      open = null
    } else if (kind && open && kind !== open.kind) {
      out.push({ start: open.start, end: t, kind: open.kind })
      open = { start: t, kind }
    }
  }
  if (open) out.push({ start: open.start, end: Date.now(), kind: open.kind })
  return out.filter((b) => b.end > fromTs && b.start < toTs)
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      <span>{label}</span>
    </span>
  )
}

/* ─── Latest probe ──────────────────────────────────────────────────────── */

function latestProbePoint(points: ServiceMetricPoint[], check: ServiceCheck): ServiceMetricPoint | null {
  if (points.length > 0) return points[points.length - 1]
  if (!check.last_check_at) return null
  return {
    timestamp: check.last_check_at,
    response_ms: check.last_response_ms,
    is_up: check.status === 'up' ? true : check.status === 'down' ? false : null,
    status_code: null,
    tls_days_remaining: check.tls_days_remaining,
    error_message: check.last_error,
  }
}

function LatestProbeCard({ check, latest, recent, manualProbe }: {
  check: ServiceCheck
  latest: ServiceMetricPoint | null
  recent: ServiceMetricPoint[]
  manualProbe: { result: ProbeEvidenceResult; observedAt: string } | null
}) {
  const expected = check.http_expected_statuses || String(check.http_expected_status || 200)
  const authLabel = check.credential_name
    ? `${check.credential_name} · ${check.credential_auth_type === 'ntlm' ? 'NTLM' : check.credential_auth_type}`
    : 'No saved credential'
  const resultLabel = latest?.is_up == null ? 'No data' : latest.is_up ? 'UP' : 'DOWN'
  const resultTone = latest?.is_up == null ? 'text-muted' : latest.is_up ? 'text-success' : 'text-danger'

  return (
    <SectionCard
      title="Latest probe"
      actions={check.check_type === 'http' ? (
        <span className="rounded-full border border-border bg-surface2/50 px-2 py-0.5 text-[10px] text-muted">Expect HTTP {expected}</span>
      ) : undefined}
    >
      <div className="grid grid-cols-2 gap-2">
        <ProbeStat label="Result" value={resultLabel} className={resultTone} sub={latest?.timestamp ? relativeTime(latest.timestamp) : 'Waiting for poller'} />
        <ProbeStat label="Response" value={formatMs(latest?.response_ms)} className="text-info" sub={latest?.status_code != null ? `HTTP ${latest.status_code}` : authLabel} />
      </div>
      {(latest?.error_message || check.last_error) && latest?.is_up === false && (
        <p className="mt-2 break-words rounded-md border border-danger/30 bg-danger/10 px-2 py-1.5 font-mono text-[11px] text-danger">
          {latest?.error_message || check.last_error}
        </p>
      )}
      {manualProbe && (
        <div className="mt-2 rounded-md border border-border bg-surface2/40 px-2.5 py-2">
          <div className="flex items-center justify-between text-[11px] font-semibold">
            <span className="inline-flex items-center gap-1.5">
              <Play className="h-3 w-3" /> Manual probe
              <span className={manualProbe.result.status === 'up' ? 'text-success' : 'text-danger'}>
                {manualProbe.result.status.toUpperCase()}
              </span>
            </span>
            <span className="font-normal text-muted">{relativeTime(manualProbe.observedAt)}</span>
          </div>
          <div className="mt-1 text-[11px] text-muted">
            {formatMs(manualProbe.result.response_time_ms)}
            {manualProbe.result.details?.status_code != null ? ` · HTTP ${manualProbe.result.details.status_code}` : ''}
          </div>
          {manualProbe.result.error && <div className="mt-1 break-words font-mono text-[11px] text-danger">{manualProbe.result.error}</div>}
          {(manualProbe.result.details?.steps?.length || 0) > 1 && (
            <div className="mt-2 space-y-1 border-t border-border/60 pt-1.5">
              {manualProbe.result.details!.steps!.map((s) => (
                <div key={s.index} className="flex items-center gap-2 text-[10px]">
                  <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', s.status === 'up' ? 'bg-success' : 'bg-danger')} />
                  <span className="min-w-0 flex-1 truncate" title={s.name}>{s.index}. {s.name}</span>
                  <span className="shrink-0 tabular-nums text-muted">{s.status_code ?? '—'}</span>
                  <span className="shrink-0 tabular-nums text-muted">{formatMs(s.response_time_ms)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {recent.length > 0 && (
        <div className="mt-3 border-t border-border/60 pt-2">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Recent scheduled</div>
          <div className="space-y-1">
            {recent.slice(0, 5).map((p, i) => (
              <div key={`${p.timestamp}-${i}`} className="flex items-center gap-2 text-[11px]">
                <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', p.is_up ? 'bg-success' : 'bg-danger')} />
                <span className="w-[68px] shrink-0 tabular-nums text-muted">
                  {new Date(p.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
                <span className={cn('font-medium', p.is_up ? 'text-success' : 'text-danger')}>{p.is_up ? 'UP' : 'DOWN'}</span>
                <span className="tabular-nums text-muted">{formatMs(p.response_ms)}</span>
                <span className="min-w-0 flex-1 truncate text-muted" title={p.error_message || undefined}>
                  {p.status_code != null ? `HTTP ${p.status_code}` : p.error_message || ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </SectionCard>
  )
}

function ProbeStat({ label, value, sub, className }: { label: string; value: string; sub: string; className?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface2/30 px-2.5 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('mt-0.5 text-lg font-semibold tabular-nums', className)}>{value}</div>
      <div className="truncate text-[10px] text-muted" title={sub}>{sub}</div>
    </div>
  )
}

/* ─── Health score ──────────────────────────────────────────────────────── */

function HealthScoreCard({ score, tint, label, factors, onViewDetails }: {
  score: number
  tint: string
  label: string
  factors: HealthFactor[]
  onViewDetails: () => void
}) {
  const radius = 36
  const circ = 2 * Math.PI * radius
  const offset = circ - (score / 100) * circ
  return (
    <SectionCard
      title="Health score"
      actions={<button type="button" onClick={onViewDetails} className="text-xs text-primary hover:underline">How it's scored</button>}
    >
      <div className="flex items-center gap-4">
        <div className="relative h-[84px] w-[84px] shrink-0">
          <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
            <circle cx="50" cy="50" r={radius} fill="none" stroke="rgb(var(--border))" strokeWidth="8" />
            <circle cx="50" cy="50" r={radius} fill="none" stroke={tint} strokeWidth="8" strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={offset} />
          </svg>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <div className="text-2xl font-bold leading-none tabular-nums" style={{ color: tint }}>{score}</div>
            <div className="text-[9px] font-semibold" style={{ color: tint }}>{label}</div>
          </div>
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          {factors.map((f) => (
            <div key={f.key} className="flex items-center gap-2 text-[10px]">
              <span className="w-[68px] shrink-0 truncate text-muted">{f.label}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${f.subScore}%`, background: f.subScore > 90 ? UP_COLOR : f.subScore > 70 ? WARN_COLOR : DOWN_COLOR }}
                />
              </div>
              <span className="w-6 shrink-0 text-right font-mono tabular-nums">{f.subScore}</span>
            </div>
          ))}
        </div>
      </div>
    </SectionCard>
  )
}

/* ─── Activity ──────────────────────────────────────────────────────────── */

function buildActivityEvents(
  history: StatusHistoryEvent[],
  alerts: ServiceAlert[],
  fromISO: string,
  toISO: string,
  check?: ServiceCheck,
): ActivityEvent[] {
  const fromMs = Date.parse(fromISO)
  const toMs = Date.parse(toISO)
  const inRange = (iso: string) => {
    const t = Date.parse(iso)
    return Number.isFinite(t) && t >= fromMs && t <= toMs
  }

  const statusEvents: ActivityEvent[] = history.filter((h) => inRange(h.timestamp)).map((h, i) => {
    const up = h.new_status === 'up'
    const down = h.new_status === 'down'
    return {
      id: `status-${h.timestamp}-${i}`,
      timestamp: h.timestamp,
      kind: 'status',
      severity: up ? 'success' : down ? 'critical' : 'warning',
      title: up ? 'Service recovered' : down ? 'Service went down' : `Status → ${statusOf(h.new_status).label}`,
      subtitle: [h.reason, h.old_status ? `${h.old_status} → ${h.new_status}` : null].filter(Boolean).join(' · ') || undefined,
    }
  })

  const alertEvents: ActivityEvent[] = alerts.filter((a) => inRange(a.triggered_at)).map((a) => ({
    id: `alert-${a.id}`,
    timestamp: a.triggered_at,
    kind: 'alert',
    severity: normalizeSeverity(a.severity),
    title: a.status === 'resolved' ? 'Alert resolved' : a.status === 'acknowledged' ? 'Alert acknowledged' : 'Alert fired',
    subtitle: cleanAlertMessage(a.message),
    href: `/alerts/${a.id}`,
  }))

  const extra: ActivityEvent[] = []
  if (
    check &&
    (check.status === 'down' || check.status === 'warning') &&
    check.last_error &&
    check.last_check_at &&
    inRange(check.last_check_at) &&
    !statusEvents.some((e) => e.severity === 'critical' || e.severity === 'warning')
  ) {
    extra.push({
      id: 'live-down',
      timestamp: check.last_check_at,
      kind: 'status',
      severity: check.status === 'down' ? 'critical' : 'warning',
      title: check.status === 'down' ? 'Service is down' : 'Service is degraded',
      subtitle: check.last_error,
    })
  }

  return [...statusEvents, ...alertEvents, ...extra].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
}

function ActivityLogCard({ events, rangeLabel, showingFallback, onViewAll, onWiden }: {
  events: ActivityEvent[]
  rangeLabel: string
  showingFallback?: boolean
  onViewAll: () => void
  onWiden: () => void
}) {
  const [filter, setFilter] = useState<'all' | 'status' | 'alert'>('all')
  const filtered = filter === 'all' ? events : events.filter((e) => e.kind === filter)
  const shown = filtered.slice(0, 7)

  return (
    <SectionCard
      title="Activity"
      subtitle={showingFallback ? `Nothing in ${rangePhrase(rangeLabel)} — showing the most recent history` : rangeLabel}
      actions={<button type="button" onClick={onViewAll} className="text-xs text-primary hover:underline">View all</button>}
    >
      <div className="mb-3 flex gap-0.5 rounded-md bg-surface2/60 p-0.5">
        {(['all', 'status', 'alert'] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              'flex-1 rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors',
              filter === f ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
            )}
          >
            {f === 'all' ? 'All' : f === 'status' ? 'Status' : 'Alerts'}
          </button>
        ))}
      </div>
      <div className="relative">
        {shown.length > 0 && <div className="absolute bottom-2 left-[13px] top-2 w-px bg-border/60" />}
        <div className="space-y-3">
          {shown.length === 0 && (
            <EmptyState
              icon={Info}
              title={filter === 'all' ? `No activity in ${rangePhrase(rangeLabel)}` : `No ${filter === 'alert' ? 'alerts' : 'status changes'} here`}
              description="Status changes and alerts appear here when the service goes down or recovers."
              action={<Button variant="outline" size="sm" onClick={onWiden}>Widen to 30 days</Button>}
            />
          )}
          {shown.map((e) => {
            const tone =
              e.severity === 'critical' ? 'bg-danger/15 text-danger ring-danger/30'
                : e.severity === 'warning' ? 'bg-warning/15 text-warning ring-warning/30'
                  : e.severity === 'success' ? 'bg-success/15 text-success ring-success/30'
                    : 'bg-info/15 text-info ring-info/30'
            const Icon = e.kind === 'alert' ? Bell : e.severity === 'success' ? CheckCircle2 : e.severity === 'critical' ? XCircle : AlertTriangle
            const inner = (
              <>
                <span className={cn('relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-2', tone)}>
                  <Icon className="h-3.5 w-3.5" />
                </span>
                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex items-start justify-between gap-2">
                    <div className="truncate text-[11px] font-semibold" title={e.title}>{e.title}</div>
                    <div className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(e.timestamp)}</div>
                  </div>
                  <div className="font-mono text-[10px] tabular-nums text-text2">
                    {new Date(e.timestamp).toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                  </div>
                  {e.subtitle && <div className="truncate text-[10px] text-muted" title={e.subtitle}>{e.subtitle}</div>}
                </div>
              </>
            )
            return e.href ? (
              <Link key={e.id} to={e.href} className="relative flex items-start gap-3 rounded-md hover:bg-surface2/40">{inner}</Link>
            ) : (
              <div key={e.id} className="relative flex items-start gap-3">{inner}</div>
            )
          })}
        </div>
      </div>
    </SectionCard>
  )
}

function EventsDialog({ open, onOpenChange, events, rangeLabel, checkId }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  events: ActivityEvent[]
  rangeLabel: string
  checkId: string
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Activity · {rangeLabel}</DialogTitle>
        </DialogHeader>
        <div className="max-h-[60vh] space-y-2 overflow-y-auto pr-1">
          {events.length === 0 && <div className="py-8 text-center text-xs text-muted">No events in this window.</div>}
          {events.map((e) => (
            <div key={e.id} className="rounded-md border border-border bg-surface2/30 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold">{e.title}</span>
                <span className="text-[10px] text-muted">{relativeTime(e.timestamp)}</span>
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-muted">{new Date(e.timestamp).toLocaleString()}</div>
              {e.subtitle && <div className="mt-1 break-words text-[11px] text-muted">{e.subtitle}</div>}
              {e.href && <Link to={e.href} className="mt-1 inline-block text-[11px] text-primary hover:underline">Open alert</Link>}
            </div>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
          <Button size="sm" asChild>
            <Link to={`/services/${checkId}/incidents?filter=all`}>Incident log</Link>
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ─── Related ───────────────────────────────────────────────────────────── */

function hostOf(c: Pick<ServiceCheck, 'target_host' | 'target_url'>): string {
  const h = (c.target_host || '').trim().toLowerCase()
  if (h) return h
  try {
    return new URL(c.target_url || '').hostname.toLowerCase()
  } catch {
    return ''
  }
}

async function fetchRelatedFallback(check: ServiceCheck): Promise<RelatedResponse> {
  const out: RelatedResponse = { parent: null, children: [], same_device: [], same_host: [], same_group: [] }
  const seen = new Set<string>([check.id])
  const take = (rows: ServiceCheck[]) => rows.filter((c) => !seen.has(c.id)).slice(0, 16)

  const queries: Promise<ServiceCheck[]>[] = [
    api.get<{ data: ServiceCheck[] }>('/service-checks?limit=200').then((r) => r.data.data || []).catch(() => []),
  ]
  if (check.device_id) {
    queries.push(api.get<{ data: ServiceCheck[] }>(`/service-checks?device_id=${check.device_id}&limit=50`).then((r) => r.data.data || []).catch(() => []))
  }
  if (check.group_id) {
    queries.push(api.get<{ data: ServiceCheck[] }>(`/service-checks?group_id=${check.group_id}&limit=50`).then((r) => r.data.data || []).catch(() => []))
  }
  const batches = await Promise.all(queries)
  const all = new Map<string, ServiceCheck>()
  for (const batch of batches) for (const c of batch) all.set(c.id, c)
  const pool = [...all.values()]

  if (check.parent_check_id) {
    out.parent = all.get(check.parent_check_id) || null
    if (out.parent) seen.add(out.parent.id)
  }
  out.children = take(pool.filter((c) => c.parent_check_id === check.id))
  out.children.forEach((c) => seen.add(c.id))

  if (check.device_id) {
    out.same_device = take(pool.filter((c) => c.device_id === check.device_id))
    out.same_device.forEach((c) => seen.add(c.id))
  }
  const host = hostOf(check)
  if (host) {
    out.same_host = take(pool.filter((c) => hostOf(c) === host))
    out.same_host.forEach((c) => seen.add(c.id))
  }
  if (check.group_id) out.same_group = take(pool.filter((c) => c.group_id === check.group_id))
  return out
}

function RelatedServicesCard({ related, failed }: { related?: RelatedResponse; failed?: boolean }) {
  const groups: Array<{ title: string; items: ServiceCheck[] }> = [
    { title: 'Parent', items: related?.parent ? [related.parent] : [] },
    { title: 'Dependents', items: related?.children || [] },
    { title: 'Same device', items: related?.same_device || [] },
    { title: 'Same host', items: related?.same_host || [] },
    { title: 'Same group', items: related?.same_group || [] },
  ].filter((g) => g.items.length > 0)
  const total = groups.reduce((n, g) => n + g.items.length, 0)

  return (
    <SectionCard title="Related checks" actions={total > 0 ? <span className="text-[11px] tabular-nums text-muted">{total}</span> : undefined}>
      {failed ? (
        <div className="py-6 text-center text-xs text-danger">Could not load related checks.</div>
      ) : groups.length === 0 ? (
        <EmptyState
          icon={Route}
          title="No related checks"
          description="Checks appear here when they share this host, device or group, or when one is linked as a parent or dependent."
          action={<Button variant="outline" size="sm" asChild><Link to="/services">Browse all checks</Link></Button>}
        />
      ) : (
        <div className="space-y-3">
          {groups.map((g) => (
            <div key={g.title}>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">{g.title}</div>
              <div className="space-y-1">
                {g.items.map((c) => {
                  const sm = statusOf(c.status)
                  const target = c.target_url || `${c.target_host}${c.target_port ? `:${c.target_port}` : ''}`
                  return (
                    <Link
                      key={c.id}
                      to={`/services/${c.id}`}
                      className="flex items-center gap-2 rounded-md border border-border bg-surface2/30 px-2 py-1.5 transition-colors hover:bg-surface2"
                    >
                      <span className={cn(
                        'h-1.5 w-1.5 shrink-0 rounded-full',
                        c.status === 'up' ? 'bg-success' : c.status === 'down' ? 'bg-danger' : 'bg-warning',
                      )} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[11px] font-medium">{c.name}</div>
                        <div className="truncate font-mono text-[10px] text-muted">{target}</div>
                      </div>
                      <span className="shrink-0 text-[10px] text-muted">{sm.label}</span>
                      <ChevronRight className="h-3 w-3 shrink-0 text-muted" />
                    </Link>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  )
}

/* ─── Config summary (overview) ─────────────────────────────────────────── */

function ConfigSummaryCard({ check, onEdit, onOpenConfig }: { check: ServiceCheck; onEdit: () => void; onOpenConfig: () => void }) {
  const rows: Array<{ label: string; value: string; mono?: boolean }> = [
    { label: 'Target', value: check.target_url || check.target_host || '—', mono: true },
    { label: 'Method', value: check.http_method || '—' },
    { label: 'Auth', value: check.credential_name ? `${check.credential_name} (${check.credential_auth_type})` : 'None' },
    { label: 'Journey', value: (check.workflow_steps?.length || 0) > 0 ? `${check.workflow_steps?.length} steps · ${(check.workflow_operator || 'all').toUpperCase()}` : 'Single request' },
    { label: 'Expected', value: check.http_expected_statuses || String(check.http_expected_status || '—') },
    { label: 'Timeout', value: `${check.timeout}s` },
    { label: 'Interval', value: `${check.check_interval}s` },
    { label: 'Group', value: check.group_name || 'Unassigned' },
  ]
  return (
    <SectionCard
      title="Configuration"
      actions={
        <div className="flex items-center gap-2">
          <button type="button" onClick={onOpenConfig} className="text-xs text-primary hover:underline">Full</button>
          <button type="button" onClick={onEdit} className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
            <Pencil className="h-3 w-3" /> Edit
          </button>
        </div>
      }
    >
      <dl className="space-y-1.5">
        {rows.map((r) => (
          <div key={r.label} className="grid grid-cols-[80px_1fr] items-start gap-2 text-[11px]">
            <dt className="text-muted">{r.label}</dt>
            <dd className={cn('min-w-0 truncate font-medium', r.mono && 'font-mono')} title={r.value}>{r.value}</dd>
          </div>
        ))}
      </dl>
      {check.tags.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1 border-t border-border/60 pt-2.5">
          {check.tags.map((t) => (
            <span key={t} className="rounded-full border border-border bg-surface2/50 px-2 py-0.5 text-[10px] text-muted">{t}</span>
          ))}
        </div>
      )}
    </SectionCard>
  )
}

/* ─── Uptime tab: 30-day calendar ───────────────────────────────────────── */

type DayUptime = {
  key: string
  start: number
  end: number
  date: Date
  uptimePct: number | null
  samples: number
  downtimeSec: number
  hours: Array<{ hour: number; uptimePct: number | null; samples: number }>
}

function buildDailyUptime(hours: HourlyUptime[], dayCount: number, intervalSec = 60): DayUptime[] {
  const byDay = new Map<string, { up: number; total: number; hours: Map<number, { pct: number | null; samples: number }> }>()
  for (const h of hours) {
    const t = Date.parse(h.ts)
    if (!Number.isFinite(t)) continue
    const d = new Date(t)
    const key = dayKey(d)
    let entry = byDay.get(key)
    if (!entry) { entry = { up: 0, total: 0, hours: new Map() }; byDay.set(key, entry) }
    const samples = h.sample_count || 0
    if (h.uptime_pct != null && samples > 0) {
      entry.up += (h.uptime_pct / 100) * samples
      entry.total += samples
    }
    entry.hours.set(d.getHours(), { pct: h.uptime_pct, samples })
  }

  const out: DayUptime[] = []
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  for (let i = dayCount - 1; i >= 0; i--) {
    const date = new Date(today)
    date.setDate(today.getDate() - i)
    const key = dayKey(date)
    const entry = byDay.get(key)
    const start = date.getTime()
    const end = start + 86_400_000
    const uptimePct = entry && entry.total > 0 ? (entry.up / entry.total) * 100 : null
    // Downtime is estimated from the samples actually taken (samples × polling interval), not
    // from whole measured hours — a monitor created 15 minutes ago that straddles an hour
    // boundary must not be billed two hours of downtime.
    const measuredSec = entry ? Math.min(86_400, entry.total * intervalSec) : 0
    const downtimeSec = uptimePct == null ? 0 : ((100 - uptimePct) / 100) * measuredSec
    out.push({
      key,
      start,
      end,
      date,
      uptimePct,
      samples: entry?.total ?? 0,
      downtimeSec,
      hours: Array.from({ length: 24 }, (_, hour) => {
        const hv = entry?.hours.get(hour)
        return { hour, uptimePct: hv?.pct ?? null, samples: hv?.samples ?? 0 }
      }),
    })
  }
  return out
}

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function UptimeCalendar({ days, loading, error, onRetry, onSelectDay }: {
  days: DayUptime[]
  loading: boolean
  error: boolean
  onRetry: () => void
  onSelectDay: (d: DayUptime) => void
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const measured = days.filter((d) => d.uptimePct != null)
  const overall = measured.length
    ? measured.reduce((s, d) => s + (d.uptimePct as number) * d.samples, 0) / Math.max(1, measured.reduce((s, d) => s + d.samples, 0))
    : null
  const totalDowntime = days.reduce((s, d) => s + d.downtimeSec, 0)
  const worst = measured.length ? measured.reduce((w, d) => ((d.uptimePct as number) < (w.uptimePct as number) ? d : w)) : null
  const selectedDay = days.find((d) => d.key === selected) || null

  // Monday-first grid: pad the first week so each column is a weekday.
  const leadPad = days.length ? (days[0].date.getDay() + 6) % 7 : 0

  return (
    <SectionCard
      title="Uptime calendar"
      subtitle="Last 30 days · click a day to zoom the whole page to it"
      actions={
        <div className="flex items-center gap-3 text-xs">
          {overall != null && (
            <span className={cn('font-mono font-semibold tabular-nums', BAND_TEXT[uptimeBand(overall)])}>{overall.toFixed(3)}%</span>
          )}
          {totalDowntime > 0 && <span className="text-muted">{formatDur(totalDowntime)} down</span>}
        </div>
      }
    >
      <PanelState loading={loading} error={error} onRetry={onRetry} loadingText="Loading 30 days of uptime…" errorText="Could not load uptime history.">
        {measured.length === 0 && !loading ? (
          <EmptyState
            icon={CalendarDays}
            title="No uptime history yet"
            description="Once the poller has stored a few probe results, each day of the last 30 will appear here shaded by its availability."
          />
        ) : (
          <>
            <div className="grid grid-cols-7 gap-1.5">
              {WEEKDAYS.map((w) => (
                <div key={w} className="pb-0.5 text-center text-[10px] font-semibold uppercase tracking-wider text-muted">{w}</div>
              ))}
              {Array.from({ length: leadPad }).map((_, i) => <div key={`pad-${i}`} />)}
              {days.map((d) => {
                const band = uptimeBand(d.uptimePct)
                const isSelected = selected === d.key
                return (
                  <button
                    key={d.key}
                    type="button"
                    onClick={() => setSelected(isSelected ? null : d.key)}
                    title={
                      d.uptimePct == null
                        ? `${d.date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })} — no data`
                        : `${d.date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}\n${d.uptimePct.toFixed(3)}% up · ${d.samples} samples${d.downtimeSec > 0 ? `\n${formatDur(d.downtimeSec)} down` : ''}`
                    }
                    className={cn(
                      'group relative flex h-[62px] flex-col items-center justify-center rounded-lg border transition-all',
                      isSelected ? 'border-primary ring-2 ring-primary/40' : 'border-border hover:border-border-strong',
                      d.uptimePct == null && 'bg-surface2/40',
                    )}
                    style={d.uptimePct != null ? { backgroundColor: withAlpha(BAND_FILL[band], band === 'perfect' ? 0.22 : 0.28) } : undefined}
                  >
                    <span className="text-[11px] font-semibold tabular-nums text-text">{d.date.getDate()}</span>
                    <span className={cn('text-[9px] font-medium tabular-nums', BAND_TEXT[band])}>
                      {d.uptimePct == null ? '—' : d.uptimePct >= 99.95 ? '100%' : `${d.uptimePct.toFixed(d.uptimePct >= 99 ? 1 : 0)}%`}
                    </span>
                    <span
                      className="absolute inset-x-1.5 bottom-1 h-[3px] rounded-full"
                      style={{ backgroundColor: BAND_FILL[band] }}
                    />
                  </button>
                )
              })}
            </div>

            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-3">
              <div className="flex flex-wrap items-center gap-3 text-[10px] text-muted">
                <Swatch color={BAND_FILL.perfect} label="100%" />
                <Swatch color={BAND_FILL.good} label="≥ 99%" />
                <Swatch color={BAND_FILL.fair} label="≥ 95%" />
                <Swatch color={BAND_FILL.poor} label="< 95%" />
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-sm bg-surface2 ring-1 ring-inset ring-border" />No data
                </span>
              </div>
              {worst && (worst.uptimePct as number) < 100 && (
                <span className="text-[11px] text-muted">
                  Worst day{' '}
                  <span className={BAND_TEXT[uptimeBand(worst.uptimePct)]}>
                    {worst.date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · {(worst.uptimePct as number).toFixed(2)}%
                  </span>
                </span>
              )}
            </div>

            {selectedDay && (
              <div className="mt-3 rounded-lg border border-border bg-surface2/30 p-3">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-semibold">
                    {selectedDay.date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
                    <span className={cn('ml-2 font-mono font-medium tabular-nums', BAND_TEXT[uptimeBand(selectedDay.uptimePct)])}>
                      {selectedDay.uptimePct == null ? 'no data' : `${selectedDay.uptimePct.toFixed(3)}%`}
                    </span>
                    {selectedDay.downtimeSec > 0 && (
                      <span className="ml-2 text-[11px] font-normal text-muted">{formatDur(selectedDay.downtimeSec)} down</span>
                    )}
                  </div>
                  <Button variant="outline" size="sm" className="h-7" onClick={() => onSelectDay(selectedDay)}>
                    Zoom to this day
                  </Button>
                </div>
                <div className="flex gap-[2px]">
                  {selectedDay.hours.map((h) => (
                    <div
                      key={h.hour}
                      className="h-6 flex-1 rounded-sm"
                      style={{
                        backgroundColor: h.samples === 0 ? 'rgb(var(--surface3) / 0.5)' : BAND_FILL[uptimeBand(h.uptimePct)],
                      }}
                      title={
                        h.samples === 0
                          ? `${String(h.hour).padStart(2, '0')}:00 — no data`
                          : `${String(h.hour).padStart(2, '0')}:00 — ${(h.uptimePct ?? 0).toFixed(1)}% up · ${h.samples} samples`
                      }
                    />
                  ))}
                </div>
                <div className="mt-1 flex justify-between text-[9px] tabular-nums text-muted">
                  <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span>
                </div>
              </div>
            )}
          </>
        )}
      </PanelState>
    </SectionCard>
  )
}

function withAlpha(rgbExpr: string, alpha: number): string {
  const inner = rgbExpr.replace(/^rgb\(/, '').replace(/\)$/, '').split('/')[0].trim()
  return `rgb(${inner} / ${alpha})`
}

function DailyUptimeChart({ days }: { days: DayUptime[] }) {
  const data = days.map((d) => ({
    label: d.date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
    pct: d.uptimePct,
    band: uptimeBand(d.uptimePct),
  }))
  const hasData = data.some((d) => d.pct != null)
  return (
    <SectionCard title="Daily availability" subtitle="Last 30 days" bodyClassName="flex flex-col p-4">
      {!hasData ? (
        <EmptyState icon={Gauge} title="No daily data" description="Daily availability appears once probe results have been stored." />
      ) : (
        <div className="min-h-[220px] w-full flex-1">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="rgb(var(--border) / 0.35)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 9, fill: 'rgb(var(--muted))' }}
                axisLine={false}
                tickLine={false}
                interval={Math.max(0, Math.floor(data.length / 8) - 1)}
              />
              <YAxis
                domain={[(dataMin: number) => (Number.isFinite(dataMin) ? Math.min(90, Math.floor(dataMin)) : 90), 100]}
                tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                width={42}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) => `${v}%`}
              />
              <Tooltip
                cursor={{ fill: 'rgb(var(--surface2) / 0.6)' }}
                contentStyle={{
                  background: 'rgb(var(--surface))',
                  border: '1px solid rgb(var(--border))',
                  borderRadius: 6,
                  fontSize: 11,
                  color: 'rgb(var(--text))',
                }}
                formatter={(v: any) => [v == null ? 'no data' : `${Number(v).toFixed(3)}%`, 'Uptime']}
              />
              <Bar dataKey="pct" radius={[3, 3, 0, 0]} isAnimationActive={false}>
                {data.map((d, i) => <Cell key={i} fill={BAND_FILL[d.band]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </SectionCard>
  )
}

function SlaSummaryCard({ sla, rangeLabel, loading, error, onRetry }: {
  sla?: SlaStats
  rangeLabel: string
  loading: boolean
  error: boolean
  onRetry: () => void
}) {
  const rows: Array<{ label: string; value: string; tone?: string; hint?: string }> = [
    { label: 'Availability', value: pct(sla?.uptime_pct, 3), tone: BAND_TEXT[uptimeBand(sla?.uptime_pct)] },
    { label: 'Error rate', value: pct(sla?.error_rate_pct, 3) },
    // A clean window really has zero downtime; "—" would read as "unknown".
    { label: 'Total downtime', value: (sla?.total_downtime_sec || 0) > 0 ? formatDur(sla?.total_downtime_sec) : '0s', tone: (sla?.total_downtime_sec || 0) > 0 ? 'text-danger' : 'text-success' },
    { label: 'Incidents', value: String(sla?.incident_count ?? 0) },
    { label: 'Longest outage', value: (sla?.longest_incident_sec || 0) > 0 ? formatDur(sla?.longest_incident_sec) : '0s' },
    { label: 'MTTR', value: sla?.mttr_sec != null ? formatDur(sla.mttr_sec) : 'n/a', hint: 'mean time to recovery' },
    { label: 'MTBF', value: sla?.mtbf_sec != null ? formatDur(sla.mtbf_sec) : 'n/a', hint: 'mean time between failures' },
    { label: 'Avg response', value: formatMs(sla?.avg_response_ms) },
    { label: 'P95 response', value: formatMs(sla?.p95_response_ms) },
    { label: 'Max response', value: formatMs(sla?.max_response_ms) },
    { label: 'Samples', value: (sla?.sample_count ?? 0).toLocaleString() },
    { label: 'Current streak', value: formatDur(sla?.uptime_streak_sec), tone: 'text-success' },
  ]
  return (
    <SectionCard title="SLA summary" subtitle={rangeLabel} bodyClassName="p-0">
      <PanelState loading={loading} error={error} onRetry={onRetry} loadingText="Computing SLA…" errorText="Could not compute SLA for this window.">
        <dl className="divide-y divide-border/60">
          {rows.map((r) => (
            <div key={r.label} className="flex items-baseline justify-between gap-3 px-4 py-2">
              <dt className="text-[11px] text-muted">
                {r.label}
                {r.hint && <span className="ml-1 text-[10px] text-muted/70">({r.hint})</span>}
              </dt>
              <dd className={cn('font-mono text-xs font-medium tabular-nums', r.tone)}>{r.value}</dd>
            </div>
          ))}
        </dl>
      </PanelState>
    </SectionCard>
  )
}

/* ─── Incidents tab ─────────────────────────────────────────────────────── */

function buildOutages(history: StatusHistoryEvent[], fromTs: number, toTs: number, check: ServiceCheck): Outage[] {
  const sorted = [...history]
    .filter((h) => Number.isFinite(Date.parse(h.timestamp)))
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))

  const out: Outage[] = []
  let open: { start: number; kind: 'down' | 'warn'; reason?: string; clipped: boolean } | null = null

  // A window that opens mid-outage has no "went down" row inside it, so a leading recovery
  // implies the service was already down. Clamp that inferred start to when the check was
  // created — otherwise a 30-day window over a week-old check invents weeks of downtime.
  const createdTs = Date.parse(check.created_at)
  const inferredStart = Math.max(fromTs, Number.isFinite(createdTs) ? createdTs : fromTs)
  if (sorted.length > 0 && sorted[0].new_status === 'up') {
    open = { start: inferredStart, kind: 'down', reason: undefined, clipped: inferredStart <= fromTs }
  } else if (sorted.length === 0 && check.status !== 'up' && check.status !== 'unknown') {
    open = {
      start: inferredStart,
      kind: check.status === 'down' ? 'down' : 'warn',
      reason: check.last_error || undefined,
      clipped: inferredStart <= fromTs,
    }
  }

  for (const ev of sorted) {
    const t = Date.parse(ev.timestamp)
    if (ev.new_status === 'up') {
      if (open) {
        out.push({ start: open.start, end: t, kind: open.kind, reason: open.reason, clippedStart: open.clipped })
        open = null
      }
    } else if (!open) {
      open = { start: t, kind: ev.new_status === 'down' ? 'down' : 'warn', reason: ev.reason || undefined, clipped: false }
    }
  }
  if (open) out.push({ start: open.start, end: null, kind: open.kind, reason: open.reason, clippedStart: open.clipped })

  return out.filter((o) => (o.end ?? Date.now()) > fromTs && o.start < toTs).sort((a, b) => b.start - a.start)
}

function IncidentsTab({
  outages, alerts, rangeLabel, checkId, ruleCovered, onAck, ackDisabled, onWiden,
}: {
  outages: Outage[]
  alerts: ServiceAlert[]
  rangeLabel: string
  checkId: string
  ruleCovered?: boolean
  onAck: () => void
  ackDisabled: boolean
  onWiden: () => void
}) {
  const [kind, setKind] = useState<'all' | 'down' | 'warn'>('all')
  const [alertStatus, setAlertStatus] = useState<'all' | 'active' | 'acknowledged' | 'resolved'>('all')

  const filteredOutages = kind === 'all' ? outages : outages.filter((o) => o.kind === kind)
  const filteredAlerts = alertStatus === 'all' ? alerts : alerts.filter((a) => a.status === alertStatus)
  const totalDown = outages.reduce((s, o) => s + ((o.end ?? Date.now()) - o.start), 0) / 1000

  return (
    <div className="space-y-4">
      <SectionCard
        title="Outages"
        subtitle={
          outages.length > 0
            ? `${outages.length} in ${rangePhrase(rangeLabel)} · ${formatDur(totalDown)} total`
            : rangeLabel
        }
        actions={
          <div className="flex gap-0.5 rounded-md bg-surface2/60 p-0.5">
            {(['all', 'down', 'warn'] as const).map((k) => (
              <button
                key={k}
                onClick={() => setKind(k)}
                className={cn(
                  'rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors',
                  kind === k ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
                )}
              >
                {k === 'all' ? 'All' : k === 'down' ? 'Down' : 'Warning'}
              </button>
            ))}
          </div>
        }
        bodyClassName="p-0"
      >
        {filteredOutages.length === 0 ? (
          <EmptyState
            icon={CheckCircle2}
            title={outages.length === 0 ? `No outages in ${rangePhrase(rangeLabel)}` : 'Nothing matches this filter'}
            description={
              outages.length === 0
                ? 'The service stayed healthy for the whole window. Widen the range to look further back.'
                : 'Try switching the filter back to All.'
            }
            action={outages.length === 0 ? <Button variant="outline" size="sm" onClick={onWiden}>Widen to 30 days</Button> : undefined}
          />
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>Status</Th>
                <Th>Started</Th>
                <Th>Recovered</Th>
                <Th className="text-right">Duration</Th>
                <Th>Reason</Th>
              </Tr>
            </THead>
            <TBody>
              {filteredOutages.map((o, i) => {
                const end = o.end ?? Date.now()
                const ongoing = o.end == null
                return (
                  <Tr key={`${o.start}-${i}`}>
                    <Td>
                      <Badge variant={o.kind === 'down' ? 'danger' : 'warning'}>
                        {o.kind === 'down' ? <XCircle className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
                        {o.kind === 'down' ? 'Down' : 'Warning'}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="font-mono text-[11px] tabular-nums">{new Date(o.start).toLocaleString()}</div>
                      <div className="text-[10px] text-muted">
                        {o.clippedStart ? 'started before this window' : relativeTime(new Date(o.start).toISOString())}
                      </div>
                    </Td>
                    <Td>
                      {ongoing ? (
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-danger">
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-danger" /> Ongoing
                        </span>
                      ) : (
                        <>
                          <div className="font-mono text-[11px] tabular-nums">{new Date(end).toLocaleString()}</div>
                          <div className="text-[10px] text-muted">{relativeTime(new Date(end).toISOString())}</div>
                        </>
                      )}
                    </Td>
                    <Td className="text-right font-mono text-xs font-medium tabular-nums">{formatDur((end - o.start) / 1000)}</Td>
                    <Td>
                      <div className="max-w-[380px] truncate text-[11px] text-muted" title={o.reason || undefined}>{o.reason || '—'}</div>
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        )}
      </SectionCard>

      <SectionCard
        title="Alerts"
        subtitle={alerts.length > 0 ? `${alerts.length} for this check` : 'Alerts raised for this service check'}
        actions={
          <div className="flex items-center gap-2">
            <div className="flex gap-0.5 rounded-md bg-surface2/60 p-0.5">
              {(['all', 'active', 'acknowledged', 'resolved'] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setAlertStatus(s)}
                  className={cn(
                    'rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors',
                    alertStatus === s ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
                  )}
                >
                  {s === 'acknowledged' ? 'Ack' : s}
                </button>
              ))}
            </div>
            <Button variant="outline" size="sm" className="h-7" onClick={onAck} disabled={ackDisabled}>
              <Check className="h-3 w-3" /> Ack all
            </Button>
          </div>
        }
        bodyClassName="p-0"
      >
        {filteredAlerts.length === 0 ? (
          ruleCovered === false ? (
            <EmptyState
              icon={BellOff}
              title="No alert rule covers this check"
              description="Outages are recorded above, but nothing will page anyone: no enabled alert rule targets this check, its group, or the service_status metric. Create one to get notified when it goes down."
              action={<Button variant="outline" size="sm" asChild><Link to="/alert-rules">Create an alert rule</Link></Button>}
            />
          ) : (
            <EmptyState
              icon={Bell}
              title={alerts.length === 0 ? 'No alerts for this check' : 'Nothing matches this filter'}
              description={
                alerts.length === 0
                  ? 'Alerts raised by a rule scoped to this check will be listed here.'
                  : 'Try switching the status filter back to All.'
              }
            />
          )
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>Severity</Th>
                <Th>Message</Th>
                <Th>Status</Th>
                <Th>Triggered</Th>
                <Th>Resolved</Th>
              </Tr>
            </THead>
            <TBody>
              {filteredAlerts.map((a) => (
                <Tr key={a.id} className="cursor-pointer">
                  <Td>
                    <Badge variant={a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info'}>
                      {a.severity}
                    </Badge>
                  </Td>
                  <Td>
                    <Link to={`/alerts/${a.id}`} className="block max-w-[420px] truncate text-[11px] hover:text-primary hover:underline" title={cleanAlertMessage(a.message)}>
                      {cleanAlertMessage(a.message)}
                    </Link>
                  </Td>
                  <Td>
                    <span className={cn(
                      'text-[11px] font-medium capitalize',
                      a.status === 'active' ? 'text-danger' : a.status === 'acknowledged' ? 'text-warning' : 'text-success',
                    )}>
                      {a.status}
                    </span>
                  </Td>
                  <Td className="text-[11px] tabular-nums text-muted">{relativeTime(a.triggered_at)}</Td>
                  <Td className="text-[11px] tabular-nums text-muted">{a.resolved_at ? relativeTime(a.resolved_at) : '—'}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </SectionCard>
    </div>
  )
}

/* ─── Config tab ────────────────────────────────────────────────────────── */

function ConfigTab({ check, onEdit, onExport }: { check: ServiceCheck; onEdit: () => void; onExport: () => void }) {
  const steps = check.workflow_steps || []
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
        <SectionCard
          title="Endpoint"
          actions={<button type="button" onClick={onEdit} className="inline-flex items-center gap-1 text-xs text-primary hover:underline"><Pencil className="h-3 w-3" /> Edit</button>}
        >
          <InfoGrid rows={[
            { label: 'Type', value: (typeMeta[check.check_type] || typeMeta.http).label },
            { label: 'Target URL', value: check.target_url || '—', mono: true },
            { label: 'Host', value: check.target_host || '—', mono: true },
            { label: 'Port', value: check.target_port != null ? String(check.target_port) : '—' },
            { label: 'Method', value: check.http_method || '—' },
            { label: 'Expected status', value: check.http_expected_statuses || String(check.http_expected_status || '—') },
            { label: 'Content match', value: check.http_content_match || '—', mono: !!check.http_content_match },
            { label: 'Follow redirects', value: check.http_follow_redirects ? 'Yes' : 'No' },
          ]} />
        </SectionCard>

        <SectionCard title="Scheduling & retries">
          <InfoGrid rows={[
            { label: 'Interval', value: `${check.check_interval}s` },
            { label: 'Timeout', value: `${check.timeout}s` },
            { label: 'Retries', value: `${check.retry_count ?? 1} × ${check.retry_delay_s ?? 30}s delay` },
            { label: 'Level', value: `L${check.level ?? 1}` },
            { label: 'Enabled', value: check.enabled ? 'Yes' : 'Paused' },
            { label: 'Maintenance', value: check.in_maintenance ? 'Active window' : 'None' },
            { label: 'Group', value: check.group_name || 'Unassigned' },
            { label: 'Parent check', value: check.parent_check_name || '—' },
          ]} />
        </SectionCard>

        <SectionCard title="Security & TLS">
          <InfoGrid rows={[
            { label: 'Credential', value: check.credential_name || 'None' },
            { label: 'Auth type', value: check.credential_auth_type || '—' },
            { label: 'Ignore TLS errors', value: check.http_ignore_tls_errors ? 'Yes' : 'No' },
            { label: 'Allow insecure auth', value: check.http_allow_insecure_auth ? 'Yes' : 'No' },
            { label: 'Cert expires', value: check.tls_expiry_date ? new Date(check.tls_expiry_date).toLocaleDateString() : '—' },
            { label: 'Days remaining', value: check.tls_days_remaining != null ? String(check.tls_days_remaining) : '—' },
            { label: 'Issuer', value: check.tls_issuer || '—' },
            { label: 'Warn / critical', value: `${check.tls_warn_days}d / ${check.tls_critical_days}d` },
          ]} />
        </SectionCard>
      </div>

      {steps.length > 0 && (
        <SectionCard
          title="Authenticated journey"
          subtitle={`Cookie-preserving · ${(check.workflow_operator || 'all').toUpperCase()} rule · ${steps.length} step${steps.length === 1 ? '' : 's'}`}
          actions={check.credential_name ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-success/10 px-2.5 py-1 text-[10px] font-semibold text-success">
              <LockKeyhole className="h-3 w-3" /> {check.credential_name} · {check.credential_auth_type}
            </span>
          ) : undefined}
        >
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {steps.map((step, index) => (
              <div key={`${step.name}-${index}`} className="rounded-lg border border-border bg-surface2/30 p-3">
                <div className="flex items-center gap-2">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[10px] font-bold text-primary">{index + 1}</span>
                  <span className="truncate text-xs font-semibold">{step.name}</span>
                </div>
                <div className="mt-2 truncate font-mono text-[10px] text-muted" title={step.url}>{step.method} {step.url}</div>
                <div className="mt-1 text-[10px] text-muted">
                  Expect {step.expected_statuses || '200'}{step.content_match ? ' + content validation' : ''}
                </div>
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      <SectionCard
        title="Metadata"
        actions={<Button variant="outline" size="sm" className="h-7" onClick={onExport}><Download className="h-3 w-3" /> Export JSON</Button>}
      >
        <InfoGrid rows={[
          { label: 'Description', value: check.description || '—' },
          { label: 'Tags', value: check.tags.length ? check.tags.join(', ') : '—' },
          { label: 'Check id', value: check.id, mono: true },
          { label: 'Created', value: new Date(check.created_at).toLocaleString() },
          { label: 'Updated', value: check.updated_at ? new Date(check.updated_at).toLocaleString() : '—' },
        ]} />
      </SectionCard>
    </div>
  )
}

function InfoGrid({ rows }: { rows: Array<{ label: string; value: string; mono?: boolean }> }) {
  return (
    <dl className="space-y-1.5">
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-[112px_1fr] items-start gap-2 text-[11px]">
          <dt className="text-muted">{r.label}</dt>
          <dd className={cn('min-w-0 truncate font-medium', r.mono && 'font-mono')} title={r.value}>{r.value}</dd>
        </div>
      ))}
    </dl>
  )
}

/* ─── Maintenance dialog ────────────────────────────────────────────────── */

function MaintenanceDialog({
  open, onOpenChange, inMaintenance, onStart, onStartCustom, onEnd, starting, ending,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  inMaintenance: boolean
  onStart: (hours: number) => void
  onStartCustom: (startsAtISO: string, endsAtISO: string) => void
  onEnd: () => void
  starting: boolean
  ending: boolean
}) {
  const [mode, setMode] = useState<'preset' | 'custom'>('preset')
  const toLocalInput = (d: Date) => {
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const [startsAt, setStartsAt] = useState(() => toLocalInput(new Date()))
  const [endsAt, setEndsAt] = useState(() => toLocalInput(new Date(Date.now() + 60 * 60_000)))
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setMode('preset')
      setError(null)
      setStartsAt(toLocalInput(new Date()))
      setEndsAt(toLocalInput(new Date(Date.now() + 60 * 60_000)))
    }
  }, [open])

  const presets = [
    { label: '15 min', hours: 0.25 },
    { label: '1 hour', hours: 1 },
    { label: '4 hours', hours: 4 },
    { label: '24 hours', hours: 24 },
  ]

  const submitCustom = () => {
    setError(null)
    const s = new Date(startsAt)
    const e = new Date(endsAt)
    if (Number.isNaN(s.getTime()) || Number.isNaN(e.getTime())) {
      setError('Please enter both start and end times.')
      return
    }
    if (e <= s) {
      setError('End time must be after the start time.')
      return
    }
    onStartCustom(s.toISOString(), e.toISOString())
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wrench className="h-4 w-4 text-primary" />
            {inMaintenance ? 'Active maintenance' : 'Start maintenance'}
          </DialogTitle>
        </DialogHeader>
        <p className="text-xs text-muted">
          {inMaintenance
            ? 'Alerts are suppressed for this service while the window is open.'
            : 'Suppress alerts for this service for a defined period. Probes keep running.'}
        </p>
        {inMaintenance ? (
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
            <Button size="sm" variant="destructive" onClick={onEnd} disabled={ending}>{ending ? 'Ending…' : 'End now'}</Button>
          </DialogFooter>
        ) : (
          <>
            <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
              {(['preset', 'custom'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={cn('flex-1 rounded px-2 py-1 text-[11px] font-medium', mode === m ? 'bg-surface text-text' : 'text-muted')}
                >
                  {m === 'preset' ? 'Quick presets' : 'Custom'}
                </button>
              ))}
            </div>
            {mode === 'preset' ? (
              <div className="grid grid-cols-2 gap-2">
                {presets.map((p) => (
                  <Button key={p.hours} variant="outline" size="sm" disabled={starting} onClick={() => onStart(p.hours)}>
                    <Clock className="h-3 w-3" />{p.label}
                  </Button>
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                <label className="block">
                  <span className="mb-1 block text-[10px] uppercase tracking-wider text-muted">Starts at</span>
                  <input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} className="w-full rounded-md border border-border bg-surface2 px-2 py-1.5 text-xs" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[10px] uppercase tracking-wider text-muted">Ends at</span>
                  <input type="datetime-local" value={endsAt} onChange={(e) => setEndsAt(e.target.value)} className="w-full rounded-md border border-border bg-surface2 px-2 py-1.5 text-xs" />
                </label>
                {error && <div className="rounded-md border border-danger/40 bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{error}</div>}
              </div>
            )}
            <DialogFooter>
              <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={starting}>Cancel</Button>
              {mode === 'custom' && (
                <Button size="sm" onClick={submitCustom} disabled={starting}>{starting ? 'Starting…' : 'Start'}</Button>
              )}
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

/* ─── Health score model ────────────────────────────────────────────────── */

type HealthScoreConfig = {
  weights: { availability: number; latency: number; errors: number; incidents: number }
  thresholds: { excellent: number; good: number; degraded: number }
  latencyTargetMs: number
  errorScale: number
  incidentScale: number
}

const DEFAULT_HEALTH_SCORE_CONFIG: HealthScoreConfig = {
  weights: { availability: 50, latency: 15, errors: 20, incidents: 15 },
  thresholds: { excellent: 90, good: 70, degraded: 50 },
  latencyTargetMs: 1000,
  errorScale: 10,
  incidentScale: 15,
}

const HEALTH_SCORE_CONFIG_KEY = 'zp-health-score-config-v1'

function loadHealthScoreConfig(): HealthScoreConfig {
  try {
    const raw = localStorage.getItem(HEALTH_SCORE_CONFIG_KEY)
    if (!raw) return { ...DEFAULT_HEALTH_SCORE_CONFIG }
    const parsed = JSON.parse(raw)
    return {
      weights: { ...DEFAULT_HEALTH_SCORE_CONFIG.weights, ...(parsed.weights || {}) },
      thresholds: { ...DEFAULT_HEALTH_SCORE_CONFIG.thresholds, ...(parsed.thresholds || {}) },
      latencyTargetMs: parsed.latencyTargetMs ?? DEFAULT_HEALTH_SCORE_CONFIG.latencyTargetMs,
      errorScale: parsed.errorScale ?? DEFAULT_HEALTH_SCORE_CONFIG.errorScale,
      incidentScale: parsed.incidentScale ?? DEFAULT_HEALTH_SCORE_CONFIG.incidentScale,
    }
  } catch {
    return { ...DEFAULT_HEALTH_SCORE_CONFIG }
  }
}

function saveHealthScoreConfig(cfg: HealthScoreConfig) {
  try { localStorage.setItem(HEALTH_SCORE_CONFIG_KEY, JSON.stringify(cfg)) } catch { /* ignore */ }
}

type HealthFactor = {
  key: 'availability' | 'latency' | 'errors' | 'incidents'
  label: string
  raw: string
  formula: string
  subScore: number
  weight: number
  contribution: number
}

function computeHealthScore(
  args: { uptime_pct: number | null; error_rate_pct: number | null; incident_count: number; p95_response_ms: number | null },
  cfg: HealthScoreConfig = DEFAULT_HEALTH_SCORE_CONFIG,
) {
  const up = args.uptime_pct ?? 100
  const err = args.error_rate_pct ?? 0
  const inc = args.incident_count || 0
  const p95 = args.p95_response_ms
  const availabilitySub = clamp(up, 0, 100)
  const target = Math.max(1, cfg.latencyTargetMs)
  const latencySub = p95 == null ? 100 : p95 <= target ? 100 : clamp(100 - ((p95 - target) / target) * 100, 0, 100)
  const errorsSub = clamp(100 - err * cfg.errorScale, 0, 100)
  const incidentsSub = clamp(100 - inc * cfg.incidentScale, 0, 100)
  const w = cfg.weights
  const totalW = Math.max(1, w.availability + w.latency + w.errors + w.incidents)
  const factors: HealthFactor[] = [
    {
      key: 'availability',
      label: 'Availability',
      raw: args.uptime_pct == null ? 'no data' : `${up.toFixed(2)}%`,
      formula: args.uptime_pct == null ? 'no samples — defaults to 100' : `uptime % = ${availabilitySub.toFixed(1)}`,
      subScore: Math.round(availabilitySub),
      weight: w.availability,
      contribution: (availabilitySub * w.availability) / totalW,
    },
    {
      key: 'latency',
      label: 'Latency',
      raw: p95 == null ? 'no data' : `${Math.round(p95)} ms p95`,
      formula: p95 == null ? 'no samples — defaults to 100' : p95 <= target
        ? `${Math.round(p95)} ms ≤ ${cfg.latencyTargetMs} ms → 100`
        : `100 − ((${Math.round(p95)} − ${cfg.latencyTargetMs}) / ${cfg.latencyTargetMs}) × 100`,
      subScore: Math.round(latencySub),
      weight: w.latency,
      contribution: (latencySub * w.latency) / totalW,
    },
    {
      key: 'errors',
      label: 'Errors',
      raw: args.error_rate_pct == null ? 'no data' : `${err.toFixed(2)}%`,
      formula: args.error_rate_pct == null ? 'no data — defaults to 100' : `100 − ${err.toFixed(2)} × ${cfg.errorScale}`,
      subScore: Math.round(errorsSub),
      weight: w.errors,
      contribution: (errorsSub * w.errors) / totalW,
    },
    {
      key: 'incidents',
      label: 'Incidents',
      raw: `${inc} in window`,
      formula: `100 − ${inc} × ${cfg.incidentScale}`,
      subScore: Math.round(incidentsSub),
      weight: w.incidents,
      contribution: (incidentsSub * w.incidents) / totalW,
    },
  ]
  const rawScore = factors.reduce((s, f) => s + f.contribution, 0)
  const score = Math.max(0, Math.min(100, Math.round(rawScore)))
  const t = cfg.thresholds
  const tint = score >= t.excellent ? UP_COLOR : score >= t.good ? WARN_COLOR : DOWN_COLOR
  const label = score >= t.excellent ? 'Excellent' : score >= t.good ? 'Good' : score >= t.degraded ? 'Degraded' : 'Critical'
  return { score, tint, label, factors }
}

function HealthScoreDetailsDialog({
  open, onOpenChange, score, tint, label, factors, config, onConfigChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  score: number
  tint: string
  label: string
  factors: HealthFactor[]
  config: HealthScoreConfig
  onConfigChange: (next: HealthScoreConfig) => void
}) {
  const [tab, setTab] = useState<'breakdown' | 'configure'>('breakdown')
  const [draft, setDraft] = useState<HealthScoreConfig>(config)

  useEffect(() => {
    if (open) {
      setDraft(config)
      setTab('breakdown')
    }
  }, [open, config])

  const totalW = Math.max(1, draft.weights.availability + draft.weights.latency + draft.weights.errors + draft.weights.incidents)
  const dirty = JSON.stringify(draft) !== JSON.stringify(config)
  const setWeight = (k: keyof HealthScoreConfig['weights'], v: number) => {
    const n = Number.isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : 0
    setDraft({ ...draft, weights: { ...draft.weights, [k]: n } })
  }
  const setThreshold = (k: keyof HealthScoreConfig['thresholds'], v: number) => {
    const n = Number.isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : 0
    setDraft({ ...draft, thresholds: { ...draft.thresholds, [k]: n } })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-primary" /> Health score
          </DialogTitle>
        </DialogHeader>
        <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
          <div>
            <div className="text-xs text-muted">Current score</div>
            <div className="text-[11px]" style={{ color: tint }}>{label}</div>
          </div>
          <div className="text-3xl font-bold tabular-nums" style={{ color: tint }}>
            {score}<span className="text-sm text-muted">/100</span>
          </div>
        </div>
        <Tabs value={tab} onValueChange={(v) => setTab(v as 'breakdown' | 'configure')}>
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="breakdown">Breakdown</TabsTrigger>
            <TabsTrigger value="configure"><SettingsIcon className="mr-1.5 h-3.5 w-3.5" />Configure</TabsTrigger>
          </TabsList>
          <TabsContent value="breakdown" className="space-y-2 pt-3">
            {factors.map((f) => {
              const tone = f.subScore > 90 ? 'text-success' : f.subScore > 70 ? 'text-warning' : 'text-danger'
              return (
                <div key={f.key} className="grid grid-cols-[1fr_auto_auto_auto] items-start gap-x-3 border-b border-border/40 py-2 last:border-0">
                  <div className="min-w-0">
                    <div className="text-xs font-semibold">{f.label}</div>
                    <div className="text-[11px] text-muted">{f.raw}</div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted/80">{f.formula}</div>
                  </div>
                  <span className={cn('text-right font-mono text-xs tabular-nums', tone)}>{f.subScore}</span>
                  <span className="text-right font-mono text-xs tabular-nums text-muted">{f.weight}%</span>
                  <span className="text-right font-mono text-xs tabular-nums">{f.contribution.toFixed(1)}</span>
                </div>
              )
            })}
          </TabsContent>
          <TabsContent value="configure" className="space-y-4 pt-3">
            <div className="flex items-center justify-between text-xs">
              <span className="font-semibold">Factor weights</span>
              <span className={cn('font-mono', totalW === 100 ? 'text-muted' : 'text-warning')}>
                Total: {totalW}{totalW !== 100 ? ' (normalized)' : ''}
              </span>
            </div>
            {(['availability', 'latency', 'errors', 'incidents'] as const).map((k) => (
              <div key={k} className="grid grid-cols-[140px_1fr_70px] items-center gap-3">
                <label className="text-xs capitalize">{k}</label>
                <input type="range" min={0} max={100} value={draft.weights[k]} onChange={(e) => setWeight(k, Number(e.target.value))} className="accent-primary" />
                <Input type="number" min={0} max={100} value={draft.weights[k]} onChange={(e) => setWeight(k, Number(e.target.value))} className="h-8 text-xs" />
              </div>
            ))}
            <div className="text-xs font-semibold">Status thresholds (score ≥)</div>
            {(['excellent', 'good', 'degraded'] as const).map((k) => (
              <div key={k} className="grid grid-cols-[140px_1fr_70px] items-center gap-3">
                <label className="text-xs capitalize">{k}</label>
                <input type="range" min={0} max={100} value={draft.thresholds[k]} onChange={(e) => setThreshold(k, Number(e.target.value))} className="accent-primary" />
                <Input type="number" min={0} max={100} value={draft.thresholds[k]} onChange={(e) => setThreshold(k, Number(e.target.value))} className="h-8 text-xs" />
              </div>
            ))}
            <div className="grid grid-cols-[140px_1fr] items-center gap-3">
              <label className="text-xs">Latency target (ms)</label>
              <Input type="number" min={1} value={draft.latencyTargetMs} onChange={(e) => setDraft({ ...draft, latencyTargetMs: Math.max(1, Number(e.target.value) || 1) })} className="h-8 text-xs" />
            </div>
            <div className="grid grid-cols-[140px_1fr] items-center gap-3">
              <label className="text-xs">Error scale</label>
              <Input type="number" min={0} step={0.5} value={draft.errorScale} onChange={(e) => setDraft({ ...draft, errorScale: Math.max(0, Number(e.target.value) || 0) })} className="h-8 text-xs" />
            </div>
            <div className="grid grid-cols-[140px_1fr] items-center gap-3">
              <label className="text-xs">Incident penalty</label>
              <Input type="number" min={0} step={1} value={draft.incidentScale} onChange={(e) => setDraft({ ...draft, incidentScale: Math.max(0, Number(e.target.value) || 0) })} className="h-8 text-xs" />
            </div>
          </TabsContent>
        </Tabs>
        <DialogFooter>
          {tab === 'configure' ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setDraft({ ...DEFAULT_HEALTH_SCORE_CONFIG })}>Reset</Button>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
                <Button size="sm" disabled={!dirty} onClick={() => { onConfigChange(draft); toast.success('Health score criteria updated'); onOpenChange(false) }}>Save</Button>
              </div>
            </>
          ) : (
            <Button size="sm" onClick={() => onOpenChange(false)}>Close</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ─── Series helpers ────────────────────────────────────────────────────── */

function rollingPercentile(
  data: Array<{ ts: number; ms: number | null }>,
  q: number,
  window: number,
): Array<{ ts: number; p95: number | null }> {
  const out: Array<{ ts: number; p95: number | null }> = []
  for (let i = 0; i < data.length; i++) {
    const slice = data.slice(Math.max(0, i - window), i + 1).map((d) => d.ms).filter((x): x is number => x != null && x > 0)
    if (slice.length < 3) {
      out.push({ ts: data[i].ts, p95: null })
      continue
    }
    slice.sort((a, b) => a - b)
    out.push({ ts: data[i].ts, p95: slice[Math.max(0, Math.floor(slice.length * q) - 1)] })
  }
  return out
}

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n))
}

function deriveWindowStats(
  points: ServiceMetricPoint[],
  history: StatusHistoryEvent[],
  check: ServiceCheck,
  fromTs: number,
  toTs: number,
) {
  const buckets = buildAvailabilityBuckets(points, history, check, fromTs, toTs)
  const covered = buckets.filter((b) => b.state !== 'gap')
  const upCount = covered.filter((b) => b.state === 'up').length
  const downCount = covered.filter((b) => b.state === 'down').length
  const uptime_pct = covered.length ? (upCount / covered.length) * 100 : null
  const error_rate_pct = covered.length ? (downCount / covered.length) * 100 : null
  const incident_count = history.filter((h) => h.new_status !== 'up').length

  const samples = points.filter((p) => p.is_up && p.response_ms != null && p.response_ms > 0).map((p) => p.response_ms as number)
  const avg_ms = samples.length ? samples.reduce((a, b) => a + b, 0) / samples.length : null
  const sorted = [...samples].sort((a, b) => a - b)
  const p95_ms = sorted.length >= 3 ? sorted[Math.max(0, Math.floor(sorted.length * 0.95) - 1)] : null

  const recovered = [...history].filter((h) => h.new_status === 'up').sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0]
  const lastDown = [...history].filter((h) => h.new_status !== 'up').sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0]
  let streak_sec: number | null = null
  if (check.status === 'up' && recovered) {
    const rec = Date.parse(recovered.timestamp)
    if (!lastDown || Date.parse(lastDown.timestamp) <= rec) streak_sec = Math.max(0, (Date.now() - rec) / 1000)
  } else if (check.status === 'up' && check.last_check_at && !lastDown) {
    streak_sec = Math.max(0, (Date.now() - Date.parse(check.last_check_at)) / 1000)
  }

  return { uptime_pct, error_rate_pct, incident_count, avg_ms, p95_ms, streak_sec }
}
