import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  BellRing,
  Box,
  Bell,
  CheckCircle2,
  Clock,
  Cpu,
  Database,
  Fan,
  FileText,
  GitBranch,
  HardDrive,
  Info,
  Key,
  Layers,
  Loader2,
  LockKeyhole,
  MapPin,
  MemoryStick,
  Minus,
  MoreVertical,
  Network,
  Pencil,
  Play,
  Plug,
  Power,
  Radar,
  RefreshCw,
  Router as RouterIcon,
  Save,
  Search,
  Server,
  Settings as SettingsIcon,
  Shield,
  SquareStack,
  Tag as TagIcon,
  Thermometer,
  Trash2,
  Wrench,
  TrendingDown,
  TrendingUp,
  Wifi,
  Wrench,
  Zap,
  ZapOff,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage, axisRightPad, cn, formatBps, formatBpsAxis, formatBytes, formatDuration, relativeTime, timeAxisTickFormatter, timeTicks, timeTooltipLabelFormatter } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { DeviceFormDialog } from '@/components/forms/DeviceFormDialog'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker, rangePhrase, useTimeRange } from '@/components/TimeRangePicker'

/* ── Shared helpers ────────────────────────────────────────── */

/** Axis labels for the fixed 5-tick device charts. A plain "10:54 AM" at both
 *  ends of a 24h window is ambiguous, and there is room here for the date. */
