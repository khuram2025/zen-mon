/**
 * Server team dashboard.
 *
 * One screen for the on-call server engineer:
 *   is the fleet healthy · which hosts are under pressure · are agents
 *   reporting · what is alerting · are baselines compliant.
 *
 * Sources: /server-monitoring/overview, /servers, /servers/latest-metrics,
 * /alerts (server-attached), /server-baselines.
 */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  Bot,
  ClipboardCheck,
  Cpu,
  Flame,
  HardDrive,
  LayoutList,
  MemoryStick,
  Network,
  Server as ServerIcon,
  Layers,
} from 'lucide-react'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { formatBps, relativeTime } from '@/lib/utils'
import { OsIcon, ServerStatusBadge, UsageBar } from '@/components/servers/shared'
import type {
  ServerItem, ServerListResponse, ServerLiveMetrics, ServerMonitoringOverview, TopPressureItem,
} from '@/types/servers'
import {
  CATEGORICAL,
  Empty,
  KpiCard,
  LiveClock,
  PctBar,
  SectionHeader,
  ShareBar,
  TeamHeader,
  fmtCount,
} from './shared'

type AlertRow = {
  id: string
  device_id: string | null
  server_id: string | null
  service_check_id: string | null
  device_hostname: string | null
  severity: string
  message: string
  triggered_at: string
  metadata?: Record<string, unknown>
}

type Baseline = {
  id: string
  name: string
  enabled: boolean
  rule_count: number
  servers_evaluated: number
  servers_compliant: number
  violations: number
}

const STATUS_RANK: Record<string, number> = {
  critical: 0, warning: 1, stale: 2, unknown: 3, healthy: 4, disabled: 5,
}

const OS_LABEL: Record<string, string> = {
  windows: 'Windows', linux: 'Linux', macos: 'macOS', bsd: 'BSD', other: 'Other', unknown: 'Unknown',
}

