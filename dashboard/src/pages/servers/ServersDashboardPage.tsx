/** Server Fleet Dashboard — NOC-style overview for the server monitoring team. */

import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  ChevronRight,
  CloudOff,
  Cpu,
  HardDrive,
  KeyRound,
  LayoutList,
  MemoryStick,
  Network,
  Plus,
  Server,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatBps, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { RingGauge } from '@/components/dashboard/RingGauge'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import { ServerFormDialog } from '@/components/servers/ServerFormDialog'
import {
  AgentStatusBadge, OsIcon, ServerStatusBadge, UsageBar,
} from '@/components/servers/shared'
import type {
  ServerItem, ServerListResponse, ServerLiveMetrics, ServerMonitoringOverview,
} from '@/types/servers'
import { useState } from 'react'

const KPI_ACCENT: Record<string, string> = {
  success: 'from-emerald-500/80 to-green-500/80',
  danger: 'from-rose-500/80 to-red-500/80',
  warning: 'from-amber-500/80 to-orange-500/80',
  info: 'from-cyan-500/80 to-sky-500/80',
  primary: 'from-blue-500/80 to-indigo-500/80',
}

function FleetKpi({
  label, value, sub, icon: Icon, accent, to,
}: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  icon: React.ComponentType<{ className?: string }>
  accent: keyof typeof KPI_ACCENT
  to?: string
}) {
  const inner = (
    <Card className={cn('relative h-full overflow-hidden transition', to && 'hover:border-primary/40')}>
      <span className={cn('absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r', KPI_ACCENT[accent])} />
      <CardContent className="flex items-start justify-between gap-3 p-4">
        <div className="min-w-0">
          <div className="text-[10.5px] font-semibold uppercase tracking-wider text-muted">{label}</div>
          <div className="mt-1 text-2xl font-bold tabular-nums leading-none text-text">{value}</div>
          {sub && <div className="mt-1.5 text-[11px] text-muted">{sub}</div>}
        </div>
        <span className={cn('flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-white shadow', KPI_ACCENT[accent])}>
          <Icon className="h-4 w-4" />
        </span>
      </CardContent>
    </Card>
  )
  return to ? <Link to={to}>{inner}</Link> : inner
}