function deviceAxisFormatter(rangeHours: number): (ts: number) => string {
  if (rangeHours <= 12) {
    return (ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  if (rangeHours <= 24 * 7) {
    return (ts) => {
      const d = new Date(ts)
      return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
    }
  }
  return (ts) => new Date(ts).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

const ttStyle = () => ({
  contentStyle: {
    backgroundColor: 'rgb(var(--surface))',
    border: '1px solid rgb(var(--border))',
    borderRadius: 6,
    color: 'rgb(var(--text))',
    fontSize: 11,
    padding: '5px 8px',
  },
  labelFormatter: timeTooltipLabelFormatter,
})

type HealthKind = 'healthy' | 'warning' | 'critical' | 'offline' | 'maintenance'

function healthOf(status: string): HealthKind {
  if (status === 'up') return 'healthy'
  if (status === 'degraded') return 'warning'
  if (status === 'down') return 'critical'
  if (status === 'maintenance') return 'maintenance'
  return 'offline'
}

const typeIconMap: Record<string, React.ComponentType<{ className?: string }>> = {
  router: RouterIcon,
  switch: Network,
  firewall: Shield,
  server: Server,
  access_point: Wifi,
  storage: Database,
  hypervisor: Server,
  other: Box,
}

/* ════════════════════════════════════════════════════════════
   Main page
   ════════════════════════════════════════════════════════════ */

export function DeviceDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [editOpen, setEditOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()

  const { data: device, isLoading } = useQuery<any>({
    queryKey: ['device', id],
    queryFn: async () => (await api.get(`/devices/${id}`)).data,
    refetchInterval: 15_000,
    enabled: !!id,
  })

  const [maintOpen, setMaintOpen] = useState(false)
  const { data: maint } = useQuery<{ active: any[]; upcoming: any[] }>({
    queryKey: ['device-maintenance', id],
    queryFn: async () => (await api.get(`/devices/${id}/maintenance`)).data,
    refetchInterval: 60_000,
    enabled: !!id,
  })

  const del = useMutation({
    mutationFn: async () => api.delete(`/devices/${id}`),
    onSuccess: () => {
      toast.success('Device deleted')
      qc.invalidateQueries({ queryKey: ['devices'] })
      navigate('/devices')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  if (isLoading || !device) {
    return (
      <div className="flex items-center justify-center py-20 text-muted">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <DeviceHeader
        device={device}
        onEdit={() => setEditOpen(true)}
        onDelete={() => setDelOpen(true)}
        onMaintenance={() => setMaintOpen(true)}
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
      />

      <MaintenanceBanner windows={maint?.active || []} onManage={() => setMaintOpen(true)} />

      <DashboardSection device={device} deviceId={id!} range={range} />

      <DeviceFormDialog open={editOpen} onOpenChange={setEditOpen} device={device} />
      <DeviceMaintenanceDialog
        open={maintOpen}
        onOpenChange={setMaintOpen}
        device={device}
        windows={maint}
      />
      <ConfirmDialog
        open={delOpen}
        onOpenChange={setDelOpen}
        title="Delete device"
        description={
          <>
            Permanently delete <b>{device.hostname}</b> and all its history?
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => del.mutate()}
      />
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   Device header card (icon + name + chips + metadata rows + actions)
   ════════════════════════════════════════════════════════════ */

function DeviceHeader({
  device, onEdit, onDelete, onMaintenance, rangePicker,
}: { device: any; onEdit: () => void; onDelete: () => void; onMaintenance?: () => void; rangePicker?: React.ReactNode }) {
  const health = healthOf(device.status)
  const Icon = typeIconMap[device.device_type] || Box

  // Fetch availability % over the active window so the Uptime pill always has
  // a value when ping data exists (system boot duration is often null).
  const { range } = useTimeRange()
  const { data: uptimeStats } = useQuery<{ devices: Record<string, number> }>({
    queryKey: ['uptime-stats', range.hours],
    queryFn: async () =>
      (await api.get(`/devices/dashboard/uptime-stats?hours=${Math.max(1, Math.round(range.hours))}`)).data,
    staleTime: 30_000,
  })
  const availabilityPct = device?.id && uptimeStats?.devices ? uptimeStats.devices[device.id] : undefined

  /* Diagnostics = the reachability checks we can actually run from the server:
     an ICMP probe plus, when SNMP is configured, a live SNMP GET. */
  const [diagOpen, setDiagOpen] = useState(false)
  const [diagResult, setDiagResult] = useState<DiagnosticsResult | null>(null)
  const diagnostics = useMutation({
    mutationFn: async (): Promise<DiagnosticsResult> => {
      const ping = api.post(`/devices/${device.id}/ping-test`).then((r) => r.data, (e) => ({ ok: false, reason: apiErrorMessage(e) }))
      const snmp = device.snmp_enabled
        ? api.post(`/devices/${device.id}/snmp-test`).then((r) => r.data, (e) => ({ ok: false, reason: apiErrorMessage(e) }))
        : Promise.resolve(null)
      const [pingRes, snmpRes] = await Promise.all([ping, snmp])
      return { ping: pingRes, snmp: snmpRes }
    },
    onMutate: () => setDiagResult(null),
    onSuccess: (data) => setDiagResult(data),
    onError: (e: any) => toast.error('Diagnostics failed', apiErrorMessage(e)),
  })

  const kind = {
    healthy: { pill: 'bg-success/15 text-success border-success/30', dot: 'bg-success', label: 'Healthy' },
    warning: { pill: 'bg-warning/15 text-warning border-warning/30', dot: 'bg-warning', label: 'Warning' },
    critical: { pill: 'bg-danger/15 text-danger border-danger/30', dot: 'bg-danger', label: 'Critical' },
    offline: { pill: 'bg-surface2 text-muted border-border', dot: 'bg-muted', label: 'Offline' },
    maintenance: { pill: 'bg-primary/15 text-primary border-primary/30', dot: 'bg-primary', label: 'Maintenance' },
  }[health]

  /* Primary row (5 metadata fields) */
  const primary: Array<{ label: string; value: string }> = [
    { label: 'IP Address', value: device.ip_address || '—' },
    { label: 'Type', value: titleCase((device.device_type || 'other').replace('_', ' ')) },
    { label: 'Location', value: device.location || '—' },
    { label: 'Vendor / Model', value: [device.vendor, device.model].filter(Boolean).join(' ') || '—' },
    { label: 'OS / Version', value: device.os_version || '—' },
  ]

  /* Secondary row */
  const uptimeSec = readUptimeSeconds(device)
  const uptimeDisplay =
    uptimeSec != null
      ? formatDuration(uptimeSec)
      : availabilityPct !== undefined
        ? `${availabilityPct.toFixed(availabilityPct >= 99.95 ? 1 : 2)}% (${rangePhrase(range.label)})`
        : '—'
  const secondary: Array<{ icon: React.ComponentType<{ className?: string }>; label: string; value: string; color?: string }> = [
    { icon: Clock, label: 'Uptime', value: uptimeDisplay, color: 'text-success' },
    { icon: Activity, label: 'Last Seen', value: relativeTime(device.last_seen), color: 'text-muted' },
    { icon: HardDrive, label: 'Serial Number', value: device.serial_number || '—', color: 'text-muted' },
    { icon: GitBranch, label: 'Firmware Version', value: device.firmware_version || device.os_version || '—', color: 'text-muted' },
    { icon: MapPin, label: device.group_name ? '' : 'Group', value: device.group_name || '—', color: 'text-muted' },
  ]

  return (
    <Card>
      <CardContent className="p-4 md:p-5">
        <div className="flex flex-wrap items-start gap-4">
          {/* Left: identity + primary metadata */}
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border border-primary/30 bg-primary/10 text-primary">
              <Icon className="h-6 w-6" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="truncate text-[22px] font-bold tracking-tight">{device.hostname}</h2>
                <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${kind.pill}`}>
                  <span className={`h-1.5 w-1.5 rounded-full ${kind.dot}`} />
                  {kind.label}
                </span>
              </div>

              {/* Primary metadata row */}
              <div className="mt-2 flex flex-wrap items-start gap-x-6 gap-y-1">
                {primary.map((m) => (
                  <div key={m.label} className="min-w-0">
                    <div className="truncate text-sm font-medium text-text" title={m.value}>{m.value}</div>
                    <div className="text-[10px] uppercase tracking-wider text-muted">{m.label}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Right: actions + secondary metadata stacked below */}
          <div className="flex flex-col items-end gap-2">
            <div className="flex flex-wrap items-center justify-end gap-2">
              {rangePicker}
              {rangePicker && <span className="hidden h-5 w-px bg-border sm:inline-block" />}
              <Button variant="outline" size="default" className="h-9" onClick={onEdit}>
                <Pencil className="h-4 w-4" />
                Edit Device
              </Button>
              {onMaintenance && (
                <Button variant="outline" size="default" className="h-9" onClick={onMaintenance}>
                  <Wrench className="h-4 w-4" />
                  Maintenance
                </Button>
              )}
              <Button
                variant="outline"
                size="default"
                className="h-9"
                disabled={diagnostics.isPending}
                onClick={() => { setDiagOpen(true); diagnostics.mutate() }}
              >
                {diagnostics.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
                Run Diagnostics
              </Button>
              <Button
                variant="outline"
                size="icon"
                className="h-9 w-9 text-muted hover:text-danger"
                onClick={onDelete}
                title="Delete device"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>

            {/* Secondary metadata — under the action buttons */}
            <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-1 text-[11px]">
              {secondary.map((s, i) => (
                <span key={i} className="inline-flex items-center gap-1.5">
                  <s.icon className={`h-3 w-3 ${s.color || 'text-muted'}`} />
                  {s.label && <span className="text-muted">{s.label}</span>}
                  <span className="font-medium text-text">{s.value}</span>
                </span>
              ))}
            </div>
          </div>
        </div>
      </CardContent>

      <DiagnosticsDialog
        open={diagOpen}
        onOpenChange={setDiagOpen}
        running={diagnostics.isPending}
        result={diagResult}
        snmpEnabled={!!device.snmp_enabled}
        onRerun={() => diagnostics.mutate()}
      />
    </Card>
  )
}

/* ════════════════════════════════════════════════════════════
   Diagnostics — ping + SNMP reachability, run on demand
   ════════════════════════════════════════════════════════════ */

type DiagnosticsResult = { ping: any; snmp: any | null }

function DiagnosticsDialog({
  open, onOpenChange, running, result, snmpEnabled, onRerun,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  running: boolean
  result: DiagnosticsResult | null
  snmpEnabled: boolean
  onRerun: () => void
}) {
  const ping = result?.ping
  const snmp = result?.snmp

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Zap className="h-5 w-5 text-primary" />
            Diagnostics
          </DialogTitle>
          <DialogDescription>Live reachability checks run from the ZenPlus server.</DialogDescription>
        </DialogHeader>

        {running || !result ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" />
            Running checks…
          </div>
        ) : (
          <div className="space-y-2">
            <DiagnosticRow
              icon={Activity}
              label="ICMP ping"
              ok={!!ping?.ok}
              detail={
                ping?.ok
                  ? `${ping.received}/${ping.transmitted} replies · ${ping.rtt_avg_ms != null ? `${ping.rtt_avg_ms.toFixed(1)} ms avg` : 'no RTT'}`
                  : ping?.reason || 'No reply'
              }
            />
            {snmpEnabled ? (
              <DiagnosticRow
                icon={Shield}
                label="SNMP GET"
                ok={!!snmp?.ok}
                detail={snmp?.ok ? (snmp.sys_descr || 'Responded') : snmp?.reason || 'No response'}
              />
            ) : (
              <DiagnosticRow icon={Shield} label="SNMP GET" ok={null} detail="SNMP is disabled for this device" />
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
          <Button size="sm" disabled={running} onClick={onRerun}>
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            Run again
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DiagnosticRow({
  icon: Icon, label, ok, detail,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  ok: boolean | null
  detail: string
}) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-border bg-surface2/40 p-3">
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${ok == null ? 'text-muted' : ok ? 'text-success' : 'text-danger'}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{label}</span>
          <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider ${
            ok == null ? 'bg-surface2 text-muted' : ok ? 'bg-success/15 text-success' : 'bg-danger/15 text-danger'
          }`}>
            {ok == null ? 'Skipped' : ok ? 'Pass' : 'Fail'}
          </span>
        </div>
        <div className="mt-0.5 break-words text-xs text-muted">{detail}</div>
      </div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   Dashboard grid
   ════════════════════════════════════════════════════════════ */

const TIME_RANGES = [
  { label: 'Last Hour', hours: 1 },
  { label: 'Last 6 Hours', hours: 6 },
  { label: 'Last 24 Hours', hours: 24 },
  { label: 'Last 7 Days', hours: 168 },
]

type MetricSeriesMap = Record<string, { unit?: string; points: { ts: number; value: number }[] }>

type StatusHistoryEvent = {
  old_status: string
  new_status: string
  reason?: string
  timestamp: string
  duration_sec?: number | null
}

type ActivityEvent = {
  id: string
  timestamp: string
  severity: 'critical' | 'warning' | 'info' | 'success'
  icon: React.ComponentType<{ className?: string }>
  title: string
  subtitle?: string
}

type NetflowOverview = {
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

type DeviceAlert = {
  id: string
  status: 'active' | 'acknowledged' | 'resolved'
  severity: string
  message: string
  triggered_at: string
}

type NetflowSeriesPoint = { ts: number; bps: number; bytes: number; packets: number; flows: number }
type NetflowApplication = { name: string; bytes: number; packets: number; flows: number }
type NetflowConversation = { src: string; dst: string; protocol_name: string; dst_port: number; service: string; bytes: number; packets: number; flows: number }

function DashboardSection({
  device, deviceId, range,
}: {
  device: any
  deviceId: string
  range: { hours: number; fromISO: string; toISO: string; isCustom: boolean; label: string }
}) {
  const snmp = !!device.snmp_enabled
  const hoursRange = range.hours
  // Chart x-domains are the selected window, not the extent of the returned
  // samples — sparse data must read as sparse, not as full coverage.
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const [eventsOpen, setEventsOpen] = useState(false)
  const [healthOpen, setHealthOpen] = useState(false)
  const [inventoryOpen, setInventoryOpen] = useState(false)

  const { data: pingData } = useQuery<{
    points: { timestamp: string; rtt_ms: number; packet_loss: number; jitter_ms: number; is_up: boolean }[]
  }>({
    queryKey: ['device', deviceId, 'ping-metrics', range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(
        `/devices/${deviceId}/metrics?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}`,
      )).data,
    refetchInterval: 30_000,
    enabled: device.ping_enabled,
  })

  const { data: metrics } = useQuery<MetricSeriesMap>({
    queryKey: ['device', deviceId, 'snmp-metrics', hoursRange],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-metrics?hours=${hoursRange}`)).data,
    refetchInterval: 30_000,
    enabled: snmp,
  })

  const { data: ifs } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'interfaces'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    refetchInterval: 30_000,
    enabled: snmp,
  })
  // The endpoint accepts up to 720h and buckets server-side; follow the page
  // range so the tiles below aren't labelled 1M while showing 24h of data.
  const ifMetricHours = Math.min(720, Math.max(1, Math.round(hoursRange)))
  const { data: ifMetrics } = useQuery<Record<string, any[]>>({
    queryKey: ['device', deviceId, 'if-metrics', ifMetricHours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-if-metrics?hours=${ifMetricHours}`)).data,
    refetchInterval: 30_000,
    enabled: snmp,
  })

  const { data: entities } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'entities'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/entities`)).data,
    enabled: snmp,
  })
  const { data: sensors } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'sensors'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/sensors`)).data,
    enabled: snmp,
  })
  // Cap to the traps endpoint's max (720h). Custom ranges may exceed it.
  const trapHours = Math.min(720, Math.max(1, Math.round(hoursRange)))
  const { data: traps } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'traps-summary', trapHours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/traps?hours=${trapHours}&limit=20`)).data,
    refetchInterval: 30_000,
    enabled: snmp,
  })

  const { data: statusHistory } = useQuery<StatusHistoryEvent[]>({
    queryKey: ['device', deviceId, 'status-history', range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(
        `/devices/${deviceId}/status-history?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}&limit=100`,
      )).data,
    refetchInterval: 30_000,
  })

  const netflowExporter = normalizeIp(device.ip_address)
  const netflowQS = useMemo(() => {
    const params = new URLSearchParams({ hours: String(Math.max(1, Math.round(range.hours))) })
    params.set('exporter', netflowExporter)
    if (range.isCustom) {
      params.set('from', range.fromISO)
      params.set('to', range.toISO)
    }
    return params.toString()
  }, [netflowExporter, range.hours, range.isCustom, range.fromISO, range.toISO])

  const { data: netflowOverview } = useQuery<NetflowOverview>({
    queryKey: ['device', deviceId, 'netflow-overview', netflowQS],
    queryFn: async () => (await api.get(`/netflow/overview?${netflowQS}`)).data,
    refetchInterval: range.isCustom ? false : 30_000,
    enabled: !!netflowExporter,
  })
  const hasNetflow = (netflowOverview?.flows || 0) > 0
  const { data: netflowSeries } = useQuery<NetflowSeriesPoint[]>({
    queryKey: ['device', deviceId, 'netflow-timeseries', netflowQS],
    queryFn: async () => (await api.get(`/netflow/timeseries?${netflowQS}`)).data,
    refetchInterval: range.isCustom ? false : 30_000,
    enabled: hasNetflow,
  })
  const { data: netflowApplications } = useQuery<NetflowApplication[]>({
    queryKey: ['device', deviceId, 'netflow-applications', netflowQS],
    queryFn: async () => (await api.get(`/netflow/applications?${netflowQS}`)).data,
    refetchInterval: range.isCustom ? false : 60_000,
    enabled: hasNetflow,
  })
  const { data: netflowConversations } = useQuery<NetflowConversation[]>({
    queryKey: ['device', deviceId, 'netflow-conversations', netflowQS],
    queryFn: async () => (await api.get(`/netflow/top-conversations?${netflowQS}&limit=5`)).data,
    refetchInterval: range.isCustom ? false : 30_000,
    enabled: hasNetflow,
  })

  /* Derived series / KPIs */
  const cpu = metrics?.cpu
  const mem = metrics?.memory
  const cpuVal = cpu?.points?.length ? cpu.points[cpu.points.length - 1].value : null
  const memVal = mem?.points?.length ? mem.points[mem.points.length - 1].value : null

  const pts = pingData?.points || []
  const rttPts = pts.map((p) => ({ ts: new Date(p.timestamp).getTime(), rtt: p.rtt_ms, loss: p.packet_loss, jitter: p.jitter_ms }))
  const lastRtt = rttPts.length ? rttPts[rttPts.length - 1].rtt : device.last_rtt_ms
  const avgLoss = rttPts.length ? rttPts.reduce((s, p) => s + p.loss, 0) / rttPts.length : null

  /* Total bandwidth across interfaces (sum of last in+out bps) */
  const bwSeries = useMemo(() => bandwidthSeries(ifMetrics || {}), [ifMetrics])
  const lastBw = bwSeries.length ? bwSeries[bwSeries.length - 1].value : 0

  const ifUp = (ifs || []).filter((i) => i.oper_status === 'up').length
  const ifTotal = ifs?.length || 0

  /* Interface utilization: highest active util, as % */
  const ifUtilPct = useMemo(() => {
    const s = ifs || []
    let best = 0
    s.forEach((i) => {
      const m = ifMetrics?.[i.if_index]
      if (!m?.length || !i.if_speed) return
      const l = m[m.length - 1]
      const total = (l.in_bps || 0) + (l.out_bps || 0)
      const u = total / Number(i.if_speed) * 100
      if (u > best) best = u
    })
    return Math.min(100, best)
  }, [ifs, ifMetrics])

  const uptimeSec = readUptimeSeconds(device, metrics)
  const uptimeDaysCompact = uptimeSec != null ? formatDaysCompact(uptimeSec) : '—'

  // Availability % over the active time-filter window — pulled from ClickHouse
  // ping aggregation. Re-fetches whenever range.hours changes.
  const { data: uptimeStats } = useQuery<{ devices: Record<string, number> }>({
    queryKey: ['uptime-stats', range.hours],
    queryFn: async () =>
      (await api.get(`/devices/dashboard/uptime-stats?hours=${Math.max(1, Math.round(range.hours))}`)).data,
    staleTime: 30_000,
  })
  const availabilityPct = deviceId && uptimeStats?.devices ? uptimeStats.devices[deviceId] : undefined
  const availabilityLabel =
    availabilityPct === undefined
      ? '—'
      : `${availabilityPct.toFixed(availabilityPct >= 99.95 ? 1 : 2)}%`

  const cpuTrend = percentTrend(cpu?.points?.map((p) => p.value) || [])
  const memTrend = percentTrend(mem?.points?.map((p) => p.value) || [])
  const ifTrend = percentTrend(bwSeries.map((p) => p.value))
  const latTrend = percentTrend(rttPts.map((p) => p.rtt))
  const lossTrend = percentTrend(rttPts.map((p) => p.loss))

  /* Merged performance chart — CPU + Memory only. Bandwidth has its own
     "Throughput" tile in the Environmental / System Stats card. */
  const perfSeries = useMemo(() => {
    const byTs: Record<number, { ts: number; cpu?: number; mem?: number }> = {}
    ;(cpu?.points || []).forEach((p) => { byTs[p.ts] = { ...(byTs[p.ts] || {}), ts: p.ts, cpu: p.value } })
    ;(mem?.points || []).forEach((p) => { byTs[p.ts] = { ...(byTs[p.ts] || {}), ts: p.ts, mem: p.value } })
    return Object.values(byTs).sort((a, b) => a.ts - b.ts)
  }, [cpu, mem])

  const cpuSeries = cpu?.points?.map((p) => p.value) || []
  const memSeries = mem?.points?.map((p) => p.value) || []
  const latSeries = rttPts.map((p) => p.rtt)
  const lossSeries = rttPts.map((p) => p.loss)
  const bwMbpsSeries = bwSeries.map((p) => p.value / 1_000_000)
  // Real availability sparkline: 100 when the check was up, 0 when it was down.
  const uptimeSpark = pts.map((p) => (isUpPoint(p.is_up) ? 100 : 0))

  const perfStats = {
    cpu: { avg: avg(cpuSeries), max: Math.max(0, ...cpuSeries) },
    mem: { avg: avg(memSeries), max: Math.max(0, ...memSeries) },
  }

  const healthScore = computeHealthScore({
    status: device.status, cpu: cpuVal, mem: memVal, loss: avgLoss, ifUp, ifTotal,
  })

  /* Real alerts raised against this device. The card used to list SNMP traps
     under an "Alerts" heading, which is a different thing entirely. */
  const { data: alertsResp } = useQuery<{ data: DeviceAlert[]; meta: { total: number } }>({
    queryKey: ['device', deviceId, 'alerts'],
    queryFn: async () => (await api.get(`/alerts?device_id=${deviceId}&limit=20`)).data,
    refetchInterval: 30_000,
  })
  const deviceAlerts = alertsResp?.data || []
  // The list above is capped at 20 — ask the API for the real active count so
  // the Acknowledge button can't understate what it is about to change.
  const { data: activeAlertsResp } = useQuery<{ meta: { total: number } }>({
    queryKey: ['device', deviceId, 'alerts', 'active-count'],
    queryFn: async () => (await api.get(`/alerts?device_id=${deviceId}&status=active&limit=1`)).data,
    refetchInterval: 30_000,
  })
  const activeAlertCount = activeAlertsResp?.meta?.total ?? 0
  // Open alerts first (that's what needs attention), then recent history.
  // Legacy rows stored the rendered SMS template as the message — strip the
  // "[ZenPlus SEVERITY]" transport prefix so the list reads as events.
  const recentAlerts = useMemo(() => {
    const rank = (s: string) => (s === 'active' ? 0 : s === 'acknowledged' ? 1 : 2)
    return [...deviceAlerts]
      .sort((a, b) => rank(a.status) - rank(b.status) || Date.parse(b.triggered_at) - Date.parse(a.triggered_at))
      .slice(0, 5)
      .map((a) => ({
        id: a.id,
        severity: normalizeSeverity(a.severity),
        title: (a.message || 'Alert').replace(/^\[ZenPlus\s+[A-Z]+\]\s*/, ''),
        ago: relativeTime(a.triggered_at),
        acknowledged: a.status === 'acknowledged',
        resolved: a.status === 'resolved',
      }))
  }, [deviceAlerts])
  const activityEvents = useMemo(
    () => buildActivityEvents(traps || [], statusHistory || [], metrics || {}, range.fromISO, range.toISO),
    [traps, statusHistory, metrics, range.fromISO, range.toISO],
  )

  // The activity card is range-bound; a quiet hour on a healthy device showed
  // a permanently-empty timeline that read as broken. When the selected range
  // has nothing, pull the latest events from a wide window and say so.
  const wantsEventFallback = activityEvents.length === 0 && statusHistory !== undefined
  const wideFrom = useMemo(() => new Date(Date.now() - 720 * 3600_000).toISOString(), [wantsEventFallback])
  const { data: fbTraps } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'traps-fallback'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/traps?hours=720&limit=10`)).data,
    enabled: snmp && wantsEventFallback,
    staleTime: 60_000,
  })
  const { data: fbStatus } = useQuery<StatusHistoryEvent[]>({
    queryKey: ['device', deviceId, 'status-history-fallback'],
    queryFn: async () =>
      (await api.get(
        `/devices/${deviceId}/status-history?from=${encodeURIComponent(wideFrom)}&to=${encodeURIComponent(new Date().toISOString())}&limit=20`,
      )).data,
    enabled: wantsEventFallback,
    staleTime: 60_000,
  })
  const fallbackEvents = useMemo(
    () => (wantsEventFallback ? buildActivityEvents(fbTraps || [], fbStatus || [], {}, wideFrom, range.toISO) : []),
    [wantsEventFallback, fbTraps, fbStatus, wideFrom, range.toISO],
  )

  return (
    <>
      {/* ═══════════ KPI row (6 cards) ═══════════ */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <KpiTile
          icon={<Cpu className="h-4 w-4" />}
          label="CPU Usage"
          value={cpuVal != null ? `${cpuVal.toFixed(0)}%` : '—'}
          trend={cpuTrend}
          color="info"
          invertTrend
          series={cpuSeries.slice(-22)}
        />
        <KpiTile
          icon={<MemoryStick className="h-4 w-4" />}
          label="Memory Usage"
          value={memVal != null ? `${memVal.toFixed(0)}%` : '—'}
          trend={memTrend}
          color="accent"
          invertTrend
          series={memSeries.slice(-22)}
        />
        <KpiTile
          icon={<Network className="h-4 w-4" />}
          label="Interface Utilization"
          value={ifTotal ? formatUtilPct(ifUtilPct) : '—'}
          trend={ifTrend}
          trendLabel="total throughput vs. earlier in range"
          color="success"
          invertTrend
          series={bwMbpsSeries.slice(-22)}
          subtitle={ifTotal ? `Busiest of ${ifTotal} interfaces` : undefined}
        />
        <KpiTile
          icon={<Activity className="h-4 w-4" />}
          label="Latency"
          value={lastRtt != null ? `${Number(lastRtt).toFixed(0)} ms` : '—'}
          trend={latTrend}
          color="info"
          invertTrend
          series={latSeries.slice(-22)}
        />
        <KpiTile
          icon={<ZapOff className="h-4 w-4" />}
          label="Packet Loss"
          value={avgLoss != null ? `${avgLoss.toFixed(1)}%` : '—'}
          trend={lossTrend}
          // Zero loss is a healthy state — don't paint it in the danger colour.
          color={avgLoss == null ? 'info' : avgLoss === 0 ? 'success' : avgLoss < 2 ? 'warning' : 'danger'}
          invertTrend
          series={lossSeries.slice(-22)}
          subtitle={avgLoss != null ? `Avg over ${rangePhrase(range.label)}` : undefined}
        />
        <KpiTile
          icon={<Clock className="h-4 w-4" />}
          label={`Uptime (${range.label})`}
          value={availabilityLabel}
          trend={null}
          color={
            availabilityPct === undefined
              ? 'warning'
              : availabilityPct >= 99.9
                ? 'success'
                : availabilityPct >= 95
                  ? 'warning'
                  : 'danger'
          }
          series={uptimeSpark}
          // Device boot time, distinct from the availability % above it.
          subtitle={uptimeSec != null ? `Booted ${uptimeDaysCompact} ago` : undefined}
        />
      </div>

      {/* ═══════════ Availability timeline (full width) ═══════════ */}
      {device.ping_enabled && (
        <AvailabilityTimelineCard points={pts} rangeLabel={range.label} fromTs={fromTs} toTs={toTs} />
      )}

      {/* ═══════════ Middle row (3 cols) ═══════════ */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,0.95fr)_minmax(0,0.8fr)]">
        <PerformanceOverviewCard
          series={perfSeries}
          stats={perfStats}
          rangeLabel={range.label}
          rangeHours={range.hours}
          fromTs={fromTs}
          toTs={toTs}
        />
        <InterfaceStatusCard
          ifs={ifs || []}
          ifMetrics={ifMetrics || {}}
          deviceId={deviceId}
        />
        <HealthScoreCard
          score={healthScore}
          alerts={recentAlerts}
          totalAlerts={deviceAlerts.length}
          deviceId={deviceId}
          metrics={metrics || {}}
          sensors={sensors || []}
          memVal={memVal}
          cpuVal={cpuVal}
          avgLoss={avgLoss}
          onViewDetails={() => setHealthOpen(true)}
        />
      </div>

      {hasNetflow && (
        <DeviceNetflowCard
          exporterIp={netflowExporter}
          overview={netflowOverview!}
          series={netflowSeries || []}
          applications={netflowApplications || []}
          conversations={netflowConversations || []}
          rangeLabel={range.label}
          rangeHours={range.hours}
          fromTs={fromTs}
          toTs={toTs}
        />
      )}

      {/* ═══════════ Bottom row ═══════════ */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <InventoryConfigCard
          device={device}
          entities={entities || []}
          onDetails={() => setInventoryOpen(true)}
        />
        <ActivityLogCard
          events={activityEvents.length > 0 ? activityEvents : fallbackEvents}
          rangeLabel={range.label}
          showingFallback={activityEvents.length === 0 && fallbackEvents.length > 0}
          onViewAll={() => setEventsOpen(true)}
        />
        <EnvironmentalActionsCard
          deviceId={deviceId}
          snmpEnabled={snmp}
          metrics={metrics || {}}
          sensors={sensors || []}
          lastBw={lastBw}
          openAlerts={activeAlertCount}
        />
      </div>

      <EventsDialog
        open={eventsOpen}
        onOpenChange={setEventsOpen}
        deviceId={deviceId}
        initialHours={trapHours}
        rangeLabel={range.label}
      />
      <InventoryDialog open={inventoryOpen} onOpenChange={setInventoryOpen} entities={entities || []} />
      <HealthDetailsDialog
        open={healthOpen}
        onOpenChange={setHealthOpen}
        device={device}
        cpu={cpuVal}
        mem={memVal}
        loss={avgLoss}
        ifUp={ifUp}
        ifTotal={ifTotal}
        score={healthScore}
      />
    </>
  )
}

/* ════════════════════════════════════════════════════════════
   KPI tile — circled icon + label + value + trend + sparkline
   ════════════════════════════════════════════════════════════ */

function KpiTile({
  icon, label, value, trend, trendLabel, color, series, invertTrend, subtitle,
}: {
  icon: React.ReactNode
  label: string
  value: string
  /** Percent change between the first and second half of the selected range. */
  trend: number | null
  /** What the trend is measuring, for the badge tooltip. */
  trendLabel?: string
  color: 'primary' | 'success' | 'warning' | 'danger' | 'info' | 'accent'
  series?: number[]
  /** Set when a rising value is bad (usage, latency, loss). */
  invertTrend?: boolean
  subtitle?: string
}) {
  const COLORS: Record<typeof color, { chip: string; stroke: string; from: string; to: string }> = {
    primary: { chip: 'bg-primary/10 text-primary', stroke: 'rgb(var(--primary))', from: 'rgb(var(--primary) / 0.35)', to: 'rgb(var(--primary) / 0)' },
    success: { chip: 'bg-success/10 text-success', stroke: 'rgb(var(--success))', from: 'rgb(var(--success) / 0.35)', to: 'rgb(var(--success) / 0)' },
    warning: { chip: 'bg-warning/10 text-warning', stroke: 'rgb(var(--warning))', from: 'rgb(var(--warning) / 0.35)', to: 'rgb(var(--warning) / 0)' },
    danger:  { chip: 'bg-danger/10 text-danger',   stroke: 'rgb(var(--danger))',  from: 'rgb(var(--danger) / 0.35)',  to: 'rgb(var(--danger) / 0)' },
    info:    { chip: 'bg-info/10 text-info',       stroke: 'rgb(var(--info))',    from: 'rgb(var(--info) / 0.35)',    to: 'rgb(var(--info) / 0)' },
    accent:  { chip: 'bg-accent/10 text-accent',   stroke: 'rgb(var(--accent))',  from: 'rgb(var(--accent) / 0.35)',  to: 'rgb(var(--accent) / 0)' },
  }
  const c = COLORS[color]
  // A change under 0.05% rounds to "0.0%" — render it as flat rather than
  // colouring an arrow that points at nothing.
  const flat = trend != null && Math.abs(trend) < 0.05
  const good = trend == null || flat ? null : invertTrend ? trend < 0 : trend > 0

  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-border bg-surface p-3 transition-colors hover:border-border-strong">
      <div className="flex items-center gap-2">
        <span className={`inline-flex h-7 w-7 items-center justify-center rounded-full ${c.chip}`}>{icon}</span>
        <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</span>
      </div>

      <div className="flex items-baseline gap-2">
        <div className="text-[26px] font-bold leading-none tabular-nums" style={{ color: c.stroke }}>{value}</div>
        {trend != null && (
          <span
            className={`inline-flex items-center gap-0.5 text-[11px] font-medium tabular-nums ${
              good == null ? 'text-muted' : good ? 'text-success' : 'text-danger'
            }`}
            title={`${flat ? 'No change' : trend > 0 ? 'Up' : 'Down'} ${Math.abs(trend).toFixed(1)}% — ${trendLabel || 'second half of the selected range vs. the first'}`}
          >
            {flat ? <Minus className="h-3 w-3" /> : trend > 0 ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />}
            {Math.abs(trend).toFixed(1)}%
          </span>
        )}
      </div>

      {subtitle && (
        <div className="text-[10px] font-medium text-muted">{subtitle}</div>
      )}

      {series && series.length > 1 && (
        <MiniSparkline data={series} stroke={c.stroke} from={c.from} to={c.to} className="h-8 w-full" />
      )}
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   Performance Overview — wide multi-line chart + bottom stats
   ════════════════════════════════════════════════════════════ */

function PerformanceOverviewCard({
  series, stats, rangeLabel, rangeHours, fromTs, toTs,
}: {
  series: Array<{ ts: number; cpu?: number; mem?: number }>
  stats: { cpu: { avg: number; max: number }; mem: { avg: number; max: number } }
  rangeLabel: string
  rangeHours: number
  fromTs: number
  toTs: number
}) {
  const hasData = series.length > 0
  const tickFormatter = useMemo(() => deviceAxisFormatter(rangeHours), [rangeHours])
  const ticks = useMemo(() => timeTicks(fromTs, toTs, rangeHours), [fromTs, toTs, rangeHours])
  // A single sample draws no line — show its marker instead of an empty plot.
  const showDots = series.length === 1

  /* Retention and polling gaps mean the samples often cover a fraction of the
     selected window. The axis now shows that honestly, which makes a clustered
     plot look broken unless we say why. */
  const coverage = useMemo(() => {
    if (series.length < 2 || toTs <= fromTs) return null
    const span = series[series.length - 1].ts - series[0].ts
    const pct = (span / (toTs - fromTs)) * 100
    return pct < 90 ? pct : null
  }, [series, fromTs, toTs])

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-semibold">Performance Overview</h3>
          </div>
          <div className="flex items-baseline gap-2">
            {coverage != null && (
              <span
                className="text-[10px] text-muted"
                title="Samples only exist for part of the selected window — the rest of the axis has no data."
              >
                data covers {coverage < 1 ? '<1' : coverage.toFixed(0)}%
              </span>
            )}
            <span className="text-[11px] font-medium text-muted">{rangeLabel}</span>
          </div>
        </div>

        {/* Legend */}
        <div className="flex flex-wrap items-center gap-4 text-[11px] text-muted">
          <LegendDot color="rgb(var(--info))" label="CPU (%)" />
          <LegendDot color="rgb(var(--accent))" label="Memory (%)" />
        </div>

        <div className="mt-2 h-52">
          {hasData ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series} margin={{ top: 4, right: axisRightPad(rangeHours), bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                {/* Numeric time scale: samples are irregular, so a category
                    axis would space them evenly and misplace old points. */}
                <XAxis
                  dataKey="ts"
                  type="number"
                  scale="time"
                  domain={[fromTs, toTs]}
                  ticks={ticks}
                  /* We choose the ticks, so render them all rather than let
                     Recharts drop the ends to satisfy its own spacing rule. */
                  interval={0}
                  tickFormatter={tickFormatter}
                  tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis domain={[0, 100]} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} width={38} axisLine={false} tickLine={false} tickFormatter={(v) => `${v}%`} />
                <Tooltip {...ttStyle()} />
                <Line type="monotone" dataKey="cpu" name="CPU" stroke="rgb(var(--info))" strokeWidth={1.8} dot={showDots} connectNulls />
                <Line type="monotone" dataKey="mem" name="Memory" stroke="rgb(var(--accent))" strokeWidth={1.8} dot={showDots} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center text-xs text-muted">Collecting metrics…</div>
          )}
        </div>

        {/* Bottom stats */}
        <div className="mt-3 grid grid-cols-2 gap-3 border-t border-border/50 pt-3 text-xs">
          <StatBlock dotColor="rgb(var(--info))" label="CPU" avg={`${stats.cpu.avg.toFixed(0)}%`} max={`${stats.cpu.max.toFixed(0)}%`} />
          <StatBlock dotColor="rgb(var(--accent))" label="Memory" avg={`${stats.mem.avg.toFixed(0)}%`} max={`${stats.mem.max.toFixed(0)}%`} />
        </div>
      </CardContent>
    </Card>
  )
}