export function ServerTeamDashboard() {
  const overview = useQuery<ServerMonitoringOverview>({
    queryKey: ['srvteam', 'overview'],
    queryFn: async () => (await api.get('/server-monitoring/overview')).data,
    refetchInterval: 30_000,
  }).data

  const servers = useQuery<ServerListResponse>({
    queryKey: ['srvteam', 'servers'],
    queryFn: async () => (await api.get('/servers?page_size=100')).data,
    refetchInterval: 30_000,
  }).data

  const live = useQuery<{ servers: Record<string, ServerLiveMetrics> }>({
    queryKey: ['srvteam', 'live'],
    queryFn: async () => (await api.get('/servers/latest-metrics')).data,
    refetchInterval: 15_000,
  }).data

  const activeAlerts = useQuery<{ data: AlertRow[] }>({
    queryKey: ['srvteam', 'alerts'],
    queryFn: async () => (await api.get('/alerts?status=active&limit=100')).data,
    refetchInterval: 15_000,
  }).data?.data

  const baselines = useQuery<{ items: Baseline[] }>({
    queryKey: ['srvteam', 'baselines'],
    queryFn: async () => (await api.get('/server-baselines')).data,
    refetchInterval: 5 * 60_000,
    retry: 1,
  }).data

  /* — derived — */

  const total = overview?.total ?? 0
  const sc = overview?.status_counts ?? {}
  const healthy = sc.healthy ?? 0
  const critical = sc.critical ?? 0
  const warning = sc.warning ?? 0
  const stale = (sc.stale ?? 0) + (sc.unknown ?? 0)

  const agents = overview?.agent_counts ?? {}
  const agentsOnline = agents.online ?? 0
  const agentsTotal = Object.values(agents).reduce((s, v) => s + (v ?? 0), 0)
  const agentsBad = (agents.offline ?? 0) + (agents.stale ?? 0) + (agents.error ?? 0)

  const fleet = useMemo(() => {
    const lm = live?.servers ?? {}
    const items = servers?.items ?? []
    const cpus: number[] = []
    const mems: number[] = []
    let diskHot = 0
    const rows = items
      .filter((s) => s.status !== 'disabled')
      .map((s) => {
        const m = lm[s.id] ?? {}
        if (m.cpu_pct != null) cpus.push(m.cpu_pct)
        if (m.memory_pct != null) mems.push(m.memory_pct)
        if ((m.disk_max_pct ?? 0) >= 85) diskHot++
        const pressure = Math.max(m.cpu_pct ?? 0, m.memory_pct ?? 0, m.disk_max_pct ?? 0)
        return { s, m, pressure }
      })
      .sort((a, b) =>
        (STATUS_RANK[a.s.status] ?? 9) - (STATUS_RANK[b.s.status] ?? 9) || b.pressure - a.pressure)
    return {
      rows: rows.slice(0, 10),
      avgCpu: cpus.length ? cpus.reduce((s, v) => s + v, 0) / cpus.length : null,
      avgMem: mems.length ? mems.reduce((s, v) => s + v, 0) / mems.length : null,
      sampled: cpus.length,
      diskHot,
    }
  }, [servers, live])

  const serverAlerts = useMemo(
    () => (activeAlerts || []).filter((a) => a.server_id != null),
    [activeAlerts],
  )
  const serverAlertsCrit = serverAlerts.filter((a) => a.severity === 'critical').length

  const serversById = useMemo(
    () => new Map((servers?.items ?? []).map((s) => [s.id, s])),
    [servers],
  )

  const osMix = useMemo(() => {
    const oc = overview?.os_counts ?? {}
    return Object.entries(oc)
      .filter(([, n]) => (n ?? 0) > 0)
      .sort(([, a], [, b]) => (b ?? 0) - (a ?? 0))
      .map(([os, n], i) => ({ os, n: n ?? 0, color: CATEGORICAL[i % CATEGORICAL.length] }))
  }, [overview])

  const baselineList = baselines?.items?.filter((b) => b.enabled) ?? []
  const baselineViolations = baselineList.reduce((s, b) => s + b.violations, 0)

  /* — render — */

  return (
    <div className="space-y-4 animate-fade-in">
      <TeamHeader
        title="Server Operations"
        subtitle={<>{total} servers · {agentsTotal} agents · {fleet.sampled} reporting metrics · refreshes every 15s</>}
        right={<LiveClock />}
      />

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <KpiCard to="/servers/inventory" label="Fleet Health" icon={<ServerIcon className="h-4 w-4" />}
          accent={critical > 0 ? 'danger' : warning > 0 ? 'warning' : 'success'}
          value={<>{healthy}<span className="text-base font-medium text-muted">/{total}</span></>}
          sub={critical > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{critical} critical</span>
            : warning > 0
              ? <span className="text-[10.5px] text-warning">{warning} warning</span>
              : stale > 0
                ? <span className="text-[10.5px] text-muted">{stale} stale / unknown</span>
                : <span className="text-[10.5px] text-success">all healthy</span>}
          foot={<FleetStatusBar healthy={healthy} warning={warning} critical={critical} stale={stale} />}
        />
        <KpiCard to="/servers/inventory?status=critical" label="Critical" icon={<Flame className="h-4 w-4" />}
          accent={critical > 0 ? 'danger' : 'success'}
          value={fmtCount(critical)}
          sub={<span className="text-[10.5px] text-muted">{warning} warning · {stale} stale</span>}
          foot={<div className="text-[10px] text-muted">status from health rules</div>}
        />
        <KpiCard to="/server-agents" label="Agents Online" icon={<Bot className="h-4 w-4" />}
          accent={agentsBad > 0 ? 'warning' : 'success'}
          value={<>{agentsOnline}<span className="text-base font-medium text-muted">/{agentsTotal}</span></>}
          sub={agentsBad > 0
            ? <span className="text-[10.5px] text-warning">{agentsBad} offline / stale</span>
            : <span className="text-[10.5px] text-success">all reporting</span>}
          foot={<div className="text-[10px] text-muted">heartbeats & metric flow</div>}
        />
        <KpiCard to="/servers" label="Fleet CPU" icon={<Cpu className="h-4 w-4" />} accent="primary"
          value={fleet.avgCpu != null ? `${fleet.avgCpu.toFixed(0)}%` : '—'}
          sub={<span className="text-[10.5px] text-muted">avg across {fleet.sampled} hosts</span>}
          foot={<PctBar value={fleet.avgCpu ?? 0} warnAt={70} dangerAt={90} />}
        />
        <KpiCard to="/servers" label="Fleet Memory" icon={<MemoryStick className="h-4 w-4" />} accent="accent"
          value={fleet.avgMem != null ? `${fleet.avgMem.toFixed(0)}%` : '—'}
          sub={<span className="text-[10.5px] text-muted">avg across {fleet.sampled} hosts</span>}
          foot={<PctBar value={fleet.avgMem ?? 0} warnAt={75} dangerAt={90} />}
        />
        <KpiCard to="/servers" label="Disk Hotspots" icon={<HardDrive className="h-4 w-4" />}
          accent={fleet.diskHot > 0 ? 'danger' : 'success'}
          value={fmtCount(fleet.diskHot)}
          sub={<span className="text-[10.5px] text-muted">filesystems ≥ 85% full</span>}
          foot={<div className="text-[10px] text-muted">worst filesystem per host</div>}
        />
        <KpiCard to="/alerts" label="Server Alerts" icon={<AlertTriangle className="h-4 w-4" />}
          accent={serverAlertsCrit > 0 ? 'danger' : serverAlerts.length > 0 ? 'warning' : 'success'}
          value={fmtCount(serverAlerts.length)}
          sub={serverAlerts.length
            ? <span className="text-[10.5px] text-danger">{serverAlertsCrit} critical</span>
            : <span className="text-[10.5px] text-success">nothing firing</span>}
          foot={<div className="text-[10px] text-muted">host CPU · memory · disk · services</div>}
        />
      </div>

      {/* Fleet table + pressure */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        <Card className="xl:col-span-8">
          <SectionHeader icon={<LayoutList className="h-3.5 w-3.5" />} title="Fleet Status" hint="worst first · live"
            right={<Link to="/servers/inventory" className="text-xs text-primary hover:underline">Inventory →</Link>} />
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-left text-[10px] uppercase tracking-wider text-muted">
                  <th className="px-4 py-2 font-semibold">Server</th>
                  <th className="px-2 py-2 font-semibold">Status</th>
                  <th className="px-2 py-2 font-semibold">CPU</th>
                  <th className="px-2 py-2 font-semibold">Memory</th>
                  <th className="px-2 py-2 font-semibold">Disk</th>
                  <th className="px-2 py-2 font-semibold">Network</th>
                  <th className="px-2 py-2 text-right font-semibold">Last seen</th>
                </tr>
              </thead>
              <tbody>
                {!fleet.rows.length && (
                  <tr><td colSpan={7}><Empty text="No servers enrolled yet — add one from Servers → Inventory" /></td></tr>
                )}
                {fleet.rows.map(({ s, m }) => (
                  <tr key={s.id} className="border-b border-border/50 transition hover:bg-surface2/40">
                    <td className="max-w-[220px] px-4 py-2">
                      <Link to={`/servers/${s.id}`} className="flex min-w-0 items-center gap-2">
                        <OsIcon os={s.os_type} />
                        <span className="min-w-0">
                          <span className="block truncate font-medium text-text">{s.display_name}</span>
                          <span className="block truncate text-[10px] text-muted">{s.primary_ip || s.hostname || '—'}{s.environment ? ` · ${s.environment}` : ''}</span>
                        </span>
                      </Link>
                    </td>
                    <td className="px-2 py-2"><ServerStatusBadge status={s.status} reasons={s.status_reasons} /></td>
                    <td className="px-2 py-2"><UsageBar pct={m.cpu_pct} warn={70} crit={90} /></td>
                    <td className="px-2 py-2"><UsageBar pct={m.memory_pct} warn={75} crit={90} /></td>
                    <td className="px-2 py-2"><UsageBar pct={m.disk_max_pct} /></td>
                    <td className="px-2 py-2 font-mono text-[11px] tabular-nums text-text2">
                      {m.net_bps != null ? formatBps(m.net_bps * 8) : '—'}
                    </td>
                    <td className="px-2 py-2 text-right text-[10px] tabular-nums text-muted">{relativeTime(s.last_seen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<Flame className="h-3.5 w-3.5" />} title="Resource Pressure" hint="top hosts · 10 min" />
          <div className="space-y-3 px-4 pb-4 pt-3">
            <PressureGroup icon={<Cpu className="h-3 w-3" />} label="CPU" items={overview?.top_cpu} unit="pct" serversById={serversById} />
            <PressureGroup icon={<MemoryStick className="h-3 w-3" />} label="Memory" items={overview?.top_memory} unit="pct" serversById={serversById} />
            <PressureGroup icon={<HardDrive className="h-3 w-3" />} label="Disk (fullest fs)" items={overview?.top_disk} unit="pct" serversById={serversById} />
            <PressureGroup icon={<Network className="h-3 w-3" />} label="Network" items={overview?.top_network} unit="bps" serversById={serversById} />
          </div>
        </Card>
      </div>

      {/* Alerts · baselines · fleet mix */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-12">
        <Card className="xl:col-span-5">
          <SectionHeader icon={<AlertTriangle className="h-3.5 w-3.5" />} title="Server Alert Feed"
            right={<Link to="/alerts" className="text-xs text-primary hover:underline">View all →</Link>} />
          <div className="px-3 pb-3 pt-2">
            {!serverAlerts.length && <Empty text="No active server alerts 🎉" />}
            <div className="space-y-0.5">
              {serverAlerts.slice(0, 8).map((a) => {
                const srv = a.server_id ? serversById.get(a.server_id) : undefined
                return (
                  <Link to={`/alerts/${a.id}`} key={a.id} className="group flex gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-surface2/50">
                    <div className={`mt-0.5 w-1 shrink-0 self-stretch rounded-full ${a.severity === 'critical' ? 'bg-danger' : a.severity === 'warning' ? 'bg-warning' : 'bg-info'}`} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-xs font-semibold text-text">{srv?.display_name || a.device_hostname || 'server'}</span>
                        <span className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(a.triggered_at)}</span>
                      </div>
                      <div className="truncate text-[11px] text-text2" title={a.message}>{a.message.replace(/^\[[^\]]+\]\s*/, '')}</div>
                    </div>
                    <Badge variant={a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info'} className="self-center capitalize">{a.severity}</Badge>
                  </Link>
                )
              })}
            </div>
          </div>
        </Card>

        <Card className="xl:col-span-4">
          <SectionHeader icon={<ClipboardCheck className="h-3.5 w-3.5" />} title="Baseline Compliance"
            hint={baselineViolations > 0 ? `${baselineViolations} violations` : 'all clear'}
            right={<Link to="/server-baselines" className="text-xs text-primary hover:underline">Baselines →</Link>} />
          <div className="space-y-2.5 px-4 pb-4 pt-3">
            {!baselineList.length && <Empty text="No software baselines defined yet" />}
            {baselineList.slice(0, 6).map((b) => {
              const pct = b.servers_evaluated > 0 ? (b.servers_compliant / b.servers_evaluated) * 100 : null
              return (
                <div key={b.id}>
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="min-w-0 truncate font-medium text-text">{b.name}</span>
                    <span className="shrink-0 font-mono text-[11px] font-semibold tabular-nums text-text2">
                      {pct != null ? `${b.servers_compliant}/${b.servers_evaluated}` : 'not evaluated'}
                    </span>
                  </div>
                  <div className="mt-1"><PctBar value={pct ?? 0} warnAt={99.9} dangerAt={101} /></div>
                  <div className="mt-0.5 flex justify-between text-[9.5px] text-muted">
                    <span>{b.rule_count} rules</span>
                    {b.violations > 0 && <span className="text-warning">{b.violations} violations</span>}
                  </div>
                </div>
              )
            })}
          </div>
        </Card>

        <Card className="xl:col-span-3">
          <SectionHeader icon={<Layers className="h-3.5 w-3.5" />} title="Fleet Mix" />
          <div className="space-y-3 px-4 pb-4 pt-3">
            <div>
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Operating systems</div>
              <ShareBar parts={osMix.map((o) => ({ label: OS_LABEL[o.os] || o.os, value: o.n, color: o.color }))} height={10} />
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                {osMix.map((o) => (
                  <span key={o.os} className="flex items-center gap-1.5 text-[10.5px] text-text2">
                    <span className="h-2 w-2 rounded-sm" style={{ background: o.color }} />
                    {OS_LABEL[o.os] || o.os} <span className="font-mono text-muted">{o.n}</span>
                  </span>
                ))}
                {!osMix.length && <span className="text-[10.5px] text-muted">no servers yet</span>}
              </div>
            </div>
            <div className="border-t border-border pt-2.5">
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Agent status</div>
              <div className="space-y-1">
                <AgentRow label="Online" value={agents.online ?? 0} total={agentsTotal} tone="bg-success" />
                <AgentRow label="Stale" value={agents.stale ?? 0} total={agentsTotal} tone="bg-warning" />
                <AgentRow label="Offline" value={(agents.offline ?? 0) + (agents.error ?? 0)} total={agentsTotal} tone="bg-danger" />
                <AgentRow label="Enrolling" value={agents.enrolling ?? 0} total={agentsTotal} tone="bg-info" />
              </div>
            </div>
            {(overview?.sites?.length ?? 0) > 0 && (
              <div className="border-t border-border pt-2.5">
                <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Sites</div>
                <div className="space-y-1">
                  {(overview?.sites || []).slice(0, 4).map((s) => (
                    <div key={s.id} className="flex items-center justify-between text-xs">
                      <span className="min-w-0 truncate text-text2">{s.name}</span>
                      <span className="font-mono text-[11px] tabular-nums text-muted">{s.server_count}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}

/* ── Local building blocks ──────────────────────────────────────────────── */

function FleetStatusBar({ healthy, warning, critical, stale }: {
  healthy: number; warning: number; critical: number; stale: number
}) {
  const total = healthy + warning + critical + stale
  if (!total) return <div className="h-1.5 rounded-full bg-surface2" />
  const seg = (n: number) => `${(n / total) * 100}%`
  return (
    <div className="flex h-1.5 w-full gap-px overflow-hidden rounded-full bg-surface2">
      <div style={{ width: seg(healthy) }} className="bg-success" />
      <div style={{ width: seg(warning) }} className="bg-warning" />
      <div style={{ width: seg(critical) }} className="bg-danger" />
      <div style={{ width: seg(stale) }} className="bg-muted/60" />
    </div>
  )
}

function PressureGroup({ icon, label, items, unit, serversById }: {
  icon: React.ReactNode
  label: string
  items?: TopPressureItem[]
  unit: 'pct' | 'bps'
  serversById: Map<string, ServerItem>
}) {
  const top = (items || []).slice(0, 3)
  const max = unit === 'bps' ? Math.max(...top.map((t) => t.value), 1) : 100
  return (
    <div>
      <div className="flex items-center gap-1.5 pb-1 text-[10px] font-bold uppercase tracking-wider text-muted">
        {icon}{label}
      </div>
      {!top.length && <div className="pb-1 text-[10.5px] text-muted">no samples in the last 10 minutes</div>}
      <div className="space-y-1.5">
        {top.map((t) => {
          const name = t.display_name || serversById.get(t.server_id)?.display_name || t.hostname || t.server_id.slice(0, 8)
          const pct = Math.min(100, (t.value / max) * 100)
          const hot = unit === 'pct' && t.value >= 85
          return (
            <Link key={`${label}-${t.server_id}`} to={`/servers/${t.server_id}`} className="block rounded px-1 py-0.5 transition hover:bg-surface2/40">
              <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="min-w-0 truncate text-text2">{name}</span>
                <span className={`shrink-0 font-mono text-[11px] font-semibold tabular-nums ${hot ? 'text-danger' : 'text-text2'}`}>
                  {unit === 'pct' ? `${t.value.toFixed(1)}%` : formatBps(t.value * 8)}
                </span>
              </div>
              <div className="mt-0.5 h-1 w-full overflow-hidden rounded-full bg-surface2">
                <div className={`h-full rounded-full ${hot ? 'bg-danger' : 'bg-primary'}`} style={{ width: `${Math.max(2, pct)}%` }} />
              </div>
            </Link>
          )
        })}
      </div>
    </div>
  )
}

function AgentRow({ label, value, total, tone }: { label: string; value: number; total: number; tone: string }) {
  const pct = total ? (value / total) * 100 : 0
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className={`h-2 w-2 shrink-0 rounded-full ${tone}`} />
      <span className="w-16 text-text2">{label}</span>
      <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface2">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(value > 0 ? 3 : 0, pct)}%` }} />
      </div>
      <span className="w-6 text-right font-mono text-[11px] font-semibold tabular-nums">{value}</span>
    </div>
  )
}
