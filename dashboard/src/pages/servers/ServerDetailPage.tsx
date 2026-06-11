/** Server detail — single-pane view of one monitored server.
 *
 *  Tabs: Overview · Performance · Processes · Services · Storage · Network
 *        · Software · Compliance · Events · Agent · Settings
 */

import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  CheckCircle2,
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
import { api } from '@/lib/api'
import {
  apiErrorMessage, cn, formatBps, formatBytes, relativeTime,
  timeAxisTickFormatter, timeTooltipLabelFormatter,
} from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import { ServerFormDialog } from '@/components/servers/ServerFormDialog'
import {
  AgentStatusBadge, KpiTile, OsIcon, ServerStatusBadge, TagList, UsageBar,
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
    borderRadius: 6,
    color: 'rgb(var(--text))',
    fontSize: 11,
    padding: '5px 8px',
  },
  labelFormatter: timeTooltipLabelFormatter,
})

const TABS = [
  { key: 'overview', label: 'Overview', icon: Gauge },
  { key: 'performance', label: 'Performance', icon: Cpu },
  { key: 'processes', label: 'Processes', icon: ListTree },
  { key: 'services', label: 'Services', icon: Wrench },
  { key: 'storage', label: 'Storage', icon: HardDrive },
  { key: 'network', label: 'Network', icon: NetworkIcon },
  { key: 'software', label: 'Software', icon: Package },
  { key: 'compliance', label: 'Compliance', icon: ClipboardCheck },
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

  const { data: processes } = useQuery<{ items: ServerProcess[] }>({
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
            to="/servers"
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
          sub={svcItems.length ? 'running / watched' : undefined}
          tone={svcItems.length && svcRunning < svcItems.length ? 'warning' : 'default'}
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
      {tab === 'processes' && <ProcessesTab items={processes?.items || []} />}
      {tab === 'services' && <ServicesTab items={svcItems} />}
      {tab === 'storage' && <StorageTab serverId={server.id} items={fsItems} />}
      {tab === 'network' && <NetworkTab serverId={server.id} />}
      {tab === 'software' && <SoftwareTab serverId={server.id} />}
      {tab === 'compliance' && <ComplianceTab serverId={server.id} />}
      {tab === 'events' && <EventsTab serverId={server.id} />}
      {tab === 'agent' && <AgentTab serverId={server.id} />}
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

/* ── Overview ────────────────────────────────────────────────────── */

function OverviewTab({ serverId, filesystems, processes }: {
  serverId: string
  filesystems: ServerFilesystem[]
  processes: ServerProcess[]
}) {
  const to = useMemo(() => new Date(), [])
  const from = useMemo(() => new Date(Date.now() - 6 * 3_600_000), [])
  const { data: metrics } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'metrics', 'overview'],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/metrics`, {
        params: {
          metrics: 'cpu_total_pct,memory_used_pct,network_rx_bps,network_tx_bps',
          from: from.toISOString(), to: to.toISOString(),
        },
      })).data,
    refetchInterval: 60_000,
  })

  const cpuMem = toChartRows(metrics, { cpu_total_pct: 'cpu', memory_used_pct: 'mem' })
  const net = toChartRows(metrics, { network_rx_bps: 'rx', network_tx_bps: 'tx' })
  const topProcs = [...processes].sort((a, b) => (b.cpu_pct || 0) - (a.cpu_pct || 0)).slice(0, 8)
  const tick = timeAxisTickFormatter(6)

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader className="pb-0">
          <CardTitle className="text-sm">CPU & Memory — last 6h</CardTitle>
        </CardHeader>
        <CardContent className="h-56 pt-2">
          {cpuMem.length === 0 ? <NoData /> : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={cpuMem} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tick} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <YAxis domain={[0, 100]} width={32} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [`${Number(v).toFixed(1)}%`, n === 'cpu' ? 'CPU' : 'Memory']} />
                <Line type="monotone" dataKey="cpu" stroke="rgb(var(--info))" strokeWidth={1.8} dot={false} connectNulls />
                <Line type="monotone" dataKey="mem" stroke="rgb(var(--primary))" strokeWidth={1.8} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-0">
          <CardTitle className="text-sm">Network throughput — last 6h</CardTitle>
        </CardHeader>
        <CardContent className="h-56 pt-2">
          {net.length === 0 ? <NoData /> : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={net} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="srvRx" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="rgb(var(--primary))" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="rgb(var(--primary))" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="srvTx" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tick} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <YAxis width={48} tickFormatter={(v) => formatBps(Number(v) * 8)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [formatBps(Number(v) * 8), n === 'rx' ? 'Receive' : 'Transmit']} />
                <Area type="monotone" dataKey="rx" stroke="rgb(var(--primary))" strokeWidth={1.6} fill="url(#srvRx)" dot={false} />
                <Area type="monotone" dataKey="tx" stroke="rgb(var(--success))" strokeWidth={1.6} fill="url(#srvTx)" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-0">
          <CardTitle className="text-sm">Filesystems</CardTitle>
        </CardHeader>
        <CardContent className="pt-3">
          {filesystems.length === 0 ? <NoData /> : (
            <div className="space-y-2.5">
              {filesystems.map((fs) => (
                <div key={fs.mount} className="flex items-center gap-3">
                  <span className="w-14 shrink-0 font-mono text-xs">{fs.mount}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface2">
                    <div
                      className={cn(
                        'h-full rounded-full',
                        (fs.used_pct || 0) >= 95 ? 'bg-danger' : (fs.used_pct || 0) >= 85 ? 'bg-warning' : 'bg-primary',
                      )}
                      style={{ width: `${Math.min(100, fs.used_pct || 0)}%` }}
                    />
                  </div>
                  <span className="w-28 shrink-0 text-right text-xs tabular-nums text-muted">
                    {formatBytes(fs.used_bytes || 0)} / {formatBytes(fs.total_bytes || 0)}
                  </span>
                  <span className={cn('w-12 shrink-0 text-right text-xs font-medium tabular-nums', (fs.used_pct || 0) >= 95 ? 'text-danger' : (fs.used_pct || 0) >= 85 ? 'text-warning' : '')}>
                    {(fs.used_pct || 0).toFixed(1)}%
                  </span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-0">
          <CardTitle className="text-sm">Top processes by CPU</CardTitle>
        </CardHeader>
        <CardContent className="pt-2">
          {topProcs.length === 0 ? <NoData /> : (
            <Table>
              <THead>
                <Tr>
                  <Th className="h-8">Process</Th>
                  <Th className="h-8 text-right">CPU</Th>
                  <Th className="h-8 text-right">Memory</Th>
                  <Th className="h-8">User</Th>
                </Tr>
              </THead>
              <TBody>
                {topProcs.map((p) => (
                  <Tr key={`${p.pid}-${p.name}`}>
                    <Td className="py-1.5 text-xs font-medium">{p.name} <span className="text-muted">({p.pid})</span></Td>
                    <Td className="py-1.5 text-right text-xs tabular-nums">{(p.cpu_pct || 0).toFixed(1)}%</Td>
                    <Td className="py-1.5 text-right text-xs tabular-nums">{formatBytes(p.memory_bytes || 0)}</Td>
                    <Td className="py-1.5 text-xs text-muted">{p.user_name || '—'}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
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
    refetchInterval: 60_000,
  })

  const tick = timeAxisTickFormatter(range.hours)
  const cpu = toChartRows(metrics, { cpu_total_pct: 'v' })
  const mem = toChartRows(metrics, { memory_used_pct: 'v' })
  const net = toChartRows(metrics, { network_rx_bps: 'rx', network_tx_bps: 'tx' })
  const disk = toChartRows(metrics, { disk_read_bps: 'read', disk_write_bps: 'write' })

  const chart = (title: string, rows: ReturnType<typeof toChartRows>, render: () => React.ReactNode) => (
    <Card>
      <CardHeader className="pb-0"><CardTitle className="text-sm">{title}</CardTitle></CardHeader>
      <CardContent className="h-52 pt-2">
        {isLoading ? <Skeleton className="h-full w-full" /> : rows.length === 0 ? <NoData /> : (
          <ResponsiveContainer width="100%" height="100%">{render() as any}</ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )

  const axis = (
    <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tick} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end">
        <TimeRangePicker
          rangeIdx={rangeIdx} isCustom={isCustom}
          customFrom={range.fromISO} customTo={range.toISO}
          onPreset={setPreset} onCustom={setCustom}
        />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        {chart('CPU utilization', cpu, () => (
          <LineChart data={cpu} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
            {axis}
            <YAxis domain={[0, 100]} width={32} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <Tooltip {...ttStyle()} formatter={(v: number) => [`${Number(v).toFixed(1)}%`, 'CPU']} />
            <Line type="monotone" dataKey="v" stroke="rgb(var(--info))" strokeWidth={1.8} dot={false} connectNulls />
          </LineChart>
        ))}
        {chart('Memory utilization', mem, () => (
          <LineChart data={mem} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
            {axis}
            <YAxis domain={[0, 100]} width={32} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <Tooltip {...ttStyle()} formatter={(v: number) => [`${Number(v).toFixed(1)}%`, 'Memory']} />
            <Line type="monotone" dataKey="v" stroke="rgb(var(--primary))" strokeWidth={1.8} dot={false} connectNulls />
          </LineChart>
        ))}
        {chart('Network throughput', net, () => (
          <LineChart data={net} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
            {axis}
            <YAxis width={48} tickFormatter={(v) => formatBps(Number(v) * 8)} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [formatBps(Number(v) * 8), n === 'rx' ? 'RX' : 'TX']} />
            <Line type="monotone" dataKey="rx" stroke="rgb(var(--primary))" strokeWidth={1.6} dot={false} connectNulls />
            <Line type="monotone" dataKey="tx" stroke="rgb(var(--success))" strokeWidth={1.6} dot={false} connectNulls />
          </LineChart>
        ))}
        {chart('Disk I/O', disk, () => (
          <LineChart data={disk} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
            {axis}
            <YAxis width={52} tickFormatter={(v) => `${formatBytes(Number(v))}/s`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
            <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [`${formatBytes(Number(v))}/s`, n === 'read' ? 'Read' : 'Write']} />
            <Line type="monotone" dataKey="read" stroke="rgb(var(--info))" strokeWidth={1.6} dot={false} connectNulls />
            <Line type="monotone" dataKey="write" stroke="rgb(var(--warning))" strokeWidth={1.6} dot={false} connectNulls />
          </LineChart>
        ))}
      </div>
    </div>
  )
}

/* ── Processes ───────────────────────────────────────────────────── */

function ProcessesTab({ items }: { items: ServerProcess[] }) {
  const [q, setQ] = useState('')
  const [sortBy, setSortBy] = useState<'cpu' | 'mem' | 'name'>('cpu')
  const filtered = useMemo(() => {
    let out = items
    if (q) {
      const needle = q.toLowerCase()
      out = out.filter((p) =>
        p.name.toLowerCase().includes(needle) ||
        (p.user_name || '').toLowerCase().includes(needle) ||
        String(p.pid).includes(needle))
    }
    return [...out].sort((a, b) =>
      sortBy === 'cpu' ? (b.cpu_pct || 0) - (a.cpu_pct || 0)
      : sortBy === 'mem' ? (b.memory_bytes || 0) - (a.memory_bytes || 0)
      : a.name.localeCompare(b.name))
  }, [items, q, sortBy])

  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="h-8 pl-8" placeholder="Filter by name, user, PID…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <Select value={sortBy} onValueChange={(v) => setSortBy(v as 'cpu' | 'mem' | 'name')}>
            <SelectTrigger className="h-8 w-[150px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="cpu">Sort by CPU</SelectItem>
              <SelectItem value="mem">Sort by memory</SelectItem>
              <SelectItem value="name">Sort by name</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-xs text-muted">{filtered.length} processes (top-N snapshot from agent)</span>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Process</Th><Th className="text-right">PID</Th><Th className="text-right">CPU</Th>
                <Th className="text-right">Memory</Th><Th>User</Th><Th>Updated</Th>
              </Tr>
            </THead>
            <TBody>
              {filtered.length === 0 && (
                <Tr><Td colSpan={6}><div className="py-8 text-center text-xs text-muted">No processes reported</div></Td></Tr>
              )}
              {filtered.map((p) => (
                <Tr key={`${p.pid}-${p.name}`}>
                  <Td className="text-sm font-medium" title={p.cmdline || undefined}>{p.name}</Td>
                  <Td className="text-right text-xs tabular-nums text-muted">{p.pid}</Td>
                  <Td className="text-right text-xs tabular-nums">{(p.cpu_pct || 0).toFixed(1)}%</Td>
                  <Td className="text-right text-xs tabular-nums">{formatBytes(p.memory_bytes || 0)}</Td>
                  <Td className="text-xs text-muted">{p.user_name || '—'}</Td>
                  <Td className="text-xs text-muted">{relativeTime(p.updated_at)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}

/* ── Services ────────────────────────────────────────────────────── */

function ServicesTab({ items }: { items: ServerService[] }) {
  const [filter, setFilter] = useState('all')
  const filtered = items.filter((s) => {
    const st = (s.state || '').toLowerCase()
    if (filter === 'running') return st === 'running'
    if (filter === 'stopped') return st !== 'running' && st !== 'not_found'
    if (filter === 'not_found') return st === 'not_found'
    return true
  })

  const stateBadge = (state: string | null) => {
    const st = (state || '').toLowerCase()
    if (st === 'running') return <Badge variant="success">Running</Badge>
    if (st === 'not_found') return <Badge variant="outline">Not installed</Badge>
    if (!st) return <Badge variant="outline">Unknown</Badge>
    return <Badge variant="danger">{state}</Badge>
  }

  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-3 flex items-center gap-2">
          <Select value={filter} onValueChange={setFilter}>
            <SelectTrigger className="h-8 w-[160px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All states</SelectItem>
              <SelectItem value="running">Running</SelectItem>
              <SelectItem value="stopped">Stopped / failed</SelectItem>
              <SelectItem value="not_found">Not installed</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-xs text-muted">
            {items.length} watched services — the watchlist comes from the agent policy
          </span>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr><Th>Service</Th><Th>State</Th><Th>Start mode</Th><Th className="text-right">PID</Th><Th>Updated</Th></Tr>
            </THead>
            <TBody>
              {filtered.length === 0 && (
                <Tr><Td colSpan={5}><div className="py-8 text-center text-xs text-muted">No services match</div></Td></Tr>
              )}
              {filtered.map((s) => (
                <Tr key={s.service_name}>
                  <Td>
                    <div className="text-sm font-medium">{s.display_name || s.service_name}</div>
                    <div className="text-[11px] text-muted">{s.service_name}</div>
                  </Td>
                  <Td>{stateBadge(s.state)}</Td>
                  <Td className="text-xs">{s.start_mode || '—'}</Td>
                  <Td className="text-right text-xs tabular-nums text-muted">{s.pid || '—'}</Td>
                  <Td className="text-xs text-muted">{relativeTime(s.updated_at)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}

/* ── Storage ─────────────────────────────────────────────────────── */

function StorageTab({ serverId, items }: { serverId: string; items: ServerFilesystem[] }) {
  const to = useMemo(() => new Date(), [])
  const from = useMemo(() => new Date(Date.now() - 24 * 3_600_000), [])
  const { data: metrics } = useQuery<ServerMetricsResponse>({
    queryKey: ['servers', serverId, 'metrics', 'diskio'],
    queryFn: async () =>
      (await api.get(`/servers/${serverId}/metrics`, {
        params: { metrics: 'disk_read_bps,disk_write_bps', from: from.toISOString(), to: to.toISOString() },
      })).data,
    refetchInterval: 60_000,
  })
  const disk = toChartRows(metrics, { disk_read_bps: 'read', disk_write_bps: 'write' })
  const tick = timeAxisTickFormatter(24)

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Mount</Th><Th>Type</Th><Th>Device</Th>
                  <Th>Usage</Th><Th className="text-right">Used</Th><Th className="text-right">Free</Th><Th className="text-right">Total</Th>
                </Tr>
              </THead>
              <TBody>
                {items.length === 0 && (
                  <Tr><Td colSpan={7}><div className="py-8 text-center text-xs text-muted">No filesystems reported</div></Td></Tr>
                )}
                {items.map((fs) => (
                  <Tr key={fs.mount}>
                    <Td className="font-mono text-sm">{fs.mount}</Td>
                    <Td className="text-xs">{fs.fs_type || '—'}</Td>
                    <Td className="text-xs text-muted">{fs.device || '—'}</Td>
                    <Td><UsageBar pct={fs.used_pct ?? null} /></Td>
                    <Td className="text-right text-xs tabular-nums">{formatBytes(fs.used_bytes || 0)}</Td>
                    <Td className="text-right text-xs tabular-nums">{formatBytes(fs.free_bytes || 0)}</Td>
                    <Td className="text-right text-xs tabular-nums">{formatBytes(fs.total_bytes || 0)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-0"><CardTitle className="text-sm">Disk I/O — last 24h</CardTitle></CardHeader>
        <CardContent className="h-52 pt-2">
          {disk.length === 0 ? <NoData /> : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={disk} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border)/0.25)" vertical={false} />
                <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']} tickFormatter={tick} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <YAxis width={52} tickFormatter={(v) => `${formatBytes(Number(v))}/s`} tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
                <Tooltip {...ttStyle()} formatter={(v: number, n: string) => [`${formatBytes(Number(v))}/s`, n === 'read' ? 'Read' : 'Write']} />
                <Line type="monotone" dataKey="read" stroke="rgb(var(--info))" strokeWidth={1.6} dot={false} connectNulls />
                <Line type="monotone" dataKey="write" stroke="rgb(var(--warning))" strokeWidth={1.6} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>
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

  return (
    <Card>
      <CardContent className="pt-4">
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Interface</Th><Th>State</Th><Th>IP addresses</Th>
                <Th>MAC</Th><Th className="text-right">Speed</Th><Th className="text-right">MTU</Th><Th>Updated</Th>
              </Tr>
            </THead>
            <TBody>
              {items.length === 0 && (
                <Tr><Td colSpan={7}><div className="py-8 text-center text-xs text-muted">No interfaces reported</div></Td></Tr>
              )}
              {items.map((nic) => (
                <Tr key={nic.if_name}>
                  <Td className="text-sm font-medium">{nic.if_name}</Td>
                  <Td>{nic.is_up ? <Badge variant="success">Up</Badge> : <Badge variant="danger">Down</Badge>}</Td>
                  <Td className="font-mono text-xs">{(nic.ip_addresses || []).join(', ') || '—'}</Td>
                  <Td className="font-mono text-xs text-muted">{nic.mac_address || '—'}</Td>
                  <Td className="text-right text-xs tabular-nums">{nic.speed_mbps ? `${nic.speed_mbps} Mbps` : '—'}</Td>
                  <Td className="text-right text-xs tabular-nums">{nic.mtu || '—'}</Td>
                  <Td className="text-xs text-muted">{relativeTime(nic.updated_at)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
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
    <Card>
      <CardContent className="pt-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="h-8 pl-8" placeholder="Filter packages or vendors…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <span className="text-xs text-muted">{filtered.length} of {items.length} installed packages</span>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr><Th>Package</Th><Th>Version</Th><Th>Vendor</Th><Th>Installed</Th><Th>Last reported</Th></Tr>
            </THead>
            <TBody>
              {filtered.length === 0 && (
                <Tr><Td colSpan={5}><div className="py-8 text-center text-xs text-muted">No software inventory yet — the agent uploads it with its inventory snapshot</div></Td></Tr>
              )}
              {filtered.map((s) => (
                <Tr key={s.package_name}>
                  <Td className="text-sm font-medium">{s.package_name}</Td>
                  <Td className="text-xs tabular-nums">{s.version || '—'}</Td>
                  <Td className="text-xs text-muted">{s.vendor || '—'}</Td>
                  <Td className="text-xs text-muted">{s.install_date ? new Date(s.install_date).toLocaleDateString() : '—'}</Td>
                  <Td className="text-xs text-muted">{relativeTime(s.updated_at)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
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
    <Card>
      <CardContent className="pt-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {summary && summary.total > 0 ? (
              <>
                <Badge variant="success">{summary.compliant} compliant</Badge>
                {summary.missing > 0 && <Badge variant="danger">{summary.missing} missing</Badge>}
                {summary.outdated > 0 && <Badge variant="warning">{summary.outdated} outdated</Badge>}
                {summary.prohibited > 0 && <Badge variant="danger">{summary.prohibited} prohibited</Badge>}
              </>
            ) : (
              <span className="text-muted">No baselines apply to this server yet</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Link to="/server-baselines" className="text-xs font-medium text-primary hover:underline">
              Manage baselines
            </Link>
            <Button variant="outline" size="sm" onClick={() => evaluate.mutate()} disabled={evaluate.isPending}>
              <RefreshCw className={cn('h-3.5 w-3.5', evaluate.isPending && 'animate-spin')} /> Evaluate now
            </Button>
          </div>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Status</Th><Th>Baseline</Th><Th>Rule</Th>
                <Th>Found</Th><Th>Expected</Th><Th>Severity</Th><Th>Failing since</Th>
              </Tr>
            </THead>
            <TBody>
              {items.length === 0 && (
                <Tr>
                  <Td colSpan={7}>
                    <div className="flex flex-col items-center gap-2 py-8 text-center">
                      <ClipboardCheck className="h-7 w-7 text-muted/50" />
                      <div className="text-xs text-muted">
                        Define a software baseline matching this server's OS or tags — required apps,
                        minimum versions, prohibited software — and violations will alert automatically.
                      </div>
                    </div>
                  </Td>
                </Tr>
              )}
              {items.map((r) => (
                <Tr key={r.rule_id}>
                  <Td>{statusBadge(r.status)}</Td>
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
                  <Td className="text-xs text-muted">{r.first_failed_at ? relativeTime(r.first_failed_at) : '—'}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
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

  return (
    <Card>
      <CardContent className="pt-4">
        <div className="mb-3 flex items-center gap-2">
          <Select value={level} onValueChange={setLevel}>
            <SelectTrigger className="h-8 w-[150px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All levels</SelectItem>
              <SelectItem value="critical">Critical</SelectItem>
              <SelectItem value="error">Error</SelectItem>
              <SelectItem value="warning">Warning</SelectItem>
            </SelectContent>
          </Select>
          <span className="text-xs text-muted">
            Event-log counts per collection interval, last 24h (sampled summaries, not full event text)
          </span>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <Table>
            <THead className="bg-surface2/50">
              <Tr><Th>Time</Th><Th>Log</Th><Th>Level</Th><Th className="text-right">Events</Th></Tr>
            </THead>
            <TBody>
              {items.length === 0 && (
                <Tr><Td colSpan={4}><div className="py-8 text-center text-xs text-muted">No event summaries in the last 24h</div></Td></Tr>
              )}
              {items.map((e, i) => (
                <Tr key={`${e.timestamp}-${e.log_name}-${e.level}-${i}`}>
                  <Td className="text-xs tabular-nums">{new Date(e.timestamp).toLocaleString()}</Td>
                  <Td className="text-xs">{e.log_name}</Td>
                  <Td>{levelBadge(e.level)}</Td>
                  <Td className="text-right text-xs tabular-nums">{e.count}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}

/* ── Agent ───────────────────────────────────────────────────────── */

function AgentTab({ serverId }: { serverId: string }) {
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
      <Card>
        <CardContent className="flex flex-col items-center gap-2 py-10 text-center">
          <Bot className="h-8 w-8 text-muted/50" />
          <div className="text-sm font-medium">No agent enrolled on this server</div>
          <div className="max-w-md text-xs text-muted">
            Use “Deploy agent” above to generate an enrollment token, or switch the server to an
            agentless collection mode in Settings.
          </div>
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

      <Card>
        <CardHeader className="flex-row items-center justify-between pb-3">
          <CardTitle className="text-sm">Agent</CardTitle>
          <div className="flex items-center gap-2">
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
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 md:grid-cols-3 xl:grid-cols-4">
            {info.map(([label, value]) => (
              <div key={label}>
                <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
                <div className="mt-0.5 text-sm">{value}</div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-0"><CardTitle className="text-sm">Command history</CardTitle></CardHeader>
        <CardContent className="pt-3">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr><Th>Command</Th><Th>Status</Th><Th>Requested by</Th><Th>Created</Th><Th>Completed</Th></Tr>
              </THead>
              <TBody>
                {(commands?.items || []).length === 0 && (
                  <Tr><Td colSpan={5}><div className="py-6 text-center text-xs text-muted">No commands sent yet — actions above queue commands the agent picks up on its next poll (≤30s)</div></Td></Tr>
                )}
                {(commands?.items || []).map((c) => (
                  <Tr key={c.id}>
                    <Td className="font-mono text-xs">{c.command}</Td>
                    <Td>{cmdStatus(c)}</Td>
                    <Td className="text-xs text-muted">{c.requested_by_name || 'system'}</Td>
                    <Td className="text-xs text-muted">{relativeTime(c.created_at)}</Td>
                    <Td className="text-xs text-muted">{c.completed_at ? relativeTime(c.completed_at) : '—'}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>
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
      <Card>
        <CardHeader className="flex-row items-center justify-between pb-3">
          <CardTitle className="text-sm">Server record</CardTitle>
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2.5 md:grid-cols-3">
            {rows.map(([label, value]) => (
              <div key={label}>
                <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
                <div className="mt-0.5 break-words text-sm">{value}</div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card className="border-danger/30">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm text-danger">
            <AlertTriangle className="h-4 w-4" /> Danger zone
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
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
