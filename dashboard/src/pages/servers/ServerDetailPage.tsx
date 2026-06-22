/** Server detail — single-pane view of one monitored server.
 *
 *  Tabs: Overview · Performance · Processes · Services · Storage · Network
 *        · Software · Compliance · Events · Agent · Settings
 */

import { Fragment, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Bell,
  Bot,
  CheckCircle2,
  ChevronRight,
  ClipboardCheck,
  CloudOff,
  Cpu,
  Database,
  FileDown,
  Gauge,
  HardDrive,
  KeyRound,
  ListTree,
  MemoryStick,
  Network as NetworkIcon,
  Package,
  Pencil,
  RefreshCw,
  ScrollText,
  Search,
  Settings2,
  Trash2,
  Wrench,
} from 'lucide-react'
import {
  Area, AreaChart, CartesianGrid, Legend, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import {
  apiErrorMessage, cn, formatBps, formatBytes, relativeTime,
  timeAxisTickFormatter, timeTooltipLabelFormatter,
} from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import { ServerFormDialog } from '@/components/servers/ServerFormDialog'
import {
  AgentStatusBadge, KpiTile, OsIcon, ServerStatusBadge, TagList,
} from '@/components/servers/shared'
import type {
  AgentItem, ComplianceResult, ComplianceSummary, ServerCommand, ServerEventRow,
  ServerFilesystem, ServerItem, ServerLiveMetrics, ServerMetricsResponse,
  ServerNetworkInterface, ServerProcess, ServerService, ServerSoftware,
} from '@/types/servers'

const ttStyle = () => ({
  contentStyle: {
    backgroundColor: 'rgb(var(--surface))',
    border: '1px solid rgb(var(--border))',
    borderRadius: 8,
    boxShadow: '0 8px 24px rgb(0 0 0 / 0.18)',
    color: 'rgb(var(--text))',
    fontSize: 12,
    padding: '8px 10px',
  },
  itemStyle: { paddingTop: 2, paddingBottom: 2 },
  labelFormatter: timeTooltipLabelFormatter,
})

type ChartRow = Record<string, number | null> & { ts: number }

function seriesStats(rows: ChartRow[], key: string) {
  const vals = rows.map((r) => r[key]).filter((v): v is number => v != null && !Number.isNaN(v))
  if (!vals.length) return null
  return {
    current: vals[vals.length - 1],
    avg: vals.reduce((a, b) => a + b, 0) / vals.length,
    peak: Math.max(...vals),
  }
}

function netTotalStats(rows: ChartRow[]) {
  const vals = rows.map((r) => (Number(r.rx) || 0) + (Number(r.tx) || 0)).filter((v) => v > 0)
  if (!vals.length) return null
  const sorted = [...vals].sort((a, b) => a - b)
  const p95 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))]
  return {
    current: vals[vals.length - 1],
    avg: vals.reduce((a, b) => a + b, 0) / vals.length,
    peak: Math.max(...vals),
    p95,
  }
}

const TABS = [
  { key: 'overview', label: 'Overview', icon: Gauge },
  { key: 'performance', label: 'Performance', icon: Cpu },
  { key: 'processes', label: 'Processes', icon: ListTree },
  { key: 'services', label: 'Services', icon: Wrench },
  { key: 'storage', label: 'Storage', icon: HardDrive },
  { key: 'network', label: 'Network', icon: NetworkIcon },
  { key: 'software', label: 'Software', icon: Package },
  { key: 'compliance', label: 'Compliance', icon: ClipboardCheck },
  { key: 'alerts', label: 'Alerts', icon: Bell },
  { key: 'events', label: 'Events', icon: ScrollText },
  { key: 'agent', label: 'Agent', icon: Bot },
  { key: 'settings', label: 'Settings', icon: Settings2 },
] as const

type TabKey = typeof TABS[number]['key']

/** series[] → recharts rows keyed by epoch ms, one column per metric. */
function toChartRows(resp: ServerMetricsResponse | undefined, keys: Record<string, string>) {
  if (!resp) return []
  const rows = new Map<number, Record<string, number | null>>()
  for (const s of resp.series) {
    const col = keys[s.metric]
    if (!col) continue
    for (const p of s.points) {
      const ts = Date.parse(p.timestamp)
      if (!rows.has(ts)) rows.set(ts, { ts })
      rows.get(ts)![col] = p.value
    }
  }
  return [...rows.values()].sort((a, b) => (a.ts as number) - (b.ts as number))
}

export function ServerDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as TabKey) || 'overview'
  const setTab = (t: TabKey) => {
    const next = new URLSearchParams(params)
    next.set('tab', t)
    setParams(next, { replace: true })
  }

  const [deployOpen, setDeployOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [confirm, setConfirm] = useState<'delete' | 'decommission' | null>(null)

  const { data: server, isLoading } = useQuery<ServerItem>({
    queryKey: ['servers', id],
    queryFn: async () => (await api.get(`/servers/${id}`)).data,
    refetchInterval: 15_000,
    enabled: Boolean(id),
  })

  const { data: live } = useQuery<{ servers: Record<string, ServerLiveMetrics> }>({
    queryKey: ['servers', 'latest-metrics'],
    queryFn: async () => (await api.get('/servers/latest-metrics')).data,
    refetchInterval: 15_000,
  })
  const lm: ServerLiveMetrics = (id && live?.servers?.[id]) || {}

  const { data: processes } = useQuery<{ items: ServerProcess[]; mem_total_bytes?: number }>({
    queryKey: ['servers', id, 'processes'],
    queryFn: async () => (await api.get(`/servers/${id}/processes`)).data,
    refetchInterval: 30_000,
    enabled: Boolean(id),
  })
  const { data: services } = useQuery<{ items: ServerService[] }>({
    queryKey: ['servers', id, 'services'],
    queryFn: async () => (await api.get(`/servers/${id}/services`)).data,
    refetchInterval: 30_000,
    enabled: Boolean(id),
  })
  const { data: filesystems } = useQuery<{ items: ServerFilesystem[] }>({
    queryKey: ['servers', id, 'filesystems'],
    queryFn: async () => (await api.get(`/servers/${id}/filesystems`)).data,
    refetchInterval: 60_000,
    enabled: Boolean(id),
  })

  const del = useMutation({
    mutationFn: async () => (await api.delete(`/servers/${id}`)).data,
    onSuccess: () => {
      toast.success('Server deleted')
      navigate('/servers')
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })
  const decommission = useMutation({
    mutationFn: async () => (await api.post(`/servers/${id}/decommission`)).data,
    onSuccess: () => {
      toast.success('Server decommissioned')
      qc.invalidateQueries({ queryKey: ['servers'] })
    },
    onError: (e) => toast.error('Decommission failed', apiErrorMessage(e)),
  })

  if (isLoading || !server) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  const svcItems = services?.items || []
  const svcRunning = svcItems.filter((s) => (s.state || '').toLowerCase() === 'running').length
  const fsItems = filesystems?.items || []
  const isLive = server.status !== 'stale' && server.status !== 'disabled' && server.status !== 'unknown'

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <Link
            to="/servers/inventory"
            className="mt-1 flex h-8 w-8 items-center justify-center rounded-md border border-border text-muted transition-colors hover:bg-surface2 hover:text-text"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div>
            <div className="flex flex-wrap items-center gap-2.5">
              <OsIcon os={server.os_type} className="h-5 w-5" />
              <h1 className="text-2xl font-semibold">{server.display_name}</h1>
              <ServerStatusBadge status={server.status} reasons={server.status_reasons} />
              <AgentStatusBadge status={server.agent_status} />
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
              {server.primary_ip && <span className="font-mono">{server.primary_ip}</span>}
              {server.fqdn && <span>{server.fqdn}</span>}
              <span>{server.os_name || server.os_type} {server.os_version || ''}</span>
              {server.architecture && <span>{server.architecture}</span>}
              {server.environment && <Badge variant="outline" className="text-[10px]">{server.environment}</Badge>}
              {server.owner && <span>owner: {server.owner}</span>}
              <span>seen {relativeTime(server.last_seen)}</span>
            </div>
            {server.status_reasons.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {server.status_reasons.map((r) => (
                  <span
                    key={r}
                    className={cn(
                      'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]',
                      server.status === 'critical' ? 'bg-danger/10 text-danger' : 'bg-warning/10 text-warning',
                    )}
                  >
                    <AlertTriangle className="h-3 w-3" /> {r}
                  </span>
                ))}
              </div>
            )}
            <div className="mt-1.5">
              <TagList tags={server.tags} max={8} />
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
          <Button size="sm" onClick={() => setDeployOpen(true)}>
            <KeyRound className="h-3.5 w-3.5" /> Deploy agent
          </Button>
        </div>
      </div>

      {/* KPI tiles */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <KpiTile
          icon={Cpu} label="CPU"
          value={isLive && lm.cpu_pct != null ? `${lm.cpu_pct.toFixed(1)}%` : '—'}
          tone={lm.cpu_pct != null && lm.cpu_pct >= 90 ? 'danger' : 'default'}
        />
        <KpiTile
          icon={MemoryStick} label="Memory"
          value={isLive && lm.memory_pct != null ? `${lm.memory_pct.toFixed(1)}%` : '—'}
          tone={lm.memory_pct != null && lm.memory_pct >= 90 ? 'danger' : 'default'}
        />
        <KpiTile
          icon={HardDrive} label="Disk (max)"
          value={isLive && lm.disk_max_pct != null ? `${lm.disk_max_pct.toFixed(1)}%` : '—'}
          tone={lm.disk_max_pct != null && lm.disk_max_pct >= 95 ? 'danger' : lm.disk_max_pct != null && lm.disk_max_pct >= 85 ? 'warning' : 'default'}
          sub={fsItems.length ? `${fsItems.length} volumes` : undefined}
        />
        <KpiTile
          icon={NetworkIcon} label="Network"
          value={isLive && lm.net_bps != null ? formatBps(lm.net_bps * 8) : '—'}
        />
        <KpiTile icon={ListTree} label="Processes" value={processes?.items?.length ?? '—'} />
        <KpiTile
          icon={Wrench} label="Services"
          value={svcItems.length ? `${svcRunning}/${svcItems.length}` : '—'}
          sub={svcItems.length ? 'running / reported' : undefined}
        />
      </div>

      {/* Tab nav */}
      <div className="flex flex-wrap gap-1 border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cn(
              'inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors',
              tab === t.key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted hover:text-text',
            )}
          >
            <t.icon className="h-3.5 w-3.5" /> {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && <OverviewTab serverId={server.id} filesystems={fsItems} processes={processes?.items || []} />}
      {tab === 'performance' && <PerformanceTab serverId={server.id} />}
      {tab === 'processes' && <ProcessesTab serverId={id || ''} items={processes?.items || []} memTotal={processes?.mem_total_bytes || 0} />}
      {tab === 'services' && <ServicesTab serverId={server.id} items={svcItems} />}
      {tab === 'storage' && <StorageTab serverId={server.id} items={fsItems} />}
      {tab === 'network' && <NetworkTab serverId={server.id} />}
      {tab === 'software' && <SoftwareTab serverId={server.id} />}
      {tab === 'compliance' && <ComplianceTab serverId={server.id} />}
      {tab === 'alerts' && (
        <ServerAlertsTab
          serverId={server.id}
          serverStatus={server.status}
          statusReasons={server.status_reasons}
        />
      )}
      {tab === 'events' && <EventsTab serverId={server.id} />}
      {tab === 'agent' && (
        <AgentTab
          serverId={server.id}
          serverName={server.display_name}
          onDeployAgent={() => setDeployOpen(true)}
        />
      )}
      {tab === 'settings' && (
        <SettingsTab
          server={server}
          onEdit={() => setEditOpen(true)}
          onDecommission={() => setConfirm('decommission')}
          onDelete={() => setConfirm('delete')}
        />
      )}

      {/* Dialogs */}
      <InstallTokenDialog open={deployOpen} onOpenChange={setDeployOpen} serverId={server.id} serverName={server.display_name} />
      <ServerFormDialog open={editOpen} onOpenChange={setEditOpen} server={server} />
      <ConfirmDialog
        open={Boolean(confirm)}
        onOpenChange={(o) => { if (!o) setConfirm(null) }}
        title={confirm === 'delete' ? 'Delete server' : 'Decommission server'}
        description={
          confirm === 'delete'
            ? `Permanently remove ${server.display_name}, its inventory, and alert history?`
            : `Disable monitoring and alerting for ${server.display_name}? The record is kept and can be re-enabled by editing the server.`
        }
        confirmText={confirm === 'delete' ? 'Delete' : 'Decommission'}
        destructive={confirm === 'delete'}
        onConfirm={() => {
          if (confirm === 'delete') del.mutate()
          else decommission.mutate()
          setConfirm(null)
        }}
      />
    </div>
  )
}

