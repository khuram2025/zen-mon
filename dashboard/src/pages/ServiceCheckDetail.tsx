import { DailyProbeFailures } from '@/components/services/DailyProbeFailures'
import { explainServiceFailure } from '@/components/services/failureReason'
import { ServiceFailureReason } from '@/components/services/ServiceFailureReason'
import { MonitoringSites } from '@/components/MonitoringSites'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Bell,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronRight,
  Copy,
  Download,
  FileSpreadsheet,
  FileText,
  Gauge,
  Globe,
  Info,
  LineChart,
  Loader2,
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
  Timer,
  Trash2,
  TrendingUp,
  Users,
  Wrench,
  XCircle,
} from 'lucide-react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { serverNow, useClockOffset } from '@/lib/serverClock'
import {
  apiErrorMessage,
  cn,
  relativeTime,
  timeTooltipLabelFormatter,
} from '@/lib/utils'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { TimeRangePicker, rangePhrase, timezoneLabel, useTimeRange } from '@/components/TimeRangePicker'
import {
  BAND_FILL,
  BAND_TEXT,
  PulseDot,
  UptimeBars,
  pulseStatusOf,
} from '@/components/services/uptime'
import { ClockSkewBanner, useServerNowTick } from '@/components/services/detail/clock'
import { withCapability } from '@/components/services/detail/capabilities'
import {
  NEVER_CHECKED,
  NO_DATA,
  availabilityBand,
  cleanAlertMessage,
  colourPct,
  exactTime,
  formatAvailability,
  formatDur,
  formatMs,
  isRecoveryAlert,
  pct,
} from '@/components/services/detail/format'
import {
  buildOutages,
  lastEventBefore,
  seedHistoryWithPrior,
  useServerIncidents,
  type SeededEvent,
} from '@/components/services/detail/incidents'
import {
  MaintenanceBanner,
  MaintenanceDialog,
  isActiveWindow,
  useCheckMaintenance,
  useEndMaintenance,
  useStartMaintenance,
} from '@/components/services/detail/maintenance'
import { useAckAll, useActiveAlertCount, useRuleCoverage, useWindowAlerts } from '@/components/services/detail/alerts'
import { PerformanceChart } from '@/components/services/detail/PerformanceChart'
import { SlaSummaryCard, SloCard } from '@/components/services/detail/SlaCards'
import { ConfigSummaryCard, ConfigTab, RunbookLink } from '@/components/services/detail/ConfigSections'
import { IncidentsTab } from '@/components/services/detail/IncidentsTab'
import { DetailTabPanel, DetailTabs } from '@/components/services/detail/DetailTabs'
import { downloadServiceReport } from '@/components/services/detail/report'
import type {
  HourlyUptime,
  MetricPoint,
  MetricResponse,
  Outage,
  ServiceAlert,
  ServiceCheckExt,
  SlaStats,
  StatusHistoryEvent,
} from '@/components/services/detail/types'
import type { ServiceCheck } from '@/types'

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
  unknown: { label: 'Unknown', tone: 'outline', Icon: Info },
}