function StatBlock({ dotColor, label, avg, max }: { dotColor: string; label: string; avg: string; max: string }) {
  return (
    <div>
      <div className="flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full" style={{ background: dotColor }} />
        <span className="text-[11px] font-semibold">{label}</span>
      </div>
      <div className="mt-1 text-[11px] text-muted">Avg: <span className="font-medium text-text tabular-nums">{avg}</span></div>
      <div className="text-[11px] text-muted">Max: <span className="font-medium text-text tabular-nums">{max}</span></div>
    </div>
  )
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      <span>{label}</span>
    </span>
  )
}

/* ════════════════════════════════════════════════════════════
   NetFlow — shown only when this device exports flow records
   ════════════════════════════════════════════════════════════ */

function DeviceNetflowCard({
  exporterIp, overview, series, applications, conversations, rangeLabel, rangeHours, fromTs, toTs,
}: {
  exporterIp: string
  overview: NetflowOverview
  series: NetflowSeriesPoint[]
  applications: NetflowApplication[]
  conversations: NetflowConversation[]
  rangeLabel: string
  rangeHours: number
  fromTs: number
  toTs: number
}) {
  const tickFormatter = useMemo(() => deviceAxisFormatter(rangeHours), [rangeHours])
  const ticks = useMemo(() => timeTicks(fromTs, toTs, rangeHours), [fromTs, toTs, rangeHours])
  const topApp = applications[0]
  const topConversation = conversations[0]
  // Long ranges collapse into few (sometimes one) buckets. One bucket is still
  // data — plot it as a marker rather than pretending we're still collecting.
  const showDots = series.length === 1

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Network className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-semibold">NetFlow</h3>
            <span className="rounded-full border border-border bg-surface2 px-2 py-0.5 font-mono text-[10px] text-muted">{exporterIp}</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-[11px] font-medium text-muted">{rangeLabel}</span>
            <Link
              to={`/netflow/devices/${encodeURIComponent(exporterIp)}`}
              className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              Open NetFlow
              <ArrowRight className="h-3 w-3" />
            </Link>
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(280px,0.75fr)]">
          <div className="min-w-0">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {/* "Rate" is the instantaneous rate, which is 0 whenever the
                  exporter has stopped sending — say so rather than implying
                  the whole window averaged zero. */}
              <NetflowStat label="Current Rate" value={overview.current_bps > 0 ? formatBps(overview.current_bps) : 'Idle'} />
              <NetflowStat label="Volume" value={formatBytes(overview.bytes)} />
              <NetflowStat label="Flows" value={overview.flows.toLocaleString()} />
              <NetflowStat label="Last Seen" value={relativeTime(overview.last_seen)} />
            </div>

            <div className="mt-3 h-44">
              {series.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={series} margin={{ top: 4, right: axisRightPad(rangeHours), bottom: 0, left: 0 }}>
                    <defs>
                      <linearGradient id="deviceNetflowBps" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.35} />
                        <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                    <XAxis
                      dataKey="ts"
                      type="number"
                      scale="time"
                      domain={[fromTs, toTs]}
                      ticks={ticks}
                      interval={0}
                      tickFormatter={tickFormatter}
                      tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                      width={52}
                      axisLine={false}
                      tickLine={false}
                      tickFormatter={(v) => formatBpsAxis(Number(v))}
                    />
                    <Tooltip
                      {...ttStyle()}
                      formatter={(value: any, name: string) => [
                        name === 'bps' ? formatBps(Number(value)) : value,
                        name === 'bps' ? 'Throughput' : name,
                      ]}
                    />
                    <Area type="monotone" dataKey="bps" stroke="rgb(var(--primary))" strokeWidth={1.8} fill="url(#deviceNetflowBps)" dot={showDots} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full flex-col items-center justify-center gap-1 text-center">
                  <Network className="h-5 w-5 text-muted/60" />
                  <div className="text-[11px] font-medium text-text">No flow series in {rangePhrase(rangeLabel)}</div>
                  <div className="text-[10px] text-muted">Totals above cover the whole window</div>
                </div>
              )}
            </div>
          </div>

          <div className="grid content-start gap-3 text-xs">
            <div className="rounded-lg border border-border/60 bg-surface2/40 p-3">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Top Application</div>
              <div className="mt-1 truncate text-sm font-semibold text-text">{topApp?.name || '—'}</div>
              <div className="mt-1 text-muted">
                {topApp ? `${formatBytes(topApp.bytes)} · ${topApp.flows.toLocaleString()} flows` : 'No application data'}
              </div>
            </div>

            <div className="rounded-lg border border-border/60 bg-surface2/40 p-3">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Top Conversation</div>
              {topConversation ? (
                <>
                  <div className="mt-1 truncate font-mono text-[11px] text-text" title={`${topConversation.src} → ${topConversation.dst}`}>
                    {topConversation.src} → {topConversation.dst}
                  </div>
                  <div className="mt-1 text-muted">
                    {topConversation.service} · {formatBytes(topConversation.bytes)} · {topConversation.flows.toLocaleString()} flows
                  </div>
                </>
              ) : (
                <div className="mt-1 text-muted">No conversations</div>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <NetflowStat label="Source Hosts" value={overview.src_hosts.toLocaleString()} compact />
              <NetflowStat label="Dest Hosts" value={overview.dst_hosts.toLocaleString()} compact />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function NetflowStat({ label, value, compact }: { label: string; value: string; compact?: boolean }) {
  return (
    <div className={`rounded-lg border border-border/60 bg-surface2/40 ${compact ? 'p-2' : 'p-3'}`}>
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={`${compact ? 'text-sm' : 'text-base'} mt-1 font-semibold tabular-nums text-text`}>{value}</div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   Interface Status — compact table
   ════════════════════════════════════════════════════════════ */

function InterfaceStatusCard({
  ifs, ifMetrics, deviceId,
}: {
  ifs: any[]
  ifMetrics: Record<string, any[]>
  deviceId: string
}) {
  const lastBps = (idx: number) => {
    const s = ifMetrics[idx]
    if (!s?.length) return { in: 0, out: 0 }
    const l = s[s.length - 1]
    return { in: l.in_bps || 0, out: l.out_bps || 0 }
  }

  const allRows = useMemo(() => (
    ifs
      .map((i) => {
        const { in: inBps, out: outBps } = lastBps(i.if_index)
        const speed = Number(i.if_speed) || 0
        const util = speed > 0 ? Math.min(100, ((inBps + outBps) / speed) * 100) : 0
        const errors = Number(i.in_errors || 0) + Number(i.out_errors || 0)
        return { ...i, inBps, outBps, util, errors, total: inBps + outBps }
      })
      .sort((a, b) => {
        // Active-up-with-traffic first, then down, then inactive.
        if ((a.oper_status === 'up') !== (b.oper_status === 'up')) return a.oper_status === 'up' ? -1 : 1
        return b.total - a.total
      })
  ), [ifs, ifMetrics])

  const rows = allRows.slice(0, 7)
  const hasMore = allRows.length > 7

  return (
    <Card className="flex flex-col">
      <CardContent className="flex flex-1 flex-col p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Interface Status <span className="text-[11px] font-normal text-muted">({allRows.length})</span></h3>
          {hasMore ? (
            <Link
              to={`/devices/${deviceId}/interfaces`}
              className="text-xs text-primary hover:underline"
            >
              View all {allRows.length}
            </Link>
          ) : (
            <Link
              to={`/devices/${deviceId}/interfaces`}
              className="text-xs text-primary hover:underline"
            >
              Open
            </Link>
          )}
        </div>

        <div className="overflow-hidden">
          <table className="w-full text-[11px]">
            <thead className="text-[10px] uppercase tracking-wider text-muted">
              <tr className="border-b border-border/50">
                <th className="pb-1.5 pr-2 text-left font-medium">Interface</th>
                <th className="pb-1.5 px-1 text-left font-medium">Status</th>
                <th className="pb-1.5 px-1 text-left font-medium">Speed</th>
                <th className="pb-1.5 px-1 text-right font-medium">In</th>
                <th className="pb-1.5 px-1 text-right font-medium">Out</th>
                <th className="pb-1.5 px-1 text-right font-medium">Err</th>
                <th className="pb-1.5 pl-1 text-right font-medium">Util</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((i) => {
                const up = i.oper_status === 'up'
                const warn = up && i.util > 80
                const speedLabel = formatSpeed(i.if_speed)
                return (
                  <tr key={i.id || i.if_index} className="border-b border-border/30 last:border-0">
                    <td className="py-2 pr-2">
                      <div className="truncate font-medium text-text" title={i.if_name || i.if_descr}>{i.if_name || i.if_descr}</div>
                    </td>
                    <td className="py-2 px-1">
                      <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase ${
                        !up ? 'bg-danger/15 text-danger'
                          : warn ? 'bg-warning/15 text-warning'
                          : 'bg-success/15 text-success'
                      }`}>
                        <span className={`h-1 w-1 rounded-full ${!up ? 'bg-danger' : warn ? 'bg-warning' : 'bg-success'}`} />
                        {up ? (warn ? 'Warn' : 'Up') : 'Down'}
                      </span>
                    </td>
                    <td className="py-2 px-1 text-muted">{speedLabel}</td>
                    <td className="py-2 px-1 text-right font-mono text-[10px] tabular-nums">{formatBpsShort(i.inBps)}</td>
                    <td className="py-2 px-1 text-right font-mono text-[10px] tabular-nums">{formatBpsShort(i.outBps)}</td>
                    <td className="py-2 px-1 text-right font-mono text-[10px] tabular-nums">{i.errors}</td>
                    <td className="py-2 pl-1">
                      <div className="flex items-center justify-end gap-1.5">
                        <div className="h-1.5 w-12 overflow-hidden rounded-full bg-surface2">
                          <div
                            className={`h-full rounded-full ${
                              i.util > 80 ? 'bg-danger' : i.util > 50 ? 'bg-warning' : 'bg-success'
                            }`}
                            /* Idle links get no bar at all; anything carrying
                               traffic gets a visible minimum sliver. */
                            style={{ width: i.util <= 0 ? '0%' : `${Math.max(4, i.util)}%` }}
                          />
                        </div>
                        <span className="w-8 text-right text-[10px] tabular-nums">{formatUtilPct(i.util)}</span>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {rows.length === 0 && (
                <tr><td colSpan={7} className="py-6 text-center text-muted">No interfaces discovered yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

/* ════════════════════════════════════════════════════════════
   Health Score — gauge + chip row + Recent Alerts
   ════════════════════════════════════════════════════════════ */

function HealthScoreCard({
  score, alerts, totalAlerts, deviceId, metrics, sensors, memVal, cpuVal, avgLoss, onViewDetails,
}: {
  score: number
  alerts: Array<{ id: string; severity: 'critical' | 'warning' | 'info' | 'success'; title: string; ago: string; acknowledged: boolean; resolved?: boolean }>
  totalAlerts: number
  deviceId: string
  metrics: Record<string, { points: { ts: number; value: number }[] }>
  sensors: any[]
  memVal: number | null
  cpuVal: number | null
  avgLoss: number | null
  onViewDetails: () => void
}) {
  const color =
    score >= 80 ? 'rgb(var(--success))'
    : score >= 60 ? 'rgb(var(--warning))'
    : 'rgb(var(--danger))'
  const label =
    score >= 80 ? 'Excellent' : score >= 60 ? 'Good' : score >= 40 ? 'Fair' : 'Poor'

  // Summary health chips — only render chips for sensors that are actually
  // present. No hardcoded "Normal" / "OK" fallbacks.
  const tempVal = latestSensor(metrics, sensors, ['temperature', 'celsius'])
  const fanVal = latestSensor(metrics, sensors, ['fan', 'rpm'])
  const voltVal = latestSensor(metrics, sensors, ['voltage', 'volts'])

  type ChipTone = 'success' | 'warning' | 'danger' | 'info' | 'accent' | 'muted'
  const chips: Array<{ icon: React.ComponentType<{ className?: string }>; label: string; value: string; tone: ChipTone }> = []
  if (tempVal != null) {
    chips.push({
      icon: Thermometer,
      label: 'Temperature',
      value: `${tempVal.toFixed(0)}°C`,
      tone: tempVal > 70 ? 'danger' : tempVal > 55 ? 'warning' : 'success',
    })
  }
  if (voltVal != null) {
    chips.push({
      icon: Power,
      label: 'Voltage',
      value: `${voltVal.toFixed(1)} V`,
      tone: 'success',
    })
  }
  if (fanVal != null) {
    chips.push({
      icon: Fan,
      label: 'Fan',
      value: fanVal > 100 ? `${fanVal.toFixed(0)} rpm` : `${fanVal.toFixed(0)}%`,
      tone: 'info',
    })
  }
  // The score is driven by CPU, memory and loss — show those alongside any
  // environmental readings so the gauge isn't sitting next to empty space.
  if (cpuVal != null) {
    chips.push({
      icon: Cpu,
      label: 'CPU',
      value: `${cpuVal.toFixed(0)}%`,
      tone: cpuVal > 90 ? 'danger' : cpuVal > 75 ? 'warning' : 'info',
    })
  }
  if (memVal != null) {
    chips.push({
      icon: MemoryStick,
      label: 'Memory',
      value: `${memVal.toFixed(0)}%`,
      tone: memVal > 85 ? 'danger' : memVal > 70 ? 'warning' : 'accent',
    })
  }
  if (avgLoss != null) {
    chips.push({
      icon: ZapOff,
      label: 'Loss',
      value: `${avgLoss.toFixed(1)}%`,
      tone: avgLoss >= 2 ? 'danger' : avgLoss > 0 ? 'warning' : 'success',
    })
  }

  return (
    <Card className="flex flex-col">
      <CardContent className="flex flex-1 flex-col p-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Health Score</h3>
          <button
            type="button"
            onClick={onViewDetails}
            className="text-xs text-primary hover:underline"
          >
            View Details
          </button>
        </div>

        <div className="flex items-center gap-4">
          <div className="flex flex-col items-center">
            <HealthGauge value={score} color={color} />
            <div className="mt-1 text-[11px] font-semibold" style={{ color }}>{label}</div>
          </div>
          <div className="grid flex-1 grid-cols-2 gap-2">
            {chips.length === 0 ? (
              <div className="col-span-2 rounded-md border border-border bg-surface2/30 px-3 py-3 text-center text-[11px] text-muted">
                No sensor data
              </div>
            ) : (
              chips.map((c, i) => <ChipTile key={i} {...c} />)
            )}
          </div>
        </div>

        {/* Recent Alerts */}
        <div className="mt-4 border-t border-border/60 pt-3">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="text-xs font-semibold">
              Recent Alerts
              {totalAlerts > 0 && <span className="ml-1 font-normal text-muted">({totalAlerts})</span>}
            </h4>
            <Link to={`/alerts?device_id=${deviceId}`} className="text-[11px] text-primary hover:underline">
              View All
            </Link>
          </div>
          <div className="space-y-1.5">
            {alerts.length === 0 && (
              <div className="py-3 text-center text-[11px] text-muted">No alerts for this device</div>
            )}
            {alerts.map((a) => (
              <Link
                key={a.id}
                to={`/alerts/${a.id}`}
                className={`flex items-center gap-2 rounded px-1 py-0.5 -mx-1 hover:bg-surface2/60 ${a.resolved ? 'opacity-55' : ''}`}
              >
                <span className={`inline-flex items-center gap-0.5 rounded-sm px-1 text-[9px] font-semibold uppercase tracking-wider ${
                  a.severity === 'critical' ? 'bg-danger/15 text-danger'
                  : a.severity === 'warning' ? 'bg-warning/15 text-warning'
                  : a.severity === 'success' ? 'bg-success/15 text-success'
                  : 'bg-info/15 text-info'
                }`}>
                  {a.severity === 'critical' ? 'CRIT'
                  : a.severity === 'warning' ? 'WARN'
                  : a.severity === 'success' ? 'OK'
                  : 'INFO'}
                </span>
                <span className="min-w-0 flex-1 truncate text-[11px]" title={a.title}>{a.title}</span>
                {a.resolved && (
                  <span className="shrink-0 rounded-sm bg-success/10 px-1 text-[9px] font-medium text-success">resolved</span>
                )}
                {a.acknowledged && <CheckCircle2 className="h-3 w-3 shrink-0 text-muted" aria-label="Acknowledged" />}
                <span className="shrink-0 text-[10px] text-muted">{a.ago}</span>
              </Link>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function ChipTile({ icon: Icon, label, value, tone }: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: string
  tone: 'success' | 'warning' | 'danger' | 'info' | 'accent' | 'muted'
}) {
  const MAP: Record<typeof tone, string> = {
    success: 'border-success/30 bg-success/5 text-success',
    warning: 'border-warning/30 bg-warning/5 text-warning',
    danger:  'border-danger/30 bg-danger/5 text-danger',
    info:    'border-info/30 bg-info/5 text-info',
    accent:  'border-accent/30 bg-accent/5 text-accent',
    muted:   'border-border bg-surface2 text-muted',
  }
  return (
    <div className={`flex flex-col items-center justify-center gap-0.5 rounded-lg border px-2 py-2 text-center ${MAP[tone]}`}>
      <Icon className="h-3.5 w-3.5" />
      <div className="text-[13px] font-bold leading-tight tabular-nums">{value}</div>
      <div className="text-[9px] font-medium uppercase tracking-wider opacity-80">{label}</div>
    </div>
  )
}

function HealthGauge({ value, color }: { value: number; color: string }) {
  const pct = Math.min(100, Math.max(0, value))
  const size = 120
  const r = (size - 12) / 2
  const cx = size / 2
  const cy = size / 2
  const start = 135
  const end = 405
  const sweep = end - start
  const filledEnd = start + sweep * (pct / 100)

  const polar = (angle: number) => {
    const rad = ((angle - 90) * Math.PI) / 180
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
  }
  const arc = (a: number, b: number) => {
    const s = polar(a)
    const e = polar(b)
    const large = b - a > 180 ? 1 : 0
    return `M ${s.x} ${s.y} A ${r} ${r} 0 ${large} 1 ${e.x} ${e.y}`
  }

  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <path d={arc(start, end)} fill="none" stroke="rgb(var(--surface2))" strokeWidth={9} strokeLinecap="round" />
        {pct > 0 && (
          <path
            d={arc(start, filledEnd)}
            fill="none"
            stroke={color}
            strokeWidth={9}
            strokeLinecap="round"
            style={{ filter: `drop-shadow(0 0 4px ${color})`, transition: 'all 0.6s ease' }}
          />
        )}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center pt-1">
        <div className="text-[30px] font-bold leading-none" style={{ color }}>{pct.toFixed(0)}</div>
        <div className="text-[9px] tabular-nums text-muted">/100</div>
      </div>
    </div>
  )
}

/* Connected Topology — removed. Real CDP/LLDP neighbour discovery is
   not yet implemented, so rather than show a hard-coded diagram we hide
   this card entirely. A future TODO is to walk LLDP-MIB or CDP-MIB on
   each poll and reconstruct real neighbours. */

/* ════════════════════════════════════════════════════════════
   Device Inventory & Configuration
   ════════════════════════════════════════════════════════════ */

function InventoryConfigCard({
  device, entities, onDetails,
}: { device: any; entities: any[]; onDetails: () => void }) {
  const snmp = !!device.snmp_enabled
  const rows: Array<{ icon: React.ComponentType<{ className?: string }>; label: string; value: React.ReactNode }> = [
    { icon: Network, label: 'Management IP', value: device.ip_address || '—' },
    { icon: Shield, label: 'SNMP', value: snmp ? `v${device.snmp_version} · port ${device.snmp_port}` : 'Disabled' },
    { icon: Wifi, label: 'Ping', value: device.ping_enabled ? `Enabled · ${device.ping_interval}s` : 'Disabled' },
    { icon: HardDrive, label: 'Vendor / Model', value: [device.vendor, device.model].filter(Boolean).join(' ') || '—' },
    { icon: FileText, label: 'OS Version', value: device.os_version || '—' },
    { icon: Info, label: 'System OID', value: <span className="font-mono text-[10px] break-all">{device.sys_object_id || '—'}</span> },
    { icon: Layers, label: 'Hardware', value: entities.length > 0 ? `${entities.length} component${entities.length === 1 ? '' : 's'}` : '—' },
    { icon: Clock, label: 'Last Updated', value: relativeTime(device.updated_at || device.last_seen) },
  ]
  const tags: string[] = Array.isArray(device.tags) ? device.tags : []

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Device Inventory &amp; Configuration</h3>
          <button
            type="button"
            onClick={onDetails}
            disabled={entities.length === 0}
            className="text-xs text-primary hover:underline disabled:text-muted disabled:no-underline disabled:cursor-not-allowed"
          >
            Details
          </button>
        </div>
        <div className="space-y-1.5 text-[11px]">
          {rows.map((r, i) => (
            <div key={i} className="flex items-start justify-between gap-2 border-b border-border/30 pb-1.5 last:border-0">
              <span className="flex shrink-0 items-center gap-2 text-muted">
                <r.icon className="h-3.5 w-3.5" />
                {r.label}
              </span>
              <span className="min-w-0 truncate text-right font-medium">{r.value}</span>
            </div>
          ))}
          <div className="flex items-start justify-between gap-2 pt-1">
            <span className="flex items-center gap-2 text-muted">
              <TagIcon className="h-3.5 w-3.5" />
              Tags
            </span>
            <div className="flex flex-wrap justify-end gap-1">
              {tags.length === 0 ? (
                <span className="text-muted">—</span>
              ) : (
                tags.slice(0, 4).map((t) => (
                  <span key={t} className="rounded-full border border-border bg-surface2 px-1.5 py-0.5 text-[9px] font-medium">{t}</span>
                ))
              )}
              {tags.length > 4 && (
                <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[9px] font-medium text-primary">+{tags.length - 4}</span>
              )}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

/* ════════════════════════════════════════════════════════════
   Availability Timeline — horizontal green/red up/down strip
   ════════════════════════════════════════════════════════════ */

const TIMELINE_MAX_BUCKETS = 96
const TIMELINE_MIN_BUCKETS = 24
const UP_COLOR = '#22C55E'
const DOWN_COLOR = '#EF4444'

function AvailabilityTimelineCard({
  points, rangeLabel, fromTs, toTs,
}: {
  points: { timestamp: string; is_up: boolean }[]
  rangeLabel: string
  fromTs: number
  toTs: number
}) {
  const total = points.length
  const upCount = points.filter((p) => isUpPoint(p.is_up)).length
  const pct = total ? (upCount / total) * 100 : null
  const pctColor =
    pct == null ? 'text-muted' : pct > 99 ? 'text-success' : pct > 95 ? 'text-warning' : 'text-danger'

  /* Lay the checks out on the real clock. Rendering one equal-width segment
     per check made three hourly pings fill a whole month of timeline and read
     as full coverage; gaps between checks must stay visibly empty. */
  const buckets = useMemo(() => {
    const span = Math.max(1, toTs - fromTs)
    const stamps = points
      .map((p) => Date.parse(p.timestamp))
      .filter((t) => Number.isFinite(t))
      .sort((a, b) => a - b)

    /* Buckets must be at least one polling interval wide. Narrower than that
       and a perfectly healthy device alternates bar/gap purely from aliasing. */
    const gaps = stamps.slice(1).map((t, i) => t - stamps[i]).sort((a, b) => a - b)
    const medianGap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 0
    const count = medianGap > 0
      ? Math.max(TIMELINE_MIN_BUCKETS, Math.min(TIMELINE_MAX_BUCKETS, Math.round(span / medianGap)))
      : TIMELINE_MAX_BUCKETS

    const width = span / count
    const slots: Array<{ up: number; down: number; start: number }> = Array.from(
      { length: count },
      (_, i) => ({ up: 0, down: 0, start: fromTs + i * width }),
    )
    points.forEach((p) => {
      const ts = Date.parse(p.timestamp)
      if (!Number.isFinite(ts)) return
      const i = Math.min(count - 1, Math.floor((ts - fromTs) / width))
      if (i < 0) return
      if (isUpPoint(p.is_up)) slots[i].up += 1
      else slots[i].down += 1
    })
    return slots.map((s) => ({
      ...s,
      end: s.start + width,
      state: s.up + s.down === 0 ? 'gap' as const : s.down > 0 ? 'down' as const : 'up' as const,
    }))
  }, [points, fromTs, toTs])

  const coveredBuckets = buckets.filter((b) => b.state !== 'gap').length
  const coveragePct = buckets.length ? (coveredBuckets / buckets.length) * 100 : 0

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-baseline gap-2">
            <h3 className="text-sm font-semibold">Availability Timeline</h3>
            <span className="truncate text-[11px] font-medium text-muted">
              {rangeLabel}{total ? ` · ${total} check${total === 1 ? '' : 's'}` : ''}
            </span>
          </div>
          {pct != null && (
            <div className="flex items-baseline gap-2">
              {coveragePct < 95 && (
                <span
                  className="text-[10px] text-muted"
                  title="Share of the selected window that has ping data. Uptime is measured only over the checks that exist."
                >
                  {coveragePct < 1 ? '<1' : coveragePct.toFixed(0)}% of range covered
                </span>
              )}
              <span className={cn('text-xs font-mono font-medium', pctColor)}>{pct.toFixed(2)}% uptime</span>
            </div>
          )}
        </div>

        {total === 0 ? (
          <div className="flex flex-col items-center gap-1 py-6 text-center">
            <Activity className="h-5 w-5 text-muted/60" />
            <div className="text-[11px] font-medium text-text">No availability data in {rangePhrase(rangeLabel)}</div>
            <div className="text-[10px] text-muted">Ping checks for this device will appear here</div>
          </div>
        ) : (
          <>
            <div className="flex h-7 gap-[1px] overflow-hidden rounded-lg bg-surface2">
              {buckets.map((b, i) => (
                <div
                  key={i}
                  className="flex-1 transition-opacity hover:opacity-70"
                  style={{
                    backgroundColor:
                      b.state === 'up' ? UP_COLOR : b.state === 'down' ? DOWN_COLOR : 'transparent',
                  }}
                  title={
                    b.state === 'gap'
                      ? `${timeTooltipLabelFormatter(b.start)} — no data`
                      : `${timeTooltipLabelFormatter(b.start)} — ${b.state === 'up' ? 'UP' : `DOWN (${b.down} of ${b.up + b.down} checks)`}`
                  }
                />
              ))}
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span className="text-[10px] text-muted">{timeTooltipLabelFormatter(fromTs)}</span>
              <div className="flex items-center gap-3">
                <span className="flex items-center gap-1 text-[10px] text-muted">
                  <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: UP_COLOR }} />Up
                </span>
                <span className="flex items-center gap-1 text-[10px] text-muted">
                  <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: DOWN_COLOR }} />Down
                </span>
                <span className="flex items-center gap-1 text-[10px] text-muted">
                  <span className="h-2 w-2 rounded-sm bg-surface2 ring-1 ring-inset ring-border" />No data
                </span>
              </div>
              <span className="text-[10px] text-muted">{timeTooltipLabelFormatter(toTs)}</span>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/* ════════════════════════════════════════════════════════════
   Recent Events / Activity Log — vertical timeline
   ════════════════════════════════════════════════════════════ */

function ActivityLogCard({
  events: activityEvents, rangeLabel, showingFallback, onViewAll,
}: { events: ActivityEvent[]; rangeLabel: string; showingFallback?: boolean; onViewAll: () => void }) {
  const events = activityEvents.slice(0, 5).map((e) => ({
    icon: e.icon,
    tone: toneForSeverity(e.severity),
    ago: relativeTime(e.timestamp),
    at: formatEventTimestamp(e.timestamp),
    title: e.title,
    subtitle: e.subtitle || '',
  }))

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-baseline gap-2">
            <h3 className="text-sm font-semibold">Recent Events / Activity Log</h3>
            <span className="truncate text-[11px] font-medium text-muted">
              {showingFallback ? `nothing in ${rangePhrase(rangeLabel)} · showing latest` : rangeLabel}
            </span>
          </div>
          {/* Always enabled: the dialog has its own range picker, so an empty
              page range is exactly when widening the window is useful. */}
          <button
            type="button"
            onClick={onViewAll}
            className="text-xs text-primary hover:underline"
          >
            View All
          </button>
        </div>
        <div className="relative">
          {/* Vertical timeline line */}
          {events.length > 0 && <div className="absolute left-[14px] top-2 bottom-2 w-px bg-border/60" />}
          <div className="space-y-3">
            {events.length === 0 && (
              <div className="flex flex-col items-center gap-1 py-6 text-center">
                <Info className="h-5 w-5 text-muted/60" />
                <div className="text-[11px] font-medium text-text">No events in {rangePhrase(rangeLabel)}</div>
                <div className="text-[10px] text-muted">Status changes, reboots, and SNMP traps will appear here</div>
              </div>
            )}
            {events.map((e, i) => {
              const MAP: Record<typeof e.tone, string> = {
                success: 'bg-success/15 text-success ring-success/30',
                warning: 'bg-warning/15 text-warning ring-warning/30',
                danger:  'bg-danger/15 text-danger ring-danger/30',
                info:    'bg-info/15 text-info ring-info/30',
                accent:  'bg-accent/15 text-accent ring-accent/30',
              }
              const Icon = e.icon
              return (
                <div key={i} className="relative flex items-start gap-3 pl-0">
                  <span className={`relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-2 ${MAP[e.tone]}`}>
                    <Icon className="h-3.5 w-3.5" />
                  </span>
                  <div className="min-w-0 flex-1 pt-0.5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="truncate text-[11px] font-semibold" title={e.title}>{e.title}</div>
                      <div className="shrink-0 text-[10px] text-muted">{e.ago}</div>
                    </div>
                    <div className="mt-0.5 font-mono text-[10px] text-text/80">{e.at}</div>
                    {e.subtitle && (
                      <div className="truncate text-[10px] text-muted" title={String(e.subtitle)}>{e.subtitle}</div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function formatEventTimestamp(value: string): string {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value || '—'
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function iconForTrap(t: any): React.ComponentType<{ className?: string }> {
  const s = (t.trap_name || t.message || '').toLowerCase()
  if (s.includes('config')) return SettingsIcon
  if (s.includes('interface') || s.includes('link')) return Network
  if (s.includes('backup')) return Save
  if (s.includes('poll') || s.includes('snmp')) return Radar
  if (s.includes('health')) return CheckCircle2
  return Info
}

function buildActivityEvents(
  traps: any[],
  statusHistory: StatusHistoryEvent[],
  metrics: MetricSeriesMap,
  fromISO: string,
  toISO: string,
): ActivityEvent[] {
  const trapEvents = traps.map((t, i): ActivityEvent => ({
    id: `trap-${t.timestamp || i}-${t.trap_oid || t.trap_name || i}`,
    timestamp: t.timestamp,
    severity: normalizeSeverity(t.severity),
    icon: iconForTrap(t),
    title: t.trap_name || 'SNMP trap',
    subtitle: t.message || t.trap_oid || '',
  }))

  const statusEvents = statusHistory.map((e, i): ActivityEvent => {
    const next = (e.new_status || '').toLowerCase()
    const severity =
      next === 'down' ? 'critical'
      : next === 'degraded' ? 'warning'
      : next === 'up' ? 'success'
      : 'info'
    const title =
      next === 'down' ? 'Device went down'
      : next === 'degraded' ? 'Device degraded'
      : next === 'up' ? 'Device recovered'
      : `Status changed to ${titleCase(next || 'unknown')}`
    const previous = e.old_status ? `${titleCase(e.old_status)} → ${titleCase(e.new_status)}` : titleCase(e.new_status)
    const durationSec = e.duration_sec && e.duration_sec > 0
      ? e.duration_sec
      : inferStatusDurationSec(e, statusHistory)
    const duration = statusDurationLabel(e.old_status, e.new_status, durationSec)
    return {
      id: `status-${e.timestamp || i}-${e.old_status}-${e.new_status}`,
      timestamp: e.timestamp,
      severity,
      icon: next === 'up' ? CheckCircle2 : next === 'down' ? AlertTriangle : Activity,
      title,
      subtitle: [previous, e.reason, duration].filter(Boolean).join(' · '),
    }
  })

  const reboot = inferRebootEvent(metrics, fromISO, toISO)
  return [...trapEvents, ...statusEvents, ...(reboot ? [reboot] : [])]
    .filter((e) => Number.isFinite(Date.parse(e.timestamp)))
    .sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
}

function inferStatusDurationSec(event: StatusHistoryEvent, history: StatusHistoryEvent[]): number | null {
  const oldState = (event.old_status || '').toLowerCase()
  const eventTs = Date.parse(event.timestamp)
  if (!oldState || !Number.isFinite(eventTs)) return null

  const ordered = [...history]
    .filter((item) => Number.isFinite(Date.parse(item.timestamp)))
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))

  const currentIndex = ordered.findIndex((item) =>
    item.timestamp === event.timestamp &&
    item.old_status === event.old_status &&
    item.new_status === event.new_status
  )
  if (currentIndex <= 0) return null

  for (let i = currentIndex - 1; i >= 0; i--) {
    const previous = ordered[i]
    if ((previous.new_status || '').toLowerCase() !== oldState) continue
    const previousTs = Date.parse(previous.timestamp)
    const seconds = Math.round((eventTs - previousTs) / 1000)
    return seconds > 0 ? seconds : null
  }
  return null
}

function statusDurationLabel(oldStatus?: string, newStatus?: string, durationSec?: number | null): string {
  if (!durationSec || durationSec <= 0) return ''
  const oldState = (oldStatus || '').toLowerCase()
  const nextState = (newStatus || '').toLowerCase()
  const duration = formatDuration(durationSec)
  if (oldState === 'down' && nextState === 'up') return `Down for ${duration}`
  if (oldState === 'up' && nextState === 'down') return `Was up for ${duration} before outage`
  if (oldState === 'degraded' && nextState === 'up') return `Degraded for ${duration}`
  if (oldState === 'up' && nextState === 'degraded') return `Was healthy for ${duration}`
  return `${titleCase(oldState || 'previous state')} lasted ${duration}`
}

function inferRebootEvent(metrics: MetricSeriesMap, fromISO: string, toISO: string): ActivityEvent | null {
  const uptime = metrics.uptime || metrics.sysUpTime || metrics.sys_uptime
  const points = uptime?.points || []
  if (!points.length) return null

  const latest = points.reduce((best, p) => (p.ts > best.ts ? p : best), points[0])
  if (!latest || !Number.isFinite(latest.ts) || !Number.isFinite(latest.value) || latest.value <= 0) return null

  const rebootTs = latest.ts - latest.value * 1000
  const from = Date.parse(fromISO)
  const to = Date.parse(toISO)
  if (!Number.isFinite(rebootTs) || rebootTs < from || rebootTs > to) return null

  const iso = new Date(rebootTs).toISOString()
  return {
    id: `reboot-${Math.round(rebootTs)}`,
    timestamp: iso,
    severity: 'warning',
    icon: Power,
    title: 'Device reboot detected',
    subtitle: `SNMP sysUpTime reset; current uptime ${formatDuration(latest.value)}`,
  }
}

/* ════════════════════════════════════════════════════════════
   Environmental / System Stats + Quick Actions
   ════════════════════════════════════════════════════════════ */

function EnvironmentalActionsCard({
  deviceId, snmpEnabled, metrics, sensors, lastBw, openAlerts,
}: {
  deviceId: string
  snmpEnabled: boolean
  metrics: Record<string, { points: { ts: number; value: number }[] }>
  sensors: any[]
  lastBw: number
  openAlerts: number
}) {
  const qc = useQueryClient()
  const tempVal = latestSensor(metrics, sensors, ['temperature', 'celsius'])
  const voltVal = latestSensor(metrics, sensors, ['voltage', 'volts'])
  const fanVal = latestSensor(metrics, sensors, ['fan', 'rpm'])
  const sesCount = latestFromMetrics(metrics, ['session', 'sessions'])

  // Persisted last-run state for Ping Test + SNMP Test (per-device).
  const pingKey = `zp-ping-last-${deviceId}`
  const snmpKey = `zp-snmp-last-${deviceId}`
  const [lastPing, setLastPing] = useState<LastRun | null>(() => loadLastRun(pingKey))
  const [lastSnmp, setLastSnmp] = useState<LastRun | null>(() => loadLastRun(snmpKey))
  // Tick every 15s so the "1m ago" labels stay fresh.
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 15_000)
    return () => window.clearInterval(id)
  }, [])

  const [snmpTestOpen, setSnmpTestOpen] = useState(false)
  const [snmpResult, setSnmpResult] = useState<any | null>(null)
  const snmpTest = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/snmp-test`)).data,
    onMutate: () => { setSnmpResult(null); setSnmpTestOpen(true) },
    onSuccess: (data) => {
      setSnmpResult(data)
      const run: LastRun = {
        at: new Date().toISOString(),
        ok: !!data.ok,
        summary: data.ok ? 'responded' : (data.reason || 'failed'),
      }
      saveLastRun(snmpKey, run); setLastSnmp(run)
    },
    onError: (e: any) => {
      const msg = apiErrorMessage(e)
      setSnmpResult({ ok: false, reason: msg, snmp_responded: false })
      const run: LastRun = { at: new Date().toISOString(), ok: false, summary: msg }
      saveLastRun(snmpKey, run); setLastSnmp(run)
    },
  })

  const pingTest = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/ping-test`)).data,
    onSuccess: (data) => {
      const summary = data.ok
        ? `${data.received}/${data.transmitted} · ${data.rtt_avg_ms != null ? data.rtt_avg_ms.toFixed(1) + ' ms' : '—'}`
        : (data.reason || 'failed')
      const run: LastRun = { at: new Date().toISOString(), ok: !!data.ok, summary }
      saveLastRun(pingKey, run); setLastPing(run)
      if (data.ok) toast.success('Ping succeeded', `${data.received}/${data.transmitted} replies · ${data.rtt_avg_ms?.toFixed(1) || '—'} ms`)
      else toast.error('Ping failed', data.reason || 'no reply')
    },
    onError: (e: any) => {
      const msg = apiErrorMessage(e)
      const run: LastRun = { at: new Date().toISOString(), ok: false, summary: msg }
      saveLastRun(pingKey, run); setLastPing(run)
      toast.error('Ping failed', msg)
    },
  })

  // On-demand SSH config pull. Requires the device to be enrolled in NCM —
  // the API answers 400 with a usable message when it isn't.
  const backup = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/config-fetch`)).data,
    onSuccess: (data: any) => {
      if (data?.is_change === false) toast.success('Backup complete', 'No change since the last backup')
      else toast.success('Backup complete', 'New configuration version saved')
      qc.invalidateQueries({ queryKey: ['device', deviceId, 'configs'] })
    },
    onError: (e: any) => toast.error('Backup failed', apiErrorMessage(e)),
  })

  const [ackConfirm, setAckConfirm] = useState(false)
  const ackAll = useMutation({
    mutationFn: async () => {
      const { data } = await api.get(`/alerts?device_id=${deviceId}&status=active&limit=200`)
      const ids: string[] = (data?.data || []).map((a: any) => a.id)
      // Acknowledge in small batches — a device with dozens of open alerts
      // would otherwise fire that many simultaneous requests.
      let done = 0
      for (let i = 0; i < ids.length; i += 8) {
        await Promise.all(ids.slice(i, i + 8).map((id) => api.post(`/alerts/${id}/acknowledge`)))
        done += Math.min(8, ids.length - i)
      }
      return done
    },
    onSuccess: (n) => {
      setAckConfirm(false)
      toast.success('Alerts acknowledged', `${n} alert${n === 1 ? '' : 's'} acknowledged`)
      qc.invalidateQueries({ queryKey: ['device', deviceId, 'alerts'] })
      qc.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (e: any) => {
      setAckConfirm(false)
      toast.error('Acknowledge failed', apiErrorMessage(e))
    },
  })

  // Environmental tiles — only render what we actually have.
  type Tone = 'success' | 'warning' | 'danger' | 'info' | 'accent'
  const tiles: Array<{ icon: React.ComponentType<{ className?: string }>; label: string; value: string; unit?: string; tone: Tone }> = []
  if (tempVal != null) {
    tiles.push({ icon: Thermometer, label: 'Temperature', value: tempVal.toFixed(0), unit: '°C', tone: tempVal > 70 ? 'danger' : tempVal > 55 ? 'warning' : 'success' })
  }
  if (voltVal != null) {
    tiles.push({ icon: Zap, label: 'Voltage', value: voltVal.toFixed(1), unit: 'V', tone: 'success' })
  }
  if (fanVal != null) {
    const fanPct = fanVal > 100 ? null : fanVal
    tiles.push({
      icon: Fan, label: 'Fan Speed',
      value: fanPct != null ? fanPct.toFixed(0) : fanVal.toFixed(0),
      unit: fanPct != null ? '%' : 'rpm',
      tone: 'info',
    })
  }

  // System stats — throughput is always available (derived from iface counters);
  // session count and routing table come from vendor MIBs if collected.
  const bwMbps = lastBw / 1_000_000
  const stats: Array<{ label: string; value: string }> = []
  if (sesCount != null) {
    stats.push({ label: 'Active Sessions', value: sesCount.toLocaleString() })
  }
  if (lastBw > 0) {
    stats.push({ label: 'Throughput', value: `${bwMbps.toFixed(bwMbps < 1 ? 2 : 0)} Mbps` })
  }

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Environmental / System Stats</h3>
          <span className="text-[10px] text-muted">{sensors.length} sensor{sensors.length === 1 ? '' : 's'}</span>
        </div>

        {/* Sensor tiles — only real values */}
        {tiles.length > 0 ? (
          <div className={`grid gap-2 ${tiles.length === 1 ? 'grid-cols-1' : tiles.length === 2 ? 'grid-cols-2' : 'grid-cols-3'}`}>
            {tiles.map((t, i) => <EnvSensorTile key={i} {...t} />)}
          </div>
        ) : (
          <div className="rounded-md border border-border bg-surface2/30 px-3 py-3 text-center text-[11px] text-muted">
            No environmental sensors reported
          </div>
        )}

        {/* System stats — only if we have real data */}
        {stats.length > 0 && (
          <div className={`mt-2 grid gap-2 ${stats.length === 1 ? 'grid-cols-1' : 'grid-cols-2'}`}>
            {stats.map((t, i) => (
              <div key={i} className="rounded-lg border border-border bg-surface2/40 p-2 text-center">
                <div className="text-[9px] font-semibold uppercase tracking-wider text-muted">{t.label}</div>
                <div className="mt-0.5 text-lg font-bold leading-tight tabular-nums">{t.value}</div>
              </div>
            ))}
          </div>
        )}

        {/* Quick actions */}
        <div className="mt-4 border-t border-border/60 pt-3">
          <div className="mb-2 text-xs font-semibold">Quick Actions</div>
          <div className="grid grid-cols-2 gap-2">
            <ActionCard
              icon={Activity}
              label="Ping Test"
              lastRun={lastPing}
              loading={pingTest.isPending}
              tone="primary"
              onRun={() => pingTest.mutate()}
            />
            <ActionCard
              icon={Shield}
              label="SNMP Test"
              lastRun={lastSnmp}
              loading={snmpTest.isPending}
              tone="outline"
              disabled={!snmpEnabled}
              disabledReason={!snmpEnabled ? 'SNMP is disabled' : undefined}
              onRun={() => snmpTest.mutate()}
            />
            <Button
              variant="outline"
              size="sm"
              className="h-8 justify-center"
              disabled={backup.isPending}
              onClick={() => backup.mutate()}
            >
              {backup.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              Backup Config
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-8 justify-center border-warning/30 bg-warning/5 text-warning hover:bg-warning/10 hover:text-warning disabled:border-border disabled:bg-transparent disabled:text-muted"
              disabled={openAlerts === 0 || ackAll.isPending}
              title={openAlerts === 0 ? 'No active alerts to acknowledge' : `Acknowledge ${openAlerts} active alert${openAlerts === 1 ? '' : 's'}`}
              onClick={() => setAckConfirm(true)}
            >
              {ackAll.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BellRing className="h-3.5 w-3.5" />}
              Acknowledge{openAlerts > 0 ? ` (${openAlerts})` : ''}
            </Button>
          </div>
        </div>

        <SnmpTestDialog
          open={snmpTestOpen}
          onOpenChange={setSnmpTestOpen}
          running={snmpTest.isPending}
          result={snmpResult}
          onRetest={() => snmpTest.mutate()}
        />

        <ConfirmDialog
          open={ackConfirm}
          onOpenChange={setAckConfirm}
          title="Acknowledge alerts"
          description={
            <>
              Acknowledge all <b>{openAlerts}</b> active alert{openAlerts === 1 ? '' : 's'} for this device?
            </>
          }
          confirmText="Acknowledge"
          loading={ackAll.isPending}
          onConfirm={() => ackAll.mutate()}
        />
      </CardContent>
    </Card>
  )
}

type LastRun = { at: string; ok: boolean; summary: string }

function loadLastRun(key: string): LastRun | null {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const v = JSON.parse(raw)
    if (!v || typeof v.at !== 'string') return null
    return v as LastRun
  } catch {
    return null
  }
}