/* ── Shared tab UI ───────────────────────────────────────────────── */

function diskIoStats(rows: ChartRow[]) {
  const vals = rows.map((r) => (Number(r.read) || 0) + (Number(r.write) || 0)).filter((v) => v > 0)
  if (!vals.length) return null
  const sorted = [...vals].sort((a, b) => a - b)
  return {
    current: vals[vals.length - 1],
    avg: vals.reduce((a, b) => a + b, 0) / vals.length,
    peak: Math.max(...vals),
    p95: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))],
  }
}

const CHART_H = 240

function PanelHeader({
  icon, title, hint, right,
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
      <div className="flex min-w-0 items-center gap-2">
        <span className="text-muted">{icon}</span>
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        {hint && <span className="truncate text-[10px] uppercase tracking-wider text-muted">{hint}</span>}
      </div>
      {right}
    </div>
  )
}

function PanelMiniStat({ label, value, tone = 'text-text2' }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-border/60 bg-surface2/30 px-2 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('mt-0.5 text-sm font-bold tabular-nums leading-none', tone)}>{value}</div>
    </div>
  )
}

function PanelToolbar({ label, children }: { label?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      {label && <p className="text-xs text-muted">{label}</p>}
      {children}
    </div>
  )
}

function chartAxis(tick: (v: number) => string) {
  return {
    dataKey: 'ts' as const,
    type: 'number' as const,
    scale: 'time' as const,
    domain: ['dataMin', 'dataMax'] as [string, string],
    tickFormatter: tick,
    tick: { fontSize: 10, fill: 'rgb(var(--muted))' },
    axisLine: false,
    tickLine: false,
    minTickGap: 42,
  }
}

