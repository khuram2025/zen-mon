import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Activity, ArrowDown, ArrowUp, Box, CheckCircle2, Clock, Cpu, Fan, HardDrive,
  Loader2, MapPin, Network, Pencil, Plug, RefreshCw, Router as RouterIcon,
  Search, Server, Shield, SquareStack, Tag as TagIcon, Thermometer, Trash2,
  Wifi, Zap, AlertTriangle, ChevronRight,
} from 'lucide-react'
import {
  Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage, formatBps, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { DeviceFormDialog } from '@/components/forms/DeviceFormDialog'
import { toast } from '@/components/ui/Toast'

/* ── Helpers ─────────────────────────────────────────────── */
const statusColor: Record<string, string> = { up: 'bg-success', down: 'bg-danger', degraded: 'bg-warning', unknown: 'bg-muted', maintenance: 'bg-info' }
const statusVariant: Record<string, any> = { up: 'success', down: 'danger', degraded: 'warning', unknown: 'outline', maintenance: 'info' }
const ttStyle = () => ({
  contentStyle: { backgroundColor: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 6, color: 'rgb(var(--text))', fontSize: 11, padding: '5px 8px' },
  labelFormatter: (ts: any) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
})

/* ── Main Page ───────────────────────────────────────────── */
export function DeviceDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [editOpen, setEditOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)
  const [tab, setTab] = useState('summary')

  const { data: device, isLoading } = useQuery<any>({
    queryKey: ['device', id],
    queryFn: async () => (await api.get(`/devices/${id}`)).data,
    refetchInterval: 15_000, enabled: !!id,
  })

  const del = useMutation({
    mutationFn: async () => api.delete(`/devices/${id}`),
    onSuccess: () => { toast.success('Device deleted'); qc.invalidateQueries({ queryKey: ['devices'] }); navigate('/devices') },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  if (isLoading || !device) return <div className="flex items-center justify-center py-20 text-muted"><Loader2 className="h-5 w-5 animate-spin" /></div>

  const snmp = device.snmp_enabled

  return (
    <div className="space-y-4">
      <DeviceHero
        device={device}
        onEdit={() => setEditOpen(true)}
        onDelete={() => setDelOpen(true)}
        onRefresh={() => qc.invalidateQueries({ queryKey: ['device', id] })}
      />

      <DeviceKpiStrip device={device} deviceId={id!} />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="flex flex-wrap h-auto">
          <TabsTrigger value="summary">Overview</TabsTrigger>
          {snmp && <TabsTrigger value="interfaces">Interfaces</TabsTrigger>}
          {snmp && <TabsTrigger value="inventory">Inventory</TabsTrigger>}
          {snmp && <TabsTrigger value="traps">Events</TabsTrigger>}
          <TabsTrigger value="config">Configuration</TabsTrigger>
        </TabsList>

        <TabsContent value="summary"><SummaryDashboard device={device} deviceId={id!} /></TabsContent>
        {snmp && <TabsContent value="interfaces"><InterfacesTab deviceId={id!} /></TabsContent>}
        {snmp && <TabsContent value="inventory"><InventoryTab deviceId={id!} /></TabsContent>}
        {snmp && <TabsContent value="traps"><TrapsTab deviceId={id!} /></TabsContent>}
        <TabsContent value="config"><ConfigTab device={device} onEdit={() => setEditOpen(true)} /></TabsContent>
      </Tabs>

      <DeviceFormDialog open={editOpen} onOpenChange={setEditOpen} device={device} />
      <ConfirmDialog open={delOpen} onOpenChange={setDelOpen} title="Delete device"
        description={<>Permanently delete <b>{device.hostname}</b> and all its history?</>}
        confirmText="Delete" destructive loading={del.isPending} onConfirm={() => del.mutate()} />
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   HERO — large device banner with status glow + pill metadata
   ════════════════════════════════════════════════════════════ */

const deviceIconFor = (t: string) => {
  if (t === 'router') return <RouterIcon className="h-6 w-6" />
  if (t === 'switch') return <Network className="h-6 w-6" />
  if (t === 'firewall') return <Shield className="h-6 w-6" />
  if (t === 'server') return <Server className="h-6 w-6" />
  if (t === 'access_point') return <Wifi className="h-6 w-6" />
  return <Box className="h-6 w-6" />
}

function DeviceHero({
  device, onEdit, onDelete, onRefresh,
}: {
  device: any
  onEdit: () => void
  onDelete: () => void
  onRefresh: () => void
}) {
  const status = device.status as string
  const isUp = status === 'up'
  const ring =
    status === 'up'
      ? 'ring-success/40 shadow-[0_0_24px_rgb(var(--success)/0.35)]'
      : status === 'down'
        ? 'ring-danger/40 shadow-[0_0_24px_rgb(var(--danger)/0.35)]'
        : status === 'degraded'
          ? 'ring-warning/40 shadow-[0_0_24px_rgb(var(--warning)/0.35)]'
          : 'ring-border'

  return (
    <div className="relative overflow-hidden rounded-xl border border-border bg-gradient-to-br from-surface via-surface to-surface2/30 p-4 md:p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-4">
          <div
            className={`flex h-14 w-14 shrink-0 items-center justify-center rounded-xl bg-surface2/60 text-primary ring-2 ${ring}`}
          >
            {deviceIconFor(device.device_type)}
          </div>
          <div className="min-w-0">
            <Link
              to="/devices"
              className="mb-1 inline-flex items-center gap-1 text-[11px] text-muted hover:text-primary"
            >
              <ChevronRight className="h-3 w-3 rotate-180" /> All Devices
            </Link>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-2xl font-bold tracking-tight">{device.hostname}</h1>
              <code className="rounded bg-surface2/60 px-1.5 py-0.5 font-mono text-xs text-muted">
                {device.ip_address}
              </code>
              <span
                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                  isUp
                    ? 'bg-success/15 text-success'
                    : status === 'down'
                      ? 'bg-danger/15 text-danger'
                      : status === 'degraded'
                        ? 'bg-warning/15 text-warning'
                        : 'bg-surface2 text-muted'
                }`}
              >
                <span
                  className={`h-1.5 w-1.5 rounded-full ${
                    isUp ? 'animate-pulse bg-success' : status === 'down' ? 'bg-danger' : 'bg-muted'
                  }`}
                />
                {status.toUpperCase()}
              </span>
              {device.snmp_enabled && (
                <Badge variant="info" className="gap-1">
                  <Shield className="h-3 w-3" /> SNMPv{device.snmp_version}
                </Badge>
              )}
              {device.ping_enabled && (
                <Badge variant="outline" className="gap-1">
                  <Wifi className="h-3 w-3" /> ICMP
                </Badge>
              )}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted">
              <span className="inline-flex items-center gap-1 capitalize">
                {deviceIconFor(device.device_type)}
                <span>{device.device_type.replace('_', ' ')}</span>
              </span>
              {(device.vendor || device.model) && (
                <span className="inline-flex items-center gap-1">
                  <SquareStack className="h-3.5 w-3.5" />
                  {[device.vendor, device.model].filter(Boolean).join(' ') || '—'}
                </span>
              )}
              {device.group_name && (
                <span className="inline-flex items-center gap-1">
                  <TagIcon className="h-3.5 w-3.5" />
                  {device.group_name}
                </span>
              )}
              {device.location && (
                <span className="inline-flex items-center gap-1">
                  <MapPin className="h-3.5 w-3.5" />
                  {device.location}
                </span>
              )}
              <span className="inline-flex items-center gap-1">
                <Clock className="h-3.5 w-3.5" />
                seen {relativeTime(device.last_seen)}
              </span>
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="outline" size="sm" onClick={onRefresh} title="Refresh">
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="text-danger hover:bg-danger/10"
            onClick={onDelete}
            title="Delete"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   KPI STRIP — six live tiles across the top
   ════════════════════════════════════════════════════════════ */

function DeviceKpiStrip({ device, deviceId }: { device: any; deviceId: string }) {
  // 1h ping metrics drive RTT/loss/jitter tiles.
  const { data: ping } = useQuery<{ points: any[] }>({
    queryKey: ['device', deviceId, 'kpi-ping'],
    queryFn: async () => {
      const now = new Date()
      const from = new Date(now.getTime() - 3600_000).toISOString()
      return (await api.get(`/devices/${deviceId}/metrics?from=${from}&to=${now.toISOString()}`)).data
    },
    refetchInterval: 30_000,
    enabled: device.ping_enabled,
  })
  const { data: ifs } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'kpi-ifs'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    refetchInterval: 30_000,
    enabled: device.snmp_enabled,
  })
  const { data: entities } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'kpi-entities'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/entities`)).data,
    enabled: device.snmp_enabled,
  })

  const pts = ping?.points || []
  const lastPt = pts[pts.length - 1]
  const rtts = pts.map((p) => p.rtt_ms).filter((v) => v != null)
  const avgRtt = rtts.length ? rtts.reduce((a, b) => a + b, 0) / rtts.length : null
  const avgLoss = pts.length ? (pts.reduce((a, p) => a + (p.packet_loss || 0), 0) / pts.length) : null
  const avgJitter = pts.length ? (pts.reduce((a, p) => a + (p.jitter_ms || 0), 0) / pts.length) : null

  const ifTotal = ifs?.length || 0
  const ifUp = (ifs || []).filter((i) => i.oper_status === 'up').length

  // Hardware health — count PSUs + Fans from the entity inventory.
  const modules = (entities || []).filter(
    (e) => e.class === 'module' && /fan|power|psu/i.test(e.name || ''),
  )
  const hwOk = modules.length
  const chassisClass = (entities || []).find((e) => e.class === 'chassis')

  const rttTone =
    avgRtt == null ? 'muted' : avgRtt < 20 ? 'success' : avgRtt < 100 ? 'warning' : 'danger'

  const tiles: KpiTile[] = [
    {
      icon: <Activity className="h-4 w-4" />,
      label: 'Status',
      value: device.status.toUpperCase(),
      sub: `seen ${relativeTime(device.last_seen)}`,
      tone:
        device.status === 'up'
          ? 'success'
          : device.status === 'down'
            ? 'danger'
            : device.status === 'degraded'
              ? 'warning'
              : 'muted',
    },
    {
      icon: <Zap className="h-4 w-4" />,
      label: 'RTT (1h avg)',
      value: avgRtt != null ? `${avgRtt.toFixed(2)} ms` : '—',
      sub: lastPt?.rtt_ms != null ? `last ${lastPt.rtt_ms.toFixed(2)} ms` : 'no data',
      tone: rttTone,
    },
    {
      icon: <AlertTriangle className="h-4 w-4" />,
      label: 'Packet loss (1h)',
      value: avgLoss != null ? `${(avgLoss * 100).toFixed(1)}%` : '—',
      sub: `${pts.length} samples`,
      tone: avgLoss == null ? 'muted' : avgLoss > 0.01 ? 'danger' : 'success',
    },
    {
      icon: <Activity className="h-4 w-4" />,
      label: 'Jitter (1h)',
      value: avgJitter != null ? `${avgJitter.toFixed(2)} ms` : '—',
      sub: 'avg',
      tone: avgJitter == null ? 'muted' : avgJitter < 1 ? 'success' : avgJitter < 5 ? 'warning' : 'danger',
    },
    {
      icon: <Plug className="h-4 w-4" />,
      label: 'Interfaces',
      value: ifTotal > 0 ? `${ifUp}/${ifTotal}` : '—',
      sub: ifTotal > 0 ? `${ifTotal - ifUp} down` : device.snmp_enabled ? 'discovering' : 'SNMP off',
      tone: ifTotal === 0 ? 'muted' : ifUp === ifTotal ? 'success' : 'warning',
    },
    {
      icon: <Fan className="h-4 w-4" />,
      label: 'Hardware',
      value: chassisClass ? (hwOk > 0 ? 'Healthy' : 'OK') : device.snmp_enabled ? '—' : 'SNMP off',
      sub: chassisClass
        ? `${chassisClass.model_name || chassisClass.name}${hwOk ? ` · ${hwOk} modules` : ''}`
        : '',
      tone: chassisClass ? 'success' : 'muted',
    },
  ]

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {tiles.map((t, i) => <KpiCard key={i} tile={t} />)}
    </div>
  )
}

