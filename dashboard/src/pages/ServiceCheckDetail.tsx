import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { useParams, useNavigate, useSearchParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Bell,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  Database,
  Download,
  Edit3,
  ExternalLink,
  Eye,
  FileText,
  Gauge,
  Globe,
  Info,
  Network,
  LockKeyhole,
  Pause,
  Play,
  Plug,
  RefreshCw,
  Shield,
  ShieldCheck,
  Terminal,
  Trash2,
  Wrench,
  XCircle,
  HelpCircle,
  Radar,
  Settings as SettingsIcon,
  RotateCcw,
  Route,
  TrendingUp,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime, timeAxisTickFormatter, timeTooltipLabelFormatter } from '@/lib/utils'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { useTheme } from '@/stores/theme'
import type { ServiceCheck, ServiceMetricPoint, ServiceMetricResponse } from '@/types'

/* ─── Theme palette ─────────────────────────────────────────────────────
 * Two palettes — chrome (bg, panel, border, text) flips with the active
 * theme, semantic + brand colors stay stable (so module-level
 * statusMeta/typeMeta keep working).
 * --------------------------------------------------------------------- */
const C_DARK = {
  bg: '#0B111F',
  panel: '#0F172A',
  panelLift: '#121a2e',
  border: '#1f2a44',
  borderSoft: '#172135',
  text: '#E5E7EB',
  textDim: '#94A3B8',
  textMuted: '#64748B',
  up: '#22c55e',
  down: '#ef4444',
  warn: '#f59e0b',
  unknown: '#475569',
  cyan: '#22d3ee',
  violet: '#a78bfa',
  pink: '#f472b6',
  primary: '#38bdf8',
}

type Palette = typeof C_DARK

const C_LIGHT: Palette = {
  bg: '#F8FAFC',
  panel: '#FFFFFF',
  panelLift: '#F1F5F9',
  border: '#E2E8F0',
  borderSoft: '#EEF2F7',
  text: '#0F172A',
  textDim: '#475569',
  textMuted: '#64748B',
  up: '#16A34A',
  down: '#DC2626',
  warn: '#D97706',
  unknown: '#94A3B8',
  cyan: '#0891B2',
  violet: '#7C3AED',
  pink: '#DB2777',
  primary: '#2563EB',
}

const PaletteContext = createContext<Palette>(C_DARK)
function useC(): Palette {
  return useContext(PaletteContext)
}

// Module-level metas reference C_DARK semantic keys; both palettes share
// the same shape so they're swappable, but `statusMeta` / `typeMeta`
// values are only used for icons/labels — when a *color* is needed at
// runtime, components re-resolve via `useC()`.
const C = C_DARK

const TIME_RANGES = [
  { key: '1h', label: '1h', hours: 1 },
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d', label: '7d', hours: 168 },
  { key: '1M', label: '1M', hours: 720 },
] as const

function rangeIdxFromKey(k: string | null): number {
  const i = TIME_RANGES.findIndex((r) => r.key === k)
  return i >= 0 ? i : 0 // default 1h
}

function formatRangeLabel(hours: number): string {
  if (hours < 24) return `${hours}h`
  if (hours < 24 * 30) return `${Math.round(hours / 24)}d`
  return `${Math.round(hours / (24 * 30))}M`
}

const statusMeta: Record<
  string,
  { label: string; color: string; Icon: any }
> = {
  up: { label: 'Healthy', color: C.up, Icon: CheckCircle2 },
  down: { label: 'Down', color: C.down, Icon: XCircle },
  degraded: { label: 'Degraded', color: C.warn, Icon: AlertTriangle },
  warning: { label: 'Warning', color: C.warn, Icon: AlertTriangle },
  unknown: { label: 'Unknown', color: C.unknown, Icon: HelpCircle },
}

const typeMeta: Record<string, { label: string; Icon: any; tint: string }> = {
  http: { label: 'HTTP', Icon: Globe, tint: C.cyan },
  tcp: { label: 'TCP', Icon: Plug, tint: C.up },
  tls: { label: 'TLS', Icon: ShieldCheck, tint: C.warn },
  icmp: { label: 'ICMP', Icon: Radar, tint: C.violet },
  dns: { label: 'DNS', Icon: Network, tint: C.pink },
}

/* ─── Helpers ───────────────────────────────────────────────────────────── */

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
    transport_security?: string
    steps_total?: number
    steps_passed?: number
    steps?: ProbeEvidenceStep[]
  }
}

/* ─── Page ──────────────────────────────────────────────────────────────── */