function MetricChartCard({
  icon, title, hint, isLoading, rows, miniStats, children,
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  isLoading?: boolean
  rows: ChartRow[]
  miniStats?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Card className="overflow-hidden">
      <PanelHeader icon={icon} title={title} hint={hint} />
      {miniStats}
      <CardContent className="px-2 pb-3 pt-2" style={{ height: CHART_H }}>
        {isLoading ? <Skeleton className="mx-2 h-full w-[calc(100%-1rem)]" /> : rows.length === 0 ? <NoData /> : (
          <ResponsiveContainer width="100%" height="100%">{children as React.ReactElement}</ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}

function TablePanel({
  icon, title, hint, right, toolbar, children,
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  right?: React.ReactNode
  toolbar?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Card className="overflow-hidden">
      <PanelHeader icon={icon} title={title} hint={hint} right={right} />
      {toolbar && <div className="border-b border-border/50 px-4 py-3">{toolbar}</div>}
      <CardContent className={toolbar ? 'px-0 pb-2 pt-1' : 'px-0 pb-2 pt-3'}>{children}</CardContent>
    </Card>
  )
}

function InfoGrid({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-3 md:grid-cols-3 xl:grid-cols-4">
      {rows.map(([label, value]) => (
        <div key={label} className="rounded-lg border border-border/50 bg-surface2/20 px-3 py-2.5">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className="mt-1 break-words text-sm text-text">{value}</div>
        </div>
      ))}
    </div>
  )
}

/* ── Overview ────────────────────────────────────────────────────── */

function OverviewTab({ serverId, filesystems, processes }: {
  serverId: string
  filesystems: ServerFilesystem[]
  processes: ServerProcess[]
}) {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const { data: metrics, isLoading } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'metrics', 'overview', range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/metrics`, {
        params: {
          metrics: 'cpu_total_pct,memory_used_pct,network_rx_bps,network_tx_bps',
          from: range.fromISO, to: range.toISO,
        },
      })).data,
    staleTime: 30_000,
    refetchInterval: 60_000,
  })

  const cpuMem = toChartRows(metrics, { cpu_total_pct: 'cpu', memory_used_pct: 'mem' }) as ChartRow[]
  const net = toChartRows(metrics, { network_rx_bps: 'rx', network_tx_bps: 'tx' }) as ChartRow[]
  const cpuStats = seriesStats(cpuMem, 'cpu')
  const memStats = seriesStats(cpuMem, 'mem')
  const netStats = netTotalStats(net)
  const topProcs = [...processes].sort((a, b) => (b.cpu_pct || 0) - (a.cpu_pct || 0)).slice(0, 8)
  const maxProcCpu = Math.max(1, ...topProcs.map((p) => p.cpu_pct || 0))
  const tick = timeAxisTickFormatter(range.hours)
  const chartH = 240

  const axisProps = {
    dataKey: 'ts' as const,
    type: 'number' as const,
    scale: 'time' as const,
    domain: ['dataMin', 'dataMax'] as [string, string],
    tickFormatter: tick,
    tick: { fontSize: 10, fill: 'rgb(var(--muted))' },
    axisLine: false,
    tickLine: false,
    minTickGap: 42,
  }

  return (
    <div className="space-y-4">
      <PanelToolbar label={<>Resource trends for <span className="font-medium text-text2">{range.label}</span></>}>
        <TimeRangePicker
          rangeIdx={rangeIdx} isCustom={isCustom}
          customFrom={range.fromISO} customTo={range.toISO}
          onPreset={setPreset} onCustom={setCustom}
        />
      </PanelToolbar>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="overflow-hidden">
          <PanelHeader
            icon={<Cpu className="h-3.5 w-3.5" />}
            title="CPU & Memory"
            hint={range.label}
          />
          <div className="grid grid-cols-2 gap-2 px-4 pt-3 sm:grid-cols-4">
            <PanelMiniStat label="CPU now" value={cpuStats ? `${cpuStats.current.toFixed(1)}%` : '—'} tone="text-info" />
            <PanelMiniStat label="CPU avg" value={cpuStats ? `${cpuStats.avg.toFixed(1)}%` : '—'} />
            <PanelMiniStat label="Memory now" value={memStats ? `${memStats.current.toFixed(1)}%` : '—'} tone="text-primary" />
            <PanelMiniStat label="Memory peak" value={memStats ? `${memStats.peak.toFixed(1)}%` : '—'} />
          </div>
          <CardContent className="px-2 pb-3 pt-2" style={{ height: chartH }}>
            {isLoading ? <Skeleton className="mx-2 h-full w-[calc(100%-1rem)]" /> : cpuMem.length === 0 ? <NoData /> : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={cpuMem} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
                  <defs>
                    <linearGradient id="srvOverviewCpu" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="srvOverviewMem" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.28} />
                      <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
                  <XAxis {...axisProps} />
                  <YAxis domain={[0, 100]} width={36} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                  <ReferenceLine y={85} stroke="rgb(var(--warning))" strokeDasharray="4 4" strokeOpacity={0.55} />
                  <ReferenceLine y={95} stroke="rgb(var(--danger))" strokeDasharray="4 4" strokeOpacity={0.55} />
                  <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [`${Number(v).toFixed(1)}%`, n === 'cpu' ? 'CPU' : 'Memory']} />
                  <Legend
                    verticalAlign="top" align="right" iconType="circle" iconSize={8}
                    wrapperStyle={{ fontSize: 11, paddingBottom: 4 }}
                    formatter={(v) => <span className="text-text2">{v}</span>}
                  />
                  <Area type="monotone" dataKey="cpu" name="CPU" stroke="rgb(var(--info))" strokeWidth={2} fill="url(#srvOverviewCpu)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
                  <Area type="monotone" dataKey="mem" name="Memory" stroke="rgb(var(--primary))" strokeWidth={2} fill="url(#srvOverviewMem)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card className="overflow-hidden">
          <PanelHeader
            icon={<Activity className="h-3.5 w-3.5" />}
            title="Network throughput"
            hint={range.label}
          />
          <div className="grid grid-cols-2 gap-2 px-4 pt-3 sm:grid-cols-4">
            <PanelMiniStat label="Current" value={netStats ? formatBps(netStats.current * 8) : '—'} tone="text-info" />
            <PanelMiniStat label="Average" value={netStats ? formatBps(netStats.avg * 8) : '—'} />
            <PanelMiniStat label="95th pct" value={netStats ? formatBps(netStats.p95 * 8) : '—'} tone="text-primary" />
            <PanelMiniStat label="Peak" value={netStats ? formatBps(netStats.peak * 8) : '—'} tone="text-warning" />
          </div>
          <CardContent className="px-2 pb-3 pt-2" style={{ height: chartH }}>
            {isLoading ? <Skeleton className="mx-2 h-full w-[calc(100%-1rem)]" /> : net.length === 0 ? <NoData /> : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={net} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
                  <defs>
                    <linearGradient id="srvOverviewRx" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.4} />
                      <stop offset="60%" stopColor="rgb(var(--primary))" stopOpacity={0.12} />
                      <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="srvOverviewTx" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
                  <XAxis {...axisProps} />
                  <YAxis width={48} tickFormatter={(v) => formatBps(Number(v) * 8)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                  <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [formatBps(Number(v) * 8), n === 'rx' ? 'Receive' : 'Transmit']} />
                  <Legend
                    verticalAlign="top" align="right" iconType="circle" iconSize={8}
                    wrapperStyle={{ fontSize: 11, paddingBottom: 4 }}
                    formatter={(v) => <span className="text-text2">{v}</span>}
                  />
                  <Area type="monotone" dataKey="rx" name="Receive" stroke="rgb(var(--primary))" strokeWidth={2} fill="url(#srvOverviewRx)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
                  <Area type="monotone" dataKey="tx" name="Transmit" stroke="rgb(var(--success))" strokeWidth={2} fill="url(#srvOverviewTx)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="overflow-hidden">
          <PanelHeader
            icon={<HardDrive className="h-3.5 w-3.5" />}
            title="Filesystems"
            hint={`${filesystems.length} volume${filesystems.length === 1 ? '' : 's'}`}
          />
          <CardContent className="space-y-3 px-4 pb-4 pt-3">
            {filesystems.length === 0 ? <NoData /> : (
              filesystems.map((fs) => {
                const pct = Math.min(100, fs.used_pct || 0)
                const tone = pct >= 95 ? 'danger' : pct >= 85 ? 'warning' : 'primary'
                const barTone = pct >= 95 ? 'from-danger to-danger/70' : pct >= 85 ? 'from-warning to-warning/70' : 'from-primary to-info'
                return (
                  <div key={fs.mount} className="rounded-lg border border-border/50 bg-surface2/20 px-3 py-2.5">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <span className="font-mono text-xs font-semibold text-text">{fs.mount}</span>
                      <span className={cn(
                        'text-xs font-bold tabular-nums',
                        tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-text2',
                      )}>
                        {pct.toFixed(1)}%
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-surface2">
                      <div
                        className={cn('h-full rounded-full bg-gradient-to-r transition-all', barTone)}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <div className="mt-1.5 flex justify-between text-[10px] tabular-nums text-muted">
                      <span>{formatBytes(fs.used_bytes || 0)} used</span>
                      <span>{formatBytes(fs.total_bytes || 0)} total</span>
                    </div>
                  </div>
                )
              })
            )}
          </CardContent>
        </Card>

        <Card className="overflow-hidden">
          <PanelHeader
            icon={<ListTree className="h-3.5 w-3.5" />}
            title="Top processes"
            hint="by CPU"
            right={<span className="text-[10px] text-muted">live snapshot</span>}
          />
          <CardContent className="px-0 pb-2 pt-1">
            {topProcs.length === 0 ? <div className="px-4"><NoData /></div> : (
              <Table>
                <THead>
                  <Tr className="hover:bg-transparent">
                    <Th className="h-8 pl-4">Process</Th>
                    <Th className="h-8 w-28">CPU</Th>
                    <Th className="h-8 text-right">Memory</Th>
                    <Th className="h-8 pr-4">User</Th>
                  </Tr>
                </THead>
                <TBody>
                  {topProcs.map((p, i) => {
                    const cpu = p.cpu_pct || 0
                    return (
                      <Tr key={`${p.pid}-${p.name}`} className={i % 2 === 0 ? 'bg-surface2/15' : undefined}>
                        <Td className="py-2 pl-4 text-xs font-medium">
                          <span className="text-text">{p.name}</span>
                          <span className="ml-1 font-mono text-[10px] text-muted">#{p.pid}</span>
                        </Td>
                        <Td className="py-2">
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
                              <div
                                className={cn(
                                  'h-full rounded-full bg-gradient-to-r',
                                  cpu >= 50 ? 'from-danger to-warning' : cpu >= 20 ? 'from-warning to-primary' : 'from-info to-primary',
                                )}
                                style={{ width: `${Math.min(100, (cpu / maxProcCpu) * 100)}%` }}
                              />
                            </div>
                            <span className="w-10 shrink-0 text-right text-[11px] font-semibold tabular-nums text-text2">{cpu.toFixed(1)}%</span>
                          </div>
                        </Td>
                        <Td className="py-2 text-right text-xs tabular-nums text-muted">{formatBytes(p.memory_bytes || 0)}</Td>
                        <Td className="py-2 pr-4 text-xs text-muted">{p.user_name || '—'}</Td>
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

/* ── Performance ─────────────────────────────────────────────────── */

function PerformanceTab({ serverId }: { serverId: string }) {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const { data: metrics, isLoading } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'metrics', range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/metrics`, {
        params: {
          metrics: 'cpu_total_pct,memory_used_pct,network_rx_bps,network_tx_bps,disk_read_bps,disk_write_bps',
          from: range.fromISO, to: range.toISO,
        },
      })).data,
    staleTime: 30_000,
    refetchInterval: 60_000,
  })

  const tick = timeAxisTickFormatter(range.hours)
  const axis = chartAxis(tick)
  const cpu = toChartRows(metrics, { cpu_total_pct: 'v' }) as ChartRow[]
  const mem = toChartRows(metrics, { memory_used_pct: 'v' }) as ChartRow[]
  const net = toChartRows(metrics, { network_rx_bps: 'rx', network_tx_bps: 'tx' }) as ChartRow[]
  const disk = toChartRows(metrics, { disk_read_bps: 'read', disk_write_bps: 'write' }) as ChartRow[]
  const cpuStats = seriesStats(cpu, 'v')
  const memStats = seriesStats(mem, 'v')
  const netStats = netTotalStats(net)
  const diskStats = diskIoStats(disk)

  const miniRow = (stats: React.ReactNode) => (
    <div className="grid grid-cols-2 gap-2 px-4 pt-3 sm:grid-cols-4">{stats}</div>
  )

  return (
    <div className="space-y-4">
      <PanelToolbar label={<>Detailed metrics for <span className="font-medium text-text2">{range.label}</span></>}>
        <TimeRangePicker
          rangeIdx={rangeIdx} isCustom={isCustom}
          customFrom={range.fromISO} customTo={range.toISO}
          onPreset={setPreset} onCustom={setCustom}
        />
      </PanelToolbar>

      <div className="grid gap-4 xl:grid-cols-2">
        <MetricChartCard
          icon={<Cpu className="h-3.5 w-3.5" />}
          title="CPU utilization"
          hint={range.label}
          isLoading={isLoading}
          rows={cpu}
          miniStats={miniRow(<>
            <PanelMiniStat label="Current" value={cpuStats ? `${cpuStats.current.toFixed(1)}%` : '—'} tone="text-info" />
            <PanelMiniStat label="Average" value={cpuStats ? `${cpuStats.avg.toFixed(1)}%` : '—'} />
            <PanelMiniStat label="Peak" value={cpuStats ? `${cpuStats.peak.toFixed(1)}%` : '—'} tone="text-warning" />
            <PanelMiniStat label="Samples" value={cpu.length ? String(cpu.length) : '—'} />
          </>)}
        >
          <AreaChart data={cpu} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <defs>
              <linearGradient id="perfCpu" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.4} />
                <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
            <XAxis {...axis} />
            <YAxis domain={[0, 100]} width={36} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <ReferenceLine y={85} stroke="rgb(var(--warning))" strokeDasharray="4 4" strokeOpacity={0.55} />
            <ReferenceLine y={95} stroke="rgb(var(--danger))" strokeDasharray="4 4" strokeOpacity={0.55} />
            <Tooltip {...ttStyle()} formatter={(v: number) => [`${Number(v).toFixed(1)}%`, 'CPU']} />
            <Area type="monotone" dataKey="v" stroke="rgb(var(--info))" strokeWidth={2} fill="url(#perfCpu)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
          </AreaChart>
        </MetricChartCard>

        <MetricChartCard
          icon={<MemoryStick className="h-3.5 w-3.5" />}
          title="Memory utilization"
          hint={range.label}
          isLoading={isLoading}
          rows={mem}
          miniStats={miniRow(<>
            <PanelMiniStat label="Current" value={memStats ? `${memStats.current.toFixed(1)}%` : '—'} tone="text-primary" />
            <PanelMiniStat label="Average" value={memStats ? `${memStats.avg.toFixed(1)}%` : '—'} />
            <PanelMiniStat label="Peak" value={memStats ? `${memStats.peak.toFixed(1)}%` : '—'} tone="text-warning" />
            <PanelMiniStat label="Samples" value={mem.length ? String(mem.length) : '—'} />
          </>)}
        >
          <AreaChart data={mem} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <defs>
              <linearGradient id="perfMem" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.35} />
                <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
            <XAxis {...axis} />
            <YAxis domain={[0, 100]} width={36} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <ReferenceLine y={85} stroke="rgb(var(--warning))" strokeDasharray="4 4" strokeOpacity={0.55} />
            <ReferenceLine y={95} stroke="rgb(var(--danger))" strokeDasharray="4 4" strokeOpacity={0.55} />
            <Tooltip {...ttStyle()} formatter={(v: number) => [`${Number(v).toFixed(1)}%`, 'Memory']} />
            <Area type="monotone" dataKey="v" stroke="rgb(var(--primary))" strokeWidth={2} fill="url(#perfMem)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
          </AreaChart>
        </MetricChartCard>

        <MetricChartCard
          icon={<Activity className="h-3.5 w-3.5" />}
          title="Network throughput"
          hint={range.label}
          isLoading={isLoading}
          rows={net}
          miniStats={miniRow(<>
            <PanelMiniStat label="Current" value={netStats ? formatBps(netStats.current * 8) : '—'} tone="text-info" />
            <PanelMiniStat label="Average" value={netStats ? formatBps(netStats.avg * 8) : '—'} />
            <PanelMiniStat label="95th pct" value={netStats ? formatBps(netStats.p95 * 8) : '—'} tone="text-primary" />
            <PanelMiniStat label="Peak" value={netStats ? formatBps(netStats.peak * 8) : '—'} tone="text-warning" />
          </>)}
        >
          <AreaChart data={net} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <defs>
              <linearGradient id="perfRx" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.4} />
                <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="perfTx" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.35} />
                <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
            <XAxis {...axis} />
            <YAxis width={48} tickFormatter={(v) => formatBps(Number(v) * 8)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [formatBps(Number(v) * 8), n === 'rx' ? 'Receive' : 'Transmit']} />
            <Legend verticalAlign="top" align="right" iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, paddingBottom: 4 }} />
            <Area type="monotone" dataKey="rx" name="Receive" stroke="rgb(var(--primary))" strokeWidth={2} fill="url(#perfRx)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
            <Area type="monotone" dataKey="tx" name="Transmit" stroke="rgb(var(--success))" strokeWidth={2} fill="url(#perfTx)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
          </AreaChart>
        </MetricChartCard>

        <MetricChartCard
          icon={<HardDrive className="h-3.5 w-3.5" />}
          title="Disk I/O"
          hint={range.label}
          isLoading={isLoading}
          rows={disk}
          miniStats={miniRow(<>
            <PanelMiniStat label="Current" value={diskStats ? `${formatBytes(diskStats.current)}/s` : '—'} tone="text-info" />
            <PanelMiniStat label="Average" value={diskStats ? `${formatBytes(diskStats.avg)}/s` : '—'} />
            <PanelMiniStat label="95th pct" value={diskStats ? `${formatBytes(diskStats.p95)}/s` : '—'} tone="text-primary" />
            <PanelMiniStat label="Peak" value={diskStats ? `${formatBytes(diskStats.peak)}/s` : '—'} tone="text-warning" />
          </>)}
        >
          <AreaChart data={disk} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <defs>
              <linearGradient id="perfRead" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.35} />
                <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="perfWrite" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgb(var(--warning))" stopOpacity={0.35} />
                <stop offset="100%" stopColor="rgb(var(--warning))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
            <XAxis {...axis} />
            <YAxis width={52} tickFormatter={(v) => `${formatBytes(Number(v))}/s`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [`${formatBytes(Number(v))}/s`, n === 'read' ? 'Read' : 'Write']} />
            <Legend verticalAlign="top" align="right" iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, paddingBottom: 4 }} />
            <Area type="monotone" dataKey="read" name="Read" stroke="rgb(var(--info))" strokeWidth={2} fill="url(#perfRead)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
            <Area type="monotone" dataKey="write" name="Write" stroke="rgb(var(--warning))" strokeWidth={2} fill="url(#perfWrite)" dot={false} isAnimationActive={false} activeDot={{ r: 3.5, strokeWidth: 0 }} connectNulls />
          </AreaChart>
        </MetricChartCard>
      </div>
    </div>
  )
}

/* ── Processes ───────────────────────────────────────────────────── */

type ProcRow = {
  key: string
  name: string
  pid: number | null
  count: number
  cpu_pct: number
  memory_bytes: number
  user_name: string | null
  cmdline: string | null
  updated_at: string | null
}