function saveLastRun(key: string, value: LastRun) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* ignore */ }
}

function ActionCard({
  icon: Icon, label, lastRun, loading, tone, disabled, disabledReason, onRun,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  lastRun: LastRun | null
  loading: boolean
  tone: 'primary' | 'outline'
  disabled?: boolean
  disabledReason?: string
  onRun: () => void
}) {
  const base = 'group flex flex-col justify-between rounded-md border p-2.5 text-left transition-colors'
  const toneClasses = tone === 'primary'
    ? 'border-primary/40 bg-primary/10 hover:bg-primary/15 text-primary'
    : 'border-border bg-surface2/40 hover:bg-surface2 text-text'
  const canRun = !disabled && !loading
  return (
    <button
      type="button"
      onClick={onRun}
      disabled={!canRun}
      title={disabledReason}
      className={`${base} ${toneClasses} disabled:cursor-not-allowed disabled:opacity-60`}
    >
      <span className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[12px] font-semibold">
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className="h-3.5 w-3.5" />}
          {loading ? 'Running…' : label}
        </span>
        {lastRun && (
          <span
            className={`h-1.5 w-1.5 rounded-full ${lastRun.ok ? 'bg-success' : 'bg-danger'}`}
            title={lastRun.ok ? 'Last run OK' : 'Last run failed'}
          />
        )}
      </span>
      <span className="mt-1 block text-[10px] leading-tight">
        {disabled ? (
          <span className="text-muted">{disabledReason}</span>
        ) : lastRun ? (
          <>
            <span className="text-muted">Last: </span>
            <span className={lastRun.ok ? 'text-text' : 'text-danger'}>{relativeTime(lastRun.at)}</span>
            {lastRun.summary && (
              <span className="block truncate text-muted" title={lastRun.summary}>{lastRun.summary}</span>
            )}
          </>
        ) : (
          <span className="text-muted">Never run</span>
        )}
      </span>
    </button>
  )
}