function PanelHeader({ icon, title, hint, right }: {
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

function PressureBar({ label, value, max, unit, tone }: {
  label: string
  value: number
  max: number
  unit: 'pct' | 'bps'
  tone: string
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="truncate font-medium text-text">{label}</span>
        <span className="shrink-0 font-semibold tabular-nums text-text2">
          {unit === 'bps' ? formatBps(value * 8) : `${value.toFixed(1)}%`}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-surface2">
        <div className={cn('h-full rounded-full bg-gradient-to-r', tone)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function HealthRow({ color, label, value, total }: {
  color: string
  label: string
  value: number
  total: number
}) {
  const pct = total > 0 ? (value / total) * 100 : 0
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className={cn('h-2 w-2 shrink-0 rounded-full', color)} />
      <span className="flex-1 text-text2">{label}</span>
      <span className="font-semibold tabular-nums text-text">{value}</span>
      <span className="w-10 text-right tabular-nums text-muted">{pct.toFixed(0)}%</span>
    </div>
  )
}

export function ServersDashboardPage() {
  const navigate = useNavigate()
  const [deployOpen, setDeployOpen] = useState(false)
  const [formOpen, setFormOpen] = useState(false)

  const { data: overview, isLoading: ovLoading } = useQuery<ServerMonitoringOverview>({
    queryKey: ['server-monitoring', 'overview'],
    queryFn: async () => (await api.get('/server-monitoring/overview')).data,
    refetchInterval: 30_000,
  })

  const { data: live } = useQuery<{ servers: Record<string, ServerLiveMetrics> }>({
    queryKey: ['servers', 'latest-metrics'],
    queryFn: async () => (await api.get('/servers/latest-metrics')).data,
    refetchInterval: 15_000,
  })

  const { data: critical } = useQuery<ServerListResponse>({
    queryKey: ['servers', 'attention', 'critical'],
    queryFn: async () => (await api.get('/servers?status=critical&page_size=6&sort=last_seen&order=desc')).data,
    refetchInterval: 30_000,
  })

  const { data: warning } = useQuery<ServerListResponse>({
    queryKey: ['servers', 'attention', 'warning'],
    queryFn: async () => (await api.get('/servers?status=warning&page_size=4&sort=last_seen&order=desc')).data,
    refetchInterval: 30_000,
  })

  const counts = overview?.status_counts || {}
  const total = overview?.total || 0
  const healthy = counts.healthy || 0
  const criticalN = counts.critical || 0
  const warningN = counts.warning || 0
  const staleN = counts.stale || 0
  const agentOnline = overview?.agent_counts?.online || 0
  const agentTotal = Object.values(overview?.agent_counts || {}).reduce((a, b) => a + b, 0)

  const healthPct = total > 0 ? (healthy / total) * 100 : 0
  const healthColor = healthPct >= 90 ? 'success' : healthPct >= 70 ? 'warning' : 'danger'

  const fleetAvg = useMemo(() => {
    const vals = Object.values(live?.servers || {})
    if (!vals.length) return { cpu: null, mem: null, sampled: 0 }
    const cpu = vals.filter((v) => v.cpu_pct != null).map((v) => v.cpu_pct!)
    const mem = vals.filter((v) => v.memory_pct != null).map((v) => v.memory_pct!)
    return {
      cpu: cpu.length ? cpu.reduce((a, b) => a + b, 0) / cpu.length : null,
      mem: mem.length ? mem.reduce((a, b) => a + b, 0) / mem.length : null,
      sampled: vals.length,
    }
  }, [live])

  const attentionItems: ServerItem[] = [
    ...(critical?.items || []),
    ...(warning?.items || []),
  ].slice(0, 8)

  const liveById = live?.servers || {}

  const pressurePanels = [
    { title: 'CPU pressure', icon: Cpu, list: overview?.top_cpu || [], unit: 'pct' as const, tone: 'from-info to-primary', filter: 'sort=cpu' },
    { title: 'Memory pressure', icon: MemoryStick, list: overview?.top_memory || [], unit: 'pct' as const, tone: 'from-primary to-accent', filter: 'sort=memory' },
    { title: 'Disk pressure', icon: HardDrive, list: overview?.top_disk || [], unit: 'pct' as const, tone: 'from-warning to-danger', filter: 'sort=disk' },
    { title: 'Network throughput', icon: Network, list: overview?.top_network || [], unit: 'bps' as const, tone: 'from-cyan-500 to-sky-500', filter: '' },
  ]

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2.5 text-2xl font-semibold tracking-tight">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500/90 to-indigo-600/90 text-white shadow">
              <Server className="h-4 w-4" />
            </span>
            Server Fleet
          </h1>
          <p className="mt-1 text-sm text-muted">
            Live health, resource pressure, and agent coverage across your monitored hosts
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to="/servers/inventory">
              <LayoutList className="h-3.5 w-3.5" /> View inventory
            </Link>
          </Button>
          <Button variant="outline" size="sm" onClick={() => setFormOpen(true)}>
            <Plus className="h-3.5 w-3.5" /> Register server
          </Button>
          <Button size="sm" onClick={() => setDeployOpen(true)}>
            <KeyRound className="h-3.5 w-3.5" /> Deploy agent
          </Button>
        </div>
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {ovLoading ? (
          [...Array(6)].map((_, i) => <Skeleton key={i} className="h-[88px] rounded-lg" />)
        ) : (
          <>
            <FleetKpi label="Total servers" value={total} icon={Server} accent="primary" to="/servers/inventory" />
            <FleetKpi
              label="Healthy" value={healthy}
              sub={total ? `${((healthy / total) * 100).toFixed(0)}% of fleet` : undefined}
              icon={CheckCircle2} accent="success"
              to="/servers/inventory?status=healthy"
            />
            <FleetKpi
              label="Needs attention" value={criticalN + warningN}
              sub={criticalN > 0 ? `${criticalN} critical` : warningN > 0 ? `${warningN} warning` : 'all clear'}
              icon={AlertTriangle}
              accent={criticalN + warningN > 0 ? 'danger' : 'success'}
              to="/servers/inventory?status=critical"
            />
            <FleetKpi
              label="Stale" value={staleN}
              icon={CloudOff}
              accent={staleN > 0 ? 'warning' : 'primary'}
              to="/servers/inventory?status=stale"
            />
            <FleetKpi
              label="Agents online" value={agentOnline}
              sub={agentTotal ? `of ${agentTotal} enrolled` : undefined}
              icon={Bot} accent="info"
              to="/server-agents"
            />
            <FleetKpi
              label="Fleet load"
              value={fleetAvg.cpu != null ? `${fleetAvg.cpu.toFixed(0)}%` : '—'}
              sub={fleetAvg.mem != null ? `mem ${fleetAvg.mem.toFixed(0)}% · ${fleetAvg.sampled} hosts` : undefined}
              icon={Activity} accent="info"
            />
          </>
        )}
      </div>

      {/* Health + attention */}
      <div className="grid gap-4 xl:grid-cols-12">
        <Card className="overflow-hidden xl:col-span-4">
          <PanelHeader icon={<Activity className="h-3.5 w-3.5" />} title="Fleet health" hint={`${total} monitored`} />
          <CardContent className="flex items-center gap-5 px-5 py-4">
            {ovLoading ? (
              <Skeleton className="h-[130px] w-[130px] rounded-full" />
            ) : (
              <RingGauge
                value={healthPct}
                color={healthColor as 'success' | 'warning' | 'danger'}
                sub="healthy"
                centerLabel={`${healthy}`}
              />
            )}
            <div className="flex-1 space-y-2">
              <HealthRow color="bg-success" label="Healthy" value={healthy} total={total} />
              <HealthRow color="bg-warning" label="Warning" value={warningN} total={total} />
              <HealthRow color="bg-danger" label="Critical" value={criticalN} total={total} />
              <HealthRow color="bg-muted" label="Stale" value={staleN} total={total} />
              <HealthRow color="bg-info" label="Unknown" value={counts.unknown || 0} total={total} />
            </div>
          </CardContent>
        </Card>

        <Card className="overflow-hidden xl:col-span-8">
          <PanelHeader
            icon={<AlertTriangle className="h-3.5 w-3.5" />}
            title="Needs attention"
            hint="critical & warning"
            right={(
              <Link to="/servers/inventory?status=critical" className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
                View all <ChevronRight className="h-3 w-3" />
              </Link>
            )}
          />
          <CardContent className="px-2 pb-2 pt-1">
            {attentionItems.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-10 text-center">
                <CheckCircle2 className="h-8 w-8 text-success/60" />
                <div className="text-sm font-medium text-text">All servers healthy</div>
                <div className="text-xs text-muted">No critical or warning statuses right now</div>
              </div>
            ) : (
              <div className="divide-y divide-border/50">
                {attentionItems.map((s) => {
                  const lm = liveById[s.id] || {}
                  return (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => navigate(`/servers/${s.id}`)}
                      className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition hover:bg-surface2/50"
                    >
                      <OsIcon os={s.os_type} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="truncate text-sm font-medium">{s.display_name}</span>
                          <ServerStatusBadge status={s.status} />
                        </div>
                        <div className="truncate text-[11px] text-muted">
                          {s.primary_ip || s.hostname || '—'}
                          {s.status_reasons?.[0] ? ` · ${s.status_reasons[0]}` : ''}
                        </div>
                      </div>
                      <div className="hidden shrink-0 items-center gap-3 sm:flex">
                        <UsageBar pct={lm.cpu_pct ?? null} warn={85} crit={95} />
                        <UsageBar pct={lm.memory_pct ?? null} warn={85} crit={95} />
                      </div>
                      <span className="shrink-0 text-[10px] text-muted">{relativeTime(s.last_seen)}</span>
                      <ArrowRight className="h-3.5 w-3.5 shrink-0 text-muted" />
                    </button>
                  )
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Resource pressure */}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {pressurePanels.map(({ title, icon: Icon, list, unit, tone }) => {
          // Percent panels use the absolute 0–100 scale — normalizing to the
          // panel max made the top host always show a full bar at any load.
          const max = unit === 'pct' ? 100 : list.length ? Math.max(...list.map((t) => t.value), 1) : 1
          return (
            <Card key={title} className="overflow-hidden">
              <PanelHeader icon={<Icon className="h-3.5 w-3.5" />} title={title} hint="last 10 min" />
              <CardContent className="space-y-2.5 px-4 pb-4 pt-3">
                {list.length === 0 ? (
                  <div className="py-4 text-center text-xs text-muted">No recent samples</div>
                ) : (
                  list.map((t) => (
                    <button
                      key={t.server_id}
                      type="button"
                      onClick={() => navigate(`/servers/${t.server_id}`)}
                      className="block w-full rounded-md px-1 py-0.5 text-left transition hover:bg-surface2/50"
                    >
                      <PressureBar
                        label={t.display_name || t.hostname || t.server_id.slice(0, 8)}
                        value={t.value}
                        max={max}
                        unit={unit}
                        tone={tone}
                      />
                    </button>
                  ))
                )}
              </CardContent>
            </Card>
          )
        })}
      </div>

      {/* OS + agents + sites */}
      <div className="grid gap-4 md:grid-cols-3">
        <Card className="overflow-hidden">
          <PanelHeader icon={<Server className="h-3.5 w-3.5" />} title="By operating system" />
          <CardContent className="space-y-2 px-4 pb-4 pt-3">
            {Object.entries(overview?.os_counts || {}).length === 0 ? (
              <div className="py-4 text-center text-xs text-muted">No servers yet</div>
            ) : (
              Object.entries(overview?.os_counts || {})
                .sort((a, b) => b[1] - a[1])
                .map(([os, n]) => (
                  <Link
                    key={os}
                    to={`/servers/inventory?os=${os}`}
                    className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm transition hover:bg-surface2/50"
                  >
                    <span className="capitalize text-text2">{os}</span>
                    <Badge variant="outline" className="tabular-nums">{n}</Badge>
                  </Link>
                ))
            )}
          </CardContent>
        </Card>

        <Card className="overflow-hidden">
          <PanelHeader
            icon={<Bot className="h-3.5 w-3.5" />}
            title="Agent fleet"
            right={<Link to="/server-agents" className="text-xs text-primary hover:underline">Fleet →</Link>}
          />
          <CardContent className="space-y-2 px-4 pb-4 pt-3">
            {Object.entries(overview?.agent_counts || {}).length === 0 ? (
              <div className="py-4 text-center text-xs text-muted">No agents enrolled</div>
            ) : (
              Object.entries(overview?.agent_counts || {})
                .sort((a, b) => b[1] - a[1])
                .map(([st, n]) => (
                  <div key={st} className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm">
                    <AgentStatusBadge status={st as any} />
                    <span className="font-semibold tabular-nums text-text">{n}</span>
                  </div>
                ))
            )}
          </CardContent>
        </Card>

        <Card className="overflow-hidden">
          <PanelHeader icon={<LayoutList className="h-3.5 w-3.5" />} title="By site" />
          <CardContent className="space-y-2 px-4 pb-4 pt-3">
            {(overview?.sites || []).length === 0 ? (
              <div className="py-4 text-center text-xs text-muted">No sites configured</div>
            ) : (
              (overview?.sites || []).map((s) => (
                <div key={s.id} className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm">
                  <span className="truncate text-text2">{s.name}</span>
                  <Badge variant="outline" className="tabular-nums">{s.server_count}</Badge>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <InstallTokenDialog open={deployOpen} onOpenChange={setDeployOpen} />
      <ServerFormDialog open={formOpen} onOpenChange={setFormOpen} />
    </div>
  )
}