function ProcessesTab({ serverId, items, memTotal }: { serverId: string; items: ServerProcess[]; memTotal: number }) {
  const [q, setQ] = useState('')
  const [grouped, setGrouped] = useState(false)
  const [sortBy, setSortBy] = useState<'cpu' | 'mem' | 'name'>('cpu')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [expanded, setExpanded] = useState<string | null>(null)

  const matched = useMemo(() => {
    if (!q) return items
    const needle = q.toLowerCase()
    return items.filter((p) =>
      p.name.toLowerCase().includes(needle) ||
      (p.user_name || '').toLowerCase().includes(needle) ||
      String(p.pid).includes(needle))
  }, [items, q])

  const rows = useMemo<ProcRow[]>(() => {
    let base: ProcRow[]
    if (grouped) {
      const m = new Map<string, ProcRow>()
      for (const p of matched) {
        const g = m.get(p.name)
        if (g) {
          g.count += 1
          g.cpu_pct += p.cpu_pct || 0
          g.memory_bytes += p.memory_bytes || 0
          if (p.updated_at && (!g.updated_at || p.updated_at > g.updated_at)) g.updated_at = p.updated_at
        } else {
          m.set(p.name, {
            key: p.name, name: p.name, pid: null, count: 1,
            cpu_pct: p.cpu_pct || 0, memory_bytes: p.memory_bytes || 0,
            user_name: p.user_name, cmdline: p.cmdline, updated_at: p.updated_at,
          })
        }
      }
      base = [...m.values()]
    } else {
      base = matched.map((p) => ({
        key: `${p.pid}-${p.name}`, name: p.name, pid: p.pid, count: 1,
        cpu_pct: p.cpu_pct || 0, memory_bytes: p.memory_bytes || 0,
        user_name: p.user_name, cmdline: p.cmdline, updated_at: p.updated_at,
      }))
    }
    const dir = sortDir === 'asc' ? 1 : -1
    return base.sort((a, b) =>
      sortBy === 'name' ? dir * a.name.localeCompare(b.name)
      : sortBy === 'mem' ? dir * (a.memory_bytes - b.memory_bytes)
      : dir * (a.cpu_pct - b.cpu_pct))
  }, [matched, grouped, sortBy, sortDir])

  const totals = useMemo(() => ({
    cpu: matched.reduce((s, p) => s + (p.cpu_pct || 0), 0),
    mem: matched.reduce((s, p) => s + (p.memory_bytes || 0), 0),
  }), [matched])

  const newest = useMemo(
    () => matched.reduce<string | null>(
      (max, p) => (p.updated_at && (!max || p.updated_at > max) ? p.updated_at : max),
      null),
    [matched])

  const toggleSort = (col: 'cpu' | 'mem' | 'name') => {
    if (sortBy === col) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortBy(col); setSortDir(col === 'name' ? 'asc' : 'desc') }
  }
  const caret = (col: 'cpu' | 'mem' | 'name') => (sortBy === col ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '')

  const memPct = (bytes: number) => (memTotal > 0 ? (bytes / memTotal) * 100 : null)
  const pctClass = (v: number | null) => (v == null ? '' : v >= 80 ? 'text-danger' : v >= 50 ? 'text-warning' : '')

  const maxCpu = Math.max(1, ...rows.map((p) => p.cpu_pct))

  return (
    <TablePanel
      icon={<ListTree className="h-3.5 w-3.5" />}
      title="Processes"
      hint={`${rows.length} ${grouped ? 'groups' : 'processes'}`}
      right={newest ? <span className="text-[10px] text-muted">updated {relativeTime(newest)}</span> : undefined}
      toolbar={(
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="h-8 pl-8" placeholder="Filter by name, user, PID…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <Button size="sm" variant={grouped ? 'default' : 'outline'} onClick={() => setGrouped((g) => !g)}>
            Group by name
          </Button>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <PanelMiniStat label="Total CPU" value={`${totals.cpu.toFixed(1)}%`} tone="text-info" />
            <PanelMiniStat label="Total memory" value={formatBytes(totals.mem)} />
            <PanelMiniStat label="Listed" value={String(rows.length)} />
          </div>
        </div>
      )}
    >
      <div className="overflow-hidden">
        <Table>
          <THead className="bg-surface2/40">
            <Tr>
              <Th className="cursor-pointer select-none pl-4" onClick={() => toggleSort('name')}>Process{caret('name')}</Th>
              <Th className="text-right">{grouped ? 'Count' : 'PID'}</Th>
              <Th className="w-36 cursor-pointer select-none" onClick={() => toggleSort('cpu')}>CPU{caret('cpu')}</Th>
              <Th className="text-right">Mem %</Th>
              <Th className="cursor-pointer select-none text-right" onClick={() => toggleSort('mem')}>Memory{caret('mem')}</Th>
              <Th>User</Th><Th className="pr-4">Updated</Th>
            </Tr>
          </THead>
          <TBody>
            {rows.length === 0 && (
              <Tr><Td colSpan={7}><div className="py-10 text-center text-xs text-muted">No processes reported</div></Td></Tr>
            )}
            {rows.map((p, i) => {
              const mp = memPct(p.memory_bytes)
              const open = expanded === p.key
              return (
                <Fragment key={p.key}>
                  <Tr className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                    <Td className="py-2 pl-4 text-sm font-medium">
                      <button
                        type="button"
                        className="flex items-center gap-1 text-left hover:text-primary"
                        title={p.cmdline || 'Show history'}
                        onClick={() => setExpanded(open ? null : p.key)}
                      >
                        <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 text-muted transition-transform', open && 'rotate-90')} />
                        {p.name}
                      </button>
                    </Td>
                    <Td className="text-right text-xs tabular-nums text-muted">{grouped ? `×${p.count}` : p.pid}</Td>
                    <Td className="py-2">
                      <div className="flex items-center gap-2">
                        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
                          <div
                            className={cn(
                              'h-full rounded-full bg-gradient-to-r',
                              p.cpu_pct >= 50 ? 'from-danger to-warning' : p.cpu_pct >= 20 ? 'from-warning to-primary' : 'from-info to-primary',
                            )}
                            style={{ width: `${Math.min(100, (p.cpu_pct / maxCpu) * 100)}%` }}
                          />
                        </div>
                        <span className={cn('w-10 shrink-0 text-right text-[11px] font-semibold tabular-nums', pctClass(p.cpu_pct))}>{p.cpu_pct.toFixed(1)}%</span>
                      </div>
                    </Td>
                    <Td className={cn('text-right text-xs tabular-nums', pctClass(mp))}>{mp == null ? '—' : `${mp.toFixed(1)}%`}</Td>
                    <Td className="text-right text-xs tabular-nums text-muted">{formatBytes(p.memory_bytes)}</Td>
                    <Td className="text-xs text-muted">{p.user_name || '—'}</Td>
                    <Td className="pr-4 text-xs text-muted">{relativeTime(p.updated_at)}</Td>
                  </Tr>
                  {open && (
                    <Tr>
                      <Td colSpan={7} className="bg-surface2/25 p-0">
                        <ProcessHistory serverId={serverId} name={p.name} />
                      </Td>
                    </Tr>
                  )}
                </Fragment>
              )
            })}
          </TBody>
        </Table>
      </div>
    </TablePanel>
  )
}

function ProcessHistory({ serverId, name }: { serverId: string; name: string }) {
  const { data, isLoading } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'proc-history', name],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/processes/history`, { params: { name } })).data,
    refetchInterval: 60_000,
    enabled: Boolean(serverId && name),
  })
  const safeId = name.replace(/[^a-zA-Z0-9]/g, '_')
  const cpu = toChartRows(data, { cpu_pct: 'v' }) as ChartRow[]
  const mem = toChartRows(data, { memory_bytes: 'v' }) as ChartRow[]
  const tick = timeAxisTickFormatter(6)
  const axis = chartAxis(tick)

  return (
    <div className="grid gap-4 p-4 md:grid-cols-2">
      <MetricChartCard
        icon={<Cpu className="h-3.5 w-3.5" />}
        title={`CPU — ${name}`}
        hint="last 6h"
        isLoading={isLoading}
        rows={cpu}
      >
        <AreaChart data={cpu} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <defs>
            <linearGradient id={`procCpu-${safeId}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.35} />
              <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
          <XAxis {...axis} />
          <YAxis width={36} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
          <Tooltip {...ttStyle()} formatter={(v: number) => [`${Number(v).toFixed(1)}%`, 'CPU']} />
          <Area type="monotone" dataKey="v" stroke="rgb(var(--info))" strokeWidth={2} fill={`url(#procCpu-${safeId})`} dot={false} isAnimationActive={false} connectNulls />
        </AreaChart>
      </MetricChartCard>
      <MetricChartCard
        icon={<MemoryStick className="h-3.5 w-3.5" />}
        title="Memory — total"
        hint="last 6h"
        isLoading={isLoading}
        rows={mem}
      >
        <AreaChart data={mem} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <defs>
            <linearGradient id={`procMem-${safeId}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.35} />
              <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
          <XAxis {...axis} />
          <YAxis width={52} tickFormatter={(v) => formatBytes(Number(v))} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
          <Tooltip {...ttStyle()} formatter={(v: number) => [formatBytes(Number(v)), 'Memory']} />
          <Area type="monotone" dataKey="v" stroke="rgb(var(--primary))" strokeWidth={2} fill={`url(#procMem-${safeId})`} dot={false} isAnimationActive={false} connectNulls />
        </AreaChart>
      </MetricChartCard>
    </div>
  )
}

/* ── Services ────────────────────────────────────────────────────── */

interface HostRule {
  id: string
  name: string
  enabled: boolean
  metric: string
  operator: string
  threshold: number | null
  severity: string
  min_duration: number
  server_id: string | null
  target: string | null
}

type HostRulePrefill = {
  metric?: string
  target?: string
  name?: string
  severity?: string
}

