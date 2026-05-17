/**
 * ServersPage — fleet overview of monitored servers.
 *
 * Top: KPI strip (total / healthy / warning / critical / agents online).
 * Middle: top-pressure callouts (CPU, memory, disk).
 * Bottom: filterable, sortable, paginated server table.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Cpu,
  HardDrive,
  HelpCircle,
  Layers,
  MemoryStick,
  Network as NetworkIcon,
  Plus,
  RefreshCw,
  Search,
  Server as ServerIcon,
  Slash,
  Wifi,
} from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { SkeletonTable } from '@/components/ui/Skeleton'
import type {
  Server,
  ServerListResponse,
  ServerOverview,
  ServerStatus,
} from '@/types/server'

const STATUS_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'outline' | 'default'> = {
  healthy: 'success',
  warning: 'warning',
  critical: 'danger',
  stale: 'warning',
  unknown: 'outline',
  disabled: 'outline',
}

const STATUS_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  healthy: CheckCircle2,
  warning: AlertTriangle,
  critical: AlertCircle,
  stale: AlertTriangle,
  unknown: HelpCircle,
  disabled: Slash,
}

function StatusBadge({ status }: { status: string }) {
  const Icon = STATUS_ICON[status] || HelpCircle
  return (
    <Badge variant={STATUS_VARIANT[status] || 'outline'} className="capitalize">
      <Icon className="h-3 w-3" />
      {status}
    </Badge>
  )
}

function OsBadge({ os }: { os: string }) {
  const label =
    os === 'windows' ? 'Windows' : os === 'linux' ? 'Linux' : os === 'macos' ? 'macOS' : os
  return (
    <span className="rounded bg-surface2 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted">
      {label}
    </span>
  )
}

function fmtPct(v: number | null | undefined) {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v.toFixed(1)}%`
}

function fmtBps(v: number | null | undefined) {
  if (v == null || Number.isNaN(v) || v === 0) return '—'
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s']
  let i = 0
  let n = v
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(1)} ${units[i]}`
}

function KpiTile({
  label,
  value,
  icon: Icon,
  tone = 'default',
}: {
  label: string
  value: string | number
  icon: React.ComponentType<{ className?: string }>
  tone?: 'default' | 'success' | 'warning' | 'danger' | 'info'
}) {
  const ring =
    tone === 'success'
      ? 'bg-success/10 text-success'
      : tone === 'warning'
      ? 'bg-warning/10 text-warning'
      : tone === 'danger'
      ? 'bg-danger/10 text-danger'
      : tone === 'info'
      ? 'bg-info/10 text-info'
      : 'bg-primary/10 text-primary'
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex h-10 w-10 items-center justify-center rounded-lg ${ring}`}>
          <Icon className="h-5 w-5" />
        </div>
        <div>
          <div className="text-2xl font-semibold leading-none">{value}</div>
          <div className="mt-1 text-xs text-muted">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function TopPressureCard({
  title,
  icon: Icon,
  items,
  unit,
}: {
  title: string
  icon: React.ComponentType<{ className?: string }>
  items: ServerOverview['top_cpu']
  unit: 'pct' | 'bps'
}) {
  const navigate = useNavigate()
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center gap-2">
          <Icon className="h-4 w-4 text-muted" />
          <h3 className="text-sm font-semibold">{title}</h3>
        </div>
        {items.length === 0 ? (
          <div className="py-3 text-xs text-muted">No data in the last 10 minutes.</div>
        ) : (
          <ul className="space-y-1.5">
            {items.slice(0, 5).map((it) => (
              <li key={it.server_id}>
                <button
                  type="button"
                  onClick={() => navigate(`/servers/${it.server_id}`)}
                  className="flex w-full items-center justify-between gap-3 rounded px-2 py-1 text-sm hover:bg-surface2"
                >
                  <span className="truncate font-medium">
                    {it.display_name || it.hostname || it.server_id.slice(0, 8)}
                  </span>
                  <span className="font-mono text-xs tabular-nums text-muted">
                    {unit === 'pct' ? fmtPct(it.value) : fmtBps(it.value)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

export default function ServersPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') || ''
  const status = params.get('status') || ''
  const os = params.get('os') || ''
  const mode = params.get('mode') || ''
  const page = Number(params.get('page') || '1')
  const sort = params.get('sort') || 'display_name'
  const order = (params.get('order') as 'asc' | 'desc') || 'asc'

  const updateParam = (k: string, v: string) => {
    const next = new URLSearchParams(params)
    if (v) next.set(k, v)
    else next.delete(k)
    if (k !== 'page') next.delete('page')
    setParams(next, { replace: true })
  }

  const overviewQ = useQuery<ServerOverview>({
    queryKey: ['servers', 'overview'],
    queryFn: async () => (await api.get('/server-monitoring/overview')).data,
    refetchInterval: 15_000,
  })

  const serversQ = useQuery<ServerListResponse>({
    queryKey: ['servers', 'list', { q, status, os, mode, page, sort, order }],
    queryFn: async () => {
      const qp: Record<string, string> = { page: String(page), page_size: '50', sort, order }
      if (q) qp.q = q
      if (status) qp.status = status
      if (os) qp.os_type = os
      if (mode) qp.collection_mode = mode
      const search = new URLSearchParams(qp).toString()
      return (await api.get(`/servers?${search}`)).data
    },
    refetchInterval: 30_000,
  })

  const total = overviewQ.data?.total ?? 0
  const statusCounts = overviewQ.data?.status_counts ?? {}
  const agentCounts = overviewQ.data?.agent_counts ?? {}
  const healthy = statusCounts.healthy ?? 0
  const warning = (statusCounts.warning ?? 0) + (statusCounts.stale ?? 0)
  const critical = statusCounts.critical ?? 0
  const onlineAgents = agentCounts.online ?? 0

  const items = serversQ.data?.items ?? []
  const totalRows = serversQ.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalRows / 50))

  const isEmpty = !serversQ.isLoading && items.length === 0

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Servers</h1>
          <p className="mt-0.5 text-xs text-muted">
            Monitored hosts via local agents or agentless probes (WMI / WinRM / SNMP / SSH).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              overviewQ.refetch()
              serversQ.refetch()
            }}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </Button>
          <Button size="sm" onClick={() => navigate('/servers/new')}>
            <Plus className="h-3.5 w-3.5" />
            Add server
          </Button>
        </div>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <KpiTile label="Total servers" value={total} icon={ServerIcon} tone="info" />
        <KpiTile label="Healthy" value={healthy} icon={CheckCircle2} tone="success" />
        <KpiTile label="Warning / stale" value={warning} icon={AlertTriangle} tone="warning" />
        <KpiTile label="Critical" value={critical} icon={AlertCircle} tone="danger" />
        <KpiTile label="Agents online" value={onlineAgents} icon={Wifi} tone="info" />
        <KpiTile
          label="Sites covered"
          value={overviewQ.data?.sites.length ?? 0}
          icon={Layers}
          tone="default"
        />
      </div>

      {/* Top-pressure callouts */}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <TopPressureCard
          title="Top CPU pressure"
          icon={Cpu}
          items={overviewQ.data?.top_cpu ?? []}
          unit="pct"
        />
        <TopPressureCard
          title="Top memory pressure"
          icon={MemoryStick}
          items={overviewQ.data?.top_memory ?? []}
          unit="pct"
        />
        <TopPressureCard
          title="Top disk usage"
          icon={HardDrive}
          items={overviewQ.data?.top_disk ?? []}
          unit="pct"
        />
        <TopPressureCard
          title="Top network throughput"
          icon={NetworkIcon}
          items={overviewQ.data?.top_network ?? []}
          unit="bps"
        />
      </div>

      {/* Filters + table */}
      <Card>
        <CardContent className="p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[220px]">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input
                placeholder="Search by hostname, IP, owner…"
                value={q}
                onChange={(e) => updateParam('q', e.target.value)}
                className="pl-8"
              />
            </div>
            <Select value={status || '__all'} onValueChange={(v) => updateParam('status', v === '__all' ? '' : v)}>
              <SelectTrigger className="w-[150px]">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">Any status</SelectItem>
                <SelectItem value="healthy">Healthy</SelectItem>
                <SelectItem value="warning">Warning</SelectItem>
                <SelectItem value="critical">Critical</SelectItem>
                <SelectItem value="stale">Stale</SelectItem>
                <SelectItem value="unknown">Unknown</SelectItem>
                <SelectItem value="disabled">Disabled</SelectItem>
              </SelectContent>
            </Select>
            <Select value={os || '__all'} onValueChange={(v) => updateParam('os', v === '__all' ? '' : v)}>
              <SelectTrigger className="w-[140px]">
                <SelectValue placeholder="OS" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">Any OS</SelectItem>
                <SelectItem value="windows">Windows</SelectItem>
                <SelectItem value="linux">Linux</SelectItem>
                <SelectItem value="macos">macOS</SelectItem>
                <SelectItem value="other">Other</SelectItem>
                <SelectItem value="unknown">Unknown</SelectItem>
              </SelectContent>
            </Select>
            <Select value={mode || '__all'} onValueChange={(v) => updateParam('mode', v === '__all' ? '' : v)}>
              <SelectTrigger className="w-[180px]">
                <SelectValue placeholder="Collection" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all">Any collection</SelectItem>
                <SelectItem value="agent">Agent</SelectItem>
                <SelectItem value="agentless_wmi">Agentless WMI</SelectItem>
                <SelectItem value="agentless_winrm">Agentless WinRM</SelectItem>
                <SelectItem value="snmp">SNMP</SelectItem>
                <SelectItem value="ssh">SSH</SelectItem>
              </SelectContent>
            </Select>
            <div className="ml-auto text-xs text-muted">
              {totalRows} server{totalRows === 1 ? '' : 's'}
            </div>
          </div>

          {serversQ.isLoading ? (
            <SkeletonTable rows={6} cols={7} />
          ) : isEmpty ? (
            <EmptyServerState onAdd={() => navigate('/servers/new')} />
          ) : (
            <>
              <Table>
                <THead>
                  <Tr>
                    <Th onClick={() => updateParam('sort', 'display_name')}>Server</Th>
                    <Th>OS</Th>
                    <Th>Site</Th>
                    <Th onClick={() => updateParam('sort', 'status')}>Status</Th>
                    <Th>Agent</Th>
                    <Th>Mode</Th>
                    <Th onClick={() => updateParam('sort', 'last_seen')}>Last seen</Th>
                  </Tr>
                </THead>
                <TBody>
                  {items.map((s) => (
                    <Tr
                      key={s.id}
                      onClick={() => navigate(`/servers/${s.id}`)}
                      className="cursor-pointer hover:bg-surface2/60"
                    >
                      <Td>
                        <div className="flex flex-col gap-0.5">
                          <span className="font-medium">{s.display_name}</span>
                          {s.hostname && s.hostname !== s.display_name ? (
                            <span className="text-[11px] text-muted">{s.hostname}</span>
                          ) : null}
                          {s.primary_ip ? (
                            <span className="font-mono text-[10px] text-muted">{s.primary_ip}</span>
                          ) : null}
                        </div>
                      </Td>
                      <Td>
                        <div className="flex flex-col gap-0.5">
                          <OsBadge os={s.os_type} />
                          {s.os_version ? (
                            <span className="text-[10px] text-muted">{s.os_version}</span>
                          ) : null}
                        </div>
                      </Td>
                      <Td className="text-sm text-muted">{s.site_name || '—'}</Td>
                      <Td>
                        <StatusBadge status={s.status} />
                      </Td>
                      <Td>
                        {s.agent_id ? (
                          <Badge variant={s.agent_status === 'online' ? 'success' : 'outline'}>
                            <Activity className="h-3 w-3" />
                            {s.agent_status || 'unknown'}
                            {s.agent_version ? ` · v${s.agent_version}` : ''}
                          </Badge>
                        ) : (
                          <span className="text-xs text-muted">No agent</span>
                        )}
                      </Td>
                      <Td className="text-sm text-muted capitalize">
                        {s.collection_mode.replace('_', ' ')}
                      </Td>
                      <Td className="text-sm text-muted">
                        {s.last_seen ? relativeTime(s.last_seen) : '—'}
                      </Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>

              {/* Pagination */}
              {totalPages > 1 ? (
                <div className="mt-4 flex items-center justify-between text-xs text-muted">
                  <div>
                    Page {page} of {totalPages}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page <= 1}
                      onClick={() => updateParam('page', String(page - 1))}
                    >
                      <ChevronLeft className="h-3 w-3" />
                      Prev
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page >= totalPages}
                      onClick={() => updateParam('page', String(page + 1))}
                    >
                      Next
                      <ChevronRight className="h-3 w-3" />
                    </Button>
                  </div>
                </div>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function EmptyServerState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <ServerIcon className="h-6 w-6" />
      </div>
      <h3 className="text-sm font-semibold">No monitored servers yet</h3>
      <p className="mt-1 max-w-md text-xs text-muted">
        Install the Windows agent on a host or configure agentless WMI/WinRM through a remote
        sensor to start collecting CPU, memory, disk, network, and Windows service telemetry.
      </p>
      <Button size="sm" className="mt-4" onClick={onAdd}>
        <Plus className="h-3.5 w-3.5" />
        Add your first server
      </Button>
    </div>
  )
}