type KpiTile = {
  icon: React.ReactNode
  label: string
  value: string
  sub?: string
  tone?: 'success' | 'warning' | 'danger' | 'muted'
}

function KpiCard({ tile }: { tile: KpiTile }) {
  const toneClass =
    tile.tone === 'success'
      ? 'text-success'
      : tile.tone === 'warning'
        ? 'text-warning'
        : tile.tone === 'danger'
          ? 'text-danger'
          : 'text-text'
  return (
    <div className="rounded-lg border border-border bg-surface p-3 transition-colors hover:border-border-strong">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
        <span className={toneClass}>{tile.icon}</span>
        {tile.label}
      </div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${toneClass}`}>
        {tile.value}
      </div>
      {tile.sub && (
        <div className="mt-0.5 truncate text-[11px] text-muted" title={tile.sub}>
          {tile.sub}
        </div>
      )}
    </div>
  )
}

/* ════════════════════════════════════════════════════════════
   SUMMARY DASHBOARD — Everything visible on one scroll
   ════════════════════════════════════════════════════════════ */

const TIME_RANGES = [
  { label: '1H', hours: 1 },
  { label: '6H', hours: 6 },
  { label: '12H', hours: 12 },
  { label: '24H', hours: 24 },
  { label: '7D', hours: 168 },
] as const

function TimeRangeFilter({ value, onChange }: { value: number; onChange: (h: number) => void }) {
  return (
    <div className="flex items-center gap-1 rounded-lg bg-surface2/60 p-0.5">
      {TIME_RANGES.map(({ label, hours }) => (
        <button key={hours} onClick={() => onChange(hours)}
          className={`rounded-md px-3 py-1.5 text-[11px] font-medium transition-all duration-200 ${
            value === hours
              ? 'bg-bg text-primary shadow-sm ring-1 ring-border/50'
              : 'text-muted hover:text-text hover:bg-surface2'
          }`}>
          {label}
        </button>
      ))}
    </div>
  )
}

function SummaryDashboard({ device, deviceId }: { device: any; deviceId: string }) {
  const snmp = device.snmp_enabled
  const [hoursRange, setHoursRange] = useState(6)

  // Ping metrics for RTT chart
  const { data: pingData } = useQuery<{ points: { timestamp: string; rtt_ms: number; packet_loss: number; jitter_ms: number; min_rtt_ms: number; max_rtt_ms: number; is_up: boolean }[] }>({
    queryKey: ['device', deviceId, 'ping-metrics', hoursRange],
    queryFn: async () => {
      const now = new Date()
      const from = new Date(now.getTime() - hoursRange * 3600_000).toISOString()
      return (await api.get(`/devices/${deviceId}/metrics?from=${from}&to=${now.toISOString()}`)).data
    },
    refetchInterval: 30_000, enabled: device.ping_enabled,
  })

  // SNMP metrics (CPU, memory, temps)
  const { data: metrics } = useQuery<Record<string, { unit: string; points: { ts: number; value: number }[] }>>({
    queryKey: ['device', deviceId, 'snmp-metrics', hoursRange],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-metrics?hours=${hoursRange}`)).data,
    refetchInterval: 30_000, enabled: snmp,
  })

  // Interfaces for summary
  const { data: ifs } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'interfaces'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
    refetchInterval: 30_000, enabled: snmp,
  })

  // Interface metrics
  const { data: ifMetrics } = useQuery<Record<string, any[]>>({
    queryKey: ['device', deviceId, 'if-metrics'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/snmp-if-metrics?hours=1`)).data,
    refetchInterval: 30_000, enabled: snmp,
  })

  // Entities & sensors
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

  // Traps
  const { data: traps } = useQuery<any[]>({
    queryKey: ['device', deviceId, 'traps-summary'],
    queryFn: async () => (await api.get(`/devices/${deviceId}/traps?hours=24&limit=5`)).data,
    refetchInterval: 30_000, enabled: snmp,
  })

  const cpu = metrics?.cpu
  const mem = metrics?.memory
  const cpuVal = cpu?.points?.length ? cpu.points[cpu.points.length - 1].value : null
  const memVal = mem?.points?.length ? mem.points[mem.points.length - 1].value : null
  const temps = Object.entries(metrics || {}).filter(([k]) => k.startsWith('temperature'))

  // Interface stats
  const ifUp = (ifs || []).filter((i) => i.oper_status === 'up').length
  const ifDown = (ifs || []).length - ifUp

  // Top interfaces by traffic
  const topIfs = (ifs || [])
    .map((i) => {
      const s = ifMetrics?.[i.if_index]
      const last = s?.length ? s[s.length - 1] : null
      return { ...i, inBps: last?.in_bps || 0, outBps: last?.out_bps || 0, totalBps: (last?.in_bps || 0) + (last?.out_bps || 0) }
    })
    .filter((i) => i.totalBps > 0)
    .sort((a, b) => b.totalBps - a.totalBps)
    .slice(0, 5)

  const rangeLabel = hoursRange < 24 ? `Last ${hoursRange}h` : `Last ${hoursRange / 24}d`

  // Precompute ping stats
  const pts = pingData?.points || []
  const rttPts = pts.map((p) => ({ ts: new Date(p.timestamp).getTime(), rtt: p.rtt_ms, loss: p.packet_loss, jitter: p.jitter_ms }))
  const lastRtt = rttPts.length ? rttPts[rttPts.length - 1].rtt : device.last_rtt_ms
  const avgRtt = rttPts.length ? rttPts.reduce((s, p) => s + p.rtt, 0) / rttPts.length : null
  const maxRtt = rttPts.length ? Math.max(...rttPts.map((p) => p.rtt)) : null
  const minRtt = rttPts.length ? Math.min(...rttPts.map((p) => p.rtt)) : null
  const avgLoss = rttPts.length ? rttPts.reduce((s, p) => s + p.loss, 0) / rttPts.length : null
  const maxLoss = rttPts.length ? Math.max(...rttPts.map((p) => p.loss)) : null
  const avgJitter = rttPts.length ? rttPts.reduce((s, p) => s + p.jitter, 0) / rttPts.length : null

  // Count gauge items to determine grid sizing
  const gaugeItems: { label: string; value: number | null; unit: string; color: string; icon: React.ReactNode; data?: { ts: number; value: number }[] }[] = []
  if (snmp && cpu) gaugeItems.push({ label: 'CPU Utilization', value: cpuVal, unit: '%', color: 'rgb(var(--info))', icon: <Cpu className="h-4 w-4" />, data: cpu.points })
  if (snmp && mem) gaugeItems.push({ label: 'Memory Utilization', value: memVal, unit: '%', color: 'rgb(var(--warning))', icon: <HardDrive className="h-4 w-4" />, data: mem.points })
  if (snmp) temps.slice(0, 2).forEach(([k, v]) => gaugeItems.push({ label: k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()), value: v.points?.length ? v.points[v.points.length - 1].value : null, unit: '°C', color: 'rgb(var(--danger))', icon: <Thermometer className="h-4 w-4" />, data: v.points }))

  return (
    <div className="space-y-5">
      {/* ══════════ TIME RANGE BAR ══════════ */}
      <div className="flex items-center justify-between rounded-lg border border-border/50 bg-surface2/20 px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <Clock className="h-4 w-4 text-primary" />
          <span className="text-sm font-semibold">Performance Overview</span>
          <span className="text-[11px] text-muted">— {rangeLabel}</span>
        </div>
        <TimeRangeFilter value={hoursRange} onChange={setHoursRange} />
      </div>

      {/* ══════════ ROW 1: NODE INFO (left) + POLLING & PORTS (right) ══════════ */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-1"><CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted">Node Details</CardTitle></CardHeader>
          <CardContent className="space-y-1.5 text-[13px]">
            {([
              ['Hostname', device.hostname],
              ['IP Address', device.ip_address],
              ['Device Type', device.device_type],
              ['Vendor / Model', [device.vendor, device.model].filter(Boolean).join(' ') || '—'],
              ['OS Version', device.os_version || '—'],
              ['System OID', device.sys_object_id || '—'],
              ['Location', device.location || '—'],
              ['Group', device.group_name || '—'],
              ['Last Seen', relativeTime(device.last_seen)],
              ['Description', device.description || '—'],
            ] as [string, string][]).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3 border-b border-border/40 pb-1 last:border-0">
                <span className="shrink-0 text-[11px] text-muted">{k}</span>
                <span className={`text-right truncate font-medium ${k === 'System OID' ? 'font-mono text-[11px]' : ''}`}>{v}</span>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-1"><CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted">Polling & Availability</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <div>
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-[11px] text-muted">Current Status</span>
                <Badge variant={statusVariant[device.status] || 'outline'} className="capitalize text-xs">{device.status}</Badge>
              </div>
              <div className="h-2 w-full rounded-full bg-surface2 overflow-hidden">
                <div className={`h-full rounded-full ${device.status === 'up' ? 'bg-success' : device.status === 'down' ? 'bg-danger' : 'bg-warning'}`} style={{ width: '100%' }} />
              </div>
            </div>
            <div className="space-y-1.5 text-[13px]">
              {([
                ['Ping Monitoring', device.ping_enabled ? `Enabled · ${device.ping_interval}s interval` : 'Disabled'],
                ['SNMP Polling', snmp ? `v${device.snmp_version} · Port ${device.snmp_port} · ${device.snmp_poll_interval}s interval` : 'Disabled'],
                ['SNMP Timeout', snmp ? `${device.snmp_timeout_ms || 2000}ms · ${device.snmp_retries ?? 2} retries` : '—'],
                ['Response Time', device.last_rtt_ms != null ? `${device.last_rtt_ms.toFixed(2)} ms` : '—'],
              ] as [string, string][]).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3 border-b border-border/40 pb-1 last:border-0">
                  <span className="shrink-0 text-[11px] text-muted">{k}</span>
                  <span className="text-right font-medium">{v}</span>
                </div>
              ))}
            </div>
            {snmp && ifs && ifs.length > 0 && (
              <div className="rounded-md bg-surface2/50 px-3 py-2">
                <div className="text-[11px] text-muted mb-1">Ethernet Ports</div>
                <div className="flex items-center gap-3 text-sm">
                  <span className="font-bold">{ifs.length}</span><span className="text-muted">total</span>
                  <span className="text-success font-bold">{ifUp}</span><span className="text-muted">up</span>
                  <span className="text-danger font-bold">{ifDown}</span><span className="text-muted">down</span>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* ══════════ ROW 2: RESPONSE TIME & PACKET LOSS ══════════ */}
      <Card>
        <CardHeader className="pb-2 border-b border-border/30">
          <div className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2 text-sm font-semibold">
              <Activity className="h-4 w-4 text-primary" />
              Response Time & Packet Loss
            </CardTitle>
            <span className="rounded-md bg-surface2/60 px-2.5 py-1 text-[11px] text-muted">{rangeLabel}</span>
          </div>
        </CardHeader>
        <CardContent className="pt-4">
          {/* KPI Stats */}
          <div className="grid grid-cols-3 gap-3 lg:grid-cols-6 mb-5">
            {([
              ['Current RTT', lastRtt, 'ms', 'text-primary'],
              ['Avg RTT', avgRtt, 'ms', 'text-text'],
              ['Min / Max', null, 'ms', 'text-text'],
              ['Avg Jitter', avgJitter, 'ms', 'text-info'],
              ['Avg Loss', avgLoss, '%', (avgLoss ?? 0) > 1 ? 'text-danger' : 'text-success'],
              ['Max Loss', maxLoss, '%', (maxLoss ?? 0) > 5 ? 'text-danger' : (maxLoss ?? 0) > 0 ? 'text-warning' : 'text-success'],
            ] as [string, number | null, string, string][]).map(([label, val, u, cls], idx) => (
              <div key={label} className="rounded-lg border border-border/30 bg-surface2/30 px-3 py-2.5 text-center">
                <div className="text-[10px] font-medium uppercase tracking-wider text-muted mb-1.5">{label}</div>
                <div className={`text-lg font-bold leading-tight ${cls}`}>
                  {idx === 2
                    ? <>{minRtt != null ? minRtt.toFixed(1) : '—'}<span className="text-muted text-xs font-normal mx-0.5">/</span>{maxRtt != null ? maxRtt.toFixed(1) : '—'}</>
                    : <>{val != null ? val.toFixed(2) : '—'}</>
                  }
                  <span className="ml-0.5 text-[10px] text-muted font-normal">{u}</span>
                </div>
              </div>
            ))}
          </div>

          {/* Dual Charts — equal height, aligned */}
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full bg-primary" />
                <span className="text-xs font-medium">Response Time</span>
              </div>
              <div className="h-48 rounded-xl border border-border/20 bg-surface2/10 p-3">
                {rttPts.length > 2 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={rttPts}>
                      <defs>
                        <linearGradient id="rttGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.2} />
                          <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                      <XAxis dataKey="ts" tickFormatter={(ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} width={40} domain={[0, 'auto']} axisLine={false} tickLine={false} tickFormatter={(v) => `${v} ms`} />
                      <Tooltip {...ttStyle()} formatter={(v: any) => [`${Number(v).toFixed(2)} ms`, 'RTT']} />
                      <Area type="monotone" dataKey="rtt" stroke="rgb(var(--primary))" fill="url(#rttGrad)" strokeWidth={2} dot={false} activeDot={{ r: 3, strokeWidth: 0 }} />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center text-xs text-muted">
                    {device.ping_enabled ? 'Collecting ping data...' : 'Ping monitoring disabled'}
                  </div>
                )}
              </div>
            </div>

            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full bg-danger" />
                <span className="text-xs font-medium">Packet Loss</span>
              </div>
              <div className="h-48 rounded-xl border border-border/20 bg-surface2/10 p-3">
                {rttPts.length > 2 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={rttPts}>
                      <defs>
                        <linearGradient id="lossGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="rgb(var(--danger))" stopOpacity={0.25} />
                          <stop offset="100%" stopColor="rgb(var(--danger))" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                      <XAxis dataKey="ts" tickFormatter={(ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} width={40} domain={[0, (max: number) => Math.max(max, 1)]} axisLine={false} tickLine={false} tickFormatter={(v) => `${v} %`} />
                      <Tooltip {...ttStyle()} formatter={(v: any) => [`${Number(v).toFixed(2)} %`, 'Loss']} />
                      <Area type="monotone" dataKey="loss" stroke="rgb(var(--danger))" fill="url(#lossGrad)" strokeWidth={2} dot={false} activeDot={{ r: 3, strokeWidth: 0 }} />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center text-xs text-muted">
                    {device.ping_enabled ? 'Collecting data...' : 'Ping monitoring disabled'}
                  </div>
                )}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* ══════════ ROW 3: CPU + MEMORY GAUGES (adaptive grid) ══════════ */}
      {snmp && gaugeItems.length > 0 && (
        <div className={`grid grid-cols-1 gap-4 ${gaugeItems.length === 1 ? 'md:grid-cols-1 max-w-sm' : gaugeItems.length === 2 ? 'md:grid-cols-2' : gaugeItems.length === 3 ? 'md:grid-cols-3' : 'md:grid-cols-2 lg:grid-cols-4'}`}>
          {gaugeItems.map((g) => (
            <GaugeCard key={g.label} {...g} />
          ))}
        </div>
      )}
      {snmp && gaugeItems.length === 0 && (cpu || mem || temps.length > 0) === false && (
        <Card>
          <CardContent className="py-5 text-center">
            <div className="text-sm text-muted">No CPU/Memory metrics available</div>
            <div className="text-[11px] text-muted mt-1">This device may require a vendor-specific SNMP profile. Standard HOST-RESOURCES-MIB data not detected.</div>
          </CardContent>
        </Card>
      )}

      {/* ══════════ ROW 4: TOP INTERFACES + HARDWARE (equal height) ══════════ */}
      {snmp && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Card className="flex flex-col">
            <CardHeader className="pb-1">
              <div className="flex items-center justify-between">
                <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted">Top Interfaces by Traffic</CardTitle>
                <button onClick={() => setTab('interfaces')} className="text-[11px] text-primary hover:underline">View all</button>
              </div>
            </CardHeader>
            <CardContent className="flex-1 p-0">
              <Table>
                <THead className="bg-surface2/40">
                  <Tr><Th>Interface</Th><Th className="text-right">In</Th><Th className="text-right">Out</Th><Th className="w-24">Utilization</Th></Tr>
                </THead>
                <TBody>
                  {topIfs.map((i) => {
                    const pct = i.if_speed ? Math.min(100, (i.totalBps / Number(i.if_speed)) * 100) : 0
                    return (
                      <Tr key={i.id}>
                        <Td className="text-sm font-medium">{i.if_name || i.if_descr}</Td>
                        <Td className="text-right font-mono text-xs">{formatBps(i.inBps)}</Td>
                        <Td className="text-right font-mono text-xs">{formatBps(i.outBps)}</Td>
                        <Td>
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 flex-1 rounded-full bg-surface2 overflow-hidden">
                              <div className={`h-full rounded-full ${pct > 80 ? 'bg-danger' : pct > 50 ? 'bg-warning' : 'bg-primary'}`} style={{ width: `${pct}%` }} />
                            </div>
                            <span className="text-[10px] text-muted w-8 text-right">{pct.toFixed(0)}%</span>
                          </div>
                        </Td>
                      </Tr>
                    )
                  })}
                  {topIfs.length === 0 && <Tr><Td colSpan={4} className="py-4 text-center text-xs text-muted">No active traffic</Td></Tr>}
                </TBody>
              </Table>
            </CardContent>
          </Card>

          <Card className="flex flex-col">
            <CardHeader className="pb-1"><CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted">Hardware Health</CardTitle></CardHeader>
            <CardContent className="flex-1">
              {sensors && sensors.length > 0 ? (
                <div className="space-y-2">
                  {sensors.slice(0, 8).map((s) => (
                    <div key={s.id} className="flex items-center justify-between border-b border-border/40 pb-1.5 last:border-0">
                      <div className="flex items-center gap-2">
                        <Badge variant={s.sensor_type === 'celsius' ? 'danger' : s.sensor_type === 'rpm' ? 'info' : s.sensor_type === 'voltsAC' || s.sensor_type === 'voltsDC' ? 'warning' : 'outline'} className="text-[9px] px-1.5 py-0">{s.sensor_type}</Badge>
                        <span className="text-sm">{s.description || `Sensor ${s.sensor_index}`}</span>
                      </div>
                      <span className="text-xs text-muted">{s.unit || '—'}</span>
                    </div>
                  ))}
                  {sensors.length > 8 && <div className="text-[11px] text-muted">+{sensors.length - 8} more sensors</div>}
                </div>
              ) : entities && entities.length > 0 ? (
                <div className="space-y-2">
                  <div className="text-[11px] text-muted mb-2">Hardware Inventory ({entities.length} components)</div>
                  {entities.slice(0, 6).map((e) => (
                    <div key={e.id} className="flex items-center justify-between border-b border-border/40 pb-1.5 last:border-0">
                      <div className="flex items-center gap-2">
                        <Badge variant="outline" className="text-[9px] px-1.5 py-0">{e.class || 'hw'}</Badge>
                        <span className="text-sm">{e.name || '—'}</span>
                      </div>
                      <span className="font-mono text-[11px] text-muted">{e.serial_number || e.model_name || '—'}</span>
                    </div>
                  ))}
                  {entities.length > 6 && <div className="text-[11px] text-muted">+{entities.length - 6} more</div>}
                </div>
              ) : (
                <div className="py-4 text-center text-xs text-muted">No hardware data (ENTITY-MIB not supported by this device)</div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* ══════════ ROW 5: DETAILED PERFORMANCE CHARTS ══════════ */}
      {(() => {
        const rttChartPts = (pingData?.points || []).map((p) => ({ ts: new Date(p.timestamp).getTime(), value: p.rtt_ms }))
        const lossChartPts = (pingData?.points || []).map((p) => ({ ts: new Date(p.timestamp).getTime(), value: p.packet_loss }))
        const perfCharts: { title: string; unit: string; data: { ts: number; value: number }[]; color: string; gradientId: string }[] = []
        if (rttChartPts.length > 2) perfCharts.push({ title: `Response Time (${rangeLabel})`, unit: ' ms', data: rttChartPts, color: 'rgb(var(--primary))', gradientId: 'perfRtt' })
        if (cpu) perfCharts.push({ title: `CPU Usage (${rangeLabel})`, unit: '%', data: cpu.points, color: 'rgb(var(--info))', gradientId: 'perfCpu' })
        if (mem) perfCharts.push({ title: `Memory Usage (${rangeLabel})`, unit: '%', data: mem.points, color: 'rgb(var(--warning))', gradientId: 'perfMem' })
        if (lossChartPts.length > 2 && lossChartPts.some((p) => p.value > 0)) perfCharts.push({ title: `Packet Loss (${rangeLabel})`, unit: '%', data: lossChartPts, color: 'rgb(var(--danger))', gradientId: 'perfLoss' })
        if (perfCharts.length === 0) return null
        return (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Zap className="h-4 w-4 text-primary" />
              <span className="text-sm font-semibold">Detailed Performance</span>
            </div>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {perfCharts.map((c) => <PerfChart key={c.gradientId} {...c} />)}
            </div>
          </div>
        )
      })()}

      {/* ══════════ ROW 6: RECENT EVENTS ══════════ */}
      {snmp && traps && traps.length > 0 && (
        <Card>
          <CardHeader className="pb-1">
            <div className="flex items-center justify-between">
              <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted">Recent Events (24h)</CardTitle>
              <button onClick={() => setTab('traps')} className="text-[11px] text-primary hover:underline">View all</button>
            </div>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <THead className="bg-surface2/40">
                <Tr><Th>Time</Th><Th>Severity</Th><Th>Trap OID</Th><Th>Message</Th></Tr>
              </THead>
              <TBody>
                {traps.map((t, i) => (
                  <Tr key={i}>
                    <Td className="text-xs text-muted whitespace-nowrap">{relativeTime(t.timestamp)}</Td>
                    <Td><Badge variant={t.severity === 'critical' ? 'danger' : t.severity === 'warning' ? 'warning' : 'info'} className="text-[10px]">{t.severity}</Badge></Td>
                    <Td className="font-mono text-[11px] text-muted truncate max-w-[200px]">{t.trap_oid}</Td>
                    <Td className="text-sm truncate max-w-[300px]">{t.message || t.trap_name || '—'}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

/* ── Radial Gauge SVG ───────────────────────────────────── */
function RadialGauge({ value, color, size = 100 }: { value: number; color: string; size?: number }) {
  const pct = Math.min(100, Math.max(0, value))
  const r = (size - 12) / 2
  const cx = size / 2
  const cy = size / 2
  const circumference = 2 * Math.PI * r
  // Arc from 135° to 405° (270° sweep)
  const arcLength = circumference * 0.75
  const filledLength = arcLength * (pct / 100)
  const startAngle = 135

  const polarToCartesian = (angle: number) => {
    const rad = ((angle - 90) * Math.PI) / 180
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
  }

  const describeArc = (start: number, end: number) => {
    const s = polarToCartesian(start)
    const e = polarToCartesian(end)
    const largeArc = end - start > 180 ? 1 : 0
    return `M ${s.x} ${s.y} A ${r} ${r} 0 ${largeArc} 1 ${e.x} ${e.y}`
  }

  const endAngle = startAngle + 270
  const filledEnd = startAngle + (270 * pct) / 100

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {/* Background track */}
      <path d={describeArc(startAngle, endAngle)} fill="none" stroke="rgb(var(--border)/0.4)" strokeWidth={8} strokeLinecap="round" />
      {/* Filled arc */}
      {pct > 0 && (
        <path d={describeArc(startAngle, filledEnd)} fill="none" stroke={color} strokeWidth={8} strokeLinecap="round" style={{ filter: `drop-shadow(0 0 4px ${color})`, transition: 'all 0.6s ease' }} />
      )}
    </svg>
  )
}

/* ── Gauge Card ─────────────────────────────────────────── */
function GaugeCard({ label, value, unit, color, icon, data }: {
  label: string; value: number | null; unit: string; color: string; icon: React.ReactNode; data?: { ts: number; value: number }[]
}) {
  const pct = Math.min(100, Math.max(0, value ?? 0))
  const avg = data?.length ? data.reduce((s, p) => s + p.value, 0) / data.length : null
  const min = data?.length ? Math.min(...data.map((p) => p.value)) : null
  const max = data?.length ? Math.max(...data.map((p) => p.value)) : null

  const gaugeColor = unit === '%' && pct > 90 ? 'rgb(var(--danger))' : unit === '%' && pct > 75 ? 'rgb(var(--warning))' : color

  return (
    <Card>
      <CardContent className="p-4">
        {/* Header */}
        <div className="flex items-center gap-2 mb-3">
          <div className="flex items-center justify-center h-7 w-7 rounded-lg bg-surface2/80" style={{ color }}>
            {icon}
          </div>
          <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</span>
        </div>

        {/* Gauge + Value */}
        <div className="flex items-center justify-center">
          <div className="relative">
            <RadialGauge value={pct} color={gaugeColor} size={110} />
            <div className="absolute inset-0 flex flex-col items-center justify-center" style={{ paddingTop: 4 }}>
              <span className="text-2xl font-bold leading-none" style={{ color: gaugeColor }}>{value != null ? value.toFixed(1) : '—'}</span>
              <span className="text-[10px] text-muted mt-0.5">{unit}</span>
            </div>
          </div>
        </div>

        {/* Stats Row */}
        <div className="mt-2 grid grid-cols-3 gap-1 text-center">
          <div className="rounded bg-surface2/40 py-1">
            <div className="text-[9px] text-muted uppercase">Min</div>
            <div className="text-xs font-semibold">{min != null ? min.toFixed(1) : '—'}</div>
          </div>
          <div className="rounded bg-surface2/40 py-1">
            <div className="text-[9px] text-muted uppercase">Avg</div>
            <div className="text-xs font-semibold">{avg != null ? avg.toFixed(1) : '—'}</div>
          </div>
          <div className="rounded bg-surface2/40 py-1">
            <div className="text-[9px] text-muted uppercase">Max</div>
            <div className="text-xs font-semibold">{max != null ? max.toFixed(1) : '—'}</div>
          </div>
        </div>

        {/* Sparkline */}
        {data && data.length > 2 && (
          <div className="h-10 mt-2 rounded-md bg-surface2/20 px-1">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data.slice(-40)}>
                <defs>
                  <linearGradient id={`spark-${label.replace(/\s/g, '')}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={color} stopOpacity={0.15} />
                    <stop offset="100%" stopColor={color} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <Area type="monotone" dataKey="value" stroke={color} fill={`url(#spark-${label.replace(/\s/g, '')})`} strokeWidth={1.5} dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/* ── Performance Chart (larger) ─────────────────────────── */
function PerfChart({ title, unit, data, color, gradientId = 'perfGrad' }: { title: string; unit: string; data: { ts: number; value: number }[]; color: string; gradientId?: string }) {
  if (!data || data.length < 2) return null
  const current = data[data.length - 1].value
  const avg = data.reduce((s, p) => s + p.value, 0) / data.length
  const max = Math.max(...data.map((p) => p.value))
  const min = Math.min(...data.map((p) => p.value))

  return (
    <Card>
      <CardHeader className="pb-1">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-semibold">{title}</CardTitle>
          <div className="flex items-center gap-3">
            <span className="text-[10px] text-muted">Current:</span>
            <span className="text-sm font-bold" style={{ color }}>{current.toFixed(1)}{unit}</span>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {/* Mini stats */}
        <div className="flex items-center gap-4 mb-3 text-[10px] text-muted">
          <span>Min: <b className="text-text">{min.toFixed(1)}{unit}</b></span>
          <span>Avg: <b className="text-text">{avg.toFixed(1)}{unit}</b></span>
          <span>Max: <b className="text-text">{max.toFixed(1)}{unit}</b></span>
        </div>
        <div className="h-36 rounded-lg bg-surface2/20 p-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data}>
              <defs>
                <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.15} />
                  <stop offset="100%" stopColor={color} stopOpacity={0.01} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.3)" vertical={false} />
              <XAxis dataKey="ts" tickFormatter={(ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} domain={[0, 'auto']} width={38} axisLine={false} tickLine={false} />
              <Tooltip {...ttStyle()} formatter={(v: any) => [`${Number(v).toFixed(1)}${unit}`, title.split(' (')[0]]} />
              <Area type="monotone" dataKey="value" stroke={color} fill={`url(#${gradientId})`} strokeWidth={2} dot={false} activeDot={{ r: 3, strokeWidth: 0, fill: color }} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  )
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
                        <XAxis dataKey="ts" tickFormatter={(ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} />
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
                        <XAxis dataKey="ts" tickFormatter={(ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} />
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