function ServicesTab({ serverId, items }: { serverId: string; items: ServerService[] }) {
  const qc = useQueryClient()
  const [filter, setFilter] = useState('all')
  const [createOpen, setCreateOpen] = useState(false)
  const [prefill, setPrefill] = useState<HostRulePrefill | undefined>()

  const { data: rulesResp } = useQuery<{ items: HostRule[] }>({
    queryKey: ['servers', serverId, 'host-rules'],
    queryFn: async () => (await api.get('/host-alert-rules', { params: { server_id: serverId } })).data,
  })
  const serviceRules = (rulesResp?.items || []).filter(
    (r) => r.metric === 'host_service_down' && r.enabled && r.target,
  )
  const ruleByService = useMemo(() => {
    const map = new Map<string, HostRule>()
    for (const r of serviceRules) {
      if (r.target) map.set(r.target.toLowerCase(), r)
    }
    return map
  }, [serviceRules])

  const delRule = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/host-alert-rules/${id}`)).data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'host-rules'] })
      toast.success('Alert rule removed')
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const isStopped = (state: string | null) => {
    const st = (state || '').toLowerCase()
    return st !== 'running' && st !== 'not_found' && !!st
  }

  const filtered = items.filter((s) => {
    const st = (s.state || '').toLowerCase()
    const hasRule = ruleByService.has(s.service_name.toLowerCase())
    if (filter === 'running') return st === 'running'
    if (filter === 'stopped') return isStopped(s.state)
    if (filter === 'not_found') return st === 'not_found'
    if (filter === 'alert_enabled') return hasRule
    return true
  })
  const running = items.filter((s) => (s.state || '').toLowerCase() === 'running').length
  const alertBreaches = items.filter(
    (s) => ruleByService.has(s.service_name.toLowerCase()) && isStopped(s.state),
  ).length

  const openAlertDialog = (serviceName: string) => {
    setPrefill({
      metric: 'host_service_down',
      target: serviceName,
      name: `${serviceName} stopped`,
    })
    setCreateOpen(true)
  }

  const stateBadge = (state: string | null) => {
    const st = (state || '').toLowerCase()
    if (st === 'running') return <Badge variant="success">Running</Badge>
    if (st === 'not_found') return <Badge variant="outline">Not installed</Badge>
    if (!st) return <Badge variant="outline">Unknown</Badge>
    return <Badge variant="danger">{state}</Badge>
  }

  const alertBadge = (serviceName: string, state: string | null) => {
    const rule = ruleByService.get(serviceName.toLowerCase())
    if (!rule) return <span className="text-xs text-muted">—</span>
    if (isStopped(state)) return <Badge variant="danger">Alert firing</Badge>
    return <Badge variant="outline">Monitored</Badge>
  }

  return (
    <>
      <TablePanel
        icon={<Wrench className="h-3.5 w-3.5" />}
        title="Reported services"
        hint={`${items.length} in inventory · ${serviceRules.length} alert rule${serviceRules.length === 1 ? '' : 's'}`}
        toolbar={(
          <div className="flex flex-wrap items-center gap-3">
            <Select value={filter} onValueChange={setFilter}>
              <SelectTrigger className="h-8 w-[180px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All states</SelectItem>
                <SelectItem value="running">Running</SelectItem>
                <SelectItem value="stopped">Stopped / failed</SelectItem>
                <SelectItem value="not_found">Not installed</SelectItem>
                <SelectItem value="alert_enabled">Alert enabled</SelectItem>
              </SelectContent>
            </Select>
            <div className="grid flex-1 grid-cols-2 gap-2 sm:grid-cols-4">
              <PanelMiniStat label="Running" value={`${running}/${items.length}`} tone="text-success" />
              <PanelMiniStat label="Filtered" value={String(filtered.length)} />
              <PanelMiniStat label="Alert rules" value={String(serviceRules.length)} />
              <PanelMiniStat
                label="Alert breaches"
                value={String(alertBreaches)}
                tone={alertBreaches > 0 ? 'text-danger' : undefined}
              />
            </div>
          </div>
        )}
      >
        <p className="border-b border-border/50 px-4 py-2 text-[11px] text-muted">
          Services are reported for visibility. Alerts fire only for services you configure — use
          {' '}<Bell className="inline h-3 w-3" /> Alert when stopped on a row, or create a rule on the Alerts tab.
        </p>
        <div className="overflow-hidden">
          <Table>
            <THead className="bg-surface2/40">
              <Tr>
                <Th className="pl-4">Service</Th>
                <Th>State</Th>
                <Th>Start mode</Th>
                <Th>Alert</Th>
                <Th className="text-right">PID</Th>
                <Th>Updated</Th>
                <Th className="pr-4 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {filtered.length === 0 && (
                <Tr><Td colSpan={7}><div className="py-10 text-center text-xs text-muted">No services match</div></Td></Tr>
              )}
              {filtered.map((s, i) => {
                const rule = ruleByService.get(s.service_name.toLowerCase())
                return (
                  <Tr key={s.service_name} className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                    <Td className="py-2 pl-4">
                      <div className="text-sm font-medium">{s.display_name || s.service_name}</div>
                      <div className="text-[11px] text-muted">{s.service_name}</div>
                    </Td>
                    <Td>{stateBadge(s.state)}</Td>
                    <Td className="text-xs">{s.start_mode || '—'}</Td>
                    <Td>{alertBadge(s.service_name, s.state)}</Td>
                    <Td className="text-right text-xs tabular-nums text-muted">{s.pid || '—'}</Td>
                    <Td className="text-xs text-muted">{relativeTime(s.updated_at)}</Td>
                    <Td className="pr-4 text-right">
                      {rule ? (
                        rule.server_id ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => delRule.mutate(rule.id)}
                          >
                            Remove alert
                          </Button>
                        ) : (
                          <span className="text-[11px] text-muted">Global rule</span>
                        )
                      ) : (
                        <Button size="sm" variant="outline" onClick={() => openAlertDialog(s.service_name)}>
                          <Bell className="h-3 w-3" /> Alert when stopped
                        </Button>
                      )}
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        </div>
      </TablePanel>

      <CreateHostRuleDialog
        serverId={serverId}
        open={createOpen}
        onOpenChange={setCreateOpen}
        initial={prefill}
        onCreated={() => qc.invalidateQueries({ queryKey: ['servers', serverId, 'host-rules'] })}
      />
    </>
  )
}

/* ── Storage ─────────────────────────────────────────────────────── */

function StorageTab({ serverId, items }: { serverId: string; items: ServerFilesystem[] }) {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const { data: metrics, isLoading } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'metrics', 'diskio', range.fromISO, range.toISO],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/metrics`, {
        params: { metrics: 'disk_read_bps,disk_write_bps', from: range.fromISO, to: range.toISO },
      })).data,
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
  const disk = toChartRows(metrics, { disk_read_bps: 'read', disk_write_bps: 'write' }) as ChartRow[]
  const tick = timeAxisTickFormatter(range.hours)
  const axis = chartAxis(tick)
  const diskStats = diskIoStats(disk)
  const maxPct = Math.max(0, ...items.map((fs) => fs.used_pct || 0))

  return (
    <div className="space-y-4">
      <PanelToolbar label={<>Storage metrics for <span className="font-medium text-text2">{range.label}</span></>}>
        <TimeRangePicker
          rangeIdx={rangeIdx} isCustom={isCustom}
          customFrom={range.fromISO} customTo={range.toISO}
          onPreset={setPreset} onCustom={setCustom}
        />
      </PanelToolbar>

      <TablePanel
        icon={<HardDrive className="h-3.5 w-3.5" />}
        title="Filesystems"
        hint={`${items.length} volumes`}
      >
        <div className="overflow-hidden px-4 pb-2">
          {items.length === 0 ? <NoData /> : (
            <div className="space-y-3">
              {items.map((fs) => {
                const pct = Math.min(100, fs.used_pct || 0)
                const tone = pct >= 95 ? 'danger' : pct >= 85 ? 'warning' : 'primary'
                const barTone = pct >= 95 ? 'from-danger to-danger/70' : pct >= 85 ? 'from-warning to-warning/70' : 'from-primary to-info'
                return (
                  <div key={fs.mount} className="rounded-lg border border-border/50 bg-surface2/20 px-3 py-2.5">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <span className="font-mono text-sm font-semibold text-text">{fs.mount}</span>
                        <span className="ml-2 text-[11px] text-muted">{fs.fs_type || '—'} · {fs.device || '—'}</span>
                      </div>
                      <span className={cn('text-sm font-bold tabular-nums', tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-text2')}>
                        {pct.toFixed(1)}%
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-surface2">
                      <div className={cn('h-full rounded-full bg-gradient-to-r', barTone)} style={{ width: `${pct}%` }} />
                    </div>
                    <div className="mt-1.5 flex justify-between text-[10px] tabular-nums text-muted">
                      <span>{formatBytes(fs.used_bytes || 0)} used</span>
                      <span>{formatBytes(fs.free_bytes || 0)} free · {formatBytes(fs.total_bytes || 0)} total</span>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </TablePanel>

      <MetricChartCard
        icon={<Activity className="h-3.5 w-3.5" />}
        title="Disk I/O"
        hint={range.label}
        isLoading={isLoading}
        rows={disk}
        miniStats={(
          <div className="grid grid-cols-2 gap-2 px-4 pt-3 sm:grid-cols-4">
            <PanelMiniStat label="Current" value={diskStats ? `${formatBytes(diskStats.current)}/s` : '—'} tone="text-info" />
            <PanelMiniStat label="Average" value={diskStats ? `${formatBytes(diskStats.avg)}/s` : '—'} />
            <PanelMiniStat label="95th pct" value={diskStats ? `${formatBytes(diskStats.p95)}/s` : '—'} tone="text-primary" />
            <PanelMiniStat label="Max usage" value={maxPct ? `${maxPct.toFixed(1)}%` : '—'} tone={maxPct >= 95 ? 'text-danger' : 'text-warning'} />
          </div>
        )}
      >
        <AreaChart data={disk} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
          <defs>
            <linearGradient id="storageRead" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.35} />
              <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="storageWrite" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgb(var(--warning))" stopOpacity={0.35} />
              <stop offset="100%" stopColor="rgb(var(--warning))" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
          <XAxis {...axis} />
          <YAxis width={52} tickFormatter={(v) => `${formatBytes(Number(v))}/s`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
          <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [`${formatBytes(Number(v))}/s`, n === 'read' ? 'Read' : 'Write']} />
          <Legend verticalAlign="top" align="right" iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, paddingBottom: 4 }} />
          <Area type="monotone" dataKey="read" name="Read" stroke="rgb(var(--info))" strokeWidth={2} fill="url(#storageRead)" dot={false} isAnimationActive={false} connectNulls />
          <Area type="monotone" dataKey="write" name="Write" stroke="rgb(var(--warning))" strokeWidth={2} fill="url(#storageWrite)" dot={false} isAnimationActive={false} connectNulls />
        </AreaChart>
      </MetricChartCard>
    </div>
  )
}

/* ── Network ─────────────────────────────────────────────────────── */

function NetworkTab({ serverId }: { serverId: string }) {
  const { data } = useQuery<{ items: ServerNetworkInterface[] }>({
    queryKey: ['servers', serverId, 'network'],
    queryFn: async () => (await api.get(`/servers/${serverId}/network`)).data,
    refetchInterval: 60_000,
  })
  const items = data?.items || []
  const upCount = items.filter((n) => n.is_up).length

  return (
    <TablePanel
      icon={<NetworkIcon className="h-3.5 w-3.5" />}
      title="Network interfaces"
      hint={`${upCount}/${items.length} up`}
      toolbar={items.length > 0 ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          <PanelMiniStat label="Interfaces" value={String(items.length)} />
          <PanelMiniStat label="Up" value={String(upCount)} tone="text-success" />
          <PanelMiniStat label="Down" value={String(items.length - upCount)} tone={items.length - upCount > 0 ? 'text-danger' : undefined} />
        </div>
      ) : undefined}
    >
      <div className="overflow-hidden">
        <Table>
          <THead className="bg-surface2/40">
            <Tr>
              <Th className="pl-4">Interface</Th><Th>State</Th><Th>IP addresses</Th>
              <Th>MAC</Th><Th className="text-right">Speed</Th><Th className="text-right">MTU</Th><Th className="pr-4">Updated</Th>
            </Tr>
          </THead>
          <TBody>
            {items.length === 0 && (
              <Tr><Td colSpan={7}><div className="py-10 text-center text-xs text-muted">No interfaces reported</div></Td></Tr>
            )}
            {items.map((nic, i) => (
              <Tr key={nic.if_name} className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                <Td className="py-2 pl-4 text-sm font-medium">{nic.if_name}</Td>
                <Td>{nic.is_up ? <Badge variant="success">Up</Badge> : <Badge variant="danger">Down</Badge>}</Td>
                <Td className="max-w-[200px] truncate font-mono text-xs" title={(nic.ip_addresses || []).join(', ')}>{(nic.ip_addresses || []).join(', ') || '—'}</Td>
                <Td className="font-mono text-xs text-muted">{nic.mac_address || '—'}</Td>
                <Td className="text-right text-xs tabular-nums">{nic.speed_mbps ? `${nic.speed_mbps} Mbps` : '—'}</Td>
                <Td className="text-right text-xs tabular-nums">{nic.mtu || '—'}</Td>
                <Td className="pr-4 text-xs text-muted">{relativeTime(nic.updated_at)}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
    </TablePanel>
  )
}

/* ── Software ────────────────────────────────────────────────────── */

function SoftwareTab({ serverId }: { serverId: string }) {
  const [q, setQ] = useState('')
  const { data } = useQuery<{ items: ServerSoftware[] }>({
    queryKey: ['servers', serverId, 'software'],
    queryFn: async () => (await api.get(`/servers/${serverId}/software`)).data,
    refetchInterval: 60_000,
  })
  const items = data?.items || []
  const filtered = q
    ? items.filter((s) =>
        s.package_name.toLowerCase().includes(q.toLowerCase()) ||
        (s.vendor || '').toLowerCase().includes(q.toLowerCase()))
    : items

  return (
    <TablePanel
      icon={<Package className="h-3.5 w-3.5" />}
      title="Software inventory"
      hint={`${filtered.length} of ${items.length} packages`}
      toolbar={(
        <div className="relative min-w-[220px]">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <Input className="h-8 pl-8" placeholder="Filter packages or vendors…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      )}
    >
      <div className="overflow-hidden">
        <Table>
          <THead className="bg-surface2/40">
            <Tr><Th className="pl-4">Package</Th><Th>Version</Th><Th>Vendor</Th><Th>Installed</Th><Th className="pr-4">Last reported</Th></Tr>
          </THead>
          <TBody>
            {filtered.length === 0 && (
              <Tr><Td colSpan={5}><div className="py-10 text-center text-xs text-muted">No software inventory yet — the agent uploads it with its inventory snapshot</div></Td></Tr>
            )}
            {filtered.map((s, i) => (
              <Tr key={s.package_name} className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                <Td className="py-2 pl-4 text-sm font-medium">{s.package_name}</Td>
                <Td className="text-xs tabular-nums">{s.version || '—'}</Td>
                <Td className="text-xs text-muted">{s.vendor || '—'}</Td>
                <Td className="text-xs text-muted">{s.install_date ? new Date(s.install_date).toLocaleDateString() : '—'}</Td>
                <Td className="pr-4 text-xs text-muted">{relativeTime(s.updated_at)}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
    </TablePanel>
  )
}

/* ── Compliance ──────────────────────────────────────────────────── */

function ComplianceTab({ serverId }: { serverId: string }) {
  const qc = useQueryClient()
  const { data } = useQuery<{ items: ComplianceResult[]; summary: ComplianceSummary }>({
    queryKey: ['servers', serverId, 'compliance'],
    queryFn: async () => (await api.get(`/servers/${serverId}/compliance`)).data,
    refetchInterval: 60_000,
  })
  const evaluate = useMutation({
    mutationFn: async () => (await api.post(`/servers/${serverId}/evaluate-baselines`)).data,
    onSuccess: (r) => {
      toast.success('Evaluation complete', `${r.evaluated ?? 0} rules checked`)
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'compliance'] })
    },
    onError: (e) => toast.error('Evaluation failed', apiErrorMessage(e)),
  })

  const items = data?.items || []
  const summary = data?.summary

  const statusBadge = (st: string) => {
    if (st === 'compliant') return <Badge variant="success">Compliant</Badge>
    if (st === 'outdated') return <Badge variant="warning">Outdated</Badge>
    if (st === 'missing') return <Badge variant="danger">Missing</Badge>
    return <Badge variant="danger">Prohibited</Badge>
  }

  return (
    <TablePanel
      icon={<ClipboardCheck className="h-3.5 w-3.5" />}
      title="Software compliance"
      hint={summary && summary.total > 0 ? `${summary.compliant}/${summary.total} compliant` : 'no baselines'}
      right={(
        <div className="flex items-center gap-2">
          <Link to="/server-baselines" className="text-xs font-medium text-primary hover:underline">
            Manage baselines
          </Link>
          <Button variant="outline" size="sm" onClick={() => evaluate.mutate()} disabled={evaluate.isPending}>
            <RefreshCw className={cn('h-3.5 w-3.5', evaluate.isPending && 'animate-spin')} /> Evaluate now
          </Button>
        </div>
      )}
      toolbar={summary && summary.total > 0 ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <PanelMiniStat label="Compliant" value={String(summary.compliant)} tone="text-success" />
          <PanelMiniStat label="Missing" value={String(summary.missing)} tone={summary.missing > 0 ? 'text-danger' : undefined} />
          <PanelMiniStat label="Outdated" value={String(summary.outdated)} tone={summary.outdated > 0 ? 'text-warning' : undefined} />
          <PanelMiniStat label="Prohibited" value={String(summary.prohibited)} tone={summary.prohibited > 0 ? 'text-danger' : undefined} />
        </div>
      ) : (
        <p className="text-xs text-muted">Define a software baseline matching this server&apos;s OS or tags to enable compliance checks.</p>
      )}
    >
      <div className="overflow-hidden">
        <Table>
          <THead className="bg-surface2/40">
            <Tr>
              <Th className="pl-4">Status</Th><Th>Baseline</Th><Th>Rule</Th>
              <Th>Found</Th><Th>Expected</Th><Th>Severity</Th><Th className="pr-4">Failing since</Th>
            </Tr>
          </THead>
          <TBody>
            {items.length === 0 && (
              <Tr>
                <Td colSpan={7}>
                  <div className="flex flex-col items-center gap-2 py-10 text-center">
                    <ClipboardCheck className="h-7 w-7 text-muted/50" />
                    <div className="max-w-md text-xs text-muted">
                      Define a software baseline matching this server&apos;s OS or tags — required apps,
                      minimum versions, prohibited software — and violations will alert automatically.
                    </div>
                  </div>
                </Td>
              </Tr>
            )}
            {items.map((r, i) => (
              <Tr key={r.rule_id} className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                <Td className="py-2 pl-4">{statusBadge(r.status)}</Td>
                <Td className="text-xs">{r.baseline_name}</Td>
                <Td>
                  <div className="text-sm font-medium">{r.package_match}</div>
                  <div className="text-[11px] text-muted">{r.rule_type} · {r.match_type}{r.min_version ? ` · ≥ ${r.min_version}` : ''}</div>
                </Td>
                <Td className="text-xs">
                  {r.found_package ? <>{r.found_package} <span className="tabular-nums text-muted">{r.found_version}</span></> : '—'}
                </Td>
                <Td className="text-xs text-muted">{r.expected}</Td>
                <Td>
                  <Badge variant={r.severity === 'critical' ? 'danger' : r.severity === 'warning' ? 'warning' : 'info'}>
                    {r.severity}
                  </Badge>
                </Td>
                <Td className="pr-4 text-xs text-muted">{r.first_failed_at ? relativeTime(r.first_failed_at) : '—'}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
    </TablePanel>
  )
}

/* ── Alerts ──────────────────────────────────────────────────────── */

interface ServerAlert {
  id: string
  server_id: string | null
  severity: string
  status: string
  message: string
  triggered_at: string
  acknowledged_at: string | null
  resolved_at: string | null
}

const HOST_METRICS = [
  { value: 'host_cpu_pct', label: 'CPU usage', kind: 'pct' },
  { value: 'host_memory_pct', label: 'Memory usage', kind: 'pct' },
  { value: 'host_filesystem_pct', label: 'Filesystem usage', kind: 'pct' },
  { value: 'host_disk_util_pct', label: 'Disk I/O utilization', kind: 'pct' },
  { value: 'host_service_down', label: 'Service stopped', kind: 'svc' },
  { value: 'host_process_down', label: 'Process not running', kind: 'proc' },
] as const

const OP_SYMBOL: Record<string, string> = {
  gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠',
  '>': '>', '>=': '≥', '<': '<', '<=': '≤', '==': '=', '!=': '≠',
}

function metricLabel(metric: string): string {
  return HOST_METRICS.find((m) => m.value === metric)?.label || metric
}

function ruleCondition(r: HostRule): string {
  const m = HOST_METRICS.find((x) => x.value === r.metric)
  if (m?.kind === 'svc') return `Service ${r.target || '?'} stopped`
  if (m?.kind === 'proc') return `Process ${r.target || '?'} not running`
  return `${metricLabel(r.metric)} ${OP_SYMBOL[r.operator] || r.operator} ${r.threshold}%`
}

function sevBadge(sev: string) {
  if (sev === 'critical') return <Badge variant="danger">Critical</Badge>
  if (sev === 'warning') return <Badge variant="warning">Warning</Badge>
  return <Badge variant="info">{sev}</Badge>
}

function statusBadge(st: string) {
  if (st === 'active') return <Badge variant="danger">Active</Badge>
  if (st === 'acknowledged') return <Badge variant="warning">Acknowledged</Badge>
  return <Badge variant="info">Resolved</Badge>
}

function ServerAlertsTab({
  serverId,
  serverStatus,
  statusReasons,
}: {
  serverId: string
  serverStatus: string
  statusReasons: string[]
}) {
  const qc = useQueryClient()
  const [status, setStatus] = useState('active')
  const [createOpen, setCreateOpen] = useState(false)

  const { data: alertsResp } = useQuery<{ data: ServerAlert[]; meta: { total: number } }>({
    queryKey: ['servers', serverId, 'alerts', status],
    queryFn: async () => (await api.get(`/alerts`, {
      params: { server_id: serverId, limit: 100, ...(status !== 'all' ? { status } : {}) },
    })).data,
    refetchInterval: 15_000,
  })
  const alerts = alertsResp?.data || []

  const { data: openAlertsResp } = useQuery<{ data: ServerAlert[]; meta: { total: number } }>({
    queryKey: ['servers', serverId, 'alerts', 'open'],
    queryFn: async () => {
      const [active, acknowledged] = await Promise.all([
        api.get('/alerts', { params: { server_id: serverId, status: 'active', limit: 100 } }),
        api.get('/alerts', { params: { server_id: serverId, status: 'acknowledged', limit: 100 } }),
      ])
      const data = [...active.data.data, ...acknowledged.data.data]
      return { data, meta: { total: active.data.meta.total + acknowledged.data.meta.total } }
    },
    refetchInterval: 15_000,
  })
  const openAlerts = openAlertsResp?.data || []
  const acknowledgedCount = openAlerts.filter((a) => a.status === 'acknowledged').length

  const { data: rulesResp } = useQuery<{ items: HostRule[] }>({
    queryKey: ['servers', serverId, 'host-rules'],
    queryFn: async () => (await api.get(`/host-alert-rules`, { params: { server_id: serverId } })).data,
  })
  const rules = rulesResp?.items || []

  const { data: silencesResp } = useQuery<{ items: { id: string; dedupe: string; until: string | null; forever: boolean }[] }>({
    queryKey: ['servers', serverId, 'silences'],
    queryFn: async () => (await api.get(`/alerts/silences`, { params: { server_id: serverId } })).data,
    refetchInterval: 30_000,
  })
  const silences = silencesResp?.items || []

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['servers', serverId, 'alerts'] })
    qc.invalidateQueries({ queryKey: ['servers', serverId, 'host-rules'] })
    qc.invalidateQueries({ queryKey: ['servers', serverId, 'silences'] })
  }

  const ack = useMutation({
    mutationFn: async (id: string) => (await api.post(`/alerts/${id}/acknowledge`)).data,
    onSuccess: () => { invalidate(); toast.success('Alert acknowledged') },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })
  const resolve = useMutation({
    mutationFn: async (id: string) => (await api.post(`/alerts/${id}/resolve`)).data,
    onSuccess: () => { invalidate(); toast.success('Alert resolved') },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })
  const toggleRule = useMutation({
    mutationFn: async (id: string) => (await api.post(`/host-alert-rules/${id}/toggle`)).data,
    onSuccess: () => { invalidate(); toast.success('Rule updated') },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })
  const delRule = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/host-alert-rules/${id}`)).data,
    onSuccess: () => { invalidate(); toast.success('Rule deleted') },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })
  const snooze = useMutation({
    mutationFn: async ({ id, minutes }: { id: string; minutes: number | null }) =>
      (await api.post(`/alerts/${id}/snooze`, { minutes })).data,
    onSuccess: (_d, v) => { invalidate(); toast.success(v.minutes ? 'Alert snoozed' : 'Alert muted') },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })
  const removeSilence = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/alerts/silences/${id}`)).data,
    onSuccess: () => { invalidate(); toast.success('Silence removed') },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const counts = {
    critical: openAlerts.filter((a) => a.severity === 'critical').length,
    warning: openAlerts.filter((a) => a.severity === 'warning').length,
    active: openAlerts.filter((a) => a.status === 'active').length,
    acknowledged: acknowledgedCount,
  }

  const healthOnlyReasons =
    statusReasons.length > 0 &&
    (serverStatus === 'warning' || serverStatus === 'critical') &&
    openAlerts.length === 0

  return (
    <div className="space-y-4">
      {(serverStatus === 'warning' || serverStatus === 'critical') && statusReasons.length > 0 && (
        <Card className={cn('border', serverStatus === 'critical' ? 'border-danger/30 bg-danger/5' : 'border-warning/30 bg-warning/5')}>
          <CardContent className="pt-4">
            <div className="mb-2 flex items-center gap-2 text-sm font-medium">
              <AlertTriangle className={cn('h-4 w-4', serverStatus === 'critical' ? 'text-danger' : 'text-warning')} />
              Server health: {serverStatus}
            </div>
            <p className="mb-2 text-xs text-muted">
              Health status comes from live telemetry (CPU, memory, disk, services). It stays {serverStatus} until
              the underlying issue is fixed — acknowledging alerts does not clear health status.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {statusReasons.map((r) => (
                <span
                  key={r}
                  className={cn(
                    'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]',
                    serverStatus === 'critical' ? 'bg-danger/10 text-danger' : 'bg-warning/10 text-warning',
                  )}
                >
                  <AlertTriangle className="h-3 w-3" /> {r}
                </span>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {healthOnlyReasons && (
        <Card className="border-warning/30 bg-warning/5">
          <CardContent className="pt-4">
            <p className="text-xs text-muted">
              No alert rows yet for these conditions — they will appear after the next health evaluation.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Active alerts */}
      <Card>
        <CardContent className="pt-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">Alerts</span>
            <span className="text-xs text-muted">
              {counts.active} active · {counts.acknowledged} acknowledged · {counts.critical} critical · {counts.warning} warning
            </span>
            <div className="ml-auto flex items-center gap-2">
              <Select value={status} onValueChange={setStatus}>
                <SelectTrigger className="h-8 w-[150px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="active">Active</SelectItem>
                  <SelectItem value="acknowledged">Acknowledged</SelectItem>
                  <SelectItem value="resolved">Resolved</SelectItem>
                  <SelectItem value="all">All</SelectItem>
                </SelectContent>
              </Select>
              <Button size="sm" onClick={() => setCreateOpen(true)}>New alert rule</Button>
            </div>
          </div>
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr><Th>Severity</Th><Th>Alert</Th><Th>Status</Th><Th>Triggered</Th><Th className="text-right">Actions</Th></Tr>
              </THead>
              <TBody>
                {alerts.length === 0 && (
                  <Tr><Td colSpan={5}><div className="py-8 text-center text-xs text-muted">
                    No {status === 'all' ? '' : status} alerts
                    {status === 'active' && acknowledgedCount > 0 && (
                      <> — {acknowledgedCount} acknowledged alert{acknowledgedCount === 1 ? '' : 's'} still open (switch to “Acknowledged”)</>
                    )}
                    {status === 'active' && acknowledgedCount === 0 && ' — switch to “All” to see resolved history'}
                  </div></Td></Tr>
                )}
                {alerts.map((a) => (
                  <Tr key={a.id}>
                    <Td>{sevBadge(a.severity)}</Td>
                    <Td className="text-sm">{a.message}</Td>
                    <Td>{statusBadge(a.status)}</Td>
                    <Td className="text-xs text-muted">{relativeTime(a.triggered_at)}</Td>
                    <Td className="text-right">
                      <div className="flex justify-end gap-1">
                        {a.status === 'active' && (
                          <Button size="sm" variant="outline" onClick={() => ack.mutate(a.id)}>Ack</Button>
                        )}
                        {a.status !== 'resolved' && a.server_id && (
                          <Select value="" onValueChange={(v) => snooze.mutate({ id: a.id, minutes: v === 'forever' ? null : Number(v) })}>
                            <SelectTrigger className="h-8 w-[100px]"><SelectValue placeholder="Snooze" /></SelectTrigger>
                            <SelectContent>
                              <SelectItem value="60">1 hour</SelectItem>
                              <SelectItem value="240">4 hours</SelectItem>
                              <SelectItem value="1440">24 hours</SelectItem>
                              <SelectItem value="forever">Mute forever</SelectItem>
                            </SelectContent>
                          </Select>
                        )}
                        {a.status !== 'resolved' && (
                          <Button size="sm" variant="outline" onClick={() => resolve.mutate(a.id)}>Resolve</Button>
                        )}
                      </div>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {/* Snoozed / muted conditions */}
      {silences.length > 0 && (
        <Card>
          <CardContent className="pt-4">
            <div className="mb-3 text-sm font-medium">Snoozed &amp; muted</div>
            <div className="flex flex-col gap-1.5">
              {silences.map((s) => {
                const rid = s.dedupe.startsWith('rule:') ? s.dedupe.slice(5) : null
                const label = rid
                  ? (rules.find((r) => r.id === rid)?.name || 'Alert rule')
                  : s.dedupe === 'agent_offline'
                    ? 'Agent offline'
                    : s.dedupe.startsWith('health:')
                      ? 'Health threshold'
                      : s.dedupe
                return (
                  <div key={s.id} className="flex items-center gap-2 text-xs">
                    <Badge variant="info">{s.forever ? 'Muted' : 'Snoozed'}</Badge>
                    <span className="font-medium">{label}</span>
                    <span className="text-muted">
                      {s.forever ? 'until removed' : `until ${s.until ? new Date(s.until).toLocaleString() : ''}`}
                    </span>
                    <Button size="sm" variant="outline" className="ml-auto" onClick={() => removeSilence.mutate(s.id)}>
                      Remove
                    </Button>
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Alert rules */}
      <Card>
        <CardContent className="pt-4">
          <div className="mb-3 flex items-center gap-2">
            <span className="text-sm font-medium">Alert rules</span>
            <span className="text-xs text-muted">applying to this server (global + server-specific)</span>
          </div>
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr><Th>On</Th><Th>Name</Th><Th>Condition</Th><Th>Severity</Th><Th>Scope</Th><Th className="text-right">Actions</Th></Tr>
              </THead>
              <TBody>
                {rules.length === 0 && (
                  <Tr><Td colSpan={6}><div className="py-8 text-center text-xs text-muted">No host alert rules</div></Td></Tr>
                )}
                {rules.map((r) => (
                  <Tr key={r.id}>
                    <Td><Switch checked={r.enabled} onCheckedChange={() => toggleRule.mutate(r.id)} /></Td>
                    <Td className="text-sm font-medium">{r.name}</Td>
                    <Td className="text-xs text-muted">{ruleCondition(r)}</Td>
                    <Td>{sevBadge(r.severity)}</Td>
                    <Td className="text-xs text-muted">{r.server_id ? 'This server' : 'All servers'}</Td>
                    <Td className="text-right">
                      {r.server_id ? (
                        <Button size="sm" variant="outline" onClick={() => delRule.mutate(r.id)}>Delete</Button>
                      ) : (
                        <span className="text-xs text-muted">global</span>
                      )}
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <CreateHostRuleDialog
        serverId={serverId}
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={invalidate}
      />
    </div>
  )
}

function CreateHostRuleDialog({
  serverId, open, onOpenChange, onCreated, initial,
}: {
  serverId: string
  open: boolean
  onOpenChange: (v: boolean) => void
  onCreated: () => void
  initial?: HostRulePrefill
}) {
  const [name, setName] = useState('')
  const [metric, setMetric] = useState<string>('host_cpu_pct')
  const [operator, setOperator] = useState('gt')
  const [threshold, setThreshold] = useState('90')
  const [severity, setSeverity] = useState('warning')
  const [minutes, setMinutes] = useState('5')
  const [target, setTarget] = useState('')
  const [allServers, setAllServers] = useState(false)
  const [channels, setChannels] = useState<string[]>([])

  const kind = HOST_METRICS.find((m) => m.value === metric)?.kind || 'pct'

  const applyInitial = (prefill?: HostRulePrefill) => {
    if (!prefill) {
      setName(''); setMetric('host_cpu_pct'); setOperator('gt'); setThreshold('90')
      setSeverity('warning'); setMinutes('5'); setTarget(''); setAllServers(false); setChannels([])
      return
    }
    setName(prefill.name || '')
    setMetric(prefill.metric || 'host_cpu_pct')
    setTarget(prefill.target || '')
    setSeverity(prefill.severity || 'warning')
    setAllServers(false)
  }

  // Apply prefill when the dialog opens (e.g. from Services tab row action).
  useEffect(() => {
    if (open) applyInitial(initial)
  }, [open, initial])

  const { data: chResp } = useQuery<{ data: { id: string; name: string; type: string }[] }>({
    queryKey: ['notification-channels'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    enabled: open,
  })
  const allChannels = chResp?.data || []

  const create = useMutation({
    mutationFn: async () => {
      if ((kind === 'svc' || kind === 'proc') && !target.trim()) {
        throw new Error(kind === 'svc' ? 'Service name is required' : 'Process name is required')
      }
      return (await api.post('/host-alert-rules', {
        name: name || `${metricLabel(metric)} alert`,
        metric, operator,
        threshold: kind === 'pct' ? Number(threshold) : 0,
        severity,
        min_duration: kind === 'pct' ? Math.round(Number(minutes) * 60) : 0,
        target: (kind === 'svc' || kind === 'proc') ? target.trim() : null,
        server_id: allServers ? null : serverId,
        notify_channels: channels,
      })).data
    },
    onSuccess: () => { toast.success('Alert rule created'); onCreated(); onOpenChange(false); reset() },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const reset = () => {
    setName(''); setMetric('host_cpu_pct'); setOperator('gt'); setThreshold('90')
    setSeverity('warning'); setMinutes('5'); setTarget(''); setAllServers(false); setChannels([])
  }

  const toggleChannel = (id: string) =>
    setChannels((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>New alert rule</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <div>
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. High CPU on web server" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Metric</Label>
              <Select value={metric} onValueChange={(v) => { setMetric(v); if (v === 'host_filesystem_pct') setThreshold('85') }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {HOST_METRICS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>Severity</Label>
              <Select value={severity} onValueChange={setSeverity}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="info">Info</SelectItem>
                  <SelectItem value="warning">Warning</SelectItem>
                  <SelectItem value="critical">Critical</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {kind === 'pct' ? (
            <div className="grid grid-cols-3 gap-3">
              <div>
                <Label>Condition</Label>
                <Select value={operator} onValueChange={setOperator}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="gt">Above (&gt;)</SelectItem>
                    <SelectItem value="gte">At least (≥)</SelectItem>
                    <SelectItem value="lt">Below (&lt;)</SelectItem>
                    <SelectItem value="lte">At most (≤)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label>Threshold %</Label>
                <Input type="number" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
              </div>
              <div>
                <Label>For (min)</Label>
                <Input type="number" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
              </div>
            </div>
          ) : (
            <div>
              <Label>{kind === 'svc' ? 'Service name' : 'Process name'}</Label>
              <Input value={target} onChange={(e) => setTarget(e.target.value)} placeholder={kind === 'svc' ? 'e.g. MSSQLSERVER' : 'e.g. nginx.exe'} required />
            </div>
          )}

          <div className="flex items-center gap-2">
            <Switch checked={allServers} onCheckedChange={setAllServers} />
            <span className="text-sm">Apply to all servers (not just this one)</span>
          </div>

          {allChannels.length > 0 && (
            <div>
              <Label>Notify channels</Label>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {allChannels.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => toggleChannel(c.id)}
                    className={cn(
                      'rounded-md border px-2 py-1 text-xs',
                      channels.includes(c.id) ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted',
                    )}
                  >
                    {c.name} <span className="opacity-60">({c.type})</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create rule'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Events ──────────────────────────────────────────────────────── */

function EventsTab({ serverId }: { serverId: string }) {
  const [level, setLevel] = useState('all')
  const { data } = useQuery<{ items: ServerEventRow[] }>({
    queryKey: ['servers', serverId, 'events'],
    queryFn: async () => (await api.get(`/servers/${serverId}/events`)).data,
    refetchInterval: 60_000,
  })
  const items = (data?.items || []).filter((e) => level === 'all' || e.level === level)

  const levelBadge = (lv: string) => {
    if (lv === 'critical') return <Badge variant="danger">Critical</Badge>
    if (lv === 'error') return <Badge variant="danger">Error</Badge>
    if (lv === 'warning') return <Badge variant="warning">Warning</Badge>
    return <Badge variant="info">{lv}</Badge>
  }

  const totalEvents = items.reduce((s, e) => s + (e.count || 0), 0)

  return (
    <TablePanel
      icon={<ScrollText className="h-3.5 w-3.5" />}
      title="Event log summaries"
      hint="last 24h"
      toolbar={(
        <div className="flex flex-wrap items-center gap-3">
          <Select value={level} onValueChange={setLevel}>
            <SelectTrigger className="h-8 w-[150px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All levels</SelectItem>
              <SelectItem value="critical">Critical</SelectItem>
              <SelectItem value="error">Error</SelectItem>
              <SelectItem value="warning">Warning</SelectItem>
            </SelectContent>
          </Select>
          <div className="grid flex-1 grid-cols-2 gap-2 sm:grid-cols-3">
            <PanelMiniStat label="Rows" value={String(items.length)} />
            <PanelMiniStat label="Total events" value={totalEvents.toLocaleString()} tone="text-info" />
            <PanelMiniStat label="Filter" value={level === 'all' ? 'All levels' : level} />
          </div>
        </div>
      )}
    >
      <div className="overflow-hidden">
        <Table>
          <THead className="bg-surface2/40">
            <Tr><Th className="pl-4">Time</Th><Th>Log</Th><Th>Level</Th><Th className="pr-4 text-right">Events</Th></Tr>
          </THead>
          <TBody>
            {items.length === 0 && (
              <Tr><Td colSpan={4}><div className="py-10 text-center text-xs text-muted">No event summaries in the last 24h</div></Td></Tr>
            )}
            {items.map((e, i) => (
              <Tr key={`${e.timestamp}-${e.log_name}-${e.level}-${i}`} className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                <Td className="py-2 pl-4 text-xs tabular-nums">{new Date(e.timestamp).toLocaleString()}</Td>
                <Td className="text-xs font-medium">{e.log_name}</Td>
                <Td>{levelBadge(e.level)}</Td>
                <Td className="pr-4 text-right text-xs font-semibold tabular-nums">{e.count.toLocaleString()}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
    </TablePanel>
  )
}

/* ── Agent ───────────────────────────────────────────────────────── */

function AgentTab({
  serverId,
  serverName,
  onDeployAgent,
}: {
  serverId: string
  serverName: string
  onDeployAgent: () => void
}) {
  const qc = useQueryClient()
  const { data: agent } = useQuery<AgentItem | null>({
    queryKey: ['servers', serverId, 'agent'],
    queryFn: async () => (await api.get(`/servers/${serverId}/agent`)).data,
    refetchInterval: 15_000,
  })
  const { data: commands } = useQuery<{ items: ServerCommand[] }>({
    queryKey: ['servers', serverId, 'commands'],
    queryFn: async () => (await api.get(`/servers/${serverId}/commands`)).data,
    refetchInterval: 15_000,
  })

  const act = useMutation({
    mutationFn: async ({ url }: { url: string; label: string }) => (await api.post(url)).data,
    onSuccess: (_d, v) => {
      toast.success(`${v.label} queued`, 'The agent picks it up on its next poll')
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'commands'] })
    },
    onError: (e) => toast.error('Action failed', apiErrorMessage(e)),
  })

  if (!agent) {
    return (
      <Card className="overflow-hidden">
        <PanelHeader icon={<Bot className="h-3.5 w-3.5" />} title="Agent" hint="not enrolled" />
        <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-border bg-surface2/40">
            <Bot className="h-7 w-7 text-muted/60" />
          </div>
          <div className="text-sm font-medium">No agent enrolled on this server</div>
          <div className="max-w-md text-xs text-muted">
            Use “Deploy agent” above to generate an enrollment token, or switch the server to an
            agentless collection mode in Settings.
          </div>
          <Button size="sm" onClick={onDeployAgent}>
            <KeyRound className="h-3.5 w-3.5" /> Deploy agent
          </Button>
        </CardContent>
      </Card>
    )
  }

  const cmdStatus = (c: ServerCommand) => {
    if (c.status === 'succeeded') return <Badge variant="success">Succeeded</Badge>
    if (c.status === 'failed') return <Badge variant="danger" title={c.error_message || undefined}>Failed</Badge>
    if (c.status === 'queued') return <Badge variant="info">Queued</Badge>
    if (c.status === 'sent' || c.status === 'running') return <Badge variant="warning">{c.status}</Badge>
    return <Badge variant="outline">{c.status}</Badge>
  }

  const info: [string, React.ReactNode][] = [
    ['Status', <AgentStatusBadge key="st" status={agent.status} />],
    ['Version', agent.version || '—'],
    ['Agent UID', <span key="uid" className="font-mono text-xs">{agent.agent_uid}</span>],
    ['Last heartbeat', relativeTime(agent.last_heartbeat_at)],
    ['Last metrics', relativeTime(agent.last_metric_at)],
    ['Queue depth', String(agent.queue_depth)],
    ['Spool size', formatBytes(agent.spool_bytes)],
    ['Update ring', agent.update_ring],
    ['Policy', agent.policy_name || 'Platform default'],
    ['API key', agent.api_key_prefix ? `${agent.api_key_prefix}…` : '—'],
    ['Last IP', agent.last_ip || '—'],
    ['Enrolled', relativeTime(agent.created_at)],
  ]

  return (
    <div className="space-y-4">
      {agent.config_apply_error && (
        <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/5 p-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <div className="font-medium">Config apply error</div>
            <div className="text-xs">{agent.config_apply_error}</div>
          </div>
        </div>
      )}

      <Card className="overflow-hidden">
        <PanelHeader
          icon={<Bot className="h-3.5 w-3.5" />}
          title="Agent"
          hint={agent.status}
          right={(
            <div className="flex items-center gap-2">
              <Button
                variant="outline" size="sm"
                onClick={onDeployAgent}
                title={`Generate a new enrollment key for ${serverName}`}
              >
                <KeyRound className="h-3.5 w-3.5" /> Generate key
              </Button>
              <Button
                variant="outline" size="sm"
                onClick={() => act.mutate({ url: `/agent-fleet/${agent.id}/request-diagnostics`, label: 'Diagnostics upload' })}
              >
                <FileDown className="h-3.5 w-3.5" /> Request diagnostics
              </Button>
              <Button
                variant="outline" size="sm"
                onClick={() => act.mutate({ url: `/agent-fleet/${agent.id}/rotate-certificate`, label: 'Credential rotation' })}
              >
                <KeyRound className="h-3.5 w-3.5" /> Rotate credentials
              </Button>
            </div>
          )}
        />
        <CardContent className="px-4 pb-4 pt-3">
          <InfoGrid rows={info} />
        </CardContent>
      </Card>

      <TablePanel
        icon={<ScrollText className="h-3.5 w-3.5" />}
        title="Command history"
        hint={`${(commands?.items || []).length} commands`}
      >
        <div className="overflow-hidden">
          <Table>
            <THead className="bg-surface2/40">
              <Tr><Th className="pl-4">Command</Th><Th>Status</Th><Th>Requested by</Th><Th>Created</Th><Th className="pr-4">Completed</Th></Tr>
            </THead>
            <TBody>
              {(commands?.items || []).length === 0 && (
                <Tr><Td colSpan={5}><div className="py-8 text-center text-xs text-muted">No commands sent yet — actions above queue commands the agent picks up on its next poll (≤30s)</div></Td></Tr>
              )}
              {(commands?.items || []).map((c, i) => (
                <Tr key={c.id} className={i % 2 === 0 ? 'bg-surface2/10' : undefined}>
                  <Td className="py-2 pl-4 font-mono text-xs">{c.command}</Td>
                  <Td>{cmdStatus(c)}</Td>
                  <Td className="text-xs text-muted">{c.requested_by_name || 'system'}</Td>
                  <Td className="text-xs text-muted">{relativeTime(c.created_at)}</Td>
                  <Td className="pr-4 text-xs text-muted">{c.completed_at ? relativeTime(c.completed_at) : '—'}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </TablePanel>
    </div>
  )
}

/* ── Settings ────────────────────────────────────────────────────── */

function SettingsTab({ server, onEdit, onDecommission, onDelete }: {
  server: ServerItem
  onEdit: () => void
  onDecommission: () => void
  onDelete: () => void
}) {
  const rows: [string, React.ReactNode][] = [
    ['Display name', server.display_name],
    ['Hostname', server.hostname || '—'],
    ['FQDN', server.fqdn || '—'],
    ['Primary IP', server.primary_ip || '—'],
    ['OS', `${server.os_name || server.os_type} ${server.os_version || ''}`],
    ['Build', server.kernel_or_build || '—'],
    ['Architecture', server.architecture || '—'],
    ['Collection mode', server.collection_mode],
    ['Environment', server.environment || '—'],
    ['Owner', server.owner || '—'],
    ['Site', server.site_name || '—'],
    ['Tags', <TagList key="tags" tags={server.tags} max={12} />],
    ['Description', server.description || '—'],
    ['Registered', new Date(server.created_at).toLocaleString()],
  ]

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <PanelHeader
          icon={<Settings2 className="h-3.5 w-3.5" />}
          title="Server record"
          hint={server.collection_mode}
          right={(
            <Button variant="outline" size="sm" onClick={onEdit}>
              <Pencil className="h-3.5 w-3.5" /> Edit
            </Button>
          )}
        />
        <CardContent className="px-4 pb-4 pt-3">
          <InfoGrid rows={rows} />
        </CardContent>
      </Card>

      <Card className="overflow-hidden border-danger/30">
        <PanelHeader
          icon={<AlertTriangle className="h-3.5 w-3.5 text-danger" />}
          title="Danger zone"
          hint="irreversible actions"
        />
        <CardContent className="flex flex-wrap items-center gap-3 px-4 pb-4 pt-3">
          <div className="flex-1 text-xs text-muted">
            Decommissioning keeps the record but disables monitoring and alerting.
            Deleting removes the server, its inventory, and alert history permanently.
          </div>
          <Button variant="outline" size="sm" onClick={onDecommission} disabled={server.status === 'disabled'}>
            <CloudOff className="h-3.5 w-3.5" /> Decommission
          </Button>
          <Button variant="destructive" size="sm" onClick={onDelete}>
            <Trash2 className="h-3.5 w-3.5" /> Delete server
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}

/* ── Shared ──────────────────────────────────────────────────────── */

function NoData() {
  return (
    <div className="flex h-full min-h-[80px] flex-col items-center justify-center gap-1 text-center">
      <Database className="h-5 w-5 text-muted/40" />
      <span className="text-xs text-muted">No data for this window</span>
    </div>
  )
}