export function ServiceCheckDetailPage() {
  const { theme } = useTheme()
  const C = theme === 'dark' ? C_DARK : C_LIGHT
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()

  // Range can be a preset key (1h/24h/7d/1M) or "custom" — if "custom" the
  // window comes from explicit ?from=&to= ISO timestamps.
  const rangeKey = searchParams.get('range')
  const isCustom = rangeKey === 'custom' && !!searchParams.get('from') && !!searchParams.get('to')
  const rangeIdx = rangeIdxFromKey(rangeKey)
  const setPresetRange = (i: number) => {
    const next = new URLSearchParams(searchParams)
    next.set('range', TIME_RANGES[i].key)
    next.delete('from')
    next.delete('to')
    setSearchParams(next, { replace: true })
  }
  const setCustomRange = (fromISO: string, toISO: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('range', 'custom')
    next.set('from', fromISO)
    next.set('to', toISO)
    setSearchParams(next, { replace: true })
  }

  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const [nowTick, setNowTick] = useState(() => Date.now())
  const [hsOpen, setHsOpen] = useState(false)
  const [hsConfig, setHsConfig] = useState<HealthScoreConfig>(() => loadHealthScoreConfig())
  const [manualProbe, setManualProbe] = useState<{ result: ProbeEvidenceResult; observedAt: string } | null>(null)

  // Tick every second for the "Next poll in" countdown.
  useEffect(() => {
    const h = setInterval(() => setNowTick(Date.now()), 1000)
    return () => clearInterval(h)
  }, [])

  const { data: check, isLoading } = useQuery<ServiceCheck>({
    queryKey: ['service-check', id],
    queryFn: async () => (await api.get(`/service-checks/${id}`)).data,
    enabled: !!id,
    refetchInterval: 15_000,
  })

  const customFrom = searchParams.get('from')
  const customTo = searchParams.get('to')
  const fromTo = useMemo(() => {
    if (isCustom && customFrom && customTo) {
      return { from: customFrom, to: customTo }
    }
    const now = Date.now()
    return {
      from: new Date(now - TIME_RANGES[rangeIdx].hours * 3_600_000).toISOString(),
      to: new Date(now).toISOString(),
    }
  }, [isCustom, customFrom, customTo, rangeIdx])
  const rangeHours = useMemo(() => {
    if (isCustom) {
      const span = (Date.parse(fromTo.to) - Date.parse(fromTo.from)) / 3_600_000
      return Math.max(1, Math.round(span))
    }
    return TIME_RANGES[rangeIdx].hours
  }, [isCustom, fromTo, rangeIdx])
  const rangeLabel = isCustom
    ? `${new Date(fromTo.from).toLocaleString()} → ${new Date(fromTo.to).toLocaleString()}`
    : `Last ${formatRangeLabel(rangeHours)}`

  const { data: metrics } = useQuery<ServiceMetricResponse>({
    queryKey: ['service-check-metrics', id, rangeHours],
    queryFn: async () => {
      const g = rangeHours <= 6 ? 'raw' : 'auto'
      return (await api.get(
        `/service-checks/${id}/metrics?from=${encodeURIComponent(fromTo.from)}&to=${encodeURIComponent(fromTo.to)}&granularity=${g}`,
      )).data
    },
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const points: ServiceMetricPoint[] = metrics?.points || []

  const { data: statusHistory = [] } = useQuery<
    Array<{
      timestamp: string
      old_status: string | null
      new_status: string
      reason?: string | null
      duration_sec?: number | null
    }>
  >({
    queryKey: ['service-status-history', id, fromTo.from, fromTo.to],
    queryFn: async () =>
      (await api.get(
        `/service-checks/${id}/status-history?from=${encodeURIComponent(fromTo.from)}&to=${encodeURIComponent(fromTo.to)}&limit=500`,
      )).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })

  const { data: sla } = useQuery<{
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
  }>({
    queryKey: ['service-sla', id, rangeHours],
    queryFn: async () => (await api.get(`/service-checks/${id}/sla?hours=${rangeHours}`)).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })

  const { data: hourly } = useQuery<{
    hours: Array<{ ts: string; uptime_pct: number | null; sample_count: number }>
  }>({
    queryKey: ['service-hourly-uptime', id],
    queryFn: async () =>
      (await api.get(`/service-checks/${id}/hourly-uptime?days=30`)).data,
    enabled: !!id && !!check,
    refetchInterval: 60_000,
  })

  const { data: alertsResp } = useQuery<{ data: any[] }>({
    queryKey: ['service-alerts', id],
    queryFn: async () =>
      (await api.get(`/alerts?service_check_id=${id}&limit=100`)).data,
    enabled: !!id && !!check,
    refetchInterval: 30_000,
  })
  const alerts = alertsResp?.data || []
  const activeAlerts = alerts.filter((a) => a.status === 'active')
  const alertCounts = useMemo(() => {
    const c = { critical: 0, warning: 0, info: 0 }
    for (const a of alerts) {
      if (a.status !== 'active') continue
      c[a.severity as keyof typeof c] = (c[a.severity as keyof typeof c] || 0) + 1
    }
    return c
  }, [alerts])

  const { data: relatedResp } = useQuery<{ data: ServiceCheck[] }>({
    queryKey: ['service-related', check?.group_id, check?.parent_check_id, id],
    queryFn: async () => {
      // Fetch all checks once — we filter client-side.
      return (await api.get('/service-checks?limit=200')).data
    },
    enabled: !!check,
    refetchInterval: 60_000,
  })
  const allChecks = relatedResp?.data || []
  const upstream = useMemo(() => {
    if (!check?.parent_check_id) return []
    const p = allChecks.find((c) => c.id === check.parent_check_id)
    return p ? [p] : []
  }, [allChecks, check])
  const downstream = useMemo(
    () => allChecks.filter((c) => c.parent_check_id === id),
    [allChecks, id],
  )

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
    },
    onError: (e: any) => toast.error('Probe failed to run', apiErrorMessage(e)),
  })

  const togglePause = useMutation({
    mutationFn: async () =>
      (await api.put(`/service-checks/${id}`, { enabled: !check?.enabled })).data,
    onSuccess: () => {
      toast.success(check?.enabled ? 'Checks paused' : 'Checks resumed')
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Failed', apiErrorMessage(e)),
  })

  const forceRevalidate = useMutation({
    mutationFn: async () => {
      // Run two consecutive probes to confirm the current status is not transient.
      await api.post(`/service-checks/${id}/test`, {})
      return (await api.post(`/service-checks/${id}/test`, {})).data
    },
    onSuccess: (d: any) => {
      setManualProbe({ result: d as ProbeEvidenceResult, observedAt: new Date().toISOString() })
      toast.success(
        'Re-validation complete',
        d?.status === 'up'
          ? `Confirmed up · ${Math.round(d.response_time_ms || 0)} ms`
          : `Confirmed ${d?.status || 'down'}: ${d?.error || ''}`,
      )
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-check-metrics', id] })
      qc.invalidateQueries({ queryKey: ['service-sla', id] })
    },
    onError: (e: any) => toast.error('Re-validation failed', apiErrorMessage(e)),
  })

  const [maintOpen, setMaintOpen] = useState(false)

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
          reason: `Manual ${durationHours}h window from Quick Actions`,
        })
      ).data
    },
    onSuccess: () => {
      toast.success('Maintenance window started')
      setMaintOpen(false)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
    },
    onError: (e: any) => toast.error('Failed to start maintenance', apiErrorMessage(e)),
  })

  const startMaintenanceCustom = useMutation({
    mutationFn: async (args: { startsAtISO: string; endsAtISO: string }) => {
      return (
        await api.post('/service-check-maintenance', {
          scope_type: 'check',
          scope_check_id: id,
          starts_at: args.startsAtISO,
          ends_at: args.endsAtISO,
          reason: 'Custom maintenance window from Quick Actions',
        })
      ).data
    },
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
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
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
      // Delete every active window so the check fully exits maintenance, even if
      // overlapping windows exist (multi-session quick-action use).
      await Promise.all(active.map((m) => api.delete(`/service-check-maintenance/${m.id}`)))
      return active.length
    },
    onSuccess: (n: number) => {
      toast.success(n > 1 ? `Ended ${n} overlapping maintenance windows` : 'Maintenance window ended')
      setMaintOpen(false)
      qc.invalidateQueries({ queryKey: ['service-check', id] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
    },
    onError: (e: any) => toast.error('Failed to end maintenance', apiErrorMessage(e)),
  })

  const ackAllAlerts = useMutation({
    mutationFn: async () => {
      const active = alerts.filter((a: any) => a.status === 'active')
      if (active.length === 0) throw new Error('No active alerts to acknowledge')
      await Promise.all(active.map((a: any) => api.post(`/alerts/${a.id}/acknowledge`)))
      return active.length
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
    const payload = {
      ...check,
      _exported_at: new Date().toISOString(),
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-32">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span className="text-sm text-muted">Loading service…</span>
        </div>
      </div>
    )
  }

  if (!check) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-32">
        <XCircle className="h-10 w-10 text-danger" />
        <h3 className="text-lg font-semibold">Service not found</h3>
        <Link to="/services" className="inline-flex items-center gap-2 text-sm text-primary hover:underline">
          <ArrowLeft className="h-4 w-4" /> Back
        </Link>
      </div>
    )
  }

  const sm = statusMeta[check.status] || statusMeta.unknown
  const tMeta = typeMeta[check.check_type] || typeMeta.http
  const TypeIcon = tMeta.Icon
  const target = check.target_url || `${check.target_host}${check.target_port ? `:${check.target_port}` : ''}`
  const healthScore = computeHealthScore({
    uptime_pct: sla?.uptime_pct ?? null,
    error_rate_pct: sla?.error_rate_pct ?? null,
    incident_count: sla?.incident_count ?? 0,
    p95_response_ms: sla?.p95_response_ms ?? null,
  }, hsConfig)
  const lastCheckMs = check.last_check_at ? Date.parse(check.last_check_at) : null
  const intervalMs = (check.check_interval || 60) * 1000
  const nextPollMs = lastCheckMs ? lastCheckMs + intervalMs : null
  const secsToNext = nextPollMs ? Math.max(0, Math.floor((nextPollMs - nowTick) / 1000)) : null
  const env = pickEnv(check.tags || [])

  return (
    <PaletteContext.Provider value={C}>
    <div
      className="space-y-5 pb-8"
      style={{ color: C.text }}
    >
      {/* Page header */}
      <div
        className="flex flex-col gap-4 rounded-2xl p-4 shadow-sm xl:flex-row xl:items-center xl:justify-between"
        style={{ background: C.panel, border: `1px solid ${C.border}` }}
      >
        <div className="flex items-start gap-2">
          <button
            onClick={() => navigate('/services')}
            className="mt-1 rounded-md p-1.5 text-muted hover:bg-white/5"
            aria-label="Back"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div>
            <div className="mb-1 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-muted">
              Monitoring <ChevronRight className="h-3 w-3" /> Service overview
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">Service operations</h1>
            <p className="mt-0.5 text-sm text-muted">
              Live health, response evidence, reliability, and configuration in one workspace.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <TimeRangePicker
            rangeIdx={rangeIdx}
            isCustom={isCustom}
            customFrom={fromTo.from}
            customTo={fromTo.to}
            onPreset={setPresetRange}
            onCustom={setCustomRange}
          />
          <span className="mx-1 hidden h-5 w-px bg-white/10 sm:inline-block" />
          <HeaderBtn
            icon={check.enabled ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            label={check.enabled ? 'Pause Checks' : 'Resume Checks'}
            onClick={() => togglePause.mutate()}
            tone="warn"
          />
          <HeaderBtn
            icon={<Play className="h-3.5 w-3.5 fill-current" />}
            label="Run Probe"
            onClick={() => runNow.mutate()}
            loading={runNow.isPending}
            tone="success"
          />
          <HeaderBtn
            icon={<Check className="h-3.5 w-3.5" />}
            label={activeAlerts.length > 0 ? `Acknowledge (${activeAlerts.length})` : 'Acknowledge'}
            onClick={() => ackAllAlerts.mutate()}
            loading={ackAllAlerts.isPending}
            disabled={activeAlerts.length === 0}
            tone="primary"
          />
          <HeaderBtn
            icon={<FileText className="h-3.5 w-3.5" />}
            label="Open Logs"
            onClick={() => navigate(`/services/${id}/incidents?filter=all`)}
            tone="neutral"
          />
        </div>
      </div>

      {/* ── Hero metadata card ───────────────────────────────────────── */}
      <HeroCard
        name={check.name}
        status={check.status}
        type={check.check_type}
        target={target}
        environment={env}
        group={check.group_name}
        tags={check.tags || []}
        deviceHostname={check.device_hostname || null}
        deviceId={check.device_id}
        checkInterval={check.check_interval}
        lastCheckAt={check.last_check_at}
        secsToNext={secsToNext}
        inMaintenance={!!check.in_maintenance}
        enabled={check.enabled}
        probeInfo={
          check.check_type === 'http'
            ? (check.workflow_steps?.length || 0) > 0
              ? `HTTP journey · ${check.workflow_steps?.length} steps · ${(check.workflow_operator || 'all').toUpperCase()}`
              : `HTTP(S) · Status + Body`
            : check.check_type.toUpperCase()
        }
      />

      {(check.workflow_steps?.length || 0) > 0 && <WorkflowOverview check={check} />}

      {/* ── Maintenance banner ───────────────────────────────────────── */}
      {check.in_maintenance && (
        <MaintenanceBanner
          onEnd={() => endMaintenance.mutate()}
          ending={endMaintenance.isPending}
        />
      )}

      {/* ── Paused banner ────────────────────────────────────────────── */}
      {!check.enabled && !check.in_maintenance && (
        <PausedBanner
          onResume={() => togglePause.mutate()}
          resuming={togglePause.isPending}
        />
      )}

      {/* ── Failure reason banner ────────────────────────────────────── */}
      {check.last_error && (check.status === 'down' || check.status === 'warning') && !check.in_maintenance && (
        <FailureReasonBanner
          status={check.status}
          error={check.last_error}
          lastCheckAt={check.last_check_at}
        />
      )}

      <ProbeEvidenceCard
        check={check}
        latestMetric={points.length > 0 ? points[points.length - 1] : null}
        recentMetrics={points.slice(-8).reverse()}
        manualProbe={manualProbe}
      />

      {/* ── Live Probe Strip ─────────────────────────────────────────── */}
      <Card className="p-4">
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2">
            <Activity className="h-3.5 w-3.5" style={{ color: C.cyan }} />
            <span className="text-sm font-semibold">Live availability stream</span>
            <span className="text-xs text-muted">Last 60 checks</span>
            {check.in_maintenance && (
              <span
                className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider"
                style={{ background: `${C.violet}20`, color: C.violet }}
              >
                <Wrench className="h-2.5 w-2.5" />
                Alerts suppressed
              </span>
            )}
            {!check.enabled && (
              <span
                className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider"
                style={{ background: `${C.warn}20`, color: C.warn }}
              >
                <Pause className="h-2.5 w-2.5" />
                No new probes
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <Legend color={C.up} label="Up" />
            <Legend color={C.warn} label="Warn" />
            <Legend color={C.down} label="Down" />
            <span className="text-muted">
              Next poll in{' '}
              <span className="font-mono text-text">
                {!check.enabled
                  ? '—'
                  : secsToNext == null
                    ? '—'
                    : `${String(Math.floor(secsToNext / 60)).padStart(2, '0')}:${String(secsToNext % 60).padStart(2, '0')}`}
              </span>
            </span>
          </div>
        </div>
        <div style={{ opacity: check.in_maintenance || !check.enabled ? 0.55 : 1 }}>
          <ProbeStrip points={points.slice(-60)} />
        </div>
      </Card>

      {/* ── KPI row + health ring ───────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="lg:col-span-8">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <Kpi
              label="Availability"
              value={pct(sla?.uptime_pct ?? null, 2)}
              windowLabel={rangeLabel}
              tint={
                sla?.uptime_pct == null
                  ? C.textDim
                  : sla.uptime_pct >= 99.9
                    ? C.up
                    : sla.uptime_pct >= 99
                      ? C.warn
                      : C.down
              }
              delta={sla?.uptime_pct != null ? (sla.uptime_pct < 100 ? `-${(100 - sla.uptime_pct).toFixed(2)}%` : '0.00%') : '—'}
              deltaDirection={sla?.uptime_pct != null && sla.uptime_pct >= 99.99 ? 'flat' : 'down'}
              series={buildUptimeSeries(hourly?.hours || [])}
            />
            <Kpi
              label="Avg Response"
              value={formatMs(sla?.avg_response_ms ?? null)}
              windowLabel={rangeLabel}
              tint={C.cyan}
              delta="—"
              deltaDirection="flat"
              series={buildResponseSeries(points)}
            />
            <Kpi
              label="P95 Latency"
              value={formatMs(sla?.p95_response_ms ?? null)}
              windowLabel={rangeLabel}
              tint={C.violet}
              delta="—"
              deltaDirection="flat"
              series={buildResponseSeries(points, 0.95)}
            />
            <Kpi
              label="Error Rate"
              value={pct(sla?.error_rate_pct ?? null, 3)}
              windowLabel={rangeLabel}
              tint={sla?.error_rate_pct && sla.error_rate_pct > 1 ? C.down : C.up}
              delta={sla?.error_rate_pct == null ? '—' : sla.error_rate_pct > 0 ? `+${sla.error_rate_pct.toFixed(2)}%` : '0.00%'}
              deltaDirection={sla?.error_rate_pct == null ? 'flat' : sla.error_rate_pct > 0 ? 'up' : 'flat'}
              series={buildErrorSeries(points)}
              invertTrend
            />
            <Kpi
              label="Active Incidents"
              value={String(activeAlerts.length)}
              windowLabel="active now"
              tint={activeAlerts.length > 0 ? C.down : C.up}
              delta={`${activeAlerts.length > 0 ? '+' : ''}${activeAlerts.length}`}
              deltaDirection={activeAlerts.length > 0 ? 'up' : 'flat'}
              series={[]}
            />
            <Kpi
              label="Uptime Streak"
              value={formatDur(sla?.uptime_streak_sec ?? null)}
              windowLabel="current"
              tint={C.up}
              delta="no change"
              deltaDirection="flat"
              series={[]}
              big
            />
          </div>
        </div>

        {/* Health score ring */}
        <div className="lg:col-span-4">
          <HealthScoreRing
            score={healthScore.score}
            tint={healthScore.tint}
            label={healthScore.label}
            factors={healthScore.factors}
            onViewDetails={() => setHsOpen(true)}
          />
        </div>
      </div>

      {/* ── Performance + Region columns ─────────────────────────────── */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="space-y-3 lg:col-span-8">
          <PerformanceChart
            points={points}
            statusHistory={statusHistory}
            rangeLabel={rangeLabel}
            rangeHours={rangeHours}
          />

          {/* Incidents strip */}
          <IncidentsStrip history={statusHistory} fromTo={fromTo} rangeLabel={rangeLabel} checkId={id || ''} />

          {/* Uptime Calendar + Related */}
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <UptimeCalendar hours={hourly?.hours || []} />
            </div>
            <div>
              <RelatedServices upstream={upstream} downstream={downstream} />
            </div>
          </div>

          {/* Recent activity table */}
          <RecentActivityTable history={statusHistory} fromTo={fromTo} rangeLabel={rangeLabel} />
        </div>

        {/* ── Right sidebar ───────────────────────────────────────── */}
        <div className="space-y-3 lg:col-span-4">
          <CurrentChecksSummary points={points} />
          <div className="grid grid-cols-2 gap-3">
            <QuickActions
              onRunProbe={() => runNow.mutate()}
              onPauseAll={() => togglePause.mutate()}
              onForceRevalidate={() => forceRevalidate.mutate()}
              onMaintenance={() => {
                // If currently in maintenance, end it directly (no dialog).
                // Otherwise open the duration-picker dialog.
                if (check.in_maintenance) endMaintenance.mutate()
                else setMaintOpen(true)
              }}
              onAckAll={() => ackAllAlerts.mutate()}
              onExport={exportConfig}
              enabled={check.enabled}
              inMaintenance={!!check.in_maintenance}
              activeAlertCount={activeAlerts.length}
              busy={{
                probe: runNow.isPending,
                pause: togglePause.isPending,
                revalidate: forceRevalidate.isPending,
                ack: ackAllAlerts.isPending,
              }}
            />
            <AlertSummary counts={alertCounts} checkId={id || ''} />
          </div>
          <InlineConfig check={check} onEdit={() => setEditOpen(true)} />
        </div>
      </div>

      {/* Dialogs */}
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
        onStartCustom={(startsAtISO, endsAtISO) =>
          startMaintenanceCustom.mutate({ startsAtISO, endsAtISO })
        }
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
    </div>
    </PaletteContext.Provider>
  )
}

/* ─── Sub-components ────────────────────────────────────────────────────── */

function TimeRangePicker({
  rangeIdx, isCustom, customFrom, customTo, onPreset, onCustom,
}: {
  rangeIdx: number
  isCustom: boolean
  customFrom: string
  customTo: string
  onPreset: (i: number) => void
  onCustom: (fromISO: string, toISO: string) => void
}) {
  const C = useC()
  const [open, setOpen] = useState(false)
  // Initialize datetime-local fields from the active window when the popover opens.
  const toLocal = (iso: string) => {
    const d = new Date(iso)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const [fromInput, setFromInput] = useState(() => toLocal(customFrom))
  const [toInput, setToInput] = useState(() => toLocal(customTo))
  useEffect(() => {
    if (open) {
      setFromInput(toLocal(customFrom))
      setToInput(toLocal(customTo))
    }
  }, [open, customFrom, customTo])

  return (
    <div className="relative">
      <div
        className="inline-flex items-center gap-0.5 rounded-md border p-0.5"
        style={{ background: C.panel, borderColor: C.border }}
        role="tablist"
        aria-label="Time range"
      >
        <Clock className="ml-1 mr-0.5 h-3 w-3" style={{ color: C.textDim }} />
        {TIME_RANGES.map((r, i) => {
          const active = !isCustom && rangeIdx === i
          return (
            <button
              key={r.key}
              role="tab"
              aria-selected={active}
              onClick={() => { setOpen(false); onPreset(i) }}
              className="rounded px-2 py-1 text-[11px] font-semibold transition-colors"
              style={{
                background: active ? C.primary : 'transparent',
                color: active ? '#000' : C.textDim,
              }}
            >
              {r.label}
            </button>
          )
        })}
        <button
          role="tab"
          aria-selected={isCustom}
          onClick={() => setOpen((v) => !v)}
          className="rounded px-2 py-1 text-[11px] font-semibold transition-colors"
          style={{
            background: isCustom ? C.primary : 'transparent',
            color: isCustom ? '#000' : C.textDim,
          }}
        >
          Custom
        </button>
      </div>

      {open && (
        <div
          className="absolute right-0 top-full z-20 mt-1 w-72 rounded-md border p-3 shadow-lg"
          style={{ background: C.panel, borderColor: C.border }}
        >
          <div className="space-y-2">
            <label className="block">
              <span className="text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>From</span>
              <input
                type="datetime-local"
                value={fromInput}
                onChange={(e) => setFromInput(e.target.value)}
                className="mt-1 w-full rounded border bg-transparent px-2 py-1 text-xs"
                style={{ borderColor: C.border, color: C.text }}
              />
            </label>
            <label className="block">
              <span className="text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>To</span>
              <input
                type="datetime-local"
                value={toInput}
                onChange={(e) => setToInput(e.target.value)}
                className="mt-1 w-full rounded border bg-transparent px-2 py-1 text-xs"
                style={{ borderColor: C.border, color: C.text }}
              />
            </label>
            <div className="flex justify-end gap-2 pt-1">
              <button
                onClick={() => setOpen(false)}
                className="rounded px-2 py-1 text-[11px]"
                style={{ color: C.textDim }}
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  const fromISO = new Date(fromInput).toISOString()
                  const toISO = new Date(toInput).toISOString()
                  if (Date.parse(fromISO) >= Date.parse(toISO)) return
                  onCustom(fromISO, toISO)
                  setOpen(false)
                }}
                className="rounded px-2 py-1 text-[11px] font-semibold"
                style={{ background: C.primary, color: '#000' }}
              >
                Apply
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function HeaderBtn({
  icon, label, onClick, tone = 'neutral', loading, disabled,
}: {
  icon: React.ReactNode
  label: string
  onClick?: () => void
  tone?: 'primary' | 'success' | 'warn' | 'danger' | 'neutral'
  loading?: boolean
  disabled?: boolean
}) {
  const C = useC()
  const color =
    tone === 'primary' ? C.primary
    : tone === 'success' ? C.up
    : tone === 'warn' ? C.warn
    : tone === 'danger' ? C.down
    : C.textDim
  return (
    <button
      onClick={onClick}
      disabled={loading || disabled}
      className="inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50"
      style={{
        background: C.panel,
        borderColor: C.border,
        color,
      }}
    >
      {icon}
      {label}
    </button>
  )
}

function FailureReasonBanner({
  status,
  error,
  lastCheckAt,
}: {
  status: string
  error: string
  lastCheckAt: string | null
}) {
  const C = useC()
  const isDown = status === 'down'
  const tint = isDown ? C.down : C.warn
  const Icon = isDown ? XCircle : AlertTriangle
  return (
    <div
      className="rounded-xl p-3"
      style={{
        background: `${tint}10`,
        border: `1px solid ${tint}40`,
      }}
    >
      <div className="flex items-start gap-3">
        <div
          className="flex h-8 w-8 flex-none items-center justify-center rounded-lg"
          style={{ background: `${tint}20`, color: tint }}
        >
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: tint }}>
              {isDown ? 'Service is down' : 'Service is degraded'}
            </span>
            {lastCheckAt && (
              <span className="text-[11px] text-muted">
                · last probe {relativeTime(lastCheckAt)}
              </span>
            )}
          </div>
          <p
            className="mt-1 break-words font-mono text-[12.5px] leading-relaxed"
            style={{ color: C.text }}
          >
            {error}
          </p>
        </div>
      </div>
    </div>
  )
}

function MaintenanceBanner({
  onEnd,
  ending,
}: {
  onEnd: () => void
  ending: boolean
}) {
  const C = useC()
  const tint = C.violet
  return (
    <div
      className="rounded-xl p-3"
      style={{ background: `${tint}10`, border: `1px solid ${tint}40` }}
    >
      <div className="flex items-start gap-3">
        <div
          className="flex h-8 w-8 flex-none items-center justify-center rounded-lg"
          style={{ background: `${tint}20`, color: tint }}
        >
          <Wrench className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: tint }}>
              Service in maintenance
            </span>
            <span className="text-[11px] text-muted">· Alerts are suppressed for this service.</span>
          </div>
          <p className="mt-1 text-[12.5px] leading-relaxed" style={{ color: C.text }}>
            Probes continue to run, but no notifications fire and incidents from this window are not counted toward SLA.
          </p>
        </div>
        <button
          onClick={onEnd}
          disabled={ending}
          className="flex-none rounded-md px-3 py-1.5 text-[11px] font-medium transition-opacity disabled:opacity-50"
          style={{ background: tint, color: '#fff' }}
        >
          {ending ? 'Ending…' : 'End maintenance'}
        </button>
      </div>
    </div>
  )
}

function PausedBanner({
  onResume,
  resuming,
}: {
  onResume: () => void
  resuming: boolean
}) {
  const C = useC()
  const tint = C.warn
  return (
    <div
      className="rounded-xl p-3"
      style={{ background: `${tint}10`, border: `1px solid ${tint}40` }}
    >
      <div className="flex items-start gap-3">
        <div
          className="flex h-8 w-8 flex-none items-center justify-center rounded-lg"
          style={{ background: `${tint}20`, color: tint }}
        >
          <Pause className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: tint }}>
              Checks paused
            </span>
            <span className="text-[11px] text-muted">· No new probes are being scheduled.</span>
          </div>
          <p className="mt-1 text-[12.5px] leading-relaxed" style={{ color: C.text }}>
            The poller will not run this check until it's resumed. Live probe data will not update.
          </p>
        </div>
        <button
          onClick={onResume}
          disabled={resuming}
          className="flex-none rounded-md px-3 py-1.5 text-[11px] font-medium transition-opacity disabled:opacity-50"
          style={{ background: tint, color: '#0B111F' }}
        >
          {resuming ? 'Resuming…' : 'Resume checks'}
        </button>
      </div>
    </div>
  )
}

function Card({ children, className = '', ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  const C = useC()
  return (
    <div
      className={`rounded-xl p-3 ${className}`}
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
      {...rest}
    >
      {children}
    </div>
  )
}

function ProbeEvidenceCard({
  check,
  latestMetric,
  recentMetrics,
  manualProbe,
}: {
  check: ServiceCheck
  latestMetric: ServiceMetricPoint | null
  recentMetrics: ServiceMetricPoint[]
  manualProbe: { result: ProbeEvidenceResult; observedAt: string } | null
}) {
  const C = useC()
  const steps = manualProbe?.result.details?.steps || []
  const finalStep = steps.length > 0 ? steps[steps.length - 1] : null
  const manualStatusCode = finalStep?.status_code ?? manualProbe?.result.details?.status_code ?? null
  const expected = check.http_expected_statuses || String(check.http_expected_status || 200)
  const authLabel = check.credential_name
    ? `${check.credential_name} · ${check.credential_auth_type === 'ntlm' ? 'Windows Integrated' : check.credential_auth_type}`
    : check.check_type === 'http' && latestMetric?.status_code === 401
      ? 'Anonymous authentication boundary'
      : 'No saved credential'
  const byteLabel = finalStep?.response_size_bytes == null
    ? null
    : finalStep.response_size_bytes < 1024
      ? `${finalStep.response_size_bytes} B`
      : `${(finalStep.response_size_bytes / 1024).toFixed(1)} KB`

  return (
    <Card className="overflow-hidden p-0 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border p-4">
        <div className="flex items-start gap-2">
          <div className="rounded-lg bg-info/10 p-2 text-info"><Terminal className="h-4 w-4" /></div>
          <div>
            <div className="text-sm font-semibold">Latest probe evidence</div>
            <div className="mt-0.5 text-xs text-muted">
              Exact response metadata is retained; response bodies, cookies, and credential secrets are never displayed or stored.
            </div>
          </div>
        </div>
        <span className="rounded-full border border-border bg-surface2/50 px-2.5 py-1 text-xs text-muted">
          Expected HTTP {expected}
        </span>
      </div>

      <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
        <EvidenceValue
          label="Scheduled result"
          value={latestMetric?.is_up == null ? 'No data' : latestMetric.is_up ? 'UP' : 'DOWN'}
          tint={latestMetric?.is_up == null ? C.textDim : latestMetric.is_up ? C.up : C.down}
          sub={latestMetric?.timestamp ? new Date(latestMetric.timestamp).toLocaleString() : 'Waiting for poller'}
        />
        <EvidenceValue
          label="HTTP response"
          value={latestMetric?.status_code == null ? '—' : `${latestMetric.status_code}`}
          tint={latestMetric?.is_up === false ? C.down : C.text}
          sub={latestMetric?.error_message || `Accepted: ${expected}`}
        />
        <EvidenceValue
          label="Response time"
          value={formatMs(latestMetric?.response_ms)}
          tint={C.cyan}
          sub="End-to-end from appliance"
        />
        <EvidenceValue
          label="Authentication"
          value={check.credential_auth_type === 'ntlm' ? 'NTLM' : check.credential_id ? 'Credentialed' : 'Boundary only'}
          tint={check.credential_id ? C.violet : C.warn}
          sub={authLabel}
        />
      </div>

      {manualProbe && (
        <div className="mx-4 mb-4 rounded-lg border border-border bg-surface2/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-semibold">
              <Play className="h-3.5 w-3.5" style={{ color: manualProbe.result.status === 'up' ? C.up : C.down }} />
              Last manual probe
              <span style={{ color: manualProbe.result.status === 'up' ? C.up : C.down }}>
                {manualProbe.result.status.toUpperCase()}
              </span>
            </div>
            <span className="text-[10px] text-muted">{new Date(manualProbe.observedAt).toLocaleString()}</span>
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
            {manualStatusCode != null && <span><span className="text-text">HTTP {manualStatusCode}</span>{finalStep?.response_reason ? ` ${finalStep.response_reason}` : ''}</span>}
            <span>{formatMs(manualProbe.result.response_time_ms)}</span>
            {finalStep?.content_type && <span>{finalStep.content_type}</span>}
            {byteLabel && <span>{byteLabel}</span>}
            {finalStep?.redirect_count != null && <span>{finalStep.redirect_count} redirect{finalStep.redirect_count === 1 ? '' : 's'}</span>}
            {finalStep?.authentication_challenges?.length ? <span>Challenge: {finalStep.authentication_challenges.join(' / ').toUpperCase()}</span> : null}
            {finalStep?.content_matched != null && <span>Content match: {finalStep.content_matched ? 'passed' : 'failed'}</span>}
          </div>
          {finalStep?.response_url && <div className="mt-1 truncate font-mono text-[10px] text-muted">Response URL: {finalStep.response_url}</div>}
          {(manualProbe.result.error || finalStep?.error) && (
            <div className="mt-2 text-[11px]" style={{ color: C.down }}>{manualProbe.result.error || finalStep?.error}</div>
          )}
        </div>
      )}

      {recentMetrics.length > 0 && (
        <div className="overflow-x-auto border-t border-border">
          <div className="border-b border-border bg-surface2/30 px-4 py-2.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Recent scheduled responses</div>
          <table className="w-full min-w-[620px] text-left text-xs">
            <thead className="bg-surface2/20 text-[10px] uppercase tracking-wider text-muted">
              <tr className="border-b border-border">
                <th className="px-4 py-2.5 font-medium">Received</th>
                <th className="px-4 py-2.5 font-medium">Result</th>
                <th className="px-4 py-2.5 font-medium">HTTP</th>
                <th className="px-4 py-2.5 font-medium">Response</th>
                <th className="px-4 py-2.5 font-medium">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {recentMetrics.map((point, index) => (
                <tr key={`${point.timestamp}-${index}`} className="border-b border-border/60 transition-colors last:border-0 hover:bg-surface2/25">
                  <td className="whitespace-nowrap px-4 py-2.5 text-muted">{new Date(point.timestamp).toLocaleString()}</td>
                  <td className="px-4 py-2.5 font-semibold" style={{ color: point.is_up ? C.up : C.down }}>{point.is_up ? 'UP' : 'DOWN'}</td>
                  <td className="px-4 py-2.5 font-mono">{point.status_code ?? '—'}</td>
                  <td className="whitespace-nowrap px-4 py-2.5">{formatMs(point.response_ms)}</td>
                  <td className="max-w-[360px] truncate px-4 py-2.5 text-muted" title={point.error_message || undefined}>{point.error_message || 'Accepted at collection time'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

function EvidenceValue({ label, value, tint, sub }: { label: string; value: string; tint: string; sub: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-border bg-surface2/25 px-3.5 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 truncate text-lg font-semibold" style={{ color: tint }}>{value}</div>
      <div className="mt-1 truncate text-xs text-muted" title={sub}>{sub}</div>
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      <span className="text-muted">{label}</span>
    </span>
  )
}

function pickEnv(tags: string[]): string {
  const t = tags.map((s) => s.toLowerCase())
  if (t.includes('prod') || t.includes('production')) return 'Production'
  if (t.includes('staging') || t.includes('stg')) return 'Staging'
  if (t.includes('dev') || t.includes('development')) return 'Development'
  return '—'
}

function HeroCard(props: {
  name: string
  status: string
  type: string
  target: string
  environment: string
  group: string | null | undefined
  tags: string[]
  deviceHostname: string | null
  deviceId: string | null | undefined
  checkInterval: number
  lastCheckAt: string | null
  secsToNext: number | null
  probeInfo: string
  inMaintenance: boolean
  enabled: boolean
}) {
  const C = useC()
  const sm = statusMeta[props.status] || statusMeta.unknown
  const t = typeMeta[props.type] || typeMeta.http
  const TypeIcon = t.Icon
  return (
    <Card className="overflow-hidden p-0 shadow-sm">
      <div className="h-1" style={{ background: sm.color }} />
      <div className="grid gap-5 p-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0">
          <div className="flex items-start gap-4">
            <div
              className="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl shadow-sm"
              style={{ background: `${t.tint}18`, color: t.tint, border: `1px solid ${t.tint}30` }}
            >
              <TypeIcon className="h-7 w-7" />
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-2xl font-semibold tracking-tight">{props.name}</h2>
                <span
                  className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold"
                  style={{ background: `${sm.color}15`, color: sm.color, border: `1px solid ${sm.color}30` }}
                >
                  <span className="h-2 w-2 rounded-full" style={{ background: sm.color }} />
                  {sm.label}
                </span>
                {props.inMaintenance && <StateBadge icon={Wrench} label="Maintenance" color={C.violet} />}
                {!props.enabled && <StateBadge icon={Pause} label="Paused" color={C.warn} />}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted">
                <span>{props.deviceHostname || 'Synthetic service'}</span>
                <span>·</span>
                <span>{t.label} monitor</span>
                <span>·</span>
                <span>{props.probeInfo}</span>
              </div>
              <div className="mt-3 inline-flex max-w-full items-center gap-2 rounded-lg border border-border bg-surface2/50 px-3 py-2 font-mono text-xs">
                <Globe className="h-3.5 w-3.5 shrink-0 text-primary" />
                <span className="truncate" title={props.target}>{props.target}</span>
              </div>
            </div>
          </div>

          <div className="mt-5 grid gap-3 border-t pt-4 sm:grid-cols-2 xl:grid-cols-4" style={{ borderColor: C.borderSoft }}>
            <Meta label="Environment" value={props.environment} />
            <Meta label="Group / owner" value={props.group || 'Unassigned'} />
            <Meta label="Primary region" value="Appliance local" />
            <Meta label="Tags" value={props.tags.length ? props.tags.join(', ') : 'No tags'} />
          </div>
        </div>

        <div className="rounded-xl border border-border bg-surface2/45 p-4">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Polling status</div>
            <span className="inline-flex items-center gap-1.5 text-xs font-medium" style={{ color: props.enabled ? C.up : C.warn }}>
              <span className="h-2 w-2 rounded-full" style={{ background: props.enabled ? C.up : C.warn }} />
              {props.enabled ? 'Active' : 'Paused'}
            </span>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <OperationalValue label="Last checked" value={props.lastCheckAt ? relativeTime(props.lastCheckAt) : 'Waiting'} sub={props.lastCheckAt ? new Date(props.lastCheckAt).toLocaleTimeString() : 'No result'} />
            <OperationalValue label="Next probe" value={props.secsToNext == null ? '—' : `${String(Math.floor(props.secsToNext / 60)).padStart(2, '0')}:${String(props.secsToNext % 60).padStart(2, '0')}`} sub={`Every ${props.checkInterval}s`} mono />
          </div>
          <div className="mt-3 rounded-lg border border-border bg-surface px-3 py-2.5">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Monitoring policy</div>
            <div className="mt-1 text-sm font-medium">{props.inMaintenance ? 'Alerts suppressed during maintenance' : props.enabled ? 'Probes and alerting enabled' : 'Probe scheduling paused'}</div>
          </div>
        </div>
      </div>
    </Card>
  )
}

function StateBadge({ icon: Icon, label, color }: { icon: typeof Pause; label: string; color: string }) {
  return <span className="inline-flex items-center gap-1 rounded-full px-2 py-1 text-[10px] font-bold uppercase tracking-wider" style={{ background: `${color}15`, color, border: `1px solid ${color}30` }}><Icon className="h-3 w-3" />{label}</span>
}

function OperationalValue({ label, value, sub, mono }: { label: string; value: string; sub: string; mono?: boolean }) {
  return <div><div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div><div className={`mt-1 text-lg font-semibold ${mono ? 'font-mono' : ''}`}>{value}</div><div className="mt-0.5 text-[10px] text-muted">{sub}</div></div>
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  const C = useC()
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
        {label}
      </div>
      <div
        className={`mt-1 truncate text-sm font-medium ${mono ? 'font-mono' : ''}`}
        style={{ color: C.text }}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}

function ProbeStrip({ points }: { points: ServiceMetricPoint[] }) {
  const C = useC()
  if (points.length === 0) {
    return <div className="h-6 text-[11px] text-muted">No samples yet.</div>
  }
  return (
    <div className="flex h-7 gap-[2px] overflow-hidden rounded-md" style={{ background: C.borderSoft }}>
      {points.map((p, i) => {
        const color = p.is_up ? C.up : C.down
        return (
          <div
            key={i}
            className="flex-1 transition-transform hover:scale-y-110"
            style={{ background: color }}
            title={`${new Date(p.timestamp).toLocaleTimeString()} — ${p.is_up ? 'UP' : 'DOWN'}${p.response_ms != null ? ` (${Math.round(p.response_ms)}ms)` : ''}`}
          />
        )
      })}
    </div>
  )
}

function Kpi({
  label, value, tint, delta, deltaDirection, series, invertTrend, windowLabel,
}: {
  label: string
  value: string
  tint: string
  delta: string
  deltaDirection: 'up' | 'down' | 'flat'
  series: { x: number; y: number }[]
  big?: boolean
  invertTrend?: boolean
  windowLabel?: string
}) {
  const C = useC()
  const ArrowIcon = deltaDirection === 'up' ? ArrowUp : deltaDirection === 'down' ? ArrowDown : ArrowUp
  const goodDir = invertTrend ? deltaDirection === 'down' : deltaDirection !== 'up'
  const deltaColor = deltaDirection === 'flat' ? C.textMuted : goodDir ? C.up : C.down
  const gid = `kpi-${label.replace(/\s+/g, '-')}`
  return (
    <div
      className="flex h-full min-h-[152px] flex-col rounded-xl p-4 shadow-sm"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
        {label}
      </div>
      <div
        className="mt-1 text-2xl font-semibold tabular-nums"
        style={{ color: tint }}
      >
        {value}
      </div>
      <div className="mt-0.5 flex items-center gap-1 text-[10px]" style={{ color: deltaColor }}>
        {deltaDirection !== 'flat' && <ArrowIcon className="h-3 w-3" />}
        <span>{delta}</span>
        <span className="ml-1 truncate" style={{ color: C.textMuted }} title={windowLabel}>{windowLabel || 'window'}</span>
      </div>
      <div className="mt-auto pt-3" style={{ minHeight: 48 }}>
        {series.length > 1 ? (
          <ResponsiveContainer width="100%" height={52}>
            <AreaChart data={series} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={tint} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={tint} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area
                type="monotone"
                dataKey="y"
                stroke={tint}
                strokeWidth={1.5}
                fill={`url(#${gid})`}
                isAnimationActive={false}
              />
              <YAxis hide domain={['dataMin', 'dataMax']} />
              <XAxis hide dataKey="x" />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full rounded" style={{ background: `${tint}08` }} />
        )}
      </div>
    </div>
  )
}

function HealthScoreRing({
  score, tint, label, factors, onViewDetails,
}: {
  score: number
  tint: string
  label: string
  factors: HealthFactor[]
  onViewDetails?: () => void
}) {
  const C = useC()
  const radius = 52
  const circ = 2 * Math.PI * radius
  const offset = circ - (score / 100) * circ
  return (
    <div
      className="flex h-full min-h-[170px] flex-col rounded-xl p-4 shadow-sm"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: C.textMuted }}>
          Health Score
        </span>
        <button
          type="button"
          onClick={onViewDetails}
          className="text-[10px] hover:underline"
          style={{ color: C.primary }}
        >
          View all details
        </button>
      </div>
      <div className="flex flex-1 items-center gap-3">
        <div className="relative h-[120px] w-[120px] flex-shrink-0">
          <svg viewBox="0 0 130 130" className="h-full w-full -rotate-90">
            <defs>
              <linearGradient id="hsGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" stopColor={tint} stopOpacity={0.6} />
                <stop offset="100%" stopColor={tint} stopOpacity={1} />
              </linearGradient>
            </defs>
            <circle
              cx="65" cy="65" r={radius}
              fill="none"
              stroke={C.borderSoft}
              strokeWidth="10"
            />
            <circle
              cx="65" cy="65" r={radius}
              fill="none"
              stroke="url(#hsGrad)"
              strokeWidth="10"
              strokeLinecap="round"
              strokeDasharray={circ}
              strokeDashoffset={offset}
            />
          </svg>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <div className="text-4xl font-bold leading-none" style={{ color: tint }}>{score}</div>
            <div className="mt-0.5 text-[10px]" style={{ color: C.textMuted }}>/ 100</div>
            <div className="mt-1 text-[10px] font-semibold" style={{ color: tint }}>{label}</div>
          </div>
        </div>
        <div className="flex-1 space-y-1.5 text-[10px]">
          <div className="grid grid-cols-[auto_1fr_auto] items-center gap-2" style={{ color: C.textMuted }}>
            <span>Factors</span>
            <span className="h-px" style={{ background: C.border }} />
            <span>Impact</span>
          </div>
          {factors.map((f) => (
            <div key={f.key} className="flex items-center gap-2">
              <span className="w-20 truncate" style={{ color: C.textDim }}>{f.label}</span>
              <div className="h-1 flex-1 overflow-hidden rounded-full" style={{ background: C.borderSoft }}>
                <div
                  className="h-full"
                  style={{
                    width: `${f.subScore}%`,
                    background: f.subScore > 90 ? C.up : f.subScore > 70 ? C.warn : C.down,
                  }}
                />
              </div>
              <span className="w-8 text-right font-mono" style={{ color: C.text }}>
                {f.subScore}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
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

  const handleSave = () => {
    onConfigChange(draft)
    toast.success('Health Score criteria updated')
    onOpenChange(false)
  }
  const handleReset = () => setDraft({ ...DEFAULT_HEALTH_SCORE_CONFIG })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-primary" />
            Health Score Details
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
            <TabsTrigger value="configure">
              <SettingsIcon className="mr-1.5 h-3.5 w-3.5" />
              Configure
            </TabsTrigger>
          </TabsList>

          <TabsContent value="breakdown" className="space-y-2 pt-3">
            <div className="grid grid-cols-[1fr_auto_auto_auto] gap-x-3 gap-y-1 text-[10px] uppercase tracking-wider text-muted">
              <span>Factor</span>
              <span className="text-right">Sub-score</span>
              <span className="text-right">Weight</span>
              <span className="text-right">Contribution</span>
            </div>
            {factors.map((f) => {
              const tone = f.subScore > 90 ? 'text-success' : f.subScore > 70 ? 'text-warning' : 'text-danger'
              return (
                <div key={f.key} className="grid grid-cols-[1fr_auto_auto_auto] items-start gap-x-3 gap-y-0.5 border-b border-border/40 py-2 last:border-0">
                  <div className="min-w-0">
                    <div className="text-xs font-semibold">{f.label}</div>
                    <div className="text-[11px] text-muted">{f.raw}</div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted/80" title="How this sub-score is derived">
                      {f.formula}
                    </div>
                  </div>
                  <span className={`text-right font-mono text-xs tabular-nums ${tone}`}>{f.subScore}</span>
                  <span className="text-right font-mono text-xs tabular-nums text-muted">{f.weight}%</span>
                  <span className="text-right font-mono text-xs tabular-nums">{f.contribution.toFixed(1)}</span>
                </div>
              )
            })}
            <div className="flex items-center justify-between pt-2 text-[11px] text-muted">
              <span>Status thresholds</span>
              <span className="font-mono">
                Excellent ≥ {config.thresholds.excellent} · Good ≥ {config.thresholds.good} · Degraded ≥ {config.thresholds.degraded}
              </span>
            </div>
          </TabsContent>

          <TabsContent value="configure" className="space-y-4 pt-3">
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-xs font-semibold">Factor weights</div>
                <span className={`text-[11px] font-mono ${totalW === 100 ? 'text-muted' : 'text-warning'}`}>
                  Total: {totalW}{totalW !== 100 ? ' (will be normalized)' : ''}
                </span>
              </div>
              {(['availability', 'latency', 'errors', 'incidents'] as const).map((k) => (
                <div key={k} className="grid grid-cols-[140px_1fr_70px] items-center gap-3">
                  <label className="text-xs capitalize">{k}</label>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={draft.weights[k]}
                    onChange={(e) => setWeight(k, Number(e.target.value))}
                    className="accent-primary"
                  />
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    value={draft.weights[k]}
                    onChange={(e) => setWeight(k, Number(e.target.value))}
                    className="h-8 text-xs"
                  />
                </div>
              ))}
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-xs font-semibold">Status thresholds (score ≥)</div>
                {!(draft.thresholds.excellent > draft.thresholds.good && draft.thresholds.good > draft.thresholds.degraded) && (
                  <span className="text-[11px] font-medium text-warning">
                    Excellent &gt; Good &gt; Degraded must be ordered
                  </span>
                )}
              </div>
              {(['excellent', 'good', 'degraded'] as const).map((k) => (
                <div key={k} className="grid grid-cols-[140px_1fr_70px] items-center gap-3">
                  <label className="text-xs capitalize">{k}</label>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={draft.thresholds[k]}
                    onChange={(e) => setThreshold(k, Number(e.target.value))}
                    className="accent-primary"
                  />
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    value={draft.thresholds[k]}
                    onChange={(e) => setThreshold(k, Number(e.target.value))}
                    className="h-8 text-xs"
                  />
                </div>
              ))}
            </div>

            <div className="space-y-2">
              <div className="text-xs font-semibold">Penalty tuning</div>
              <div className="grid grid-cols-[140px_1fr] items-center gap-3">
                <label className="text-xs">Latency target (ms)</label>
                <Input
                  type="number"
                  min={1}
                  value={draft.latencyTargetMs}
                  onChange={(e) => setDraft({ ...draft, latencyTargetMs: Math.max(1, Number(e.target.value) || 1) })}
                  className="h-8 text-xs"
                />
              </div>
              <div className="text-[10px] leading-relaxed text-muted">
                p95 ≤ target → 100. Above target, sub-score drops linearly and reaches 0 at 2× target.
              </div>
              <div className="grid grid-cols-[140px_1fr] items-center gap-3">
                <label className="text-xs">Error scale</label>
                <Input
                  type="number"
                  min={0}
                  step={0.5}
                  value={draft.errorScale}
                  onChange={(e) => setDraft({ ...draft, errorScale: Math.max(0, Number(e.target.value) || 0) })}
                  className="h-8 text-xs"
                />
              </div>
              <div className="text-[10px] leading-relaxed text-muted">
                Penalty per 1% error rate. Default 10 → 1% drops the errors sub-score by 10 points.
              </div>
              <div className="grid grid-cols-[140px_1fr] items-center gap-3">
                <label className="text-xs">Incident penalty</label>
                <Input
                  type="number"
                  min={0}
                  step={1}
                  value={draft.incidentScale}
                  onChange={(e) => setDraft({ ...draft, incidentScale: Math.max(0, Number(e.target.value) || 0) })}
                  className="h-8 text-xs"
                />
              </div>
              <div className="text-[10px] leading-relaxed text-muted">
                Penalty per active incident. Default 15 → 7 incidents bring incidents sub-score to 0.
              </div>
            </div>
          </TabsContent>
        </Tabs>

        <DialogFooter className="flex items-center justify-between gap-2">
          {tab === 'configure' ? (
            <>
              <Button variant="outline" size="sm" onClick={handleReset}>
                <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                Reset to defaults
              </Button>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Cancel</Button>
                <Button size="sm" onClick={handleSave} disabled={!dirty}>Save</Button>
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

function PerformanceChart({
  points, statusHistory, rangeLabel, rangeHours,
}: {
  points: ServiceMetricPoint[]
  statusHistory: Array<{ timestamp: string; new_status: string; duration_sec?: number | null }>
  rangeLabel: string
  rangeHours: number
}) {
  const C = useC()
  // Pick a display bucket that yields ~150–300 plotted points regardless of
  // the source resolution. This keeps the line readable on long ranges
  // without losing meaningful detail on short ones.
  const { p95Window, displayBucketMs, errBucketMs } = useMemo(() => {
    if (rangeHours <= 6)
      return { p95Window: 20, displayBucketMs: 0, errBucketMs: 5 * 60_000 }      // raw probes, 5m buckets
    if (rangeHours <= 24)
      return { p95Window: 12, displayBucketMs: 0, errBucketMs: 30 * 60_000 }     // 5m points, 30m buckets
    if (rangeHours <= 24 * 7)
      return { p95Window: 24, displayBucketMs: 30 * 60_000, errBucketMs: 2 * 3600_000 }
    return { p95Window: 12, displayBucketMs: 3600_000, errBucketMs: 6 * 3600_000 }
  }, [rangeHours])

  // Build the plot series: rolling P95 over the source resolution, then
  // optionally bucket the whole thing down to a coarser display granularity.
  const merged = useMemo(() => {
    const base = points.map((p) => ({
      ts: new Date(p.timestamp).getTime(),
      ms: p.is_up ? p.response_ms : null,
    }))
    const p95s = rollingPercentile(base.map((b) => ({ ts: b.ts, ms: b.ms })), 0.95, p95Window)

    const errBucket = new Map<number, { up: number; down: number }>()
    for (const p of points) {
      const k = Math.floor(new Date(p.timestamp).getTime() / errBucketMs) * errBucketMs
      const b = errBucket.get(k) || { up: 0, down: 0 }
      if (p.is_up) b.up++
      else b.down++
      errBucket.set(k, b)
    }

    const enriched = base.map((b, i) => {
      const bkey = Math.floor(b.ts / errBucketMs) * errBucketMs
      const bb = errBucket.get(bkey)
      const errRate = bb ? (bb.down / Math.max(1, bb.up + bb.down)) * 100 : 0
      return { ts: b.ts, avg: b.ms ?? null, p95: p95s[i]?.p95 ?? null, err: errRate }
    })

    if (displayBucketMs === 0 || enriched.length === 0) return enriched

    // Bucket down to the display granularity. avg → mean, p95 → max,
    // err → mean. Buckets with no avg samples render a gap (null).
    type Bin = { ts: number; avgSum: number; avgN: number; p95Max: number | null; errSum: number; errN: number }
    const bins = new Map<number, Bin>()
    for (const e of enriched) {
      const k = Math.floor(e.ts / displayBucketMs) * displayBucketMs
      let bin = bins.get(k)
      if (!bin) {
        bin = { ts: k, avgSum: 0, avgN: 0, p95Max: null, errSum: 0, errN: 0 }
        bins.set(k, bin)
      }
      if (e.avg != null && Number.isFinite(e.avg)) { bin.avgSum += e.avg; bin.avgN++ }
      if (e.p95 != null && Number.isFinite(e.p95)) {
        bin.p95Max = bin.p95Max == null ? e.p95 : Math.max(bin.p95Max, e.p95)
      }
      bin.errSum += e.err; bin.errN++
    }
    return Array.from(bins.values()).sort((a, b) => a.ts - b.ts).map((b) => ({
      ts: b.ts,
      avg: b.avgN > 0 ? b.avgSum / b.avgN : null,
      p95: b.p95Max,
      err: b.errN > 0 ? b.errSum / b.errN : 0,
    }))
  }, [points, p95Window, errBucketMs, displayBucketMs])

  const tickFormatter = useMemo(() => timeAxisTickFormatter(rangeHours), [rangeHours])
  const minTickGap = rangeHours <= 24 ? 60 : rangeHours <= 24 * 7 ? 80 : 50

  // Incident bands come from real status transitions (not per-bucket is_up,
  // which loses detail at 5m granularity). Each non-up transition opens a
  // band that closes at the next "up" transition or at "now" if still open.
  const bands = useMemo(() => {
    if (!statusHistory || statusHistory.length === 0) return []
    // Endpoint returns DESC by timestamp; sort ascending to walk forward.
    const sorted = [...statusHistory].sort(
      (a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp),
    )
    const out: { start: number; end: number }[] = []
    let open: number | null = null
    for (const ev of sorted) {
      const t = Date.parse(ev.timestamp)
      const down = ev.new_status !== 'up'
      if (down && open == null) open = t
      else if (!down && open != null) {
        out.push({ start: open, end: t })
        open = null
      }
    }
    if (open != null) out.push({ start: open, end: Date.now() })
    return out
  }, [statusHistory])

  const hasData = merged.length > 0

  return (
    <div
      className="rounded-xl p-3"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <span className="text-xs font-semibold">Performance &amp; Status Timeline</span>
          <div className="flex items-center gap-3 text-[10px]" style={{ color: C.textMuted }}>
            <Legend color={C.cyan} label="Avg Response (ms)" />
            <Legend color={C.pink} label="P95 Latency (ms)" />
            <Legend color={C.warn} label="Error Rate %" />
            <Legend color={`${C.down}60`} label="Status Bands" />
          </div>
        </div>
        <span className="text-[11px] font-medium" style={{ color: C.textMuted }}>
          {rangeLabel}
        </span>
      </div>
      <div className="h-[240px] w-full">
        {!hasData ? (
          <div className="flex h-full items-center justify-center text-xs" style={{ color: C.textMuted }}>
            No data yet for this range.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={merged} margin={{ top: 8, right: 30, bottom: 0, left: -5 }}>
              <defs>
                <linearGradient id="respG" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.cyan} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={C.cyan} stopOpacity={0} />
                </linearGradient>
                <linearGradient id="p95G" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.pink} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={C.pink} stopOpacity={0} />
                </linearGradient>
                <linearGradient id="errG" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={C.warn} stopOpacity={0.55} />
                  <stop offset="100%" stopColor={C.warn} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={C.border} strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="ts"
                type="number"
                domain={['dataMin', 'dataMax']}
                scale="time"
                tickFormatter={tickFormatter}
                tick={{ fontSize: 10, fill: C.textMuted }}
                stroke={C.border}
                minTickGap={minTickGap}
              />
              <YAxis
                yAxisId="ms"
                tick={{ fontSize: 10, fill: C.textMuted }}
                stroke={C.border}
                tickFormatter={(v) => `${v}ms`}
              />
              <YAxis
                yAxisId="err"
                orientation="right"
                tick={{ fontSize: 10, fill: C.warn }}
                stroke={C.border}
                tickFormatter={(v) => `${v.toFixed(0)}%`}
                domain={[0, (dataMax: number) => Math.max(10, dataMax * 1.2)]}
              />
              <Tooltip
                contentStyle={{ background: C.panelLift, border: `1px solid ${C.border}`, borderRadius: 6, fontSize: 11, color: C.text }}
                labelFormatter={timeTooltipLabelFormatter}
                formatter={(v: any, name: any) => {
                  if (v == null) return ['—', name]
                  if (name === 'Error Rate') return [`${Number(v).toFixed(2)}%`, name]
                  return [`${Number(v).toFixed(2)} ms`, name]
                }}
              />
              {bands.map((b, i) => (
                <ReferenceArea
                  key={i}
                  yAxisId="ms"
                  x1={b.start}
                  x2={b.end}
                  stroke="none"
                  fill={C.down}
                  fillOpacity={0.12}
                />
              ))}
              <Area
                yAxisId="err"
                type="stepAfter"
                dataKey="err"
                name="Error Rate"
                stroke={C.warn}
                fill="url(#errG)"
                strokeWidth={1.5}
                isAnimationActive={false}
              />
              <Area
                yAxisId="ms"
                type="monotone"
                dataKey="avg"
                name="Avg Response"
                stroke={C.cyan}
                fill="url(#respG)"
                strokeWidth={rangeHours > 24 ? 1.25 : 2}
                strokeOpacity={rangeHours > 24 ? 0.7 : 1}
                fillOpacity={rangeHours > 24 ? 0.5 : 1}
                isAnimationActive={false}
                connectNulls={false}
              />
              <Area
                yAxisId="ms"
                type="monotone"
                dataKey="p95"
                name="P95"
                stroke={C.pink}
                fill="url(#p95G)"
                strokeWidth={rangeHours > 24 ? 2.25 : 2}
                isAnimationActive={false}
                connectNulls={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}

function IncidentsStrip({
  history, fromTo, rangeLabel, checkId,
}: {
  history: Array<{ timestamp: string; new_status: string; reason?: string | null; duration_sec?: number | null }>
  fromTo: { from: string; to: string }
  rangeLabel: string
  checkId: string
}) {
  const C = useC()
  const fromMs = Date.parse(fromTo.from)
  const toMs = Date.parse(fromTo.to)
  const recent = history
    .filter((h) => h.new_status !== 'up')
    .filter((h) => {
      const t = Date.parse(h.timestamp)
      return t >= fromMs && t <= toMs
    })
    .slice(0, 4)
  return (
    <div
      className="rounded-xl p-3"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <AlertCircle className="h-3.5 w-3.5" style={{ color: C.warn }} />
          <span className="text-xs font-semibold">Incidents Strip</span>
          <span className="text-[10px]" style={{ color: C.textMuted }}>({rangeLabel})</span>
        </div>
        <Link
          to={checkId ? `/services/${checkId}/incidents` : '#'}
          className="text-[10px] hover:underline"
          style={{ color: C.primary }}
        >
          View all
        </Link>
      </div>
      {recent.length === 0 ? (
        <div className="rounded-md p-3 text-center text-[11px]" style={{ color: C.textMuted, background: C.borderSoft }}>
          No incidents recorded in the selected window.
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          {recent.map((h, i) => {
            const tint = h.new_status === 'down' ? C.down : C.warn
            return (
              <div
                key={i}
                className="rounded-md border p-2 text-[11px]"
                style={{ borderColor: `${tint}40`, background: `${tint}10` }}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold" style={{ color: tint }}>
                    {h.reason ? h.reason.slice(0, 22) : h.new_status.toUpperCase()}
                  </span>
                  <span className="font-mono" style={{ color: tint }}>
                    {formatDur(h.duration_sec)}
                  </span>
                </div>
                <div className="mt-1" style={{ color: C.textMuted }}>
                  {relativeTime(h.timestamp) || '—'}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function UptimeCalendar({
  hours,
}: {
  hours: Array<{ ts: string; uptime_pct: number | null; sample_count: number }>
}) {
  const C = useC()
  // Build a 30d × 24h grid anchored to today's date.
  const DAYS = 30
  const now = new Date()
  now.setMinutes(0, 0, 0)
  const grid: Array<Array<{ ts: Date; pct: number | null }>> = []
  const byKey = new Map<string, { pct: number | null }>()
  for (const h of hours) {
    const d = new Date(h.ts)
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}-${d.getHours()}`
    byKey.set(key, { pct: h.uptime_pct })
  }
  for (let dayOffset = DAYS - 1; dayOffset >= 0; dayOffset--) {
    const row: Array<{ ts: Date; pct: number | null }> = []
    for (let hr = 0; hr < 24; hr++) {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - dayOffset, hr)
      const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}-${hr}`
      const v = byKey.get(key)
      row.push({ ts: d, pct: v ? v.pct : null })
    }
    grid.push(row)
  }

  function cellColor(pct: number | null): string {
    if (pct == null) return C.unknown
    if (pct >= 99.9) return C.up
    if (pct >= 95) return '#84cc16'
    if (pct >= 80) return C.warn
    return C.down
  }

  // Day labels along the bottom — show ~5 evenly-spaced tick labels.
  const tickIdx = [0, Math.floor(DAYS * 0.2), Math.floor(DAYS * 0.4), Math.floor(DAYS * 0.6), Math.floor(DAYS * 0.8), DAYS - 1]

  return (
    <div
      className="rounded-xl p-3"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold">Uptime Calendar (30 days)</span>
        <div className="flex items-center gap-3 text-[10px]" style={{ color: C.textMuted }}>
          <Legend color={C.up} label="Up" />
          <Legend color={C.warn} label="Warning" />
          <Legend color={C.down} label="Down" />
          <Legend color={C.unknown} label="No Data" />
        </div>
      </div>
      <div className="flex gap-[1.5px]">
        {grid.map((row, i) => (
          <div key={i} className="flex flex-1 flex-col gap-[1.5px]">
            {row.map((c, j) => (
              <div
                key={j}
                className="h-[9px] rounded-[1px]"
                style={{ background: cellColor(c.pct) }}
                title={`${c.ts.toLocaleString()} — ${c.pct == null ? 'no data' : `${c.pct.toFixed(1)}%`}`}
              />
            ))}
          </div>
        ))}
      </div>
      {/* Bottom date axis */}
      <div className="mt-2 flex text-[9px]" style={{ color: C.textMuted, fontFamily: 'ui-monospace, monospace' }}>
        {grid.map((row, i) => (
          <div key={i} className="flex flex-1 justify-center">
            {tickIdx.includes(i) ? row[0].ts.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''}
          </div>
        ))}
      </div>
    </div>
  )
}

function RelatedServices({
  upstream, downstream,
}: {
  upstream: ServiceCheck[]
  downstream: ServiceCheck[]
}) {
  const C = useC()
  return (
    <div
      className="rounded-xl p-3"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold">Related Services</span>
        <button className="text-[10px] hover:underline" style={{ color: C.primary }}>View dependency map</button>
      </div>
      <div className="space-y-2">
        <RelatedList title="Upstream" items={upstream} emptyLabel="No parent" />
        <RelatedList title="Downstream" items={downstream} emptyLabel="No dependents" />
      </div>
    </div>
  )
}

function RelatedList({
  title, items, emptyLabel,
}: { title: string; items: ServiceCheck[]; emptyLabel: string }) {
  const C = useC()
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>
        {title}
      </div>
      {items.length === 0 ? (
        <div className="rounded-md px-2 py-1.5 text-[11px]" style={{ background: C.borderSoft, color: C.textMuted }}>
          {emptyLabel}
        </div>
      ) : (
        <div className="space-y-1">
          {items.map((c) => {
            const sm = statusMeta[c.status] || statusMeta.unknown
            // Fake-but-stable sparkline based on current response — purely decorative.
            const seed = c.id
              .split('-')
              .reduce((a, s) => a + parseInt(s.slice(0, 4), 16), 0)
            const bars = Array.from({ length: 12 }, (_, i) => {
              const v = Math.sin((seed + i * 7) * 0.37) * 0.5 + 0.5
              const down = c.status !== 'up' && i >= 9
              return { v: down ? 0.2 : 0.3 + v * 0.6, down }
            })
            return (
              <Link
                key={c.id}
                to={`/services/${c.id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-white/5"
                style={{ background: C.borderSoft }}
              >
                <div className="flex min-w-0 flex-1 items-center gap-2 text-[11px]">
                  <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full" style={{ background: sm.color }} />
                  <span className="truncate" style={{ color: C.text }}>{c.name}</span>
                </div>
                <div className="flex items-end gap-[1.5px]" style={{ height: 14 }}>
                  {bars.map((b, i) => (
                    <span
                      key={i}
                      style={{
                        display: 'inline-block',
                        width: 2,
                        height: `${Math.max(2, b.v * 14)}px`,
                        background: b.down ? C.down : sm.color,
                        opacity: b.down ? 0.8 : 0.6 + b.v * 0.4,
                        borderRadius: 1,
                      }}
                    />
                  ))}
                </div>
                <span className="ml-2 text-[10px]" style={{ color: sm.color }}>{sm.label}</span>
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}

function RecentActivityTable({
  history, fromTo, rangeLabel,
}: {
  history: Array<{ timestamp: string; new_status: string; old_status: string | null; reason?: string | null; duration_sec?: number | null }>
  fromTo: { from: string; to: string }
  rangeLabel: string
}) {
  const C = useC()
  const fromMs = Date.parse(fromTo.from)
  const toMs = Date.parse(fromTo.to)
  const rows = history.filter((h) => {
    const t = Date.parse(h.timestamp)
    return t >= fromMs && t <= toMs
  })
  return (
    <div
      className="rounded-xl p-3"
      style={{ background: C.panel, border: `1px solid ${C.border}` }}
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold">Recent Activity &amp; Status Changes</span>
        <span className="text-[10px]" style={{ color: C.textMuted }}>{rangeLabel}</span>
      </div>
      {rows.length === 0 ? (
        <div className="py-6 text-center text-[11px]" style={{ color: C.textMuted }}>
          No status changes in this window.
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border" style={{ borderColor: C.border }}>
          <table className="w-full text-[11px]" style={{ color: C.text }}>
            <thead>
              <tr style={{ background: C.borderSoft, color: C.textMuted }}>
                <th className="px-3 py-1.5 text-left font-medium">Date &amp; Time</th>
                <th className="px-3 py-1.5 text-left font-medium">Type</th>
                <th className="px-3 py-1.5 text-left font-medium">Status</th>
                <th className="px-3 py-1.5 text-left font-medium">Message</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 10).map((h, i) => {
                const sm = statusMeta[h.new_status] || statusMeta.unknown
                return (
                  <tr key={i} style={{ borderTop: `1px solid ${C.border}` }}>
                    <td className="px-3 py-1.5 font-mono" style={{ color: C.textDim }}>
                      {new Date(h.timestamp).toLocaleString()}
                    </td>
                    <td className="px-3 py-1.5" style={{ color: C.textDim }}>Probe</td>
                    <td className="px-3 py-1.5">
                      <span
                        className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                        style={{ background: `${sm.color}20`, color: sm.color }}
                      >
                        {sm.label}
                      </span>
                    </td>
                    <td className="px-3 py-1.5" style={{ color: C.textDim }}>
                      {h.reason || (h.old_status ? `${h.old_status} → ${h.new_status}` : '—')}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function InlineConfig({
  check, onEdit,
}: { check: ServiceCheck; onEdit: () => void }) {
  const C = useC()
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-xl p-3" style={{ background: C.panel, border: `1px solid ${C.border}` }}>
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex flex-1 items-center gap-1.5 text-left"
          aria-expanded={open}
        >
          {open
            ? <ChevronDown className="h-3.5 w-3.5" style={{ color: C.textMuted }} />
            : <ChevronRight className="h-3.5 w-3.5" style={{ color: C.textMuted }} />}
          <span className="text-xs font-semibold">Inline Configuration</span>
        </button>
        <button
          onClick={onEdit}
          className="text-[10px] hover:underline"
          style={{ color: C.primary }}
        >
          Edit all
        </button>
      </div>
      {open && (
        <div className="mt-2 space-y-1.5 text-[11px]">
          <CfgRow label="URL" value={check.target_url || check.target_host || '—'} mono onEdit={onEdit} />
          <CfgRow label="Method" value={check.http_method || '—'} onEdit={onEdit} />
          <CfgRow
            label="Authentication"
            value={check.credential_name ? `${check.credential_name} (${check.credential_auth_type})` : 'None'}
            onEdit={onEdit}
          />
          <CfgRow
            label="Journey"
            value={(check.workflow_steps?.length || 0) > 0 ? `${check.workflow_steps?.length} steps · ${(check.workflow_operator || 'all').toUpperCase()}` : 'Single request'}
            onEdit={onEdit}
          />
          <CfgRow
            label="Expected Status"
            value={check.http_expected_statuses || String(check.http_expected_status || '—')}
            onEdit={onEdit}
          />
          <CfgRow label="Timeout" value={`${check.timeout}s`} onEdit={onEdit} />
          <CfgRow label="Check Interval" value={`${check.check_interval}s`} onEdit={onEdit} />
          <CfgRow
            label="Tags"
            value={check.tags.length > 0 ? check.tags.join(', ') : '—'}
            onEdit={onEdit}
          />
          <CfgRow label="Alert Policy" value={check.group_name || 'default'} onEdit={onEdit} />
          <CfgRow
            label="Maintenance Window"
            value={check.in_maintenance ? 'Active' : 'None'}
            onEdit={onEdit}
          />
        </div>
      )}
    </div>
  )
}

function WorkflowOverview({ check }: { check: ServiceCheck }) {
  const C = useC()
  const steps = check.workflow_steps || []
  return (
    <div className="rounded-xl p-4" style={{ background: C.panel, border: `1px solid ${C.border}` }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Route className="h-4 w-4" style={{ color: C.primary }} />
          <div>
            <div className="text-xs font-semibold">Authenticated service journey</div>
            <div className="text-[11px]" style={{ color: C.textMuted }}>
              Cookie-preserving navigation · {(check.workflow_operator || 'all').toUpperCase()} rule
            </div>
          </div>
        </div>
        {check.credential_name && (
          <span className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-semibold" style={{ background: `${C.up}18`, color: C.up }}>
            <LockKeyhole className="h-3 w-3" /> {check.credential_name} · {check.credential_auth_type}
          </span>
        )}
      </div>
      <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {steps.map((step, index) => (
          <div key={`${step.name}-${index}`} className="rounded-lg p-3" style={{ background: C.panelLift, border: `1px solid ${C.borderSoft}` }}>
            <div className="flex items-center gap-2">
              <span className="flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold" style={{ background: `${C.primary}20`, color: C.primary }}>{index + 1}</span>
              <span className="truncate text-xs font-semibold">{step.name}</span>
            </div>
            <div className="mt-2 truncate font-mono text-[10px]" style={{ color: C.textDim }} title={step.url}>{step.method} {step.url}</div>
            <div className="mt-1 text-[10px]" style={{ color: C.textMuted }}>
              Expect {step.expected_statuses || '200'}{step.content_match ? ' + content validation' : ''}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function CfgRow({ label, value, mono, onEdit }: { label: string; value: string; mono?: boolean; onEdit: () => void }) {
  const C = useC()
  return (
    <div
      className="grid grid-cols-[1fr_auto] items-start gap-2 border-b pb-1.5"
      style={{ borderColor: C.borderSoft }}
    >
      <div className="min-w-0">
        <div className="text-[9px] uppercase tracking-wider" style={{ color: C.textMuted }}>{label}</div>
        <div
          className={`truncate text-[11px] leading-tight ${mono ? 'font-mono' : ''}`}
          style={{ color: C.text }}
          title={value}
        >
          {value}
        </div>
      </div>
      <button
        onClick={onEdit}
        className="mt-0.5 rounded p-1 hover:bg-white/5"
        style={{ color: C.textMuted }}
        title="Edit"
      >
        <Edit3 className="h-3 w-3" />
      </button>
    </div>
  )
}

function QuickActions({
  onRunProbe,
  onPauseAll,
  onForceRevalidate,
  onMaintenance,
  onAckAll,
  onExport,
  enabled,
  inMaintenance,
  activeAlertCount,
  busy,
}: {
  onRunProbe: () => void
  onPauseAll: () => void
  onForceRevalidate: () => void
  onMaintenance: () => void
  onAckAll: () => void
  onExport: () => void
  enabled: boolean
  inMaintenance: boolean
  activeAlertCount: number
  busy: { probe: boolean; pause: boolean; revalidate: boolean; ack: boolean }
}) {
  const C = useC()
  const items: Array<{
    Icon: any
    label: string
    onClick: () => void
    tint: string
    loading?: boolean
    disabled?: boolean
  }> = [
    { Icon: Play, label: 'Run Probe Now', onClick: onRunProbe, tint: C.up, loading: busy.probe },
    {
      Icon: Pause,
      label: enabled ? 'Pause All Checks' : 'Resume All Checks',
      onClick: onPauseAll,
      tint: C.warn,
      loading: busy.pause,
    },
    { Icon: RefreshCw, label: 'Force Re-validate', onClick: onForceRevalidate, tint: C.cyan, loading: busy.revalidate },
    {
      Icon: Wrench,
      label: inMaintenance ? 'End Maintenance' : 'Maintenance Mode',
      onClick: onMaintenance,
      tint: C.violet,
    },
    {
      Icon: Bell,
      label: `Acknowledge Alerts${activeAlertCount > 0 ? ` (${activeAlertCount})` : ''}`,
      onClick: onAckAll,
      tint: C.pink,
      loading: busy.ack,
      disabled: activeAlertCount === 0,
    },
    { Icon: Download, label: 'Export Config', onClick: onExport, tint: C.textDim },
  ]
  return (
    <div className="rounded-xl p-3" style={{ background: C.panel, border: `1px solid ${C.border}` }}>
      <div className="mb-2 text-xs font-semibold">Quick Actions</div>
      <div className="space-y-1">
        {items.map((it, i) => {
          const Icon = it.Icon
          const isDisabled = it.disabled || it.loading
          return (
            <button
              key={i}
              onClick={it.onClick}
              disabled={isDisabled}
              className="flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-[10px] transition-colors hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
              style={{ background: C.borderSoft, color: C.text }}
            >
              <span
                className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded"
                style={{ background: `${it.tint}20`, color: it.tint }}
              >
                {it.loading ? (
                  <RefreshCw className="h-2.5 w-2.5 animate-spin" />
                ) : (
                  <Icon className="h-2.5 w-2.5" />
                )}
              </span>
              <span className="truncate">{it.label}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

function MaintenanceDialog({
  open,
  onOpenChange,
  inMaintenance,
  onStart,
  onStartCustom,
  onEnd,
  starting,
  ending,
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
  const C = useC()
  // Always-on hooks — must run before any early return.
  const [mode, setMode] = useState<'preset' | 'custom'>('preset')

  // datetime-local default values: now, now+1h, in the user's local timezone.
  const toLocalInput = (d: Date) => {
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const [startsAt, setStartsAt] = useState(() => toLocalInput(new Date()))
  const [endsAt, setEndsAt] = useState(() => toLocalInput(new Date(Date.now() + 60 * 60_000)))
  const [error, setError] = useState<string | null>(null)

  // Reset to preset mode whenever the dialog reopens.
  useEffect(() => {
    if (open) {
      setMode('preset')
      setError(null)
      setStartsAt(toLocalInput(new Date()))
      setEndsAt(toLocalInput(new Date(Date.now() + 60 * 60_000)))
    }
  }, [open])

  if (!open) return null

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
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.6)' }}
      onClick={() => onOpenChange(false)}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-xl p-4 shadow-xl"
        style={{ background: C.panel, border: `1px solid ${C.border}`, color: C.text }}
      >
        <div className="mb-1 flex items-center gap-2">
          <Wrench className="h-4 w-4" style={{ color: C.violet }} />
          <h3 className="text-sm font-semibold">
            {inMaintenance ? 'Active Maintenance Window' : 'Start Maintenance Window'}
          </h3>
        </div>
        <p className="mb-3 text-xs" style={{ color: C.textDim }}>
          {inMaintenance
            ? 'This service is currently in maintenance. Alerts are suppressed.'
            : 'Suppress alerts for this service for a defined period.'}
        </p>
        {inMaintenance ? (
          <div className="flex items-center justify-end gap-2">
            <button
              onClick={() => onOpenChange(false)}
              className="rounded-md px-3 py-1.5 text-xs hover:bg-white/5"
              style={{ background: C.borderSoft, color: C.text }}
            >
              Close
            </button>
            <button
              onClick={onEnd}
              disabled={ending}
              className="rounded-md px-3 py-1.5 text-xs font-medium disabled:opacity-50"
              style={{ background: C.down, color: '#fff' }}
            >
              {ending ? 'Ending…' : 'End maintenance now'}
            </button>
          </div>
        ) : (
          <>
            {/* Mode toggle: presets vs custom */}
            <div className="mb-3 flex gap-0.5 rounded-md p-0.5" style={{ background: C.borderSoft }}>
              {(['preset', 'custom'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className="flex-1 rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors"
                  style={
                    mode === m
                      ? { background: C.panel, color: C.text }
                      : { color: C.textDim }
                  }
                >
                  {m === 'preset' ? 'Quick presets' : 'Custom date & time'}
                </button>
              ))}
            </div>

            {mode === 'preset' ? (
              <div className="grid grid-cols-2 gap-2">
                {presets.map((p) => (
                  <button
                    key={p.hours}
                    onClick={() => onStart(p.hours)}
                    disabled={starting}
                    className="flex items-center justify-center gap-1.5 rounded-md py-2 text-xs font-medium hover:bg-white/5 disabled:opacity-50"
                    style={{ background: C.borderSoft, color: C.text, border: `1px solid ${C.border}` }}
                  >
                    <Clock className="h-3 w-3" style={{ color: C.violet }} />
                    {p.label}
                  </button>
                ))}
              </div>
            ) : (
              <div className="space-y-3">
                <div>
                  <label className="mb-1 block text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>
                    Starts at (your local time)
                  </label>
                  <input
                    type="datetime-local"
                    value={startsAt}
                    onChange={(e) => setStartsAt(e.target.value)}
                    className="w-full rounded-md px-2 py-1.5 text-xs"
                    style={{ background: C.borderSoft, color: C.text, border: `1px solid ${C.border}` }}
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[10px] uppercase tracking-wider" style={{ color: C.textMuted }}>
                    Ends at (your local time)
                  </label>
                  <input
                    type="datetime-local"
                    value={endsAt}
                    onChange={(e) => setEndsAt(e.target.value)}
                    className="w-full rounded-md px-2 py-1.5 text-xs"
                    style={{ background: C.borderSoft, color: C.text, border: `1px solid ${C.border}` }}
                  />
                </div>
                {error && (
                  <div
                    className="rounded-md px-2 py-1.5 text-[11px]"
                    style={{ background: `${C.down}15`, color: C.down, border: `1px solid ${C.down}40` }}
                  >
                    {error}
                  </div>
                )}
              </div>
            )}

            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => onOpenChange(false)}
                disabled={starting}
                className="rounded-md px-3 py-1.5 text-xs hover:bg-white/5 disabled:opacity-50"
                style={{ color: C.textDim }}
              >
                Cancel
              </button>
              {mode === 'custom' && (
                <button
                  onClick={submitCustom}
                  disabled={starting}
                  className="rounded-md px-3 py-1.5 text-xs font-medium disabled:opacity-50"
                  style={{ background: C.violet, color: '#fff' }}
                >
                  {starting ? 'Starting…' : 'Start maintenance'}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function CurrentChecksSummary({ points }: { points: ServiceMetricPoint[] }) {
  const C = useC()
  const recent = points.slice(-120)
  const total = recent.length || 0
  const passed = recent.filter((p) => p.is_up === true).length
  const failed = recent.filter((p) => p.is_up === false).length
  const noData = Math.max(0, total - passed - failed)
  const segments = [
    { label: 'Passed', value: passed, color: C.up },
    { label: 'Warning', value: 0, color: C.warn },
    { label: 'Failed', value: failed, color: C.down },
    { label: 'No Data', value: noData, color: C.unknown },
  ]
  const sum = segments.reduce((a, b) => a + b.value, 0) || 1
  let acc = 0
  const arcs = segments.map((s) => {
    const start = acc / sum
    acc += s.value
    const end = acc / sum
    return { ...s, start, end }
  })
  return (
    <div className="rounded-xl p-3" style={{ background: C.panel, border: `1px solid ${C.border}` }}>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold">Current Checks Summary</span>
        <button className="text-[10px] hover:underline" style={{ color: C.primary }}>View all</button>
      </div>
      <div className="flex items-center gap-3">
        <div className="relative h-[80px] w-[80px] flex-shrink-0">
          <svg viewBox="0 0 42 42" className="h-full w-full -rotate-90">
            <circle cx="21" cy="21" r="15.915" fill="none" stroke={C.borderSoft} strokeWidth="4" />
            {arcs.map((a, i) => {
              const dash = (a.end - a.start) * 100
              const gap = 100 - dash
              const offset = 100 - a.start * 100 + 25
              return (
                <circle
                  key={i}
                  cx="21" cy="21" r="15.915"
                  fill="none"
                  stroke={a.color}
                  strokeWidth="4"
                  strokeDasharray={`${dash} ${gap}`}
                  strokeDashoffset={offset}
                />
              )
            })}
          </svg>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <div className="text-base font-bold" style={{ color: C.text }}>{total}</div>
            <div className="text-[8px]" style={{ color: C.textMuted }}>Checks</div>
          </div>
        </div>
        <div className="flex-1 space-y-1 text-[11px]">
          {segments.map((s) => (
            <div key={s.label} className="flex items-center justify-between">
              <div className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: s.color }} />
                <span style={{ color: C.textDim }}>{s.label}</span>
              </div>
              <div className="flex items-center gap-2 font-mono">
                <span style={{ color: C.text }}>{s.value}</span>
                <span className="text-[9px]" style={{ color: C.textMuted }}>
                  {total > 0 ? `${((s.value / total) * 100).toFixed(1)}%` : '0%'}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function AlertSummary({
  counts,
  checkId,
}: {
  counts: { critical: number; warning: number; info: number }
  checkId: string
}) {
  const C = useC()
  const items = [
    { label: 'Critical', severity: 'critical', value: counts.critical, color: C.down, Icon: AlertCircle },
    { label: 'Warning', severity: 'warning', value: counts.warning, color: C.warn, Icon: AlertTriangle },
    { label: 'Info', severity: 'info', value: counts.info, color: C.primary, Icon: Info },
  ]
  const allHref = checkId ? `/alerts?service_check_id=${checkId}&status=active` : '/alerts'
  return (
    <div className="rounded-xl p-3" style={{ background: C.panel, border: `1px solid ${C.border}` }}>
      <div className="mb-2 flex items-center justify-between gap-1">
        <span className="text-xs font-semibold">Alert Summary</span>
        <Link to={allHref} className="text-[9px] hover:underline" style={{ color: C.primary }}>View all</Link>
      </div>
      <div className="space-y-1">
        {items.map((a) => {
          const Icon = a.Icon
          const href = checkId
            ? `/alerts?service_check_id=${checkId}&severity=${a.severity}&status=active`
            : `/alerts?severity=${a.severity}&status=active`
          const inactive = a.value === 0
          return (
            <Link
              key={a.label}
              to={href}
              className="flex items-center justify-between gap-1.5 rounded-md px-1.5 py-1 text-[10px] transition-colors hover:bg-white/10"
              style={{
                background: C.borderSoft,
                opacity: inactive ? 0.7 : 1,
                borderLeft: `2px solid ${a.color}`,
              }}
              title={`View ${a.value} ${a.label.toLowerCase()} alert${a.value === 1 ? '' : 's'} for this service`}
            >
              <span className="flex items-center gap-1.5">
                <Icon className="h-3 w-3 flex-shrink-0" style={{ color: a.color }} />
                <span style={{ color: C.textDim }}>{a.label}</span>
              </span>
              <span className="flex items-center gap-1">
                <span className="font-mono font-semibold" style={{ color: a.value > 0 ? a.color : C.text }}>
                  {a.value}
                </span>
                <ChevronRight className="h-2.5 w-2.5" style={{ color: C.textMuted }} />
              </span>
            </Link>
          )
        })}
      </div>
    </div>
  )
}

/* ─── Derivations ───────────────────────────────────────────────────────── */

/* ─── Health score configuration ────────────────────────────────────────── */

type HealthScoreConfig = {
  weights: { availability: number; latency: number; errors: number; incidents: number }
  thresholds: { excellent: number; good: number; degraded: number }
  latencyTargetMs: number   // ms at which latency sub-score reaches 0
  errorScale: number        // multiplier on error_rate_pct → penalty points
  incidentScale: number     // penalty per active incident
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
  raw: string          // human-readable measurement
  formula: string      // how subScore was derived (shown in Breakdown UI)
  subScore: number     // 0-100 normalized contribution
  weight: number       // 0-100 share of total
  contribution: number // weighted contribution to final score
}

function computeHealthScore(args: {
  uptime_pct: number | null
  error_rate_pct: number | null
  incident_count: number
  p95_response_ms: number | null
}, cfg: HealthScoreConfig = DEFAULT_HEALTH_SCORE_CONFIG) {
  const up = args.uptime_pct ?? 100
  const err = args.error_rate_pct ?? 0
  const inc = args.incident_count || 0
  const p95 = args.p95_response_ms

  const availabilitySub = clamp(up, 0, 100)
  // Latency: target is the SLO. p95 ≤ target → 100. Above target,
  // sub-score drops linearly and reaches 0 at 2× target.
  const target = Math.max(1, cfg.latencyTargetMs)
  const latencySub = p95 == null
    ? 100
    : p95 <= target
      ? 100
      : clamp(100 - ((p95 - target) / target) * 100, 0, 100)
  const errorsSub = clamp(100 - err * cfg.errorScale, 0, 100)
  const incidentsSub = clamp(100 - inc * cfg.incidentScale, 0, 100)

  const w = cfg.weights
  const totalW = Math.max(1, w.availability + w.latency + w.errors + w.incidents)

  const factors: HealthFactor[] = [
    {
      key: 'availability',
      label: 'Availability',
      raw: args.uptime_pct == null ? 'no data' : `${up.toFixed(2)}%`,
      formula: args.uptime_pct == null
        ? 'no samples in window — defaults to 100'
        : `uptime % directly = ${availabilitySub.toFixed(1)}`,
      subScore: Math.round(availabilitySub),
      weight: w.availability,
      contribution: (availabilitySub * w.availability) / totalW,
    },
    {
      key: 'latency',
      label: 'Latency',
      raw: p95 == null ? 'no data' : `${Math.round(p95)} ms p95`,
      formula: p95 == null
        ? 'no response samples — defaults to 100'
        : p95 <= target
          ? `${Math.round(p95)} ms ≤ ${cfg.latencyTargetMs} ms target → 100`
          : `100 − ((${Math.round(p95)} − ${cfg.latencyTargetMs}) / ${cfg.latencyTargetMs}) × 100 = ${latencySub.toFixed(1)}`,
      subScore: Math.round(latencySub),
      weight: w.latency,
      contribution: (latencySub * w.latency) / totalW,
    },
    {
      key: 'errors',
      label: 'Errors',
      raw: args.error_rate_pct == null ? 'no data' : `${err.toFixed(2)}%`,
      formula: args.error_rate_pct == null
        ? 'no error data — defaults to 100'
        : `100 − ${err.toFixed(2)} × ${cfg.errorScale} = ${errorsSub.toFixed(1)}`,
      subScore: Math.round(errorsSub),
      weight: w.errors,
      contribution: (errorsSub * w.errors) / totalW,
    },
    {
      key: 'incidents',
      label: 'Incidents',
      raw: `${inc} active`,
      formula: `100 − ${inc} × ${cfg.incidentScale} = ${incidentsSub.toFixed(1)}`,
      subScore: Math.round(incidentsSub),
      weight: w.incidents,
      contribution: (incidentsSub * w.incidents) / totalW,
    },
  ]

  const rawScore = factors.reduce((s, f) => s + f.contribution, 0)
  const score = Math.max(0, Math.min(100, Math.round(rawScore)))

  const t = cfg.thresholds
  const tint = score >= t.excellent ? C.up : score >= t.good ? C.warn : C.down
  const label =
    score >= t.excellent ? 'Excellent' :
    score >= t.good ? 'Good' :
    score >= t.degraded ? 'Degraded' : 'Critical'

  return { score, tint, label, factors }
}

function buildUptimeSeries(
  hours: Array<{ ts: string; uptime_pct: number | null }>,
): { x: number; y: number }[] {
  return hours
    .filter((h) => h.uptime_pct != null)
    .map((h) => ({ x: Date.parse(h.ts), y: h.uptime_pct as number }))
}

function buildResponseSeries(points: ServiceMetricPoint[], quantile = 0): { x: number; y: number }[] {
  if (quantile > 0) {
    return rollingPercentile(
      points.map((p) => ({ ts: Date.parse(p.timestamp), ms: p.is_up ? p.response_ms : null, down: 0 })),
      quantile,
      20,
    )
      .filter((p) => p.p95 != null)
      .map((p) => ({ x: p.ts, y: p.p95 as number }))
  }
  return points
    .filter((p) => p.is_up && p.response_ms != null && p.response_ms > 0)
    .map((p) => ({ x: Date.parse(p.timestamp), y: p.response_ms as number }))
}

function buildErrorSeries(points: ServiceMetricPoint[]): { x: number; y: number }[] {
  const bucketMin = 5
  const bucket = new Map<number, { up: number; down: number }>()
  for (const p of points) {
    const t = Date.parse(p.timestamp)
    const key = Math.floor(t / (bucketMin * 60_000)) * (bucketMin * 60_000)
    const b = bucket.get(key) || { up: 0, down: 0 }
    if (p.is_up) b.up++
    else b.down++
    bucket.set(key, b)
  }
  return [...bucket.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([ts, v]) => ({ x: ts, y: (v.down / Math.max(1, v.up + v.down)) * 100 }))
}

function rollingPercentile(
  data: Array<{ ts: number; ms: number | null }>,
  q: number,
  window: number,
): Array<{ ts: number; p95: number | null }> {
  const out: Array<{ ts: number; p95: number | null }> = []
  for (let i = 0; i < data.length; i++) {
    const slice = data
      .slice(Math.max(0, i - window), i + 1)
      .map((d) => d.ms)
      .filter((x): x is number => x != null && x > 0)
    if (slice.length < 3) {
      out.push({ ts: data[i].ts, p95: null })
      continue
    }
    slice.sort((a, b) => a - b)
    const idx = Math.max(0, Math.floor(slice.length * q) - 1)
    out.push({ ts: data[i].ts, p95: slice[idx] })
  }
  return out
}

function clamp(n: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, n))
}