function EnvSensorTile({
  icon: Icon, label, value, unit, tone,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string; value: string; unit?: string
  tone: 'success' | 'warning' | 'danger' | 'info' | 'accent'
}) {
  const MAP: Record<typeof tone, string> = {
    success: 'border-success/30 bg-success/5 text-success',
    warning: 'border-warning/30 bg-warning/5 text-warning',
    danger:  'border-danger/30 bg-danger/5 text-danger',
    info:    'border-info/30 bg-info/5 text-info',
    accent:  'border-accent/30 bg-accent/5 text-accent',
  }
  return (
    <div className={`rounded-lg border px-2 py-2 text-center ${MAP[tone]}`}>
      <Icon className="mx-auto h-3.5 w-3.5" />
      <div className="mt-1 flex items-baseline justify-center gap-0.5">
        <span className="text-base font-bold leading-none tabular-nums">{value}</span>
        {unit && <span className="text-[9px]">{unit}</span>}
      </div>
      <div className="mt-0.5 text-[9px] uppercase tracking-wider opacity-80">{label}</div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   SNMP Test dialog
   ════════════════════════════════════════════════════════════ */

function SnmpTestDialog({
  open, onOpenChange, running, result, onRetest,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  running: boolean
  result: any | null
  onRetest: () => void
}) {
  const ok = !!result?.ok
  const responded = !!result?.snmp_responded
  const reachable = result?.reachable
  const uptime = result?.sys_uptime_seconds != null ? formatDuration(result.sys_uptime_seconds) : null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Shield className="h-5 w-5 text-primary" />
            SNMP Test
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          {running && !result && (
            <div className="flex items-center gap-2 rounded-md border border-border bg-surface2/50 px-3 py-3 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              Probing device via SNMP…
            </div>
          )}

          {result && (
            <>
              <div className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium ${
                ok
                  ? 'border-success/30 bg-success/10 text-success'
                  : 'border-danger/30 bg-danger/10 text-danger'
              }`}>
                {ok ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
                {ok
                  ? 'SNMP responded — device is discoverable'
                  : result.reason || 'SNMP probe failed'}
              </div>

              <div className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-md border border-border bg-surface2/40 px-3 py-2.5 text-xs">
                <Kv k="Reachable (ping)" v={reachable == null ? '—' : reachable ? 'Yes' : 'No'} tone={reachable ? 'success' : reachable === false ? 'danger' : 'muted'} />
                <Kv k="SNMP responded" v={responded ? 'Yes' : 'No'} tone={responded ? 'success' : 'danger'} />
                <Kv k="Probe duration" v={result.duration_ms != null ? `${result.duration_ms} ms` : '—'} />
                {result.config && (
                  <Kv k="Version · Port" v={`v${result.config.version} · ${result.config.port}`} />
                )}
              </div>

              {responded && (
                <div className="space-y-1.5 rounded-md border border-border bg-surface2/40 px-3 py-2.5 text-xs">
                  <Kv k="sysName" v={result.sys_name || '—'} mono />
                  <Kv k="sysDescr" v={result.sys_descr || '—'} mono wrap />
                  <Kv k="sysObjectID" v={result.sys_object_id || '—'} mono />
                  <Kv k="sysUpTime" v={uptime || result.sys_uptime_raw || '—'} />
                </div>
              )}
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
          <Button onClick={onRetest} disabled={running}>
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Shield className="h-4 w-4" />}
            Re-test
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Kv({
  k, v, mono, wrap, tone,
}: { k: string; v: string; mono?: boolean; wrap?: boolean; tone?: 'success' | 'danger' | 'muted' }) {
  const toneClass = tone === 'success' ? 'text-success' : tone === 'danger' ? 'text-danger' : tone === 'muted' ? 'text-muted' : 'text-text'
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-3">
      <span className="shrink-0 text-muted">{k}</span>
      <span className={`${mono ? 'font-mono' : ''} ${wrap ? 'break-all' : 'truncate'} text-right font-medium ${toneClass}`} title={v}>{v}</span>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   Drill-down dialogs
   ════════════════════════════════════════════════════════════ */

function EventsDialog({
  open, onOpenChange, deviceId, initialHours, rangeLabel,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  deviceId: string
  initialHours?: number
  rangeLabel?: string
}) {
  const [hours, setHours] = useState(initialHours ?? 24)
  // When the page-level range changes, follow it on next dialog open.
  useEffect(() => {
    if (open && initialHours != null) setHours(initialHours)
  }, [open, initialHours])
  const trapHours = Math.min(720, Math.max(1, Math.round(hours)))
  const metricHours = Math.min(2160, Math.max(1, Math.round(hours)))
  const fromISO = useMemo(() => new Date(Date.now() - hours * 3_600_000).toISOString(), [hours])
  const toISO = useMemo(() => new Date().toISOString(), [hours])
  const { data: traps, isLoading: trapsLoading } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'events-full', 'traps', trapHours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/traps?hours=${trapHours}&limit=200`)).data,
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  })
  const { data: statusHistory, isLoading: statusLoading } = useQuery<StatusHistoryEvent[]>({
    queryKey: ['device', deviceId, 'events-full', 'status', fromISO, toISO],
    queryFn: async () =>
      (await api.get(
        `/devices/${deviceId}/status-history?from=${encodeURIComponent(fromISO)}&to=${encodeURIComponent(toISO)}&limit=200`,
      )).data,
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  })
  const { data: metrics, isLoading: metricsLoading } = useQuery<MetricSeriesMap>({
    queryKey: ['device', deviceId, 'events-full', 'metrics', metricHours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-metrics?hours=${metricHours}`)).data,
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  })
  const isLoading = trapsLoading || statusLoading || metricsLoading
  const events = useMemo(
    () => buildActivityEvents(traps || [], statusHistory || [], metrics || {}, fromISO, toISO),
    [traps, statusHistory, metrics, fromISO, toISO],
  )
  // Build the range chips: include the standard set plus the active range
  // from the page (so a 1M / custom range is selectable here too).
  const rangeOptions = useMemo(() => {
    const base = [1, 6, 24, 168, 720]
    const set = new Set<number>(base)
    if (initialHours != null) set.add(initialHours)
    return Array.from(set).sort((a, b) => a - b)
  }, [initialHours])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-primary" />
            Recent Events / Activity Log
          </DialogTitle>
        </DialogHeader>

        <div className="flex items-center justify-between gap-2 border-b border-border pb-2">
          <div className="text-xs text-muted">
            {isLoading
              ? 'Loading…'
              : `${events.length} event${events.length === 1 ? '' : 's'}${rangeLabel ? ` · ${rangeLabel}` : ''}`}
          </div>
          <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
            {rangeOptions.map((h) => (
              <button
                key={h}
                onClick={() => setHours(h)}
                className={`rounded px-2 py-1 text-[11px] font-medium ${
                  hours === h ? 'bg-surface text-primary shadow-sm' : 'text-muted hover:text-text'
                }`}
              >
                {h < 24 ? `${h}h` : h % 24 === 0 ? `${h / 24}d` : `${h}h`}
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {events.length === 0 ? (
            <div className="flex flex-col items-center gap-1 py-10 text-center">
              <Info className="h-6 w-6 text-muted/60" />
              <div className="text-sm font-medium">No events in this period</div>
              <div className="text-xs text-muted">Status changes, reboots, and SNMP traps will appear here.</div>
            </div>
          ) : (
            <div className="divide-y divide-border">
              {events.map((event) => (
                <div key={event.id} className="flex items-start gap-3 py-2 text-xs">
                  <span className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider ${
                    event.severity === 'critical' ? 'bg-danger/15 text-danger'
                    : event.severity === 'warning' ? 'bg-warning/15 text-warning'
                    : event.severity === 'success' ? 'bg-success/15 text-success'
                    : 'bg-info/15 text-info'
                  }`}>
                    {event.severity || 'info'}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium" title={event.title}>{event.title}</div>
                    <div className="font-mono text-[10px] text-text/80">{formatEventTimestamp(event.timestamp)}</div>
                    <div className="truncate text-[10px] text-muted" title={event.subtitle}>{event.subtitle || ''}</div>
                  </div>
                  <div className="shrink-0 text-[10px] text-muted">{relativeTime(event.timestamp)}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function InventoryDialog({
  open, onOpenChange, entities,
}: { open: boolean; onOpenChange: (o: boolean) => void; entities: any[] }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Layers className="h-5 w-5 text-primary" />
            Hardware Inventory
          </DialogTitle>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto">
          {entities.length === 0 ? (
            <div className="py-8 text-center text-xs text-muted">
              No hardware inventory available for this device.
            </div>
          ) : (
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-surface text-[10px] uppercase tracking-wider text-muted">
                <tr className="border-b border-border">
                  <th className="py-1.5 px-2 text-left font-medium">Class</th>
                  <th className="py-1.5 px-2 text-left font-medium">Name</th>
                  <th className="py-1.5 px-2 text-left font-medium">Model</th>
                  <th className="py-1.5 px-2 text-left font-medium">Serial</th>
                  <th className="py-1.5 px-2 text-left font-medium">HW Rev</th>
                  <th className="py-1.5 px-2 text-left font-medium">FW Rev</th>
                </tr>
              </thead>
              <tbody>
                {entities.map((e) => (
                  <tr key={e.id || `${e.entity_index}-${e.name}`} className="border-b border-border/40 last:border-0">
                    <td className="px-2 py-2">
                      <span className="rounded bg-surface2 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-muted">
                        {e.class || '—'}
                      </span>
                    </td>
                    <td className="px-2 py-2 font-medium">{e.name || '—'}</td>
                    <td className="px-2 py-2 text-muted">{e.model_name || '—'}</td>
                    <td className="px-2 py-2 font-mono text-[10px] text-muted">{e.serial_number || '—'}</td>
                    <td className="px-2 py-2 text-muted">{e.hw_revision || '—'}</td>
                    <td className="px-2 py-2 text-muted">{e.fw_revision || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function HealthDetailsDialog({
  open, onOpenChange, device, cpu, mem, loss, ifUp, ifTotal, score,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  device: any
  cpu: number | null
  mem: number | null
  loss: number | null
  ifUp: number
  ifTotal: number
  score: number
}) {
  const factors = [
    {
      label: 'Reachability',
      value: device.status === 'up' ? 'Up' : device.status === 'degraded' ? 'Degraded' : 'Down',
      impact: device.status === 'up' ? 'No penalty'
        : device.status === 'degraded' ? '−12 points'
        : '−80 points',
      ok: device.status === 'up',
    },
    {
      label: 'CPU',
      value: cpu != null ? `${cpu.toFixed(0)}%` : 'not reporting',
      impact: cpu != null && cpu > 70 ? `−${((cpu - 70) * 0.6).toFixed(0)} points` : 'No penalty',
      ok: cpu == null || cpu <= 70,
    },
    {
      label: 'Memory',
      value: mem != null ? `${mem.toFixed(0)}%` : 'not reporting',
      impact: mem != null && mem > 70 ? `−${((mem - 70) * 0.5).toFixed(0)} points` : 'No penalty',
      ok: mem == null || mem <= 70,
    },
    {
      label: 'Packet Loss',
      value: loss != null ? `${loss.toFixed(2)}%` : 'not reporting',
      impact: loss != null && loss > 0 ? `−${Math.min(30, loss * 3).toFixed(0)} points` : 'No penalty',
      ok: loss == null || loss === 0,
    },
    {
      label: 'Interfaces Up',
      value: ifTotal > 0 ? `${ifUp} / ${ifTotal}` : 'none discovered',
      impact: ifTotal > 0 && ifUp < ifTotal
        ? `−${(((ifTotal - ifUp) / ifTotal) * 15).toFixed(0)} points`
        : 'No penalty',
      ok: ifTotal === 0 || ifUp === ifTotal,
    },
  ]

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <CheckCircle2 className="h-5 w-5 text-primary" />
            Health Score Breakdown
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
            <span className="text-sm font-medium">Current score</span>
            <span className={`text-2xl font-bold tabular-nums ${
              score >= 80 ? 'text-success' : score >= 60 ? 'text-warning' : 'text-danger'
            }`}>
              {score}<span className="text-sm text-muted">/100</span>
            </span>
          </div>

          <div className="space-y-2">
            {factors.map((f, i) => (
              <div key={i} className="flex items-start justify-between gap-3 border-b border-border/40 pb-2 last:border-0">
                <div className="min-w-0">
                  <div className="text-xs font-semibold">{f.label}</div>
                  <div className="text-[11px] text-muted">{f.value}</div>
                </div>
                <div className={`text-[11px] font-medium ${f.ok ? 'text-success' : 'text-danger'}`}>
                  {f.impact}
                </div>
              </div>
            ))}
          </div>
        </div>
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ════════════════════════════════════════════════════════════
   Mini SVG sparkline
   ════════════════════════════════════════════════════════════ */

function MiniSparkline({
  data, stroke, from, to, className,
}: { data: number[]; stroke: string; from: string; to: string; className?: string }) {
  if (data.length < 2) return <div className={className} />
  const w = 100
  const h = 32
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const step = w / (data.length - 1)
  const pts = data.map((v, i) => [i * step, h - ((v - min) / range) * (h - 4) - 2] as const)
  const line = pts.map(([x, y], i) => (i === 0 ? `M${x.toFixed(2)},${y.toFixed(2)}` : `L${x.toFixed(2)},${y.toFixed(2)}`)).join(' ')
  const area = `${line} L${w},${h} L0,${h} Z`
  const id = `ms-${Math.random().toString(36).slice(2, 9)}`
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className={className} aria-hidden="true">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={from} />
          <stop offset="100%" stopColor={to} />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/* ════════════════════════════════════════════════════════════
   Helpers
   ════════════════════════════════════════════════════════════ */

function titleCase(s: string) {
  return s.replace(/\b\w/g, (c) => c.toUpperCase())
}

function normalizeIp(ip: string | null | undefined): string {
  return String(ip || '').split('/')[0].trim()
}

function avg(xs: number[]): number {
  if (!xs.length) return 0
  return xs.reduce((s, v) => s + v, 0) / xs.length
}

function percentTrend(series: number[]): number | null {
  if (series.length < 4) return null
  const half = Math.floor(series.length / 2)
  const a = series.slice(0, half).reduce((s, v) => s + v, 0) / half
  const b = series.slice(half).reduce((s, v) => s + v, 0) / (series.length - half)
  if (a === 0) return b === 0 ? 0 : null
  return ((b - a) / a) * 100
}

function formatBpsShort(bps: number): string {
  if (!bps) return '—'
  const k = 1000
  const units = ['bps', 'K', 'M', 'G', 'T']
  const i = Math.min(Math.floor(Math.log(Math.abs(bps)) / Math.log(k)), units.length - 1)
  return `${(bps / Math.pow(k, i)).toFixed(1)} ${units[i] === 'bps' ? 'bps' : units[i] + 'bps'}`
}

/** Link utilisation is often a small fraction of a 10G port — don't round
 *  live traffic down to a flat "0%". */
function formatUtilPct(util: number): string {
  if (util <= 0) return '0%'
  if (util < 0.5) return '<1%'
  return `${util.toFixed(0)}%`
}

function formatSpeed(speed: any): string {
  const n = Number(speed) || 0
  if (!n) return '—'
  if (n >= 1e10) return `${(n / 1e9).toFixed(0)} Gbps`
  if (n >= 1e9) return `${(n / 1e9).toFixed(0)} Gbps`
  if (n >= 1e6) return `${(n / 1e6).toFixed(0)} Mbps`
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} Kbps`
  return `${n} bps`
}

function bandwidthSeries(ifMetrics: Record<string, any[]>): Array<{ ts: number; value: number }> {
  const byTs: Record<number, number> = {}
  Object.values(ifMetrics).forEach((arr) => {
    arr.forEach((p) => {
      const t = p.ts || new Date(p.timestamp).getTime()
      byTs[t] = (byTs[t] || 0) + (p.in_bps || 0) + (p.out_bps || 0)
    })
  })
  return Object.entries(byTs)
    .map(([t, v]) => ({ ts: Number(t), value: v }))
    .sort((a, b) => a.ts - b.ts)
}

function readUptimeSeconds(device: any, metrics?: Record<string, { points: { value: number }[] }>): number | null {
  if (typeof device.uptime_seconds === 'number') return device.uptime_seconds
  const u = metrics?.uptime || metrics?.sysUpTime || metrics?.sys_uptime
  if (u?.points?.length) return u.points[u.points.length - 1].value
  return null
}

function formatDaysCompact(s: number): string {
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  return `${d}d ${h}h`
}

/** ClickHouse returns is_up as a bool, a UInt8 or an aggregated fraction. */
function isUpPoint(v: unknown): boolean {
  if (typeof v === 'boolean') return v
  if (typeof v === 'number') return v > 0.5
  return false
}

function computeHealthScore({
  status, cpu, mem, loss, ifUp, ifTotal,
}: {
  status: string
  cpu: number | null
  mem: number | null
  loss: number | null
  ifUp: number
  ifTotal: number
}): number {
  if (status === 'down') return 20
  let score = 100
  if (cpu != null) score -= Math.max(0, cpu - 70) * 0.6
  if (mem != null) score -= Math.max(0, mem - 70) * 0.5
  if (loss != null) score -= Math.min(30, loss * 3)
  if (ifTotal > 0) {
    const downRatio = (ifTotal - ifUp) / ifTotal
    score -= downRatio * 15
  }
  if (status === 'degraded') score -= 12
  return Math.max(0, Math.min(100, Math.round(score)))
}

function normalizeSeverity(s: string): 'critical' | 'warning' | 'info' | 'success' {
  if (s === 'critical') return 'critical'
  if (s === 'warning') return 'warning'
  if (s === 'success') return 'success'
  return 'info'
}

function toneForSeverity(s: string): 'success' | 'warning' | 'danger' | 'info' | 'accent' {
  if (s === 'critical') return 'danger'
  if (s === 'warning') return 'warning'
  if (s === 'success') return 'success'
  return 'info'
}

function latestFromMetrics(
  metrics: Record<string, { points: { value: number }[] }>,
  keywords: string[],
): number | null {
  for (const [k, v] of Object.entries(metrics)) {
    if (keywords.some((kw) => k.toLowerCase().includes(kw))) {
      if (v.points?.length) return v.points[v.points.length - 1].value
    }
  }
  return null
}

function latestSensor(
  metrics: Record<string, { points: { value: number }[] }>,
  sensors: any[],
  keywords: string[],
): number | null {
  for (const [k, v] of Object.entries(metrics)) {
    if (keywords.some((kw) => k.toLowerCase().includes(kw))) {
      if (v.points?.length) return v.points[v.points.length - 1].value
    }
  }
  for (const s of sensors) {
    const d = (s.description || '').toLowerCase()
    const t = (s.sensor_type || '').toLowerCase()
    if (keywords.some((kw) => d.includes(kw) || t.includes(kw))) {
      const raw = s.current_value ?? s.value
      if (typeof raw === 'number') return raw
    }
  }
  return null
}

/* ════════════════════════════════════════════════════════════
   INTERFACES TAB — list + detail panel
   ════════════════════════════════════════════════════════════ */
function InterfacesTab({ deviceId }: { deviceId: string }) {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'up' | 'down'>('all')
  const [hours, setHours] = useState(1)
  const [selectedIf, setSelectedIf] = useState<any>(null)

  const { data: ifs } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'interfaces'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    refetchInterval: 30_000,
  })
  const { data: ifMetrics } = useQuery<Record<string, any[]>>({
    queryKey: ['device', deviceId, 'if-metrics', hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-if-metrics?hours=${hours}`)).data,
    refetchInterval: 30_000,
  })

  const lastBps = (idx: number) => {
    const s = ifMetrics?.[idx]; if (!s?.length) return { in: 0, out: 0 }
    const l = s[s.length - 1]; return { in: l.in_bps || 0, out: l.out_bps || 0 }
  }
  const utilPct = (i: any) => {
    const bps = lastBps(i.if_index)
    if (!i.if_speed || Number(i.if_speed) === 0) return 0
    return Math.min(100, ((bps.in + bps.out) / Number(i.if_speed)) * 100)
  }

  const filtered = (ifs || []).filter((i) => {
    const name = (i.if_name || i.if_descr || '').toLowerCase()
    const alias = (i.if_alias || '').toLowerCase()
    if (search && !name.includes(search.toLowerCase()) && !alias.includes(search.toLowerCase())) return false
    if (filter === 'up' && i.oper_status !== 'up') return false
    if (filter === 'down' && i.oper_status === 'up') return false
    return true
  })
  const upCount = (ifs || []).filter((i) => i.oper_status === 'up').length

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted">{ifs?.length || 0} interfaces</span>
          <Badge variant="success">{upCount} up</Badge>
          <Badge variant="outline">{(ifs?.length || 0) - upCount} down</Badge>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-md border border-border text-[11px] overflow-hidden">
            {(['all', 'up', 'down'] as const).map((f) => (
              <button key={f} onClick={() => setFilter(f)} className={`px-2 py-1 capitalize transition ${filter === f ? 'bg-primary/10 text-primary font-medium' : 'text-muted hover:bg-surface2'}`}>{f}</button>
            ))}
          </div>
          <div className="flex rounded-md border border-border text-[11px] overflow-hidden">
            {[1, 6, 24].map((h) => (
              <button key={h} onClick={() => setHours(h)} className={`px-2 py-1 transition ${hours === h ? 'bg-primary/10 text-primary font-medium' : 'text-muted hover:bg-surface2'}`}>{h}h</button>
            ))}
          </div>
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted" />
            <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter..." className="h-7 w-40 pl-7 text-xs" />
          </div>
        </div>
      </div>

      {/* Table with inline detail expansion */}
      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/40">
              <Tr>
                <Th className="w-8">#</Th><Th>Name</Th><Th>Description</Th><Th>Speed</Th>
                <Th className="w-14">Status</Th>
                <Th className="text-right w-24"><ArrowDown className="inline h-3 w-3" /> In</Th>
                <Th className="text-right w-24"><ArrowUp className="inline h-3 w-3" /> Out</Th>
                <Th className="w-28">Utilization</Th><Th>MAC</Th>
              </Tr>
            </THead>
            <TBody>
              {filtered.map((i) => {
                const bps = lastBps(i.if_index)
                const isUp = i.oper_status === 'up'
                const pct = utilPct(i)
                const isSelected = selectedIf?.if_index === i.if_index
                return (
                  <>
                    <Tr key={i.id} onClick={() => setSelectedIf(isSelected ? null : i)}
                      className={`cursor-pointer transition-colors ${!isUp ? 'opacity-50' : ''} ${isSelected ? 'bg-primary/5' : 'hover:bg-surface2/60'}`}>
                      <Td className="font-mono text-[10px] text-muted">
                        <ChevronRight className={`inline h-3 w-3 transition-transform ${isSelected ? 'rotate-90' : ''}`} />
                      </Td>
                      <Td><span className="text-sm font-medium">{i.if_name || i.if_descr || `if${i.if_index}`}</span></Td>
                      <Td className="text-xs text-muted max-w-[180px] truncate">{i.if_alias || i.if_descr || '—'}</Td>
                      <Td className="text-xs">{i.if_speed ? formatBps(Number(i.if_speed)) : '—'}</Td>
                      <Td>
                        <div className="flex items-center gap-1.5">
                          <div className={`h-2 w-2 rounded-full ${isUp ? 'bg-success' : 'bg-danger'}`} />
                          <span className="text-[11px]">{i.oper_status || '—'}</span>
                        </div>
                      </Td>
                      <Td className="text-right font-mono text-xs">{isUp && bps.in > 0 ? formatBps(bps.in) : '—'}</Td>
                      <Td className="text-right font-mono text-xs">{isUp && bps.out > 0 ? formatBps(bps.out) : '—'}</Td>
                      <Td>
                        {isUp && i.if_speed ? (
                          <div className="flex items-center gap-1.5">
                            <div className="h-1.5 flex-1 rounded-full bg-surface2 overflow-hidden">
                              <div className={`h-full rounded-full ${pct > 80 ? 'bg-danger' : pct > 50 ? 'bg-warning' : 'bg-primary'}`} style={{ width: `${Math.max(1, pct)}%` }} />
                            </div>
                            <span className="text-[10px] text-muted w-7 text-right">{pct.toFixed(0)}%</span>
                          </div>
                        ) : <span className="text-xs text-muted">—</span>}
                      </Td>
                      <Td className="font-mono text-[10px] text-muted">{i.mac_address || '—'}</Td>
                    </Tr>
                    {/* Inline expanded detail row */}
                    {isSelected && (
                      <tr key={`${i.id}-detail`}>
                        <td colSpan={9} className="p-0 border-b border-primary/20">
                          <InterfaceDetail deviceId={deviceId} iface={i} />
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}
              {filtered.length === 0 && <Tr><Td colSpan={9} className="py-6 text-center text-xs text-muted">{ifs?.length ? 'No matching interfaces' : 'No interfaces discovered yet'}</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}

/* ── Interface Detail (inline inside table row) ──────────── */
function InterfaceDetail({ deviceId, iface }: { deviceId: string; iface: any }) {
  const [hours, setHours] = useState(6)
  const ifName = iface.if_name || iface.if_descr || `Interface ${iface.if_index}`
  const isUp = iface.oper_status === 'up'

  const { data, isLoading } = useQuery<{ traffic: any[]; errors: any[]; summary: any }>({
    queryKey: ['device', deviceId, 'if-detail', iface.if_index, hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces/${iface.if_index}/metrics?hours=${hours}`)).data,
    refetchInterval: 30_000,
  })

  const s = data?.summary || {}
  const traffic = data?.traffic || []
  const errors = data?.errors || []

  return (
    <div className="bg-surface2/20 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/50 px-5 py-3">
        <div className="flex items-center gap-3">
          <div className={`h-3 w-3 rounded-full ${isUp ? 'bg-success' : 'bg-danger'}`} />
          <div>
            <div className="text-sm font-bold">{ifName}</div>
            <div className="flex items-center gap-2 text-[11px] text-muted">
              <span>Index {iface.if_index}</span>
              {iface.if_alias && <><span>·</span><span>{iface.if_alias}</span></>}
              {iface.if_speed && <><span>·</span><span>{formatBps(Number(iface.if_speed))}</span></>}
              {iface.mac_address && <><span>·</span><span className="font-mono">{iface.mac_address}</span></>}
            </div>
          </div>
        </div>
        <div className="flex rounded-md border border-border text-[11px] overflow-hidden">
          {[1, 6, 12, 24, 168].map((h) => (
            <button key={h} onClick={(e) => { e.stopPropagation(); setHours(h) }}
              className={`px-2 py-1 transition ${hours === h ? 'bg-primary/10 text-primary font-medium' : 'text-muted hover:bg-surface2'}`}>
              {h < 24 ? `${h}h` : `${h / 24}d`}
            </button>
          ))}
        </div>
      </div>

      <div className="px-5 py-4 space-y-4" onClick={(e) => e.stopPropagation()}>
        {isLoading && <div className="py-8 text-center"><Loader2 className="mx-auto h-5 w-5 animate-spin text-muted" /></div>}

        {!isLoading && (
          <>
            {/* KPI Row */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
              <IfKpi label="Admin Status" value={iface.admin_status || '—'} tone={iface.admin_status === 'up' ? 'success' : 'muted'} />
              <IfKpi label="Oper Status" value={iface.oper_status || '—'} tone={isUp ? 'success' : 'danger'} />
              <IfKpi label="In Current" value={s.in_current_bps ? formatBps(s.in_current_bps) : '—'} />
              <IfKpi label="Out Current" value={s.out_current_bps ? formatBps(s.out_current_bps) : '—'} />
              <IfKpi label="Total Errors" value={String(s.total_errors ?? 0)} tone={(s.total_errors || 0) > 0 ? 'danger' : 'success'} />
              <IfKpi label="Total Discards" value={String(s.total_discards ?? 0)} tone={(s.total_discards || 0) > 0 ? 'warning' : 'success'} />
            </div>

            {/* Stats grid */}
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <div className="rounded-md border border-border p-3 space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Interface Properties</div>
                {([
                  ['Index', String(iface.if_index)],
                  ['Name', iface.if_name || '—'],
                  ['Description', iface.if_descr || '—'],
                  ['Alias', iface.if_alias || '—'],
                  ['Type', iface.if_type ? String(iface.if_type) : '—'],
                  ['Speed', iface.if_speed ? formatBps(Number(iface.if_speed)) : '—'],
                  ['MAC Address', iface.mac_address || '—'],
                  ['First Seen', relativeTime(iface.first_seen)],
                  ['Last Seen', relativeTime(iface.last_seen)],
                ] as [string, string][]).map(([k, v]) => (
                  <div key={k} className="flex justify-between text-[12px]">
                    <span className="text-muted">{k}</span>
                    <span className={`font-medium ${k === 'MAC Address' ? 'font-mono text-[11px]' : ''}`}>{v}</span>
                  </div>
                ))}
              </div>

              <div className="rounded-md border border-border p-3 space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-muted flex items-center gap-1">
                  <ArrowDown className="h-3 w-3 text-success" /> Inbound Traffic
                </div>
                {([
                  ['Current', s.in_current_bps ? formatBps(s.in_current_bps) : '—'],
                  ['Average', s.in_avg_bps ? formatBps(s.in_avg_bps) : '—'],
                  ['Peak', s.in_max_bps ? formatBps(s.in_max_bps) : '—'],
                  ['Utilization', iface.if_speed && s.in_current_bps ? `${((s.in_current_bps / Number(iface.if_speed)) * 100).toFixed(1)}%` : '—'],
                ] as [string, string][]).map(([k, v]) => (
                  <div key={k} className="flex justify-between text-[12px]">
                    <span className="text-muted">{k}</span><span className="font-medium font-mono">{v}</span>
                  </div>
                ))}
                <div className="text-[10px] font-semibold uppercase tracking-wider text-muted flex items-center gap-1 pt-2">
                  <ArrowUp className="h-3 w-3 text-primary" /> Outbound Traffic
                </div>
                {([
                  ['Current', s.out_current_bps ? formatBps(s.out_current_bps) : '—'],
                  ['Average', s.out_avg_bps ? formatBps(s.out_avg_bps) : '—'],
                  ['Peak', s.out_max_bps ? formatBps(s.out_max_bps) : '—'],
                  ['Utilization', iface.if_speed && s.out_current_bps ? `${((s.out_current_bps / Number(iface.if_speed)) * 100).toFixed(1)}%` : '—'],
                ] as [string, string][]).map(([k, v]) => (
                  <div key={k} className="flex justify-between text-[12px]">
                    <span className="text-muted">{k}</span><span className="font-medium font-mono">{v}</span>
                  </div>
                ))}
              </div>

              <div className="rounded-md border border-border p-3 space-y-1.5">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Errors & Discards</div>
                {([
                  ['In Errors', String(s.total_errors != null ? errors.reduce((a: number, e: any) => a + (e.in_errors || 0), 0) : '—')],
                  ['Out Errors', String(s.total_errors != null ? errors.reduce((a: number, e: any) => a + (e.out_errors || 0), 0) : '—')],
                  ['In Discards', String(s.total_discards != null ? errors.reduce((a: number, e: any) => a + (e.in_discards || 0), 0) : '—')],
                  ['Out Discards', String(s.total_discards != null ? errors.reduce((a: number, e: any) => a + (e.out_discards || 0), 0) : '—')],
                  ['Samples', String(s.samples || 0)],
                ] as [string, string][]).map(([k, v]) => (
                  <div key={k} className="flex justify-between text-[12px]">
                    <span className="text-muted">{k}</span>
                    <span className={`font-medium font-mono ${v !== '0' && v !== '—' && k !== 'Samples' ? 'text-danger' : ''}`}>{v}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Traffic Chart */}
            {traffic.length > 1 && (
              <Card>
                <CardHeader className="pb-0">
                  <CardTitle className="text-sm">Bandwidth — In / Out</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="h-44">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={traffic}>
                        <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.5)" />
                        <XAxis dataKey="ts" tickFormatter={timeAxisTickFormatter(hours)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} />
                        <YAxis tickFormatter={(v) => formatBps(v)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} width={55} />
                        <Tooltip {...ttStyle()} formatter={(v: any, name: string) => [formatBps(Number(v)), name === 'in_bps' ? 'Inbound' : 'Outbound']} />
                        <Area type="monotone" dataKey="in_bps" name="in_bps" stroke="rgb(var(--success))" fill="rgb(var(--success))" fillOpacity={0.1} strokeWidth={1.5} dot={false} />
                        <Area type="monotone" dataKey="out_bps" name="out_bps" stroke="rgb(var(--primary))" fill="rgb(var(--primary))" fillOpacity={0.1} strokeWidth={1.5} dot={false} />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="flex items-center justify-center gap-6 mt-2 text-xs">
                    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-success" /> Inbound</span>
                    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-primary" /> Outbound</span>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Errors Chart */}
            {errors.length > 1 && (s.total_errors > 0 || s.total_discards > 0) && (
              <Card>
                <CardHeader className="pb-0">
                  <CardTitle className="text-sm">Errors & Discards Over Time</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="h-32">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={errors}>
                        <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.5)" />
                        <XAxis dataKey="ts" tickFormatter={timeAxisTickFormatter(hours)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} />
                        <YAxis tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} width={30} />
                        <Tooltip {...ttStyle()} />
                        <Line type="monotone" dataKey="in_errors" name="In Errors" stroke="rgb(var(--danger))" strokeWidth={1.5} dot={false} />
                        <Line type="monotone" dataKey="out_errors" name="Out Errors" stroke="rgb(var(--warning))" strokeWidth={1.5} dot={false} />
                        <Line type="monotone" dataKey="in_discards" name="In Discards" stroke="rgb(var(--accent))" strokeWidth={1} strokeDasharray="4 2" dot={false} />
                        <Line type="monotone" dataKey="out_discards" name="Out Discards" stroke="rgb(var(--info))" strokeWidth={1} strokeDasharray="4 2" dot={false} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="flex items-center justify-center gap-4 mt-2 text-[10px]">
                    <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded bg-danger" /> In Errors</span>
                    <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded bg-warning" /> Out Errors</span>
                    <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded bg-accent" /> In Discards</span>
                    <span className="flex items-center gap-1"><span className="h-1.5 w-3 rounded bg-info" /> Out Discards</span>
                  </div>
                </CardContent>
              </Card>
            )}

            {traffic.length === 0 && <div className="py-6 text-center text-xs text-muted">No historical data for this interface in the selected period</div>}
          </>
        )}
      </div>
    </div>
  )
}

function IfKpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  const color = tone === 'success' ? 'text-success' : tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-text'
  return (
    <div className="rounded-md border border-border p-2">
      <div className="text-[9px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={`text-sm font-bold ${color}`}>{value}</div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   TRAPS TAB
   ════════════════════════════════════════════════════════════ */
function TrapsTab({ deviceId }: { deviceId: string }) {
  const [hours, setHours] = useState(24)
  const { data: traps } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'traps', hours],
    queryFn: async () => (await api.get(`/devices/${deviceId}/traps?hours=${hours}&limit=200`)).data,
    refetchInterval: 10_000,
  })

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted">{traps?.length || 0} events</span>
        <div className="flex rounded-md border border-border text-[11px] overflow-hidden">
          {[1, 6, 24, 168].map((h) => (
            <button key={h} onClick={() => setHours(h)}
              className={`px-2.5 py-1 transition ${hours === h ? 'bg-primary/10 text-primary font-medium' : 'text-muted hover:bg-surface2'}`}>
              {h < 24 ? `${h}h` : `${h / 24}d`}
            </button>
          ))}
        </div>
      </div>
      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/40"><Tr><Th>Time</Th><Th>Severity</Th><Th>Source</Th><Th>Trap OID</Th><Th>Name</Th><Th>Message</Th></Tr></THead>
            <TBody>
              {(traps || []).map((t, i) => (
                <Tr key={i}>
                  <Td className="text-xs text-muted whitespace-nowrap">{relativeTime(t.timestamp)}</Td>
                  <Td><Badge variant={t.severity === 'critical' ? 'danger' : t.severity === 'warning' ? 'warning' : 'info'} className="text-[10px]">{t.severity}</Badge></Td>
                  <Td className="font-mono text-xs">{t.source_ip}</Td>
                  <Td className="font-mono text-[10px] text-muted max-w-[180px] truncate" title={t.trap_oid}>{t.trap_oid}</Td>
                  <Td className="text-xs font-medium">{t.trap_name || '—'}</Td>
                  <Td className="text-sm max-w-[300px] truncate">{t.message || '—'}</Td>
                </Tr>
              ))}
              {(!traps || traps.length === 0) && <Tr><Td colSpan={6} className="py-6 text-center text-xs text-muted">No events in selected period</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   CONFIGURATION TAB
   ════════════════════════════════════════════════════════════ */
function ConfigTab({ device, onEdit }: { device: any; onEdit: () => void }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">Monitoring Configuration</h3>
        <Button variant="outline" size="sm" onClick={onEdit}><Pencil className="h-3.5 w-3.5" /> Edit Settings</Button>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        <CfgCard title="Ping Monitoring" icon={<Activity className="h-4 w-4" />} rows={[
          ['Status', device.ping_enabled ? 'Enabled' : 'Disabled'],
          ['Interval', `${device.ping_interval}s`],
          ['Current RTT', device.last_rtt_ms != null ? `${device.last_rtt_ms.toFixed(2)} ms` : '—'],
        ]} />
        <CfgCard title="SNMP Polling" icon={<Network className="h-4 w-4" />} rows={[
          ['Status', device.snmp_enabled ? 'Enabled' : 'Disabled'],
          ['Version', `v${device.snmp_version}`],
          ['Port', String(device.snmp_port || 161)],
          ['Timeout', `${device.snmp_timeout_ms || 2000}ms`],
          ['Retries', String(device.snmp_retries ?? 2)],
          ['Max Repetitions', String(device.snmp_max_repetitions || 25)],
          ['Poll Interval', `${device.snmp_poll_interval || 60}s`],
        ]} />
        {device.snmp_version === '3' && (
          <CfgCard title="SNMPv3 USM" icon={<Shield className="h-4 w-4" />} rows={[
            ['Username', device.snmp_v3_username || '—'],
            ['Context', device.snmp_v3_context || '—'],
            ['Auth Protocol', device.snmp_auth_protocol || 'None'],
            ['Auth Passphrase', device.snmp_auth_configured ? '••••••••' : 'Not set'],
            ['Privacy Protocol', device.snmp_priv_protocol || 'None'],
            ['Privacy Passphrase', device.snmp_priv_configured ? '••••••••' : 'Not set'],
          ]} />
        )}
        <CfgCard title="Device Properties" icon={<Server className="h-4 w-4" />} rows={[
          ['Hostname', device.hostname],
          ['IP Address', device.ip_address],
          ['Type', device.device_type],
          ['Location', device.location || '—'],
          ['Group', device.group_name || '—'],
          ['Description', device.description || '—'],
          ['Created', relativeTime(device.created_at)],
        ]} />
      </div>
    </div>
  )
}

function CfgCard({ title, icon, rows }: { title: string; icon: React.ReactNode; rows: [string, string][] }) {
  return (
    <Card>
      <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm">{icon}{title}</CardTitle></CardHeader>
      <CardContent className="space-y-1.5 text-[13px]">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between border-b border-border/40 pb-1 last:border-0">
            <span className="text-[11px] text-muted">{k}</span><span className="font-medium text-right">{v}</span>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

/* ════════════════════════════════════════════════════════════
   INVENTORY TAB — hardware entities (chassis → modules → ports)
   ════════════════════════════════════════════════════════════ */

type Entity = {
  id: number
  ent_index: number
  parent_index: number
  class: string
  name: string
  serial_number: string | null
  model_name: string | null
  hw_revision: string | null
  fw_revision: string | null
  first_seen: string
  last_seen: string
}

const entityIcon = (cls: string, name?: string | null) => {
  const n = (name || '').toLowerCase()
  const c = (cls || '').toLowerCase()
  if (c === 'chassis' || c === 'stack') return <Server className="h-4 w-4" />
  if (c === 'module' && /fan/.test(n)) return <Fan className="h-4 w-4" />
  if (c === 'module' && /(power|psu)/.test(n)) return <Zap className="h-4 w-4" />
  if (c === 'module') return <HardDrive className="h-4 w-4" />
  if (c === 'container') return <Box className="h-4 w-4" />
  if (c === 'port') return <Plug className="h-4 w-4" />
  if (c === 'sensor') return <Thermometer className="h-4 w-4" />
  return <SquareStack className="h-4 w-4" />
}

function InventoryTab({ deviceId }: { deviceId: string }) {
  const [search, setSearch] = useState('')
  const [classFilter, setClassFilter] = useState<string>('')

  const { data: entities, isLoading } = useQuery<Entity[]>({
    queryKey: ['device', deviceId, 'inventory-entities'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/entities`)).data,
    refetchInterval: 60_000,
  })
  const { data: sensors } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'inventory-sensors'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/sensors`)).data,
    refetchInterval: 60_000,
  })

  const list = entities || []

  const counts = useMemo(() => {
    const byClass: Record<string, number> = {}
    for (const e of list) byClass[e.class] = (byClass[e.class] || 0) + 1
    const withSerial = list.filter((e) => e.serial_number && e.serial_number.trim()).length
    return { byClass, total: list.length, withSerial }
  }, [list])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return list.filter((e) => {
      if (classFilter && e.class !== classFilter) return false
      if (!q) return true
      return (
        e.name?.toLowerCase().includes(q) ||
        e.model_name?.toLowerCase().includes(q) ||
        e.serial_number?.toLowerCase().includes(q) ||
        e.class?.toLowerCase().includes(q)
      )
    })
  }, [list, search, classFilter])

  // Group by class for a clean "inventory sheet" presentation.
  const grouped = useMemo(() => {
    const order = ['chassis', 'stack', 'module', 'container', 'port', 'sensor']
    const buckets: Record<string, Entity[]> = {}
    for (const e of filtered) {
      const key = e.class || 'other'
      buckets[key] = buckets[key] || []
      buckets[key].push(e)
    }
    for (const k of Object.keys(buckets)) {
      buckets[k].sort((a, b) => a.ent_index - b.ent_index)
    }
    const ordered: Array<[string, Entity[]]> = []
    for (const k of order) if (buckets[k]) ordered.push([k, buckets[k]])
    for (const [k, v] of Object.entries(buckets)) if (!order.includes(k)) ordered.push([k, v])
    return ordered
  }, [filtered])

  const chassis = list.find((e) => e.class === 'chassis')

  return (
    <div className="space-y-4">
      {/* Summary strip */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6">
        <InvStat icon={<Server className="h-4 w-4" />} label="Model" value={chassis?.model_name || '—'} />
        <InvStat icon={<SquareStack className="h-4 w-4" />} label="Serial" value={chassis?.serial_number || '—'} mono />
        <InvStat icon={<HardDrive className="h-4 w-4" />} label="HW rev" value={chassis?.hw_revision || '—'} />
        <InvStat icon={<Box className="h-4 w-4" />} label="Components" value={String(counts.total)} />
        <InvStat
          icon={<Fan className="h-4 w-4" />}
          label="Fans"
          value={String((counts.byClass.module || 0) === 0 ? 0 : list.filter((e) => /fan/i.test(e.name)).length)}
        />
        <InvStat
          icon={<Zap className="h-4 w-4" />}
          label="PSUs"
          value={String(list.filter((e) => /power|psu/i.test(e.name)).length)}
        />
      </div>

      {/* Toolbar */}
      <Card>
        <CardContent className="space-y-3 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[220px] max-w-md flex-1">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <Input
                placeholder="Filter by name, model, serial…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8"
              />
            </div>
            <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
              {['', ...Object.keys(counts.byClass)].slice(0, 8).map((c) => (
                <button
                  key={c || 'all'}
                  onClick={() => setClassFilter(c)}
                  className={`rounded px-2.5 py-1 text-xs font-medium capitalize transition-colors ${
                    classFilter === c
                      ? 'bg-surface text-text shadow-sm'
                      : 'text-muted hover:text-text'
                  }`}
                >
                  {c === '' ? 'All' : c}{' '}
                  {c && (
                    <span className="ml-0.5 text-[10px] text-muted">({counts.byClass[c]})</span>
                  )}
                </button>
              ))}
            </div>
            <span className="ml-auto text-xs text-muted">
              {filtered.length} of {list.length} components · {counts.withSerial} with serial
            </span>
          </div>

          {isLoading && (
            <div className="py-10 text-center text-sm text-muted">
              <Loader2 className="mx-auto h-5 w-5 animate-spin" />
            </div>
          )}

          {!isLoading && list.length === 0 && (
            <div className="flex flex-col items-center gap-2 py-12 text-center text-muted">
              <Box className="h-8 w-8 opacity-50" />
              <div className="text-sm font-medium text-text">No inventory discovered</div>
              <div className="text-xs">Entities populate on the next SNMP poll when ENTITY-MIB is supported.</div>
            </div>
          )}

          {grouped.length > 0 && (
            <div className="space-y-5">
              {grouped.map(([cls, items]) => (
                <section key={cls}>
                  <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
                    {entityIcon(cls)}
                    <span>{cls}</span>
                    <span className="text-muted/70">· {items.length}</span>
                  </div>
                  <div className="overflow-hidden rounded-md border border-border">
                    <Table>
                      <THead className="bg-surface2/50">
                        <Tr>
                          <Th className="w-10 text-right">#</Th>
                          <Th>Name</Th>
                          <Th>Model</Th>
                          <Th>Serial</Th>
                          <Th>HW rev</Th>
                          <Th>FW rev</Th>
                          <Th>First seen</Th>
                          <Th>Last seen</Th>
                        </Tr>
                      </THead>
                      <TBody>
                        {items.map((e) => (
                          <Tr key={e.id}>
                            <Td className="text-right font-mono text-[11px] text-muted">
                              {e.ent_index}
                            </Td>
                            <Td>
                              <span className="inline-flex items-center gap-2">
                                <span className="text-muted">{entityIcon(e.class, e.name)}</span>
                                <span className="text-sm font-medium">{e.name}</span>
                              </span>
                            </Td>
                            <Td className="text-sm">{e.model_name || <span className="text-muted">—</span>}</Td>
                            <Td className="font-mono text-[11px]">
                              {e.serial_number || <span className="text-muted">—</span>}
                            </Td>
                            <Td className="text-xs">{e.hw_revision || <span className="text-muted">—</span>}</Td>
                            <Td className="text-xs">{e.fw_revision || <span className="text-muted">—</span>}</Td>
                            <Td className="text-xs text-muted">{relativeTime(e.first_seen)}</Td>
                            <Td className="text-xs text-muted">{relativeTime(e.last_seen)}</Td>
                          </Tr>
                        ))}
                      </TBody>
                    </Table>
                  </div>
                </section>
              ))}
            </div>
          )}

          {(sensors || []).length > 0 && (
            <section>
              <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
                <Thermometer className="h-3.5 w-3.5" />
                Sensors
                <span className="text-muted/70">· {sensors!.length}</span>
              </div>
              <div className="overflow-hidden rounded-md border border-border">
                <Table>
                  <THead className="bg-surface2/50">
                    <Tr>
                      <Th>Name</Th>
                      <Th>Type</Th>
                      <Th className="text-right">Value</Th>
                      <Th>Status</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {sensors!.map((s: any, idx: number) => (
                      <Tr key={idx}>
                        <Td className="text-sm">{s.name || s.description || `Sensor ${idx}`}</Td>
                        <Td className="text-xs text-muted">{s.type || '—'}</Td>
                        <Td className="text-right font-mono text-xs">
                          {s.value != null ? `${s.value}${s.unit ? ` ${s.unit}` : ''}` : '—'}
                        </Td>
                        <Td>
                          <span className="inline-flex items-center gap-1 text-xs">
                            <CheckCircle2 className="h-3 w-3 text-success" />
                            {s.status || 'ok'}
                          </span>
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            </section>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function InvStat({
  icon,
  label,
  value,
  mono,
}: {
  icon: React.ReactNode
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
        {icon}
        {label}
      </div>
      <div
        className={`mt-1 truncate text-sm font-semibold ${mono ? 'font-mono' : ''}`}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   Maintenance windows — banner + schedule/manage dialog
   ════════════════════════════════════════════════════════════ */

function MaintenanceBanner({ windows, onManage }: { windows: any[]; onManage: () => void }) {
  if (!windows.length) return null
  const w = windows[0]
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-primary/30 bg-primary/10 px-4 py-3 text-sm">
      <Wrench className="h-4 w-4 shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <span className="font-semibold text-primary">Under maintenance</span>{' '}
        <span>
          until {new Date(w.ends_at).toLocaleString()} — status changes, alerting and SLA impact are paused.
        </span>
        {w.reason ? <span className="text-muted"> Reason: {w.reason}</span> : null}
      </div>
      <Button variant="outline" size="sm" onClick={onManage}>Manage</Button>
    </div>
  )
}

const MAINT_DURATIONS: Array<{ value: string; label: string }> = [
  { value: '1800', label: '30 minutes' },
  { value: '3600', label: '1 hour' },
  { value: '7200', label: '2 hours' },
  { value: '14400', label: '4 hours' },
  { value: '28800', label: '8 hours' },
  { value: '86400', label: '24 hours' },
  { value: 'custom', label: 'Custom end time' },
]

function DeviceMaintenanceDialog({
  open, onOpenChange, device, windows,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  device: any
  windows?: { active: any[]; upcoming: any[] }
}) {
  const qc = useQueryClient()
  const [startMode, setStartMode] = useState<'now' | 'later'>('now')
  const [startAt, setStartAt] = useState('')
  const [duration, setDuration] = useState('3600')
  const [endAt, setEndAt] = useState('')
  const [reason, setReason] = useState('')

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['device-maintenance', device.id] })
    qc.invalidateQueries({ queryKey: ['device', device.id] })
    qc.invalidateQueries({ queryKey: ['devices'] })
  }

  const computeRange = (): { start: Date; end: Date } | null => {
    const start = startMode === 'now' ? new Date() : (startAt ? new Date(startAt) : null)
    if (!start || Number.isNaN(start.getTime())) return null
    const end = duration === 'custom'
      ? (endAt ? new Date(endAt) : null)
      : new Date(start.getTime() + Number(duration) * 1000)
    if (!end || Number.isNaN(end.getTime()) || end <= start) return null
    return { start, end }
  }
  const range = computeRange()

  const create = useMutation({
    mutationFn: async () => {
      const r = computeRange()
      if (!r) throw new Error('invalid range')
      return api.post('/device-maintenance', {
        scope_type: 'device',
        scope_device_id: device.id,
        starts_at: r.start.toISOString(),
        ends_at: r.end.toISOString(),
        reason: reason.trim() || null,
      })
    },
    onSuccess: () => {
      toast.success('Maintenance scheduled', 'Alerting and SLA impact are paused for this device during the window.')
      setReason('')
      invalidate()
    },
    onError: (e: any) => toast.error('Could not schedule maintenance', apiErrorMessage(e)),
  })

  const cancelWin = useMutation({
    mutationFn: async (mid: string) => api.delete(`/device-maintenance/${mid}`),
    onSuccess: () => { toast.success('Maintenance window removed'); invalidate() },
    onError: (e: any) => toast.error('Could not remove window', apiErrorMessage(e)),
  })

  const existing = [...(windows?.active || []), ...(windows?.upcoming || [])]
  const selectCls = 'h-9 w-full rounded-md border border-border bg-surface px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wrench className="h-4 w-4 text-primary" />
            Maintenance — {device.hostname}
          </DialogTitle>
          <DialogDescription>
            While a window is active the device keeps being polled and metrics are recorded,
            but status changes, alerts and notifications are suppressed and the window is
            excluded from uptime/SLA and reports.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted">Start</div>
              <select className={selectCls} value={startMode} onChange={(e) => setStartMode(e.target.value as any)}>
                <option value="now">Now</option>
                <option value="later">At a scheduled time</option>
              </select>
            </div>
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted">Duration</div>
              <select className={selectCls} value={duration} onChange={(e) => setDuration(e.target.value)}>
                {MAINT_DURATIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
              </select>
            </div>
          </div>

          {startMode === 'later' && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted">Start time</div>
              <Input type="datetime-local" value={startAt} onChange={(e) => setStartAt(e.target.value)} />
            </div>
          )}
          {duration === 'custom' && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted">End time</div>
              <Input type="datetime-local" value={endAt} onChange={(e) => setEndAt(e.target.value)} />
            </div>
          )}

          <div>
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted">Reason (shown in reports & audit log)</div>
            <Textarea rows={2} placeholder="e.g. Firmware upgrade, change #1234" value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>

          {range && (
            <div className="rounded-md border border-border bg-surface2 px-3 py-2 text-xs text-muted">
              Window: <span className="text-text">{range.start.toLocaleString()}</span> → <span className="text-text">{range.end.toLocaleString()}</span>
            </div>
          )}

          {existing.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted">Scheduled windows</div>
              <div className="space-y-1.5">
                {existing.map((w: any) => (
                  <div key={w.id} className="flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-xs">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${w.active ? 'bg-primary' : 'bg-muted'}`} />
                    <div className="min-w-0 flex-1">
                      <span className="font-medium">{w.active ? 'Active' : 'Upcoming'}</span>{' '}
                      <span className="text-muted">
                        {new Date(w.starts_at).toLocaleString()} → {new Date(w.ends_at).toLocaleString()}
                        {w.scope_type !== 'device' ? ` · via ${w.scope_label}` : ''}
                      </span>
                      {w.reason ? <div className="truncate text-muted" title={w.reason}>{w.reason}</div> : null}
                    </div>
                    {w.scope_type === 'device' && (
                      <Button
                        variant="outline" size="sm"
                        className="h-7 px-2 text-danger"
                        disabled={cancelWin.isPending}
                        onClick={() => cancelWin.mutate(w.id)}
                      >
                        {w.active ? 'End now' : 'Remove'}
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
          <Button size="sm" disabled={!range || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wrench className="h-3.5 w-3.5" />}
            Schedule maintenance
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