const typeMeta: Record<string, { label: string; Icon: typeof Globe }> = {
  http: { label: 'HTTP', Icon: Globe },
  tcp: { label: 'TCP', Icon: Plug },
  tls: { label: 'TLS', Icon: ShieldCheck },
  icmp: { label: 'ICMP', Icon: Radar },
  dns: { label: 'DNS', Icon: Network },
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

type LatestMode = 'latest' | 'window' | 'status'

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
  const [dupOpen, setDupOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [hsOpen, setHsOpen] = useState(false)
  const [maintOpen, setMaintOpen] = useState(false)
  const [eventsOpen, setEventsOpen] = useState(false)
  const [ackConfirmOpen, setAckConfirmOpen] = useState(false)
  const [hsConfig, setHsConfig] = useState<HealthScoreConfig>(() => loadHealthScoreConfig())
  const [manualProbe, setManualProbe] = useState<{ result: ProbeEvidenceResult; observedAt: string } | null>(null)
  const [incidentLimit, setIncidentLimit] = useState(100)
  const nowTick = useServerNowTick(1000)
  // Coarser "now" for memoised reconstruction so it does not rebuild every second.
  const nowHalfMinute = Math.floor(nowTick / 30_000) * 30_000
  const { skewed } = useClockOffset()

  useEffect(() => { setIncidentLimit(100) }, [range.fromISO, range.toISO])

  const { data: check, isLoading, isError } = useQuery<ServiceCheckExt>({
    queryKey: ['service-check', id],
    queryFn: async () => (await api.get(`/service-checks/${id}`)).data,
    enabled: !!id,
    refetchInterval: 5_000,
  })

  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  // Raw for ≤ 24 h (requesting enough rows for a 10 s check); newer appliances
  // pick exact buckets for longer ranges from `auto`.
  const granularity = range.hours <= 24 ? 'raw' : 'auto'

  const metricsQ = useQuery<MetricResponse>({
    queryKey: ['service-check-metrics', id, range.fromISO, range.toISO, granularity],
    queryFn: async () =>
      (await api.get(`/service-checks/${id}/metrics`, {
        params: { from: range.fromISO, to: range.toISO, granularity, ...(granularity === 'raw' ? { limit: 20000 } : {}) },
      })).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const points: MetricPoint[] = metricsQ.data?.points || []

  // Newest scheduled probes regardless of the selected range (B13).
  const latestQ = useQuery<MetricPoint[] | null>({
    queryKey: ['service-check-latest-probes', id],
    queryFn: () => withCapability('latest-probes', async () => {
      const r = (await api.get(`/service-checks/${id}/latest-probes`, { params: { limit: 6 } })).data
      const rows: MetricPoint[] = Array.isArray(r) ? r : r?.data || []
      return [...rows].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
    }),
    enabled: !!id && !!check,
    refetchInterval: 15_000,
    retry: false,
  })

  const statusHistoryQ = useQuery<StatusHistoryEvent[]>({
    queryKey: ['service-status-history', id, range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(`/service-checks/${id}/status-history`, { params: { from: range.fromISO, to: range.toISO, limit: 500 } })).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })
  const statusHistory = useMemo(() => statusHistoryQ.data || [], [statusHistoryQ.data])

  // The state entering the window: the newest transition before `from`, from any age.
  const priorQ = useQuery<StatusHistoryEvent | null>({
    queryKey: ['service-status-prior', id, range.fromISO],
    queryFn: async () => {
      const lookback = check?.created_at && Date.parse(check.created_at) < fromTs
        ? new Date(Date.parse(check.created_at) - 60_000).toISOString()
        : new Date(fromTs - 365 * 86_400_000).toISOString()
      const rows: StatusHistoryEvent[] = (await api.get(`/service-checks/${id}/status-history`, {
        params: { from: lookback, to: new Date(fromTs - 1000).toISOString(), limit: 1 },
      })).data || []
      return lastEventBefore(rows, fromTs)
    },
    enabled: !!id && !!check,
    staleTime: 60_000,
  })

  const slaQ = useQuery<SlaStats>({
    // Explicit bounds: `hours` alone always ends at now, so a custom historical range
    // would silently be scored against the last N hours instead.
    queryKey: ['service-sla', id, range.fromISO, range.toISO, 'confirmed'],
    queryFn: async () =>
      (await api.get(`/service-checks/${id}/sla`, {
        params: { basis: 'confirmed', hours: range.hours, from: range.fromISO, to: range.toISO },
      })).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const sla = slaQ.data

  // Fixed context windows ending now, independent of the range picker.
  const sla24Q = useQuery<SlaStats>({
    queryKey: ['service-sla-fixed', id, 24, 'confirmed'],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?basis=confirmed&hours=24`)).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })
  const sla7dQ = useQuery<SlaStats>({
    queryKey: ['service-sla-fixed', id, 168, 'confirmed'],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?basis=confirmed&hours=168`)).data,
    enabled: !!id && !!check,
    refetchInterval: 120_000,
  })
  const sla30dQ = useQuery<SlaStats>({
    queryKey: ['service-sla-fixed', id, 720, 'confirmed'],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?basis=confirmed&hours=720`)).data,
    enabled: !!id && !!check,
    refetchInterval: 300_000,
  })

  const hourlyQ = useQuery<{ hours: HourlyUptime[] }>({
    queryKey: ['service-hourly-uptime', id, 'confirmed'],
    queryFn: async () => (await api.get(`/service-checks/${id}/hourly-uptime?days=30&basis=confirmed`)).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })
  const hourly = useMemo(() => hourlyQ.data?.hours || [], [hourlyQ.data])

  // Alerts triggered in the selected window (activity feed); the Incidents tab pages its own.
  const windowAlertsQ = useWindowAlerts(id, range.fromISO, range.toISO, 'all', 0, 50, !!id && !!check)
  const alerts = useMemo(() => windowAlertsQ.data?.data || [], [windowAlertsQ.data])
  const activeCountQ = useActiveAlertCount(id, !!id && !!check)
  const activeAlertCount = activeCountQ.data ?? 0
  const { data: ruleCoverage } = useRuleCoverage(id, check?.group_id, !!id && !!check)

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

  // 30-day status history: the day drill-down and the activity fallback for a quiet window.
  const wideFrom = useMemo(() => new Date(serverNow() - 720 * 3600_000).toISOString(), [id])
  const { data: wideHistory = [], isLoading: wideHistoryLoading, isError: wideHistoryError } = useQuery<StatusHistoryEvent[]>({
    queryKey: ['service-status-history-wide', id],
    queryFn: async () =>
      (await api.get(`/service-checks/${id}/status-history`, {
        params: { from: wideFrom, to: new Date(serverNow()).toISOString(), limit: 500 },
      })).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })

  const maintenance = useCheckMaintenance(check, range)
  const activeWindows = maintenance.windows.filter((w) => isActiveWindow(w, nowTick))
  const endMaintenance = useEndMaintenance(id)
  const { preset: startMaintenance, custom: startMaintenanceCustom } = useStartMaintenance(id, () => setMaintOpen(false))

  const incidentsQ = useServerIncidents(id, range.fromISO, range.toISO, incidentLimit, nowHalfMinute, !!id && !!check)
  const serverIncidents = incidentsQ.data?.supported ? incidentsQ.data : null

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
      setManualProbe({ result: d as ProbeEvidenceResult, observedAt: new Date(serverNow()).toISOString() })
      toast.success('Probe complete', d?.status === 'up' ? `Up · ${Math.round(d.response_time_ms || 0)} ms` : `Down: ${d?.error || ''}`)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-check-metrics', id] })
      qc.invalidateQueries({ queryKey: ['service-check-latest-probes', id] })
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

  const ackAll = useAckAll(id)

  const exportConfig = () => {
    if (!check) return
    const blob = new Blob([JSON.stringify({ ...check, _exported_at: new Date(serverNow()).toISOString() }, null, 2)], { type: 'application/json' })
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

  const seededHistory = useMemo(
    () => seedHistoryWithPrior(statusHistory, priorQ.data ?? lastEventBefore(wideHistory, fromTs), fromTs),
    [statusHistory, priorQ.data, wideHistory, fromTs],
  )

  const clientOutages = useMemo(
    () => (check ? buildOutages(seededHistory, fromTs, toTs, check.created_at, nowHalfMinute) : []),
    [seededHistory, fromTs, toTs, check?.created_at, nowHalfMinute],
  )
  const outages: Outage[] = serverIncidents ? serverIncidents.outages : clientOutages
  // "Currently down for" must not be clipped by a short selected range: rebuild the
  // current incident from 30 days of history (server incidents carry the real start).
  const wideOngoing = useMemo(() => {
    if (!check) return null
    const wideFromTs = Date.parse(wideFrom)
    const seeded = seedHistoryWithPrior(wideHistory, null, wideFromTs)
    return buildOutages(seeded, wideFromTs, nowHalfMinute, check.created_at, nowHalfMinute).find((o) => o.end == null) || null
  }, [wideHistory, wideFrom, check?.created_at, nowHalfMinute])
  const outageTotal = serverIncidents ? serverIncidents.total : clientOutages.length

  const activityEvents = useMemo(
    () => buildActivityEvents(statusHistory, alerts, range.fromISO, range.toISO, check),
    [statusHistory, alerts, range.fromISO, range.toISO, check],
  )
  const fallbackEvents = useMemo(
    () =>
      activityEvents.length === 0
        ? buildActivityEvents(wideHistory, alerts, wideFrom, new Date(nowHalfMinute).toISOString(), check)
        : [],
    [activityEvents.length, wideHistory, alerts, wideFrom, check, nowHalfMinute],
  )

  const derived = useMemo(
    () => (check ? deriveWindowStats(points, seededHistory, check, fromTs, toTs, nowHalfMinute) : {
      uptime_pct: null, error_rate_pct: null, incident_count: 0, avg_ms: null, p95_ms: null, streak_sec: null,
    }),
    [points, seededHistory, check, fromTs, toTs, nowHalfMinute],
  )

  const days = useMemo(() => buildDailyUptime(hourly, 30, nowHalfMinute), [hourly, nowHalfMinute])

  const outagesForDay = useMemo(
    () => (d: DayUptime): Outage[] => {
      if (!check) return []
      const inDay = wideHistory.filter((h) => {
        const t = Date.parse(h.timestamp)
        return Number.isFinite(t) && t >= d.start && t < d.end
      })
      const seeded = seedHistoryWithPrior(inDay, lastEventBefore(wideHistory, d.start), d.start)
      return buildOutages(seeded, d.start, Math.min(d.end, nowHalfMinute), check.created_at, nowHalfMinute)
    },
    [wideHistory, check, nowHalfMinute],
  )

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
  const currentInterval = check.status === 'down' ? (check.down_check_interval ?? 30)
    : (check.verification_attempt ?? 0) > 0 ? (check.retry_delay_s ?? 10) : (check.check_interval || 60)
  const intervalMs = currentInterval * 1000
  const expectedRunMs = (check.timeout || 10) * Math.max(1, check.workflow_steps?.length || 0) * 1000
  // Freshness is judged on the appliance clock (B01), so browser skew cannot fake staleness.
  const probeStale = !!lastCheckMs && nowTick - lastCheckMs > Math.max(intervalMs * 2, intervalMs + expectedRunMs * 2)
  const nextPollMs = lastCheckMs ? lastCheckMs + intervalMs : null
  const secsToNext = nextPollMs ? Math.max(0, Math.floor((nextPollMs - nowTick) / 1000)) : null

  const noLatency = sla?.latency_sample_count === 0 ||
    (!!sla && sla.latency_sample_count == null && sla.avg_response_ms == null && sla.p95_response_ms == null)
  const avgMs = sla ? (noLatency ? null : sla.avg_response_ms) : derived.avg_ms
  const p95Ms = sla ? (noLatency ? null : sla.p95_response_ms) : derived.p95_ms
  const incidentCount = sla?.incident_count ?? outageTotal

  const healthScore = computeHealthScore(
    {
      uptime_pct: sla ? sla.uptime_pct : null,
      probe_error_rate_pct: sla?.probe_error_rate_pct ?? null,
      incident_count: sla ? sla.incident_count : null,
      p95_response_ms: sla ? (noLatency ? null : sla.p95_response_ms) : null,
    },
    hsConfig,
  )

  const createdTs = Date.parse(check.created_at)
  const windowReachesNow = toTs >= nowTick - Math.max(2 * intervalMs, 120_000)

  // Latest probe (B13): independent of the range when the appliance supports it; otherwise
  // only a raw window reaching now may stand in, and a rollup is never shown as a probe.
  const latestList = latestQ.data && latestQ.data.length > 0 ? latestQ.data : null
  let latestMode: LatestMode | null = null
  let latestPoint: MetricPoint | null = null
  let recentProbes: MetricPoint[] = []
  if (latestList) {
    latestMode = 'latest'
    latestPoint = latestList[0]
    recentProbes = latestList
  } else if (metricsQ.data?.granularity === 'raw' && points.length > 0 && windowReachesNow) {
    latestMode = 'window'
    latestPoint = points[points.length - 1]
    recentProbes = points.slice(-6).reverse()
  } else if (check.last_check_at) {
    latestMode = 'status'
    latestPoint = {
      timestamp: check.last_check_at,
      response_ms: check.last_response_ms,
      is_up: check.status === 'up' ? true : check.status === 'down' ? false : null,
      status_code: null,
      tls_days_remaining: check.tls_days_remaining,
      error_message: check.last_error,
    }
  }

  // Pipeline diagnostic (B16): only for a window that reaches now, after the check existed,
  // containing a fresh result, with a successful but empty metrics response.
  const metricsStale =
    !!lastCheckMs &&
    lastCheckMs >= fromTs &&
    nowTick - lastCheckMs < Math.max(10 * 60_000, intervalMs * 5) &&
    windowReachesNow &&
    metricsQ.isSuccess &&
    points.length === 0 &&
    (metricsQ.data?.total_samples ?? 0) === 0 &&
    check.enabled &&
    toTs - Math.max(fromTs, Number.isFinite(createdTs) ? createdTs : fromTs) >= intervalMs * 3

  const incidentsLoading = serverIncidents ? false : incidentsQ.isLoading || statusHistoryQ.isLoading || priorQ.isLoading
  const incidentsError = serverIncidents ? false : incidentsQ.isError || statusHistoryQ.isError
  const dimDays = isCustom || range.hours >= 24
  const slaExcludesMaintenance = sla?.maintenance_sec !== undefined
  const policyTarget = check.sla_target_pct != null && check.sla_target_pct !== '' && Number.isFinite(Number(check.sla_target_pct))
    ? { pct: Number(check.sla_target_pct), source: 'service target' }
    : { pct: 99.9, source: 'built-in default' }

  const zoomTo = (fromISO: string, toISO: string) => setCustom(fromISO, toISO)
  const downloadReport = async (format: 'csv' | 'json') => {
    try {
      const how = await downloadServiceReport({
        check, format, fromISO: range.fromISO, toISO: range.toISO, sla, outages, maintenance: maintenance.rangeWindows,
      })
      toast.success(how === 'server' ? 'Report downloaded' : 'Summary downloaded', how === 'server' ? undefined : 'Generated in the browser: this appliance has no report export yet.')
    } catch (e) {
      toast.error('Report download failed', apiErrorMessage(e))
    }
  }

  return (
    <div className="space-y-4 pb-10">
      <ClockSkewBanner />
      {slaQ.isError && (
        <div role="alert" className="rounded-lg border border-danger/30 bg-danger/5 p-4 text-sm">Availability report could not be loaded. <button className="font-medium text-primary underline" onClick={() => slaQ.refetch()}>Retry</button></div>
      )}
      {probeStale && check.enabled && (
        <div role="status" className="flex items-start gap-3 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <div>
            <strong>Monitoring results are overdue</strong>
            <p className="mt-1 text-muted">
              Last result {relativeTime(check.last_check_at!)} ({exactTime(check.last_check_at)}), judged on the appliance clock.
              {' '}Check the assigned sensor and its connection. Missing results are shown as no data and do not count as uptime or downtime.
              {skewed && ' The browser clock difference noted above is already corrected for and is not the cause.'}
            </p>
          </div>
        </div>
      )}
      <ServiceHeader
        check={check}
        secsToNext={probeStale ? null : secsToNext}
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
        onDuplicate={() => setDupOpen(true)}
        onDelete={() => setDeleteOpen(true)}
        onRunProbe={() => runNow.mutate()}
        runPending={runNow.isPending}
        onPause={() => togglePause.mutate()}
        pausePending={togglePause.isPending}
        inMaintenance={!!check.in_maintenance || activeWindows.length > 0}
        onMaintenance={() => setMaintOpen(true)}
        onAck={() => setAckConfirmOpen(true)}
        activeAlertCount={activeAlertCount}
        onLogs={() => navigate(`/services/${id}/incidents?filter=all`)}
        onExport={exportConfig}
        onReport={downloadReport}
      />

      {(check.in_maintenance || activeWindows.length > 0) && (
        <MaintenanceBanner
          windows={activeWindows}
          supported={maintenance.supported}
          slaExcludesMaintenance={slaExcludesMaintenance}
          nowMs={nowTick}
          onEnd={(w) => endMaintenance.mutate({ window: w, legacyAllowed: true })}
          ending={endMaintenance.isPending}
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
          body={explainServiceFailure(check.last_error, { timeoutSeconds: check.timeout })}
          meta={check.last_check_at ? `Last probe ${relativeTime(check.last_check_at)} · ${exactTime(check.last_check_at)}` : undefined}
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
        slaRange={sla}
        rangeLabel={range.label}
        sla7d={sla7dQ.data}
        sla30d={sla30dQ.data}
        loading={sla24Q.isLoading && sla7dQ.isLoading}
        ongoingOutage={(serverIncidents ? outages.find((o) => o.end == null && !o.clippedStart) : null) ?? wideOngoing ?? outages.find((o) => o.end == null) ?? null}
        nowTick={nowTick}
        probeStale={probeStale}
      />

      <p className="text-xs text-muted">
        Confirmed availability for the selected window ({range.label}) and fixed 7- and 30-day context. Only confirmed incidents
        count as downtime; results remain valid for up to two check intervals and longer gaps are no data. Calendar days use {timezoneLabel(nowTick)}.
      </p>
      <ThirtyDayStrip
        days={days}
        fromTs={fromTs}
        toTs={toTs}
        dim={dimDays}
        onSelectDay={(d) => setCustom(new Date(d.start).toISOString(), new Date(Math.min(d.end, serverNow())).toISOString())}
      />

      <DetailTabs
        label="Service detail sections"
        active={tab}
        onChange={setTab}
        tabs={TABS.map((t) => ({ ...t, count: t.key === 'incidents' ? outageTotal + activeAlertCount : 0 }))}
      />

      {tab === 'overview' && (
        <DetailTabPanel tab="overview">
          <div className="space-y-4">
            <MonitoringSites targetType="service_check" targetId={check.id} />
            <AvailabilityTimeline
              points={points}
              statusHistory={seededHistory}
              sla={sla}
              check={check}
              rangeLabel={range.label}
              fromTs={fromTs}
              toTs={toTs}
            />

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.85fr)]">
              <PerformanceChart
                response={metricsQ.data}
                points={points}
                statusHistory={seededHistory}
                maintenance={maintenance.rangeWindows}
                intervalSec={check.check_interval || 60}
                createdAt={check.created_at}
                rangeLabel={range.label}
                rangeHours={range.hours}
                fromTs={fromTs}
                toTs={toTs}
                nowMs={nowHalfMinute}
                loading={metricsQ.isLoading}
                error={metricsQ.isError}
                onRetry={() => metricsQ.refetch()}
                onZoom={zoomTo}
                stats={[
                  { label: 'Avg', value: formatMs(avgMs, NO_DATA), title: 'Mean of successful probes in the window' },
                  { label: 'P95', value: formatMs(p95Ms, NO_DATA), title: 'P95 of all successful probes in the window (appliance)' },
                  { label: 'Probe failures', value: pct(sla?.probe_error_rate_pct, 2, NO_DATA), title: 'Failed probes ÷ all probes, including unconfirmed failures' },
                  { label: 'Incidents', value: String(incidentCount) },
                ]}
              />
              <div className="space-y-4">
                <LatestProbeCard check={check} latest={latestPoint} mode={latestMode} recent={recentProbes} manualProbe={manualProbe} />
                <HealthScoreCard
                  score={healthScore.score}
                  tint={healthScore.tint}
                  label={healthScore.label}
                  factors={healthScore.factors}
                  coverage={healthScore.coverage}
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
        </DetailTabPanel>
      )}

      {tab === 'uptime' && (
        <DetailTabPanel tab="uptime">
          <div className="space-y-4">
            <SectionHeading title={`Selected window · ${range.label}`} note="Results for the range chosen above." />
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <SloCard sla={sla} rangeLabel={range.label} loading={slaQ.isLoading} error={slaQ.isError} onRetry={() => slaQ.refetch()} fallbackTarget={policyTarget} />
              <SlaSummaryCard sla={sla} rangeLabel={range.label} loading={slaQ.isLoading} error={slaQ.isError} onRetry={() => slaQ.refetch()} nowMs={nowTick} />
            </div>
            <SectionHeading title="Last 30 days · context" note="Fixed to the last 30 calendar days, independent of the selected range." />
            <UptimeCalendar
              days={days}
              fromTs={fromTs}
              toTs={toTs}
              dim={dimDays}
              loading={hourlyQ.isLoading}
              error={hourlyQ.isError}
              onRetry={() => hourlyQ.refetch()}
              onSelectDay={(d) => setCustom(new Date(d.start).toISOString(), new Date(Math.min(d.end, serverNow())).toISOString())}
              outagesFor={outagesForDay}
              checkId={check.id}
              intervalSeconds={check.check_interval || 60}
              historyLoading={wideHistoryLoading}
              historyError={wideHistoryError}
              nowMs={nowTick}
            />
            <DailyUptimeChart days={days} fromTs={fromTs} toTs={toTs} dim={dimDays} />
          </div>
        </DetailTabPanel>
      )}

      {tab === 'incidents' && (
        <DetailTabPanel tab="incidents">
          <IncidentsTab
            checkId={id}
            intervalSec={check.check_interval || 60}
            timeoutSec={check.timeout}
            outages={outages}
            total={outageTotal}
            source={serverIncidents ? 'server' : 'client'}
            loading={incidentsLoading}
            error={incidentsError}
            onRetry={() => { void incidentsQ.refetch(); void statusHistoryQ.refetch(); void priorQ.refetch() }}
            canLoadMore={!!serverIncidents && serverIncidents.total > outages.length && incidentLimit < 500}
            onLoadMore={() => setIncidentLimit((n) => Math.min(500, n + 100))}
            historyTruncated={!serverIncidents && statusHistory.length >= 500}
            evidence={{
              coveragePct: serverIncidents?.response.coverage_pct ?? sla?.coverage_pct ?? null,
              sampleCount: serverIncidents?.response.sample_count ?? sla?.sample_count ?? null,
              coveredSec: sla?.covered_sec ?? null,
            }}
            fromISO={range.fromISO}
            toISO={range.toISO}
            rangeLabel={range.label}
            nowMs={nowTick}
            maintenance={maintenance.rangeWindows}
            activeAlertCount={activeCountQ.data}
            ruleCovered={ruleCoverage?.covered}
            onAckAll={() => setAckConfirmOpen(true)}
            ackPending={ackAll.isPending}
            onWiden={() => setPreset(3)}
            onZoom={zoomTo}
          />
        </DetailTabPanel>
      )}

      {tab === 'config' && (
        <DetailTabPanel tab="config">
          <ConfigTab check={check} onEdit={() => setEditOpen(true)} onExport={exportConfig} />
        </DetailTabPanel>
      )}

      <ServiceCheckFormDialog open={editOpen} onOpenChange={setEditOpen} check={check} />
      <ServiceCheckFormDialog open={dupOpen} onOpenChange={setDupOpen} check={check} mode="duplicate" />
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
      <ConfirmDialog
        open={ackConfirmOpen}
        onOpenChange={setAckConfirmOpen}
        title={`Acknowledge ${activeAlertCount} active alert${activeAlertCount === 1 ? '' : 's'}?`}
        description="Acknowledges every active alert for this monitor, including any triggered outside the selected window. Acknowledging stops escalation reminders; it does not resolve the alerts."
        confirmText="Acknowledge all"
        loading={ackAll.isPending}
        onConfirm={() => ackAll.mutate(undefined, { onSettled: () => setAckConfirmOpen(false) })}
      />
      <MaintenanceDialog
        open={maintOpen}
        onOpenChange={setMaintOpen}
        activeWindows={activeWindows}
        supported={maintenance.supported}
        nowMs={nowTick}
        onStart={(minutes) => startMaintenance.mutate(minutes)}
        onStartCustom={(startsAtISO, endsAtISO) => startMaintenanceCustom.mutate({ startsAtISO, endsAtISO })}
        onEnd={(w) => endMaintenance.mutate({ window: w, legacyAllowed: true })}
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
        coverage={healthScore.coverage}
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

function SectionHeading({ title, note }: { title: string; note?: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 pt-1">
      <h2 className="text-xs font-semibold uppercase tracking-wider text-text2">{title}</h2>
      {note && <span className="text-[11px] text-muted">{note}</span>}
    </div>
  )
}

function PanelState({
  loading, error, onRetry, loadingText, errorText, children,
}: {
  loading?: boolean
  error?: boolean
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
  check, secsToNext, rangePicker, onEdit, onDuplicate, onDelete, onRunProbe, runPending,
  onPause, pausePending, inMaintenance, onMaintenance, onAck, activeAlertCount, onLogs, onExport, onReport,
}: {
  check: ServiceCheckExt
  secsToNext: number | null
  rangePicker: React.ReactNode
  onEdit: () => void
  onDuplicate: () => void
  onDelete: () => void
  onRunProbe: () => void
  runPending: boolean
  onPause: () => void
  pausePending: boolean
  inMaintenance: boolean
  onMaintenance: () => void
  onAck: () => void
  activeAlertCount: number
  onLogs: () => void
  onExport: () => void
  onReport: (format: 'csv' | 'json') => void
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
        : check.status === 'unknown' ? (check.last_check_at ? 'No recent data' : 'Pending')
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
            {check.enabled && secsToNext != null && check.status !== 'down' && (check.verification_attempt ?? 0) > 0 && (
              <span className="rounded-full bg-warning/10 px-2 py-1 text-xs font-medium text-warning" role="status">
                Verifying failure · attempt {check.verification_attempt}/{check.retry_count ?? 1}
              </span>
            )}
            <span className="inline-flex items-center gap-1 rounded border border-border bg-surface2/60 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider text-muted">
              <t.Icon className="h-2.5 w-2.5" />{t.label} · L{check.level ?? 1}
            </span>
            {check.criticality && (
              <span className="rounded border border-border px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider text-muted" title="Business criticality">
                {check.criticality}
              </span>
            )}
            {inMaintenance && <Badge variant="info"><Wrench className="h-3 w-3" />Maintenance</Badge>}
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
            <span title={check.last_check_at ? exactTime(check.last_check_at) : undefined}>
              {check.last_check_at
                ? <>checked {relativeTime(check.last_check_at)} <span className="font-mono">({new Date(check.last_check_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })})</span></>
                : NEVER_CHECKED.toLowerCase()}
            </span>
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
            {check.owner_team && (
              <>
                <Dot />
                <span className="inline-flex items-center gap-1" title="Owner / team"><Users className="h-3 w-3" />{check.owner_team}</span>
              </>
            )}
            {check.business_owner && !check.owner_team && (
              <>
                <Dot />
                <span className="inline-flex items-center gap-1" title="Business owner"><Users className="h-3 w-3" />{check.business_owner}</span>
              </>
            )}
            {check.runbook_url && (
              <>
                <Dot />
                <RunbookLink url={check.runbook_url} className="text-[11px]" />
              </>
            )}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2">
        {rangePicker}
        <span className="hidden h-5 w-px bg-border sm:inline-block" />
        <Button size="sm" className="h-8" onClick={onRunProbe} disabled={runPending} title="Runs one probe now. A manual probe is diagnostic only and does not change status or SLA.">
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
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon" className="h-8 w-8" aria-label="More actions">
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-60">
            <DropdownMenuItem onSelect={onMaintenance}><Wrench />{inMaintenance ? 'Manage maintenance' : 'Schedule maintenance'}</DropdownMenuItem>
            <DropdownMenuItem onSelect={onAck} disabled={activeAlertCount === 0}>
              <Check />Acknowledge all active{activeAlertCount > 0 ? ` (${activeAlertCount})` : ''}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={onDuplicate}><Copy />Duplicate monitor</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onReport('csv')}><FileSpreadsheet />Download window report (CSV)</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => onReport('json')}><FileText />Download window report (JSON)</DropdownMenuItem>
            <DropdownMenuItem onSelect={onLogs}><FileText />Incident log</DropdownMenuItem>
            <DropdownMenuItem onSelect={onExport}><Download />Export config</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={onDelete} destructive><Trash2 />Delete check</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}

/* ─── Hero strip: current status, selected window, fixed context ───────── */

function HeroStrip({ check, sla24, slaRange, rangeLabel, sla7d, sla30d, loading, ongoingOutage, nowTick, probeStale }: {
  check: ServiceCheckExt
  sla24?: SlaStats
  slaRange?: SlaStats
  rangeLabel: string
  sla7d?: SlaStats
  sla30d?: SlaStats
  loading: boolean
  ongoingOutage: Outage | null
  nowTick: number
  probeStale: boolean
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
  } else if (probeStale) {
    statusCell = { label: 'Monitoring', value: 'Results overdue', tone: 'text-warning', sub: check.last_check_at ? `last result ${exactTime(check.last_check_at, false)}` : 'check the assigned sensor' }
  } else if (isDown) {
    statusCell = {
      label: check.status === 'down' ? 'Currently down for' : 'Degraded for',
      value: downForSec != null ? `${ongoingOutage?.clippedStart ? '≥ ' : ''}${formatDur(downForSec)}` : '—',
      tone: check.status === 'down' ? 'text-danger' : 'text-warning',
      sub: check.last_error ? explainServiceFailure(check.last_error, { timeoutSeconds: check.timeout }) : 'no error detail',
    }
  } else if (check.status === 'unknown') {
    statusCell = { label: 'Monitoring', value: check.last_check_at ? 'No recent data' : 'Pending', tone: 'text-muted', sub: check.last_check_at ? 'check the assigned sensor' : 'waiting for the first probe' }
  } else {
    statusCell = {
      label: 'Currently up for',
      value: formatDur(sla24?.uptime_streak_started_at ? Math.max(0, (nowTick - Date.parse(sla24.uptime_streak_started_at)) / 1000) : null),
      tone: 'text-success',
      sub: sla24?.uptime_streak_started_at
        ? `observed since ${exactTime(sla24.uptime_streak_started_at, false)}`
        : 'recovery time unavailable',
    }
  }

  const windowCell = (label: string, s?: SlaStats) => {
    const down = s?.total_downtime_sec || 0
    const inc = s?.incident_count || 0
    const coverage: string[] = []
    if (s?.uptime_pct != null) {
      coverage.push(`${(s.coverage_pct ?? 0).toFixed(1)}% of window observed`)
      if ((s.maintenance_sec ?? 0) > 0) coverage.push(`${formatDur(s.maintenance_sec)} maintenance excluded`)
    }
    return {
      label,
      value: formatAvailability(s?.uptime_pct, s?.total_downtime_sec, 2),
      tone: BAND_TEXT[availabilityBand(s?.uptime_pct, s?.total_downtime_sec)],
      sub: s?.uptime_pct == null
        ? 'no monitoring evidence in this window'
        : down > 0
          ? `${formatDur(down)} down · ${inc} incident${inc === 1 ? '' : 's'}`
          : 'no confirmed downtime',
      coverage: coverage.length ? coverage.join(' · ') : undefined,
    }
  }

  const cells = [
    { ...statusCell, coverage: undefined, selected: false },
    { ...windowCell(rangeLabel, slaRange), selected: true },
    { ...windowCell('Last 7 days', sla7d), selected: false },
    { ...windowCell('Last 30 days', sla30d), selected: false },
  ]

  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      {cells.map((c, i) => (
        <div
          key={`${i}-${c.label}`}
          className={cn(
            'rounded-xl border bg-surface px-4 py-3 transition-colors',
            c.selected ? 'border-primary/40 ring-1 ring-primary/25' : 'border-border hover:border-border-strong',
          )}
        >
          <div className="flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">
            {i === 0 && <PulseDot status={pulseStatusOf(check.status, check.enabled)} size="sm" />}
            <span className="truncate" title={c.label}>{c.label}</span>
            {c.selected ? (
              <span className="ml-auto shrink-0 rounded bg-primary/10 px-1.5 py-px text-[9px] font-semibold text-primary">Selected</span>
            ) : i > 1 ? (
              <span className="ml-auto shrink-0 rounded bg-surface2 px-1.5 py-px text-[9px] font-semibold text-muted">Context</span>
            ) : null}
          </div>
          <div className={cn('mt-1 text-[24px] font-bold leading-none tabular-nums', c.tone)}>{c.value}</div>
          <div className="mt-1 truncate text-[10.5px] text-muted" title={c.sub}>{c.sub}</div>
          {c.coverage && <div className="mt-1 text-[10px] text-muted">{c.coverage}</div>}
        </div>
      ))}
    </div>
  )
}

/* ─── 30-day bar strip ──────────────────────────────────────────────────── */

function ThirtyDayStrip({ days, fromTs, toTs, dim, onSelectDay }: {
  days: DayUptime[]
  fromTs: number
  toTs: number
  /** Dim days outside the selected range (only for day-scale ranges). */
  dim: boolean
  onSelectDay: (d: DayUptime) => void
}) {
  const overall = coveredWeightedUptime(days)
  const totalDowntime = days.reduce((s, d) => s + d.downtimeSec, 0)

  const inRange = (d: DayUptime) => d.end > fromTs && d.start < toTs
  const inCount = days.filter(inRange).length
  const dimOthers = dim && inCount > 0 && inCount < days.length

  const cells = days.map((d) => {
    const when = d.date.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
    return {
      key: d.key,
      pct: colourPct(d.uptimePct, d.downtimeSec),
      dim: dimOthers && !inRange(d),
      title: d.uptimePct == null
        ? `${when} — no data`
        : `${when} — ${formatAvailability(d.uptimePct, d.downtimeSec, 3)} available${d.downtimeSec > 0 ? ` · ${formatDur(d.downtimeSec)} down` : ''}`,
    }
  })

  return (
    <Card>
      <CardContent className="px-4 py-3">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-baseline gap-2">
            <h3 className="text-sm font-semibold tracking-tight">Last 30 days</h3>
            <span className="text-[11px] text-muted">context · click a day to zoom the page to it</span>
          </div>
          <div className="flex items-baseline gap-3 text-xs">
            {totalDowntime > 0 && <span className="text-muted">{formatDur(totalDowntime)} down</span>}
            {overall != null && (
              <span className={cn('font-mono font-semibold tabular-nums', BAND_TEXT[availabilityBand(overall, totalDowntime)])}>
                {formatAvailability(overall, totalDowntime, 3)}
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

const PARTIAL_COLOR = 'rgb(var(--warning))'

function AvailabilityTimeline({
  points, statusHistory, check, rangeLabel, fromTs, toTs, sla,
}: {
  sla?: SlaStats
  points: MetricPoint[]
  statusHistory: SeededEvent[]
  check: ServiceCheckExt
  rangeLabel: string
  fromTs: number
  toTs: number
}) {
  const buckets = useMemo(
    () => buildAvailabilityBuckets(points, statusHistory, check, fromTs, toTs),
    [points, statusHistory, check, fromTs, toTs],
  )
  const covered = buckets.filter((b) => b.state !== 'gap')
  const downCount = covered.filter((b) => b.state === 'down').length
  const partialCount = covered.filter((b) => b.state === 'partial').length
  const pctUp = sla?.probe_uptime_pct ?? null
  const coverage = sla?.coverage_pct ?? null

  return (
    <SectionCard
      title="Probe availability timeline"
      subtitle={
        <span>
          {rangeLabel} · every probe, including failures that recovered before confirmation
          {coverage != null && coverage < 99.5 && ` · ${coverage.toFixed(0)}% of the window has data`}
        </span>
      }
      actions={
        <div className="flex items-baseline gap-3 text-xs">
          {downCount > 0 && <span className="font-medium text-danger">{downCount} failed interval{downCount === 1 ? '' : 's'}</span>}
          {partialCount > 0 && <span className="font-medium text-warning">{partialCount} with partial failures</span>}
          {pctUp != null && (
            <span className="font-mono font-semibold tabular-nums text-text2">{pctUp.toFixed(2)}% probe success</span>
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
          <div className="flex h-9 gap-[1px] overflow-hidden rounded-lg bg-surface2" role="img" aria-label={`Probe availability for ${rangePhrase(rangeLabel)}: ${downCount} failed and ${partialCount} partially failed intervals`}>
            {buckets.map((b, i) => (
              <div
                key={i}
                className="flex-1 transition-opacity hover:opacity-70"
                style={{
                  backgroundColor:
                    b.state === 'up' ? UP_COLOR
                      : b.state === 'down' ? DOWN_COLOR
                        : b.state === 'partial' ? PARTIAL_COLOR
                          : 'transparent',
                }}
                title={
                  b.state === 'gap'
                    ? `${timeTooltipLabelFormatter(b.start)} — no data`
                    : `${timeTooltipLabelFormatter(b.start)} — ${b.down} failed of ${b.up + b.down} probe${b.up + b.down === 1 ? '' : 's'}`
                }
              />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <span className="text-[10px] tabular-nums text-muted">{exactTime(fromTs, false)}</span>
            <div className="flex items-center gap-3 text-[10px] text-muted">
              <Swatch color={UP_COLOR} label="All passed" />
              <Swatch color={PARTIAL_COLOR} label="Some failed" />
              <Swatch color={DOWN_COLOR} label="All failed" />
              <span className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-sm bg-surface2 ring-1 ring-inset ring-border" />No data
              </span>
            </div>
            <span className="text-[10px] tabular-nums text-muted">{exactTime(toTs, false)}</span>
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
  points: Array<{ timestamp: string; is_up: boolean | null; sample_count?: number | null; failed_count?: number | null }>,
  _history: unknown[],
  check: { check_interval?: number },
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
  const slots: Array<{ start: number; end: number; up: number; down: number }> =
    Array.from({ length: count }, (_, i) => ({ start: fromTs + i * width, end: fromTs + (i + 1) * width, up: 0, down: 0 }))

  for (const p of points) {
    const ts = Date.parse(p.timestamp)
    if (!Number.isFinite(ts) || ts < fromTs || ts >= toTs) continue
    const i = Math.min(count - 1, Math.floor((ts - fromTs) / width))
    if (i < 0) continue
    // Rollup buckets carry exact counts, so one failed probe in a healthy bucket stays visible.
    if (p.sample_count != null && p.failed_count != null) {
      slots[i].down += Math.max(0, p.failed_count)
      slots[i].up += Math.max(0, p.sample_count - p.failed_count)
    } else if (p.is_up === true) slots[i].up++
    else if (p.is_up === false) slots[i].down++
  }

  return slots.map((s) => {
    let st: 'up' | 'down' | 'partial' | 'gap' = 'gap'
    if (s.down > 0 && s.up > 0) st = 'partial'
    else if (s.down > 0) st = 'down'
    else if (s.up > 0) st = 'up'
    return { start: s.start, end: s.end, state: st, up: s.up, down: s.down }
  })
}

/* ─── Performance chart: see components/services/detail/PerformanceChart ─── */

/* ─── Latest probe ──────────────────────────────────────────────────────── */

const LATEST_TITLE: Record<LatestMode, string> = {
  latest: 'Latest probe',
  window: 'Last probe in selected window',
  status: 'Last result',
}

function LatestProbeCard({ check, latest, mode, recent, manualProbe }: {
  check: ServiceCheckExt
  latest: MetricPoint | null
  mode: LatestMode | null
  recent: MetricPoint[]
  manualProbe: { result: ProbeEvidenceResult; observedAt: string } | null
}) {
  const expected = check.http_expected_statuses || String(check.http_expected_status || 200)
  const authLabel = check.credential_name
    ? `${check.credential_name} · ${check.credential_auth_type === 'ntlm' ? 'NTLM' : check.credential_auth_type}`
    : 'No saved credential'
  const resultLabel = latest?.is_up == null ? NO_DATA : latest.is_up ? 'UP' : 'DOWN'
  const resultTone = latest?.is_up == null ? 'text-muted' : latest.is_up ? 'text-success' : 'text-danger'
  const when = latest?.timestamp
    ? `${relativeTime(latest.timestamp)} · ${new Date(latest.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`
    : check.enabled ? 'Waiting for the first probe' : NEVER_CHECKED

  return (
    <SectionCard
      title={mode ? LATEST_TITLE[mode] : 'Latest probe'}
      subtitle={mode === 'status' ? 'From the monitor status; individual probe details are not available here' : mode === 'window' ? 'This appliance cannot report probes outside the selected window' : undefined}
      actions={check.check_type === 'http' ? (
        <span className="rounded-full border border-border bg-surface2/50 px-2 py-0.5 text-[10px] text-muted">Expect HTTP {expected}</span>
      ) : undefined}
    >
      <div className="grid grid-cols-2 gap-2">
        <ProbeStat label="Result" value={resultLabel} className={resultTone} sub={when} title={latest?.timestamp ? exactTime(latest.timestamp) : undefined} />
        <ProbeStat
          label="Response"
          value={formatMs(latest?.response_ms, NO_DATA)}
          className="text-info"
          sub={latest?.status_code != null ? `HTTP ${latest.status_code}` : check.check_type === 'http' ? authLabel : (typeMeta[check.check_type]?.label || '') + ' probe'}
        />
      </div>
      {(latest?.error_message || check.last_error) && latest?.is_up === false && (
        <div className="mt-2 rounded-md border border-danger/30 bg-danger/10 px-2 py-1.5 text-danger">
          <ServiceFailureReason reason={latest?.error_message || check.last_error} failureStage={latest?.network_diagnostics?.failure_stage} timeoutSeconds={check.timeout} />
        </div>
      )}
      {latest?.network_diagnostics && (
        <div className="mt-3 rounded-md border border-border bg-surface2/30 p-2.5">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Connection diagnostics · scheduled probe</div>
          <div className="mt-1 break-all text-xs">{latest.network_diagnostics.remote_ip || 'No connection established'} · {latest.network_diagnostics.ip_version === 'ipv4' ? 'IPv4 only' : latest.network_diagnostics.ip_version === 'ipv6' ? 'IPv6 only' : 'Auto'}</div>
          <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
            <span>DNS <b>{formatMs(latest.network_diagnostics.dns_ms)}</b></span>
            <span>TCP <b>{formatMs(latest.network_diagnostics.connect_ms)}</b></span>
            <span>TLS <b>{formatMs(latest.network_diagnostics.tls_ms)}</b></span>
          </div>
          {latest.network_diagnostics.failure_stage && <div className="mt-1 text-xs text-danger">Failed stage: {latest.network_diagnostics.failure_stage.toUpperCase()}</div>}
          <p className="mt-1 text-[10px] text-muted">Phase times include connection attempts and redirects. A dash means the phase was not observed.</p>
        </div>
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
            <span className="font-normal text-muted" title={exactTime(manualProbe.observedAt)}>{relativeTime(manualProbe.observedAt)}</span>
          </div>
          <div className="mt-1 text-[11px] text-muted">
            {formatMs(manualProbe.result.response_time_ms)}
            {manualProbe.result.details?.status_code != null ? ` · HTTP ${manualProbe.result.details.status_code}` : ''}
            {' · diagnostic only; not counted in status or SLA'}
          </div>
          {manualProbe.result.error && <div className="mt-1 text-danger"><ServiceFailureReason reason={manualProbe.result.error} timeoutSeconds={check.timeout} /></div>}
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
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Recent scheduled probes</div>
          <div className="space-y-1">
            {recent.slice(0, 5).map((p, i) => (
              <div key={`${p.timestamp}-${i}`} className="flex items-center gap-2 text-[11px]">
                <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', p.is_up ? 'bg-success' : 'bg-danger')} />
                <span className="w-[68px] shrink-0 tabular-nums text-muted" title={exactTime(p.timestamp)}>
                  {new Date(p.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
                <span className={cn('font-medium', p.is_up ? 'text-success' : 'text-danger')}>{p.is_up ? 'UP' : 'DOWN'}</span>
                <span className="tabular-nums text-muted">{formatMs(p.response_ms)}</span>
                <span className="min-w-0 flex-1 truncate text-muted" title={p.error_message || undefined}>
                  {p.error_message ? explainServiceFailure(p.error_message, { failureStage: p.network_diagnostics?.failure_stage, timeoutSeconds: check.timeout }) : p.status_code != null ? `HTTP ${p.status_code}` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </SectionCard>
  )
}

function ProbeStat({ label, value, sub, className, title }: { label: string; value: string; sub: string; className?: string; title?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface2/30 px-2.5 py-2" title={title}>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('mt-0.5 text-lg font-semibold tabular-nums', className)}>{value}</div>
      <div className="truncate text-[10px] text-muted" title={sub}>{sub}</div>
    </div>
  )
}

/* ─── Health score ──────────────────────────────────────────────────────── */

function HealthScoreCard({ score, tint, label, factors, coverage, onViewDetails }: {
  score: number | null
  tint: string
  label: string
  factors: HealthFactor[]
  coverage: number
  onViewDetails: () => void
}) {
  const radius = 36
  const circ = 2 * Math.PI * radius
  const offset = circ - ((score ?? 0) / 100) * circ
  const missing = factors.filter((f) => !f.available)
  return (
    <SectionCard
      title="Health score"
      actions={<button type="button" onClick={onViewDetails} className="text-xs text-primary hover:underline">How it's scored</button>}
    >
      <div className="flex items-center gap-4">
        <div className="relative h-[84px] w-[84px] shrink-0" role="img" aria-label={score == null ? 'Health score: no data' : `Health score ${score} of 100, ${label}`}>
          <svg viewBox="0 0 100 100" className="h-full w-full -rotate-90">
            <circle cx="50" cy="50" r={radius} fill="none" stroke="rgb(var(--border))" strokeWidth="8" />
            <circle cx="50" cy="50" r={radius} fill="none" stroke={tint} strokeWidth="8" strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={offset} />
          </svg>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <div className="text-2xl font-bold leading-none tabular-nums" style={{ color: tint }}>{score ?? '—'}</div>
            <div className="text-[9px] font-semibold" style={{ color: tint }}>{label}</div>
          </div>
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          {score == null && <p className="text-xs text-muted">No observed availability in this window. A health score will appear when results are available.</p>}
          {factors.map((f) => (
            <div key={f.key} className="flex items-center gap-2 text-[10px]">
              <span className="w-[68px] shrink-0 truncate text-muted">{f.label}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
                {f.available && (
                  <div
                    className="h-full rounded-full transition-all"
                    style={{ width: `${f.subScore}%`, background: f.subScore > 90 ? UP_COLOR : f.subScore > 70 ? WARN_COLOR : DOWN_COLOR }}
                  />
                )}
              </div>
              <span className="w-10 shrink-0 text-right font-mono tabular-nums">{f.available ? f.subScore : 'n/a'}</span>
            </div>
          ))}
          {score != null && missing.length > 0 && (
            <p className="text-[10px] text-muted">
              Coverage {Math.round(coverage * 100)}%: {missing.map((f) => f.label.toLowerCase()).join(', ')} not measured and excluded; remaining weights rescaled.
            </p>
          )}
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
  check?: ServiceCheckExt,
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
    const gap = !up && !down && h.new_status !== 'warning' && h.new_status !== 'degraded'
    return {
      id: `status-${h.timestamp}-${i}`,
      timestamp: h.timestamp,
      kind: 'status',
      severity: up ? 'success' : down ? 'critical' : gap ? 'info' : 'warning',
      title: up ? (h.old_status === 'unknown' ? 'Monitoring resumed' : 'Service recovered') : down ? 'Service went down' : gap ? 'Monitoring evidence stopped' : `Status → ${statusOf(h.new_status).label}`,
      subtitle: [h.reason ? explainServiceFailure(h.reason) : null, h.old_status ? `${h.old_status} → ${h.new_status}` : null].filter(Boolean).join(' · ') || undefined,
    }
  })

  const alertEvents: ActivityEvent[] = alerts.filter((a) => inRange(a.triggered_at)).map((a) => {
    const recovery = isRecoveryAlert(a)
    return {
      id: `alert-${a.id}`,
      timestamp: a.triggered_at,
      kind: 'alert',
      severity: recovery ? 'success' : normalizeSeverity(a.severity),
      title: recovery ? 'Recovery notification' : a.status === 'resolved' ? 'Alert (resolved)' : a.status === 'acknowledged' ? 'Alert acknowledged' : 'Alert fired',
      subtitle: cleanAlertMessage(a.message),
      href: `/alerts/${a.id}`,
    }
  })

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
      subtitle: explainServiceFailure(check.last_error),
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
      subtitle={showingFallback ? `Nothing in ${rangePhrase(rangeLabel)} — showing the most recent 30-day history` : rangeLabel}
      actions={<button type="button" onClick={onViewAll} className="text-xs text-primary hover:underline">View all</button>}
    >
      <div className="mb-3 flex gap-0.5 rounded-md bg-surface2/60 p-0.5" role="group" aria-label="Activity filter">
        {(['all', 'status', 'alert'] as const).map((f) => (
          <button
            key={f}
            type="button"
            aria-pressed={filter === f}
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
            const Icon = e.kind === 'alert' ? Bell : e.severity === 'success' ? CheckCircle2 : e.severity === 'critical' ? XCircle : e.severity === 'info' ? Info : AlertTriangle
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
                  <div className="font-mono text-[10px] tabular-nums text-text2">{exactTime(e.timestamp)}</div>
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
          <DialogDescription className="text-xs">Status changes and alerts for this monitor, newest first.</DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] space-y-2 overflow-y-auto pr-1">
          {events.length === 0 && <div className="py-8 text-center text-xs text-muted">No events in this window.</div>}
          {events.map((e) => (
            <div key={e.id} className="rounded-md border border-border bg-surface2/30 px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold">{e.title}</span>
                <span className="text-[10px] text-muted">{relativeTime(e.timestamp)}</span>
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-muted">{exactTime(e.timestamp)}</div>
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
                        c.status === 'up' ? 'bg-success' : c.status === 'down' ? 'bg-danger' : c.status === 'unknown' ? 'bg-muted/60' : 'bg-warning',
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

/* ─── Uptime tab: 30-day calendar ───────────────────────────────────────── */

type DayUptime = {
  key: string
  start: number
  end: number
  date: Date
  uptimePct: number | null
  samples: number
  coveredSec: number
  downtimeSec: number
  maintenanceSec: number
  /** One slot per real clock hour of the local day (23/24/25 around DST changes). */
  hours: Array<{ hour: number; start: number; uptimePct: number | null; samples: number }>
}

function buildDailyUptime(hours: Array<{ ts: string; uptime_pct: number | null; sample_count: number; covered_sec?: number; maintenance_sec?: number }>, dayCount: number, nowMs: number = Date.now()): DayUptime[] {
  // Local-midnight boundaries via calendar arithmetic, never +24 h: DST days are 23 or 25 hours.
  const today = new Date(nowMs)
  today.setHours(0, 0, 0, 0)
  const days: DayUptime[] = []
  for (let i = dayCount - 1; i >= 0; i--) {
    const date = new Date(today)
    date.setDate(today.getDate() - i)
    const next = new Date(date)
    next.setDate(next.getDate() + 1)
    const start = date.getTime()
    const end = next.getTime()
    const slots = Math.max(1, Math.round((end - start) / 3_600_000))
    days.push({
      key: dayKey(date),
      start,
      end,
      date,
      uptimePct: null,
      samples: 0,
      coveredSec: 0,
      downtimeSec: 0,
      maintenanceSec: 0,
      hours: Array.from({ length: slots }, (_, h) => ({ hour: h, start: start + h * 3_600_000, uptimePct: null, samples: 0 })),
    })
  }
  const acc = days.map(() => ({ pctSum: 0, weight: 0, samples: 0, maint: 0 }))
  for (const h of hours) {
    const t = Date.parse(h.ts)
    if (!Number.isFinite(t)) continue
    const di = days.findIndex((d) => t >= d.start && t < d.end)
    if (di < 0) continue
    const d = days[di]
    const a = acc[di]
    const slot = Math.floor((t - d.start) / 3_600_000)
    if (slot >= 0 && slot < d.hours.length) d.hours[slot] = { ...d.hours[slot], uptimePct: h.uptime_pct, samples: h.sample_count || 0 }
    a.maint += h.maintenance_sec || 0
    // Weight each hour by observed seconds, including partial current hours.
    if (h.uptime_pct != null) {
      const weight = h.covered_sec ?? 0
      a.pctSum += h.uptime_pct * weight
      a.weight += weight
      a.samples += h.sample_count || 0
    }
  }
  days.forEach((d, i) => {
    const a = acc[i]
    const uptimePct = a.weight > 0 ? a.pctSum / a.weight : null
    const coveredSec = Math.min((d.end - d.start) / 1000, a.weight)
    d.uptimePct = uptimePct
    d.coveredSec = coveredSec
    d.samples = a.samples
    d.maintenanceSec = a.maint
    d.downtimeSec = uptimePct == null ? 0 : ((100 - uptimePct) / 100) * coveredSec
  })
  return days
}

/** Availability weighted by each day's covered wall-clock hours, so a day with one stored
    sample doesn't count as heavily as a fully monitored one. */
function coveredWeightedUptime(days: DayUptime[]): number | null {
  let up = 0
  let weight = 0
  for (const d of days) {
    if (d.uptimePct == null) continue
    const covered = d.coveredSec
    if (covered === 0) continue
    up += d.uptimePct * covered
    weight += covered
  }
  return weight > 0 ? up / weight : null
}

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function UptimeCalendar({ days, fromTs, toTs, dim, loading, error, onRetry, onSelectDay, outagesFor, checkId, intervalSeconds, historyLoading, historyError, nowMs }: {
  days: DayUptime[]
  fromTs: number
  toTs: number
  dim: boolean
  loading: boolean
  error: boolean
  onRetry: () => void
  onSelectDay: (d: DayUptime) => void
  outagesFor: (d: DayUptime) => Outage[]
  checkId: string
  intervalSeconds: number
  historyLoading: boolean
  historyError: boolean
  nowMs: number
}) {
  const [selected, setSelected] = useState<string | null>(null)
  const measured = days.filter((d) => d.uptimePct != null)
  const overall = coveredWeightedUptime(days)
  const totalDowntime = days.reduce((s, d) => s + d.downtimeSec, 0)
  const worst = measured.length ? measured.reduce((w, d) => ((d.uptimePct as number) < (w.uptimePct as number) ? d : w)) : null
  const selectedDay = days.find((d) => d.key === selected) || null
  const selectedOutages = selectedDay ? outagesFor(selectedDay) : []

  // Dim days outside a day-scale selected range; a 1-hour range would dim nearly everything.
  const inRange = (d: DayUptime) => d.end > fromTs && d.start < toTs
  const inCount = days.filter(inRange).length
  const dimOthers = dim && inCount > 0 && inCount < days.length

  // Monday-first grid: pad the first week so each column is a weekday.
  const leadPad = days.length ? (days[0].date.getDay() + 6) % 7 : 0

  return (
    <SectionCard
      title="Uptime calendar"
      subtitle="Confirmed availability · last 30 days · select a day for incidents and failed checks"
      actions={
        <div className="flex items-center gap-3 text-xs">
          {overall != null && (
            <span className={cn('font-mono font-semibold tabular-nums', BAND_TEXT[availabilityBand(overall, totalDowntime)])}>{formatAvailability(overall, totalDowntime, 3)}</span>
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
                <div key={w} aria-hidden className="pb-0.5 text-center text-[10px] font-semibold uppercase tracking-wider text-muted">{w}</div>
              ))}
              {Array.from({ length: leadPad }).map((_, i) => <div key={`pad-${i}`} aria-hidden />)}
              {days.map((d) => {
                const band = availabilityBand(d.uptimePct, d.downtimeSec)
                const isSelected = selected === d.key
                const label = formatAvailability(d.uptimePct, d.downtimeSec, 2, '—')
                const when = d.date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
                const coverage = d.uptimePct == null ? 'no monitoring evidence'
                  : `${Math.round((d.coveredSec / Math.max(1, (Math.min(d.end, nowMs) - d.start) / 1000)) * 100)}% of the day observed`
                const aria = d.uptimePct == null
                  ? `${when}: no data`
                  : `${when}: ${formatAvailability(d.uptimePct, d.downtimeSec, 3)} available, ${d.downtimeSec > 0 ? `${formatDur(d.downtimeSec)} confirmed downtime` : 'no confirmed downtime'}, ${coverage}${d.maintenanceSec > 0 ? `, ${formatDur(d.maintenanceSec)} maintenance` : ''}`
                return (
                  <button
                    key={d.key}
                    type="button"
                    aria-pressed={isSelected}
                    aria-label={aria}
                    onClick={() => setSelected(isSelected ? null : d.key)}
                    title={aria}
                    className={cn(
                      'group relative flex h-[62px] flex-col items-center justify-center rounded-lg border transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60',
                      isSelected ? 'border-primary ring-2 ring-primary/40' : 'border-border hover:border-border-strong',
                      d.uptimePct == null && 'bg-surface2/40',
                      dimOthers && !inRange(d) && !isSelected && 'opacity-60 hover:opacity-100',
                    )}
                    style={d.uptimePct != null ? { backgroundColor: withAlpha(BAND_FILL[band], band === 'perfect' ? 0.22 : 0.28) } : undefined}
                  >
                    <span className="text-[11px] font-semibold tabular-nums text-text">{d.date.getDate()}</span>
                    <span className={cn('text-[10px] font-medium tabular-nums', BAND_TEXT[band])}>{label}</span>
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
                <Swatch color={BAND_FILL.perfect} label="100% (no confirmed downtime)" />
                <Swatch color={BAND_FILL.good} label="≥ 99%" />
                <Swatch color={BAND_FILL.fair} label="≥ 95%" />
                <Swatch color={BAND_FILL.poor} label="< 95%" />
                <span className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-sm bg-surface2 ring-1 ring-inset ring-border" />No data
                </span>
              </div>
              {worst && (worst.downtimeSec > 0 || (worst.uptimePct as number) < 100) && (
                <span className="text-[11px] text-muted">
                  Worst day{' '}
                  <span className={BAND_TEXT[availabilityBand(worst.uptimePct, worst.downtimeSec)]}>
                    {worst.date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · {formatAvailability(worst.uptimePct, worst.downtimeSec, 2)}
                  </span>
                </span>
              )}
            </div>
            <p className="mt-1.5 text-[10px] text-muted">Percentages are truncated, never rounded up: 100% means no confirmed downtime that day.</p>

            {selectedDay && (
              <div className="mt-3 rounded-lg border border-border bg-surface2/30 p-3">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-semibold">
                    {selectedDay.date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
                    <span className={cn('ml-2 font-mono font-medium tabular-nums', BAND_TEXT[availabilityBand(selectedDay.uptimePct, selectedDay.downtimeSec)])}>
                      {formatAvailability(selectedDay.uptimePct, selectedDay.downtimeSec, 3)}
                    </span>
                    {selectedDay.downtimeSec > 0 && (
                      <span className="ml-2 text-[11px] font-normal text-muted">{formatDur(selectedDay.downtimeSec)} down</span>
                    )}
                    {selectedDay.maintenanceSec > 0 && (
                      <span className="ml-2 text-[11px] font-normal text-muted">{formatDur(selectedDay.maintenanceSec)} maintenance excluded</span>
                    )}
                    {selectedDay.hours.length !== 24 && (
                      <span className="ml-2 text-[11px] font-normal text-muted">({selectedDay.hours.length}-hour day, clock change)</span>
                    )}
                  </div>
                  <Button variant="outline" size="sm" className="h-7" onClick={() => onSelectDay(selectedDay)}>
                    Zoom to this day
                  </Button>
                </div>
                <div className="flex gap-[2px]">
                  {selectedDay.hours.map((h) => {
                    const hh = new Date(h.start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                    return (
                      <div
                        key={h.hour}
                        className="h-6 flex-1 rounded-sm"
                        style={{
                          backgroundColor: h.uptimePct == null ? 'rgb(var(--surface3) / 0.5)' : BAND_FILL[availabilityBand(h.uptimePct, h.uptimePct < 100 ? 1 : 0)],
                        }}
                        title={
                          h.uptimePct == null
                            ? `${hh} — no data`
                            : `${hh} — ${formatAvailability(h.uptimePct, undefined, 2)} · ${h.samples > 0 ? `${h.samples} samples` : 'observed coverage'}`
                        }
                      />
                    )
                  })}
                </div>
                <div className="mt-1 flex justify-between text-[9px] tabular-nums text-muted">
                  <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span>
                </div>

                <DailyProbeFailures checkId={checkId} start={selectedDay.start} end={selectedDay.end}
                  intervalSeconds={intervalSeconds} downtimeSeconds={selectedDay.downtimeSec}
                  hasCoverage={selectedDay.coveredSec > 0} />
                <div className="mt-3 border-t border-border/60 pt-2.5">
                  <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
                    Confirmed status incidents
                    {selectedOutages.length > 0 && (
                      <span className="ml-1.5 font-mono text-danger">{selectedOutages.length}</span>
                    )}
                  </div>
                  {historyLoading ? <p className="text-[11px] text-muted">Loading confirmed status incidents…</p>
                    : historyError ? <p className="text-[11px] text-danger">Could not load confirmed status incidents.</p>
                    : selectedOutages.length === 0 ? (
                    <p className="text-[11px] text-muted">
                      {selectedDay.coveredSec > 0 ? 'No confirmed Down or Warning incidents recorded for this day.' : 'No monitoring evidence for this day.'}
                    </p>
                  ) : (
                    <div className="space-y-1.5">
                      {selectedOutages.map((o) => {
                        const end = o.end ?? nowMs
                        const fmt = (t: number) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
                        return (
                          <div key={o.id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
                            <span className={cn(
                              'inline-flex shrink-0 items-center rounded px-1.5 py-px text-[9px] font-semibold uppercase tracking-wider',
                              o.kind === 'down' ? 'bg-danger/15 text-danger' : 'bg-warning/15 text-warning',
                            )}>
                              {o.kind === 'down' ? 'Down' : 'Warn'}
                            </span>
                            <span className="font-mono tabular-nums text-text2">
                              {o.clippedStart ? '…' : ''}{fmt(o.start)} → {o.end == null ? 'ongoing' : `${fmt(end)}${o.clippedEnd ? '…' : ''}`}
                            </span>
                            <span className="font-mono font-medium tabular-nums text-danger">
                              {o.clippedStart || o.clippedEnd ? '≥ ' : ''}{formatDur((end - o.start) / 1000)}
                            </span>
                            {o.endKind === 'gap' && <span className="text-muted">evidence stopped</span>}
                            {o.reason && (
                              <div className="basis-full pl-1 text-text2"><ServiceFailureReason reason={o.reason} /></div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
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

function DailyUptimeChart({ days, fromTs, toTs, dim }: { days: DayUptime[]; fromTs: number; toTs: number; dim: boolean }) {
  const inRangeCount = days.filter((d) => d.end > fromTs && d.start < toTs).length
  const dimOthers = dim && inRangeCount > 0 && inRangeCount < days.length
  const data = days.map((d) => ({
    label: d.date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
    pct: d.uptimePct,
    downtime: d.downtimeSec,
    band: availabilityBand(d.uptimePct, d.downtimeSec),
    dim: dimOthers && !(d.end > fromTs && d.start < toTs),
  }))
  const hasData = data.some((d) => d.pct != null)
  return (
    <SectionCard title="Daily availability" subtitle="Last 30 days · context" bodyClassName="flex flex-col p-4">
      {!hasData ? (
        <EmptyState icon={Gauge} title="No daily data" description="Daily availability appears once probe results have been stored." />
      ) : (
        <div className="h-[240px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="rgb(var(--border) / 0.35)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
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
                formatter={(v: any, _n: any, item: any) => [v == null ? 'no data' : formatAvailability(Number(v), item?.payload?.downtime, 3), 'Availability']}
              />
              <Bar dataKey="pct" radius={[3, 3, 0, 0]} isAnimationActive={false}>
                {data.map((d, i) => <Cell key={i} fill={BAND_FILL[d.band]} fillOpacity={d.dim ? 0.45 : 1} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </SectionCard>
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

/** Stored configurations are validated; anything malformed falls back to the defaults. */
function loadHealthScoreConfig(): HealthScoreConfig {
  try {
    const raw = localStorage.getItem(HEALTH_SCORE_CONFIG_KEY)
    if (!raw) return cloneDefaultHealthConfig()
    const parsed = JSON.parse(raw)
    const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)
    const D = DEFAULT_HEALTH_SCORE_CONFIG
    const cfg: HealthScoreConfig = {
      weights: {
        availability: num(parsed?.weights?.availability, D.weights.availability),
        latency: num(parsed?.weights?.latency, D.weights.latency),
        errors: num(parsed?.weights?.errors, D.weights.errors),
        incidents: num(parsed?.weights?.incidents, D.weights.incidents),
      },
      thresholds: {
        excellent: num(parsed?.thresholds?.excellent, D.thresholds.excellent),
        good: num(parsed?.thresholds?.good, D.thresholds.good),
        degraded: num(parsed?.thresholds?.degraded, D.thresholds.degraded),
      },
      latencyTargetMs: num(parsed?.latencyTargetMs, D.latencyTargetMs),
      errorScale: num(parsed?.errorScale, D.errorScale),
      incidentScale: num(parsed?.incidentScale, D.incidentScale),
    }
    return validateHealthScoreConfig(cfg).length === 0 ? cfg : cloneDefaultHealthConfig()
  } catch {
    return cloneDefaultHealthConfig()
  }
}

function cloneDefaultHealthConfig(): HealthScoreConfig {
  const D = DEFAULT_HEALTH_SCORE_CONFIG
  return { ...D, weights: { ...D.weights }, thresholds: { ...D.thresholds } }
}

function saveHealthScoreConfig(cfg: HealthScoreConfig) {
  if (validateHealthScoreConfig(cfg).length > 0) return
  try { localStorage.setItem(HEALTH_SCORE_CONFIG_KEY, JSON.stringify(cfg)) } catch { /* ignore */ }
}

type HealthFactor = {
  key: 'availability' | 'latency' | 'errors' | 'incidents'
  label: string
  raw: string
  formula: string
  subScore: number
  weight: number
  /** False when the input was not measured; the factor is then excluded and weights rescaled. */
  available: boolean
  contribution: number
}

/**
 * Health score (B09). Inputs are independent: availability is confirmed
 * availability, errors are raw probe failures (probe_error_rate_pct, which
 * includes failures that never became incidents), latency is P95 of
 * successful probes. A missing input is excluded rather than scored as
 * perfect; the remaining weights are rescaled and `coverage` reports the
 * share of configured weight that was actually measured.
 */
function computeHealthScore(
  args: { uptime_pct: number | null; probe_error_rate_pct?: number | null; incident_count?: number | null; p95_response_ms: number | null },
  cfg: HealthScoreConfig = DEFAULT_HEALTH_SCORE_CONFIG,
) {
  const GOOD_COLOR = 'rgb(var(--success) / 0.7)'
  const empty = { score: null as number | null, tint: 'rgb(var(--muted))', label: 'No data', factors: [] as HealthFactor[], coverage: 0 }
  if (args.uptime_pct == null) return empty
  const conf = validateHealthScoreConfig(cfg).length === 0 ? cfg : DEFAULT_HEALTH_SCORE_CONFIG
  const up = args.uptime_pct
  const err = args.probe_error_rate_pct
  const inc = args.incident_count
  const p95 = args.p95_response_ms
  const target = Math.max(1, conf.latencyTargetMs)
  const w = conf.weights
  const factors: HealthFactor[] = [
    {
      key: 'availability',
      label: 'Availability',
      raw: `${up.toFixed(2)}% confirmed`,
      formula: `confirmed availability % = ${clamp(up, 0, 100).toFixed(1)}`,
      subScore: Math.round(clamp(up, 0, 100)),
      weight: w.availability,
      available: true,
      contribution: 0,
    },
    {
      key: 'latency',
      label: 'Latency',
      raw: p95 == null ? 'not measured' : `${Math.round(p95)} ms p95`,
      formula: p95 == null ? 'no successful probes with a response time — excluded' : p95 <= target
        ? `${Math.round(p95)} ms ≤ ${conf.latencyTargetMs} ms → 100`
        : `100 − ((${Math.round(p95)} − ${conf.latencyTargetMs}) / ${conf.latencyTargetMs}) × 100`,
      subScore: p95 == null ? 0 : Math.round(p95 <= target ? 100 : clamp(100 - ((p95 - target) / target) * 100, 0, 100)),
      weight: w.latency,
      available: p95 != null,
      contribution: 0,
    },
    {
      key: 'errors',
      label: 'Probe errors',
      raw: err == null ? 'not measured' : `${err.toFixed(2)}% of probes failed`,
      formula: err == null ? 'raw probe error rate not reported — excluded' : `100 − ${err.toFixed(2)} × ${conf.errorScale}`,
      subScore: err == null ? 0 : Math.round(clamp(100 - err * conf.errorScale, 0, 100)),
      weight: w.errors,
      available: err != null,
      contribution: 0,
    },
    {
      key: 'incidents',
      label: 'Incidents',
      raw: inc == null ? 'not measured' : `${inc} in window`,
      formula: inc == null ? 'incident count not reported — excluded' : `100 − ${inc} × ${conf.incidentScale}`,
      subScore: inc == null ? 0 : Math.round(clamp(100 - inc * conf.incidentScale, 0, 100)),
      weight: w.incidents,
      available: inc != null,
      contribution: 0,
    },
  ]
  const totalW = factors.reduce((s, f) => s + f.weight, 0)
  const measuredW = factors.reduce((s, f) => s + (f.available ? f.weight : 0), 0)
  if (measuredW <= 0) return { ...empty, factors, coverage: 0 }
  for (const f of factors) f.contribution = f.available ? (f.subScore * f.weight) / measuredW : 0
  const score = Math.max(0, Math.min(100, Math.round(factors.reduce((s, f) => s + f.contribution, 0))))
  const t = conf.thresholds
  const tint = score >= t.excellent ? UP_COLOR : score >= t.good ? GOOD_COLOR : score >= t.degraded ? WARN_COLOR : DOWN_COLOR
  const label = score >= t.excellent ? 'Excellent' : score >= t.good ? 'Good' : score >= t.degraded ? 'Degraded' : 'Critical'
  return { score, tint, label, factors, coverage: totalW > 0 ? measuredW / totalW : 0 }
}

/** Returns human-readable problems; an empty list means the configuration is usable. */
function validateHealthScoreConfig(cfg: HealthScoreConfig): string[] {
  const errors: string[] = []
  const finite = (v: unknown) => typeof v === 'number' && Number.isFinite(v)
  const ws = [cfg?.weights?.availability, cfg?.weights?.latency, cfg?.weights?.errors, cfg?.weights?.incidents]
  if (!ws.every((v) => finite(v) && (v as number) >= 0)) errors.push('Weights must be numbers of 0 or more.')
  else if (ws.reduce((s: number, v) => s + (v as number), 0) <= 0) errors.push('At least one weight must be greater than 0.')
  const th = cfg?.thresholds
  if (![th?.excellent, th?.good, th?.degraded].every((v) => finite(v) && (v as number) >= 0 && (v as number) <= 100)) {
    errors.push('Thresholds must be numbers between 0 and 100.')
  } else if (!(th.excellent >= th.good && th.good >= th.degraded)) {
    errors.push('Thresholds must be ordered: Excellent ≥ Good ≥ Degraded.')
  }
  if (!finite(cfg?.latencyTargetMs) || cfg.latencyTargetMs <= 0) errors.push('Latency target must be greater than 0 ms.')
  if (!finite(cfg?.errorScale) || cfg.errorScale < 0) errors.push('Error scale must be 0 or more.')
  if (!finite(cfg?.incidentScale) || cfg.incidentScale < 0) errors.push('Incident penalty must be 0 or more.')
  return errors
}

function HealthScoreDetailsDialog({
  open, onOpenChange, score, tint, label, factors, coverage, config, onConfigChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  score: number | null
  tint: string
  label: string
  factors: HealthFactor[]
  coverage: number
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

  const totalW = draft.weights.availability + draft.weights.latency + draft.weights.errors + draft.weights.incidents
  const problems = validateHealthScoreConfig(draft)
  const dirty = JSON.stringify(draft) !== JSON.stringify(config)
  const toNum = (raw: string) => (raw.trim() === '' ? NaN : Number(raw))
  const setWeight = (k: keyof HealthScoreConfig['weights'], v: number) => {
    setDraft({ ...draft, weights: { ...draft.weights, [k]: Number.isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : v } })
  }
  const setThreshold = (k: keyof HealthScoreConfig['thresholds'], v: number) => {
    setDraft({ ...draft, thresholds: { ...draft.thresholds, [k]: Number.isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : v } })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-primary" /> Health score
          </DialogTitle>
          <DialogDescription className="text-xs">
            A weighted summary of confirmed availability, P95 latency, raw probe errors and incidents in the selected window.
            Criteria are stored in this browser only.
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
          <div>
            <div className="text-xs text-muted">Current score</div>
            <div className="text-[11px]" style={{ color: tint }}>{label}</div>
            {score != null && coverage < 1 && <div className="text-[10px] text-muted">Coverage {Math.round(coverage * 100)}% of configured weight</div>}
          </div>
          <div className="text-3xl font-bold tabular-nums" style={{ color: tint }}>
            {score ?? '—'}<span className="text-sm text-muted">/100</span>
          </div>
        </div>
        <Tabs value={tab} onValueChange={(v) => setTab(v as 'breakdown' | 'configure')}>
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="breakdown">Breakdown</TabsTrigger>
            <TabsTrigger value="configure"><SettingsIcon className="mr-1.5 h-3.5 w-3.5" />Configure</TabsTrigger>
          </TabsList>
          <TabsContent value="breakdown" className="space-y-2 pt-3">
            {factors.length === 0 && <p className="text-xs text-muted">No observed availability in this window.</p>}
            {factors.map((f) => {
              const tone = !f.available ? 'text-muted' : f.subScore > 90 ? 'text-success' : f.subScore > 70 ? 'text-warning' : 'text-danger'
              return (
                <div key={f.key} className="grid grid-cols-[1fr_auto_auto_auto] items-start gap-x-3 border-b border-border/40 py-2 last:border-0">
                  <div className="min-w-0">
                    <div className="text-xs font-semibold">{f.label}</div>
                    <div className="text-[11px] text-muted">{f.raw}</div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted/80">{f.formula}</div>
                  </div>
                  <span className={cn('text-right font-mono text-xs tabular-nums', tone)}>{f.available ? f.subScore : 'n/a'}</span>
                  <span className="text-right font-mono text-xs tabular-nums text-muted">{f.weight}</span>
                  <span className="text-right font-mono text-xs tabular-nums">{f.available ? f.contribution.toFixed(1) : '—'}</span>
                </div>
              )
            })}
          </TabsContent>
          <TabsContent value="configure" className="space-y-4 pt-3">
            <div className="flex items-center justify-between text-xs">
              <span className="font-semibold">Factor weights</span>
              <span className={cn('font-mono', totalW === 100 ? 'text-muted' : 'text-warning')}>
                Total: {Number.isFinite(totalW) ? totalW : '—'}{Number.isFinite(totalW) && totalW !== 100 ? ' (normalized)' : ''}
              </span>
            </div>
            {(['availability', 'latency', 'errors', 'incidents'] as const).map((k) => (
              <div key={k} className="grid grid-cols-[140px_1fr_70px] items-center gap-3">
                <label htmlFor={`hs-w-${k}`} className="text-xs capitalize">{k === 'errors' ? 'Probe errors' : k}</label>
                <input type="range" min={0} max={100} value={Number.isFinite(draft.weights[k]) ? draft.weights[k] : 0} aria-label={`${k} weight slider`} onChange={(e) => setWeight(k, Number(e.target.value))} className="accent-primary" />
                <Input id={`hs-w-${k}`} type="number" min={0} max={100} value={Number.isFinite(draft.weights[k]) ? draft.weights[k] : ''} onChange={(e) => setWeight(k, toNum(e.target.value))} className="h-8 text-xs" />
              </div>
            ))}
            <div className="text-xs font-semibold">Status thresholds (score ≥)</div>
            {(['excellent', 'good', 'degraded'] as const).map((k) => (
              <div key={k} className="grid grid-cols-[140px_1fr_70px] items-center gap-3">
                <label htmlFor={`hs-t-${k}`} className="text-xs capitalize">{k}</label>
                <input type="range" min={0} max={100} value={Number.isFinite(draft.thresholds[k]) ? draft.thresholds[k] : 0} aria-label={`${k} threshold slider`} onChange={(e) => setThreshold(k, Number(e.target.value))} className="accent-primary" />
                <Input id={`hs-t-${k}`} type="number" min={0} max={100} value={Number.isFinite(draft.thresholds[k]) ? draft.thresholds[k] : ''} onChange={(e) => setThreshold(k, toNum(e.target.value))} className="h-8 text-xs" />
              </div>
            ))}
            <div className="grid grid-cols-[140px_1fr] items-center gap-3">
              <label htmlFor="hs-latency" className="text-xs">Latency target (ms)</label>
              <Input id="hs-latency" type="number" min={1} value={Number.isFinite(draft.latencyTargetMs) ? draft.latencyTargetMs : ''} onChange={(e) => setDraft({ ...draft, latencyTargetMs: toNum(e.target.value) })} className="h-8 text-xs" />
            </div>
            <div className="grid grid-cols-[140px_1fr] items-center gap-3">
              <label htmlFor="hs-errscale" className="text-xs">Error scale</label>
              <Input id="hs-errscale" type="number" min={0} step={0.5} value={Number.isFinite(draft.errorScale) ? draft.errorScale : ''} onChange={(e) => setDraft({ ...draft, errorScale: toNum(e.target.value) })} className="h-8 text-xs" />
            </div>
            <div className="grid grid-cols-[140px_1fr] items-center gap-3">
              <label htmlFor="hs-incscale" className="text-xs">Incident penalty</label>
              <Input id="hs-incscale" type="number" min={0} step={1} value={Number.isFinite(draft.incidentScale) ? draft.incidentScale : ''} onChange={(e) => setDraft({ ...draft, incidentScale: toNum(e.target.value) })} className="h-8 text-xs" />
            </div>
            {problems.length > 0 && (
              <ul role="alert" className="space-y-0.5 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-[11px] text-danger">
                {problems.map((p) => <li key={p}>{p}</li>)}
              </ul>
            )}
          </TabsContent>
        </Tabs>
        <DialogFooter>
          {tab === 'configure' ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setDraft(cloneDefaultHealthConfig())}>Reset</Button>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
                <Button size="sm" disabled={!dirty || problems.length > 0} onClick={() => { onConfigChange(draft); toast.success('Health score criteria updated'); onOpenChange(false) }}>Save</Button>
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

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n))
}

function deriveWindowStats(
  points: MetricPoint[],
  history: StatusHistoryEvent[],
  check: ServiceCheckExt,
  fromTs: number,
  toTs: number,
  nowMs: number,
) {
  const buckets = buildAvailabilityBuckets(points, history, check, fromTs, toTs)
  const covered = buckets.filter((b) => b.state !== 'gap')
  const upCount = covered.filter((b) => b.state === 'up').length
  const failCount = covered.filter((b) => b.state === 'down' || b.state === 'partial').length
  const uptime_pct = covered.length ? (upCount / covered.length) * 100 : null
  const error_rate_pct = covered.length ? (failCount / covered.length) * 100 : null
  const incident_count = history.filter((h) => h.new_status === 'down' || h.new_status === 'warning' || h.new_status === 'degraded').length

  const samples = points.filter((p) => p.is_up && p.response_ms != null && p.response_ms > 0).map((p) => p.response_ms as number)
  const avg_ms = samples.length ? samples.reduce((a, b) => a + b, 0) / samples.length : null
  const sorted = [...samples].sort((a, b) => a - b)
  const p95_ms = sorted.length >= 3 ? sorted[Math.max(0, Math.floor(sorted.length * 0.95) - 1)] : null

  const recovered = [...history].filter((h) => h.new_status === 'up').sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0]
  const lastDown = [...history].filter((h) => h.new_status !== 'up').sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0]
  let streak_sec: number | null = null
  if (check.status === 'up' && recovered) {
    const rec = Date.parse(recovered.timestamp)
    if (!lastDown || Date.parse(lastDown.timestamp) <= rec) streak_sec = Math.max(0, (nowMs - rec) / 1000)
  } else if (check.status === 'up' && check.last_check_at && !lastDown) {
    streak_sec = Math.max(0, (nowMs - Date.parse(check.last_check_at)) / 1000)
  }

  return { uptime_pct, error_rate_pct, incident_count, avg_ms, p95_ms, streak_sec }
}
