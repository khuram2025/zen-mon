/**
 * ServerDetail — per-server view.
 *
 * Tabs: Overview · Metrics · Filesystems · Network · Processes · Services · Events · Inventory · Agent.
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import ReactECharts from 'echarts-for-react'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Bug,
  CheckCircle2,
  Clock,
  Cpu,
  HardDrive,
  HelpCircle,
  Info,
  KeySquare,
  Layers,
  MemoryStick,
  Network as NetworkIcon,
  Power,
  RefreshCw,
  RotateCw,
  Server as ServerIcon,
  Settings as SettingsIcon,
  Shield,
  Slash,
  TerminalSquare,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Skeleton, SkeletonTable } from '@/components/ui/Skeleton'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import type {
  Agent,
  Server,
  ServerEventLog,
  ServerFilesystem,
  ServerMetricsResponse,
  ServerNetworkInterface,
  ServerProcess,
  ServerService,
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

const RANGES: Array<{ id: string; label: string; from: () => Date }> = [
  { id: '1h', label: '1h', from: () => new Date(Date.now() - 3600_000) },
  { id: '6h', label: '6h', from: () => new Date(Date.now() - 6 * 3600_000) },
  { id: '24h', label: '24h', from: () => new Date(Date.now() - 24 * 3600_000) },
  { id: '7d', label: '7d', from: () => new Date(Date.now() - 7 * 24 * 3600_000) },
]

function fmtBytes(b: number | null | undefined) {
  if (b == null) return '—'
  if (b === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0
  let n = b
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(n >= 100 ? 0 : 1)} ${units[i]}`
}

function fmtBps(v: number | null | undefined) {
  if (v == null || v === 0) return '—'
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s']
  let i = 0
  let n = v
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(1)} ${units[i]}`
}

function fmtPct(v: number | null | undefined) {
  if (v == null) return '—'
  return `${v.toFixed(1)}%`
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

function MetricChart({
  data,
  unit,
  height = 220,
}: {
  data: { metric: string; points: Array<{ timestamp: string; value: number | null }>; unit: string | null; label: string | null }[]
  unit?: string
  height?: number
}) {
  const option = useMemo(() => {
    return {
      grid: { left: 50, right: 18, top: 18, bottom: 32 },
      tooltip: { trigger: 'axis' as const },
      legend: {
        show: data.length > 1,
        bottom: 0,
        textStyle: { fontSize: 11 },
      },
      xAxis: {
        type: 'time' as const,
        axisLabel: { fontSize: 10 },
      },
      yAxis: {
        type: 'value' as const,
        axisLabel: {
          fontSize: 10,
          formatter: (v: number) => {
            const u = unit || data[0]?.unit
            if (u === 'B' || u === 'B/s' || u === 'bps') {
              if (u === 'bps' || u === 'B/s') return fmtBps(v)
              return fmtBytes(v)
            }
            if (u === '%') return `${v.toFixed(0)}%`
            return String(v.toFixed(0))
          },
        },
      },
      series: data.map((s, i) => ({
        name: s.label || s.metric,
        type: 'line' as const,
        smooth: true,
        showSymbol: false,
        areaStyle: data.length === 1 ? { opacity: 0.15 } : undefined,
        data: s.points.map((p) => [p.timestamp, p.value]),
        lineStyle: { width: 1.5 },
      })),
    }
  }, [data, unit])

  if (!data.length || data.every((s) => s.points.length === 0)) {
    return (
      <div className="flex h-[180px] items-center justify-center text-xs text-muted">
        No data in this range.
      </div>
    )
  }
  return <ReactECharts option={option} style={{ height }} notMerge />
}

export default function ServerDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [tab, setTab] = useState('overview')
  const [range, setRange] = useState('6h')
  const [decommissionOpen, setDecommissionOpen] = useState(false)

  const serverQ = useQuery<Server>({
    queryKey: ['server', id],
    queryFn: async () => (await api.get(`/servers/${id}`)).data,
    refetchInterval: 30_000,
    enabled: !!id,
  })

  const rangeObj = RANGES.find((r) => r.id === range)!
  const from = useMemo(() => rangeObj.from(), [rangeObj])
  const to = useMemo(() => new Date(), [rangeObj])
  const metricsQ = useQuery<ServerMetricsResponse>({
    queryKey: ['server-metrics', id, range],
    queryFn: async () => {
      const qp = new URLSearchParams({
        metrics: 'cpu_total_pct,memory_used_pct,network_rx_bps,network_tx_bps,disk_read_bps,disk_write_bps',
        from: from.toISOString(),
        to: to.toISOString(),
      })
      return (await api.get(`/servers/${id}/metrics?${qp}`)).data
    },
    refetchInterval: 30_000,
    enabled: !!id,
  })

  const filesQ = useQuery<{ items: ServerFilesystem[] }>({
    queryKey: ['server-fs', id],
    queryFn: async () => (await api.get(`/servers/${id}/filesystems`)).data,
    refetchInterval: 30_000,
    enabled: !!id,
  })
  const netQ = useQuery<{ items: ServerNetworkInterface[] }>({
    queryKey: ['server-net', id],
    queryFn: async () => (await api.get(`/servers/${id}/network`)).data,
    refetchInterval: 30_000,
    enabled: !!id,
  })
  const procQ = useQuery<{ items: ServerProcess[] }>({
    queryKey: ['server-proc', id],
    queryFn: async () => (await api.get(`/servers/${id}/processes`)).data,
    refetchInterval: 15_000,
    enabled: !!id,
  })
  const svcQ = useQuery<{ items: ServerService[] }>({
    queryKey: ['server-svc', id],
    queryFn: async () => (await api.get(`/servers/${id}/services`)).data,
    refetchInterval: 60_000,
    enabled: !!id,
  })
  const evQ = useQuery<{ items: ServerEventLog[] }>({
    queryKey: ['server-ev', id],
    queryFn: async () => (await api.get(`/servers/${id}/events`)).data,
    refetchInterval: 60_000,
    enabled: !!id,
  })
  const agentQ = useQuery<Agent | null>({
    queryKey: ['server-agent', id],
    queryFn: async () => (await api.get(`/servers/${id}/agent`)).data,
    refetchInterval: 15_000,
    enabled: !!id,
  })

  const decommissionM = useMutation({
    mutationFn: async () => (await api.post(`/servers/${id}/decommission`)).data,
    onSuccess: () => {
      toast.success('Server decommissioned')
      qc.invalidateQueries({ queryKey: ['server', id] })
      setDecommissionOpen(false)
    },
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  const requestDiagM = useMutation({
    mutationFn: async () =>
      (await api.post(`/agent-fleet/${agentQ.data?.id}/request-diagnostics`)).data,
    onSuccess: () => toast.success('Diagnostics requested'),
    onError: (e) => toast.error(apiErrorMessage(e)),
  })

  if (serverQ.isLoading || !serverQ.data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-7 w-64" />
        <Skeleton className="h-32 w-full" />
        <SkeletonTable rows={5} cols={4} />
      </div>
    )
  }

  const s = serverQ.data
  const series = metricsQ.data?.series ?? []
  const cpuSeries = series.filter((x) => x.metric === 'cpu_total_pct')
  const memSeries = series.filter((x) => x.metric === 'memory_used_pct')
  const netSeries = series.filter(
    (x) => x.metric === 'network_rx_bps' || x.metric === 'network_tx_bps',
  )
  const diskSeries = series.filter(
    (x) => x.metric === 'disk_read_bps' || x.metric === 'disk_write_bps',
  )

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate('/servers')}>
            <ArrowLeft className="h-3.5 w-3.5" />
            Servers
          </Button>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-semibold">{s.display_name}</h1>
              <StatusBadge status={s.status} />
            </div>
            <div className="mt-0.5 text-xs text-muted">
              {s.hostname || '—'} · {s.primary_ip || '—'} · {s.os_name || s.os_type}
              {s.os_version ? ` ${s.os_version}` : ''}
              {s.last_seen ? ` · last seen ${relativeTime(s.last_seen)}` : ''}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              serverQ.refetch()
              metricsQ.refetch()
            }}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={s.status === 'disabled'}
            onClick={() => setDecommissionOpen(true)}
          >
            <Power className="h-3.5 w-3.5" />
            Decommission
          </Button>
        </div>
      </div>

      {/* Tabs */}
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="flex w-full flex-wrap">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="metrics">Metrics</TabsTrigger>
          <TabsTrigger value="filesystems">Filesystems</TabsTrigger>
          <TabsTrigger value="network">Network</TabsTrigger>
          <TabsTrigger value="processes">Processes</TabsTrigger>
          <TabsTrigger value="services">Services</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
          <TabsTrigger value="agent">Agent</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <IdentityCard server={s} />
            <AgentCard agent={agentQ.data ?? null} />
            <ResourceCard
              title="Top filesystems"
              items={filesQ.data?.items ?? []}
              renderItem={(fs) => ({
                label: fs.mount,
                sub: `${fmtBytes(fs.used_bytes)} / ${fmtBytes(fs.total_bytes)}`,
                pct: fs.used_pct ?? 0,
              })}
            />
            <EventLogCard items={evQ.data?.items ?? []} />
          </div>
          <Card>
            <CardContent className="p-4">
              <RangeBar range={range} setRange={setRange} title="Resource usage" />
              <div className="grid gap-4 md:grid-cols-2">
                <ChartBlock title="CPU" icon={Cpu}>
                  <MetricChart data={cpuSeries} unit="%" />
                </ChartBlock>
                <ChartBlock title="Memory" icon={MemoryStick}>
                  <MetricChart data={memSeries} unit="%" />
                </ChartBlock>
                <ChartBlock title="Network throughput" icon={NetworkIcon}>
                  <MetricChart data={netSeries} unit="bps" />
                </ChartBlock>
                <ChartBlock title="Disk IO" icon={HardDrive}>
                  <MetricChart data={diskSeries} unit="bps" />
                </ChartBlock>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="metrics" className="space-y-4">
          <Card>
            <CardContent className="p-4">
              <RangeBar range={range} setRange={setRange} title="Detailed metrics" />
              <div className="grid gap-4 md:grid-cols-2">
                <ChartBlock title="CPU total" icon={Cpu}>
                  <MetricChart data={cpuSeries} unit="%" height={260} />
                </ChartBlock>
                <ChartBlock title="Memory used" icon={MemoryStick}>
                  <MetricChart data={memSeries} unit="%" height={260} />
                </ChartBlock>
                <ChartBlock title="Network throughput" icon={NetworkIcon}>
                  <MetricChart data={netSeries} unit="bps" height={260} />
                </ChartBlock>
                <ChartBlock title="Disk IO" icon={HardDrive}>
                  <MetricChart data={diskSeries} unit="bps" height={260} />
                </ChartBlock>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="filesystems">
          <Card>
            <CardContent className="p-0">
              {filesQ.isLoading ? (
                <SkeletonTable rows={4} cols={5} />
              ) : (filesQ.data?.items ?? []).length === 0 ? (
                <EmptyMsg>Filesystem inventory will appear after the first metric upload.</EmptyMsg>
              ) : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>Mount</Th>
                      <Th>Type</Th>
                      <Th>Used</Th>
                      <Th>Total</Th>
                      <Th>Free</Th>
                      <Th>Usage</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {(filesQ.data!.items).map((fs) => (
                      <Tr key={fs.mount}>
                        <Td className="font-medium">{fs.mount}</Td>
                        <Td className="text-muted">{fs.fs_type || '—'}</Td>
                        <Td>{fmtBytes(fs.used_bytes)}</Td>
                        <Td>{fmtBytes(fs.total_bytes)}</Td>
                        <Td>{fmtBytes(fs.free_bytes)}</Td>
                        <Td>
                          <UsageBar pct={fs.used_pct ?? 0} />
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="network">
          <Card>
            <CardContent className="p-0">
              {netQ.isLoading ? (
                <SkeletonTable rows={4} cols={5} />
              ) : (netQ.data?.items ?? []).length === 0 ? (
                <EmptyMsg>No network interfaces collected yet.</EmptyMsg>
              ) : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>Interface</Th>
                      <Th>MAC</Th>
                      <Th>IPs</Th>
                      <Th>Speed</Th>
                      <Th>State</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {netQ.data!.items.map((nic) => (
                      <Tr key={nic.if_name}>
                        <Td className="font-medium">{nic.if_name}</Td>
                        <Td className="font-mono text-xs">{nic.mac_address || '—'}</Td>
                        <Td className="text-xs">
                          {nic.ip_addresses?.length
                            ? nic.ip_addresses.join(', ')
                            : '—'}
                        </Td>
                        <Td>{nic.speed_mbps ? `${nic.speed_mbps} Mbps` : '—'}</Td>
                        <Td>
                          <Badge variant={nic.is_up ? 'success' : 'outline'}>
                            {nic.is_up ? 'up' : 'down'}
                          </Badge>
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="processes">
          <Card>
            <CardContent className="p-0">
              {procQ.isLoading ? (
                <SkeletonTable rows={5} cols={5} />
              ) : (procQ.data?.items ?? []).length === 0 ? (
                <EmptyMsg>
                  No process snapshots collected. The policy must include a process collector
                  with top_n &gt; 0.
                </EmptyMsg>
              ) : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>PID</Th>
                      <Th>Name</Th>
                      <Th>User</Th>
                      <Th>CPU %</Th>
                      <Th>Memory</Th>
                      <Th>Updated</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {procQ.data!.items.map((p) => (
                      <Tr key={`${p.pid}-${p.name}`}>
                        <Td className="font-mono text-xs">{p.pid}</Td>
                        <Td className="font-medium">{p.name}</Td>
                        <Td className="text-xs text-muted">{p.user_name || '—'}</Td>
                        <Td>{fmtPct(p.cpu_pct)}</Td>
                        <Td>{fmtBytes(p.memory_bytes)}</Td>
                        <Td className="text-xs text-muted">{relativeTime(p.updated_at)}</Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="services">
          <Card>
            <CardContent className="p-0">
              {svcQ.isLoading ? (
                <SkeletonTable rows={5} cols={4} />
              ) : (svcQ.data?.items ?? []).length === 0 ? (
                <EmptyMsg>No service inventory yet.</EmptyMsg>
              ) : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>Service</Th>
                      <Th>Display name</Th>
                      <Th>Start mode</Th>
                      <Th>State</Th>
                      <Th>PID</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {svcQ.data!.items.map((s) => (
                      <Tr key={s.service_name}>
                        <Td className="font-mono text-xs">{s.service_name}</Td>
                        <Td>{s.display_name || '—'}</Td>
                        <Td className="text-xs text-muted">{s.start_mode || '—'}</Td>
                        <Td>
                          <Badge
                            variant={
                              s.state === 'running'
                                ? 'success'
                                : s.state === 'stopped'
                                ? 'danger'
                                : 'outline'
                            }
                          >
                            {s.state || 'unknown'}
                          </Badge>
                        </Td>
                        <Td className="font-mono text-xs">{s.pid || '—'}</Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="events">
          <Card>
            <CardContent className="p-0">
              {evQ.isLoading ? (
                <SkeletonTable rows={5} cols={4} />
              ) : (evQ.data?.items ?? []).length === 0 ? (
                <EmptyMsg>
                  No Windows Event Log summaries. Configure event filters in the agent policy.
                </EmptyMsg>
              ) : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>Time</Th>
                      <Th>Log</Th>
                      <Th>Level</Th>
                      <Th>Count</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {evQ.data!.items.map((e, i) => (
                      <Tr key={i}>
                        <Td className="text-xs text-muted">{new Date(e.timestamp).toLocaleString()}</Td>
                        <Td>{e.log_name}</Td>
                        <Td>
                          <Badge
                            variant={
                              e.level === 'critical' || e.level === 'error'
                                ? 'danger'
                                : e.level === 'warning'
                                ? 'warning'
                                : 'outline'
                            }
                          >
                            {e.level}
                          </Badge>
                        </Td>
                        <Td className="font-mono">{e.count}</Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="agent">
          {agentQ.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : agentQ.data ? (
            <Card>
              <CardContent className="space-y-3 p-5">
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  <InfoRow label="Status" value={<StatusBadge status={agentQ.data.status} />} />
                  <InfoRow label="Version" value={agentQ.data.version || '—'} />
                  <InfoRow label="Platform" value={agentQ.data.platform} />
                  <InfoRow
                    label="Last heartbeat"
                    value={
                      agentQ.data.last_heartbeat_at
                        ? relativeTime(agentQ.data.last_heartbeat_at)
                        : '—'
                    }
                  />
                  <InfoRow
                    label="Last metric"
                    value={agentQ.data.last_metric_at ? relativeTime(agentQ.data.last_metric_at) : '—'}
                  />
                  <InfoRow label="Queue depth" value={String(agentQ.data.queue_depth)} />
                  <InfoRow label="Spool" value={fmtBytes(agentQ.data.spool_bytes)} />
                  <InfoRow label="Update ring" value={agentQ.data.update_ring} />
                  <InfoRow label="Policy" value={agentQ.data.policy_name || '—'} />
                  <InfoRow label="Config hash" value={<code className="font-mono text-[11px]">{agentQ.data.last_config_hash?.slice(0, 12) || '—'}</code>} />
                  <InfoRow label="API key" value={<code className="font-mono text-[11px]">{agentQ.data.api_key_prefix || '—'}…</code>} />
                  <InfoRow label="Last IP" value={agentQ.data.last_ip || '—'} />
                </div>
                {agentQ.data.config_apply_error ? (
                  <div className="rounded-md border border-danger/30 bg-danger/5 p-3">
                    <div className="flex items-center gap-2 text-sm font-medium text-danger">
                      <AlertCircle className="h-4 w-4" /> Config apply error
                    </div>
                    <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-xs text-text2">
                      {agentQ.data.config_apply_error}
                    </pre>
                  </div>
                ) : null}
                <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                  <Button size="sm" variant="outline" onClick={() => requestDiagM.mutate()}>
                    <Bug className="h-3.5 w-3.5" /> Request diagnostics
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      await api.post(`/agent-fleet/${agentQ.data!.id}/rotate-certificate`)
                      toast.success('Certificate rotation queued')
                    }}
                  >
                    <KeySquare className="h-3.5 w-3.5" /> Rotate certificate
                  </Button>
                </div>
              </CardContent>
            </Card>
          ) : (
            <EmptyMsg>This server has no installed agent (collected agentlessly).</EmptyMsg>
          )}
        </TabsContent>
      </Tabs>

      <ConfirmDialog
        open={decommissionOpen}
        onOpenChange={setDecommissionOpen}
        title="Decommission this server?"
        description="This marks the server and its agent as disabled. Existing metrics are preserved but no new data is ingested."
        confirmText="Decommission"
        destructive
        onConfirm={() => decommissionM.mutate()}
      />
    </div>
  )
}

function ChartBlock({
  title,
  icon: Icon,
  children,
}: {
  title: string
  icon: React.ComponentType<{ className?: string }>
  children: React.ReactNode
}) {
  return (
    <div className="rounded-md border border-border bg-bg p-3">
      <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted">
        <Icon className="h-3.5 w-3.5" />
        {title}
      </div>
      {children}
    </div>
  )
}

function RangeBar({
  range,
  setRange,
  title,
}: {
  range: string
  setRange: (r: string) => void
  title: string
}) {
  return (
    <div className="mb-4 flex items-center justify-between">
      <div className="text-sm font-medium">{title}</div>
      <div className="flex items-center gap-1 rounded-md border border-border bg-surface2 p-1">
        {RANGES.map((r) => (
          <button
            key={r.id}
            type="button"
            onClick={() => setRange(r.id)}
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              range === r.id
                ? 'bg-surface text-text shadow-sm'
                : 'text-muted hover:text-text'
            }`}
          >
            {r.label}
          </button>
        ))}
      </div>
    </div>
  )
}

function IdentityCard({ server: s }: { server: Server }) {
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-center gap-2 text-xs font-medium text-muted">
          <Info className="h-3.5 w-3.5" />
          Identity
        </div>
        <div className="space-y-1 text-sm">
          <Row label="Hostname" value={s.hostname || '—'} />
          <Row label="FQDN" value={s.fqdn || '—'} />
          <Row label="Primary IP" value={s.primary_ip || '—'} />
          <Row label="Architecture" value={s.architecture || '—'} />
          <Row label="Site" value={s.site_name || '—'} />
          <Row label="Owner" value={s.owner || '—'} />
          <Row label="Environment" value={s.environment || '—'} />
          <Row label="Collection" value={s.collection_mode.replace('_', ' ')} />
        </div>
      </CardContent>
    </Card>
  )
}

function AgentCard({ agent }: { agent: Agent | null }) {
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-center gap-2 text-xs font-medium text-muted">
          <Activity className="h-3.5 w-3.5" />
          Agent
        </div>
        {agent ? (
          <div className="space-y-1 text-sm">
            <Row label="Status" value={<StatusBadge status={agent.status} />} />
            <Row label="Version" value={agent.version || '—'} />
            <Row label="Policy" value={agent.policy_name || '—'} />
            <Row
              label="Heartbeat"
              value={agent.last_heartbeat_at ? relativeTime(agent.last_heartbeat_at) : '—'}
            />
            <Row label="Queue" value={String(agent.queue_depth)} />
            <Row label="Spool" value={fmtBytes(agent.spool_bytes)} />
            <Row label="Ring" value={agent.update_ring} />
          </div>
        ) : (
          <div className="py-2 text-sm text-muted">No agent installed</div>
        )}
      </CardContent>
    </Card>
  )
}

function ResourceCard<T>({
  title,
  items,
  renderItem,
}: {
  title: string
  items: T[]
  renderItem: (it: T) => { label: string; sub: string; pct: number }
}) {
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-center gap-2 text-xs font-medium text-muted">
          <HardDrive className="h-3.5 w-3.5" />
          {title}
        </div>
        {items.length === 0 ? (
          <div className="py-2 text-sm text-muted">No data yet.</div>
        ) : (
          <ul className="space-y-2">
            {items.slice(0, 4).map((it, i) => {
              const r = renderItem(it)
              return (
                <li key={i}>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="font-medium">{r.label}</span>
                    <span className="text-muted">{r.sub}</span>
                  </div>
                  <UsageBar pct={r.pct} />
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function EventLogCard({ items }: { items: ServerEventLog[] }) {
  const errors = items.filter((e) => e.level === 'critical' || e.level === 'error')
  const warnings = items.filter((e) => e.level === 'warning')
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-center gap-2 text-xs font-medium text-muted">
          <AlertTriangle className="h-3.5 w-3.5" />
          Event log (24h)
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-md bg-danger/5 p-2">
            <div className="text-xl font-semibold text-danger">
              {errors.reduce((a, b) => a + b.count, 0)}
            </div>
            <div className="text-xs text-muted">errors / critical</div>
          </div>
          <div className="rounded-md bg-warning/5 p-2">
            <div className="text-xl font-semibold text-warning">
              {warnings.reduce((a, b) => a + b.count, 0)}
            </div>
            <div className="text-xs text-muted">warnings</div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function UsageBar({ pct }: { pct: number }) {
  const p = Math.max(0, Math.min(100, pct))
  const tone = p > 90 ? 'bg-danger' : p > 75 ? 'bg-warning' : 'bg-primary'
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <div className={`h-full ${tone}`} style={{ width: `${p}%` }} />
    </div>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-xs text-muted">{label}</span>
      <span className="truncate">{value}</span>
    </div>
  )
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-surface2/40 p-3">
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-sm">{value}</div>
    </div>
  )
}

function EmptyMsg({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 p-10 text-center text-sm text-muted">
      <Layers className="h-6 w-6 text-muted/60" />
      <div>{children}</div>
    </div>
  )
}
