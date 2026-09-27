/** Overview: is this server OK, and if not, what needs attention first.
 *
 *  Problems lead (each links to the tab that explains it), then the last
 *  hour of CPU, memory, disk and network, the facts about the host and its
 *  agent, volumes with a fill forecast, and the busiest processes. */

import {
  AlertTriangle, ArrowRight, Bell, Bot, CheckCircle2, ClipboardCheck, Clock3, Cpu, HardDrive,
  ListTree, Server as ServerIcon, Wrench,
} from 'lucide-react'
import { cn, formatBps, formatBytes, relativeTime, timeAxisTickFormatter } from '@/lib/utils'
import type { AgentItem, ServerFilesystem, ServerItem, ServerProcess } from '@/types/servers'
import { formatUptime } from '../ServerHeader'
import { type MetricDetail, type TabKey, isPseudoInterface, toRows } from '../data'
import {
  C, ChartStats, EmptyState, KeyValues, Meter, Panel, Pill, TimeSeries, fmtPct, pctTone, stats, type Tone,
} from '../ui'

const PROFILE: Record<string, string> = {
  infrastructure: 'Server monitoring',
  combined: 'Server monitoring + APM',
  apm: 'APM only',
}

interface Attention {
  tone: Tone
  icon: React.ComponentType<{ className?: string }>
  title: string
  detail?: string
  tab: TabKey
  sub?: string
}

function LinkButton({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
    >
      {children} <ArrowRight className="h-3 w-3" />
    </button>
  )
}

export function OverviewTab({
  server, agent, recent, processes, filesystems, openAlerts, complianceFailures, autoStoppedServices,
  watchedMissing, go,
}: {
  server: ServerItem
  agent?: AgentItem | null
  recent?: MetricDetail
  processes: ServerProcess[]
  filesystems: ServerFilesystem[]
  openAlerts: number
  complianceFailures: number
  autoStoppedServices: number
  watchedMissing: number
  go: (t: TabKey, sub?: string) => void
}) {
  const tick = timeAxisTickFormatter(1)
  const forecast = new Map((recent?.filesystems ?? []).map((f) => [f.mount, f]))

  /* What needs attention, most severe first. */
  const attention: Attention[] = []
  if (openAlerts) attention.push({ tone: 'danger', icon: Bell, title: `${openAlerts} open alert${openAlerts === 1 ? '' : 's'}`, detail: 'Acknowledge or resolve them in Alerts', tab: 'alerts' })
  if (server.agent_status && !['online', 'updating', 'enrolling'].includes(server.agent_status)) {
    attention.push({ tone: server.agent_status === 'offline' || server.agent_status === 'error' ? 'danger' : 'warning', icon: Bot, title: `Agent is ${server.agent_status}`, detail: `Last heartbeat ${relativeTime(server.agent_last_heartbeat_at)}`, tab: 'agent' })
  }
  if (agent?.update_status?.state === 'failed') {
    attention.push({ tone: 'danger', icon: Bot, title: agent.update_status.kind === 'profile' ? 'Monitoring-profile change failed' : `Agent upgrade to ${agent.update_status.target_version} failed`, detail: agent.update_status.error ?? undefined, tab: 'agent' })
  }
  for (const fs of filesystems) {
    const pct = fs.used_pct ?? 0
    if (pct >= 90) attention.push({ tone: pct >= 95 ? 'danger' : 'warning', icon: HardDrive, title: `${fs.mount} is ${pct.toFixed(0)}% full`, detail: `${formatBytes(fs.free_bytes ?? 0)} free`, tab: 'system', sub: 'storage' })
    else {
      const days = forecast.get(fs.mount)?.days_until_full
      if (days != null && days < 30) attention.push({ tone: days < 7 ? 'danger' : 'warning', icon: HardDrive, title: `${fs.mount} will be full in ~${Math.max(1, Math.round(days))} days`, detail: 'At its growth over the last week', tab: 'system', sub: 'storage' })
    }
  }
  if (autoStoppedServices) attention.push({ tone: 'warning', icon: Wrench, title: `${autoStoppedServices} automatic service${autoStoppedServices === 1 ? ' is' : 's are'} stopped`, detail: 'Services set to start automatically that are not running', tab: 'services' })
  if (watchedMissing) attention.push({ tone: 'warning', icon: ListTree, title: `${watchedMissing} watched process${watchedMissing === 1 ? ' is' : 'es are'} not running`, tab: 'processes' })
  if (complianceFailures) attention.push({ tone: 'warning', icon: ClipboardCheck, title: `${complianceFailures} compliance finding${complianceFailures === 1 ? '' : 's'}`, detail: 'Missing, outdated or prohibited software', tab: 'compliance' })
  if (Math.abs(server.agent_clock_skew_s ?? 0) > 120) attention.push({ tone: 'warning', icon: Clock3, title: `Clock is off by ${Math.round(Math.abs(server.agent_clock_skew_s ?? 0) / 60)} min`, detail: 'Fix time synchronisation on the host', tab: 'agent' })
  const rank: Record<Tone, number> = { danger: 0, warning: 1, info: 2, default: 3, success: 4, muted: 5 }
  attention.sort((a, b) => rank[a.tone] - rank[b.tone])

  /* Last-hour series. */
  const cpuMem = (() => {
    const mem = new Map((recent?.memory ?? []).map((m) => [m.ts, m.used_pct]))
    return toRows(recent?.cpu ?? [], (p) => ({ cpu: p.total, memory: mem.get(p.ts) ?? null }))
  })()
  const cpuStats = stats((recent?.cpu ?? []).map((p) => p.total))
  const memStats = stats((recent?.memory ?? []).map((p) => p.used_pct))
  const diskRows = (() => {
    const byTs = new Map<string, { read: number; write: number }>()
    for (const d of recent?.disks ?? []) {
      for (const p of d.points) {
        const row = byTs.get(p.ts) ?? { read: 0, write: 0 }
        row.read += p.read_bps ?? 0
        row.write += p.write_bps ?? 0
        byTs.set(p.ts, row)
      }
    }
    return [...byTs.entries()].map(([ts, v]) => ({ ts: Date.parse(ts), ...v })).sort((a, b) => a.ts - b.ts)
  })()
  const netRows = (() => {
    const byTs = new Map<string, { rx: number; tx: number }>()
    for (const nic of recent?.interfaces ?? []) {
      if (isPseudoInterface(nic.name)) continue
      for (const p of nic.points) {
        const row = byTs.get(p.ts) ?? { rx: 0, tx: 0 }
        row.rx += (p.rx_bytes_ps ?? 0) * 8
        row.tx += (p.tx_bytes_ps ?? 0) * 8
        byTs.set(p.ts, row)
      }
    }
    return [...byTs.entries()].map(([ts, v]) => ({ ts: Date.parse(ts), ...v })).sort((a, b) => a.ts - b.ts)
  })()
  const cores = recent?.cores.latest ?? []
  const topProcesses = [...processes].filter((p) => p.running !== false)
    .sort((a, b) => (b.cpu_pct ?? 0) - (a.cpu_pct ?? 0)).slice(0, 8)
  const maxProcCpu = Math.max(1, ...topProcesses.map((p) => p.cpu_pct ?? 0))
  const hasTelemetry = (recent?.cpu.length ?? 0) > 0

  const update = agent?.update_status
  const updateLine = update?.state && ['requested', 'downloading', 'installing'].includes(update.state)
    ? (update.kind === 'profile' ? 'Changing monitoring profile…' : `Upgrading to ${update.target_version}…`)
    : null

  return (
    <div className="grid gap-4 xl:grid-cols-3">
      <div className="space-y-4 xl:col-span-2">
        {/* Needs attention */}
        <Panel
          title={attention.length ? 'Needs attention' : 'All clear'}
          subtitle={attention.length ? `${attention.length} item${attention.length === 1 ? '' : 's'} on this server` : 'Nothing on this server needs action right now'}
          icon={attention.length ? AlertTriangle : CheckCircle2}
          flush
        >
          {attention.length === 0 ? (
            <div className="flex items-center gap-3 px-4 py-3.5 text-[13px] text-text2">
              <CheckCircle2 className="h-5 w-5 text-success" />
              No open alerts, compliance findings, stopped automatic services or filling volumes.
            </div>
          ) : (
            <ul className="divide-y divide-border/70">
              {attention.map((a, i) => (
                <li key={i}>
                  <button
                    type="button"
                    onClick={() => go(a.tab, a.sub)}
                    className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-surface2/40"
                  >
                    <span
                      className={cn(
                        'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
                        a.tone === 'danger' ? 'bg-danger/10 text-danger' : 'bg-warning/10 text-warning',
                      )}
                    >
                      <a.icon className="h-4 w-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[13px] font-medium text-text">{a.title}</span>
                      {a.detail && <span className="block truncate text-xs text-muted" title={a.detail}>{a.detail}</span>}
                    </span>
                    <ArrowRight className="h-4 w-4 shrink-0 text-muted" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        {/* Resources */}
        <Panel
          title="CPU and memory"
          subtitle="Last hour"
          icon={Cpu}
          actions={<LinkButton onClick={() => go('performance')}>Performance</LinkButton>}
        >
          {hasTelemetry ? (
            <>
              <ChartStats
                items={[
                  { label: 'CPU now', value: fmtPct(cpuStats?.current), tone: pctTone(cpuStats?.current) },
                  { label: 'CPU peak', value: fmtPct(cpuStats?.peak), tone: pctTone(cpuStats?.peak) },
                  { label: 'Memory now', value: fmtPct(memStats?.current), tone: pctTone(memStats?.current) },
                  { label: 'Memory peak', value: fmtPct(memStats?.peak), tone: pctTone(memStats?.peak) },
                ]}
              />
              <TimeSeries
                data={cpuMem}
                series={[
                  { key: 'cpu', label: 'CPU', color: C.blue },
                  { key: 'memory', label: 'Memory', color: C.violet },
                ]}
                height={210}
                yFormat={(v) => `${v.toFixed(0)}%`}
                yDomain={[0, 100]}
                tickFormat={tick}
              />
            </>
          ) : (
            <EmptyState
              icon={Cpu}
              title="No recent telemetry"
              hint={server.agent_status === 'online'
                ? 'The agent is online but is not reporting server metrics. Check that server monitoring is enabled in its monitoring profile.'
                : 'Metrics appear here once an authorized agent reports.'}
              compact
            />
          )}
        </Panel>

        <div className="grid gap-4 md:grid-cols-2">
          <Panel title="Disk throughput" subtitle="Last hour · all disks" icon={HardDrive}>
            {diskRows.length ? (
              <TimeSeries
                data={diskRows}
                series={[
                  { key: 'read', label: 'Read', color: C.teal },
                  { key: 'write', label: 'Write', color: C.orange },
                ]}
                height={150}
                yFormat={(v) => `${formatBytes(v)}/s`}
                tickFormat={tick}
              />
            ) : <EmptyState title="No disk activity reported" compact />}
          </Panel>
          <Panel title="Network throughput" subtitle="Last hour · physical interfaces" icon={ServerIcon}>
            {netRows.length ? (
              <TimeSeries
                data={netRows}
                series={[
                  { key: 'rx', label: 'Received', color: C.sky },
                  { key: 'tx', label: 'Sent', color: C.green },
                ]}
                height={150}
                yFormat={(v) => formatBps(v)}
                tickFormat={tick}
              />
            ) : <EmptyState title="No network activity reported" compact />}
          </Panel>
        </div>

        {/* Processes */}
        <Panel
          title="Top processes"
          subtitle="By CPU, from the latest sample"
          icon={ListTree}
          actions={<LinkButton onClick={() => go('processes')}>All processes</LinkButton>}
          flush
        >
          {topProcesses.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="border-b border-border/70 text-left text-[11px] font-medium uppercase tracking-wide text-muted">
                    <th className="px-4 py-2 font-medium">Process</th>
                    <th className="w-[34%] px-4 py-2 font-medium">CPU</th>
                    <th className="px-4 py-2 text-right font-medium">Memory</th>
                    <th className="hidden px-4 py-2 font-medium lg:table-cell">User</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/50">
                  {topProcesses.map((p) => (
                    <tr key={`${p.pid}-${p.name}`} className="hover:bg-surface2/40">
                      <td className="max-w-[220px] truncate px-4 py-2" title={p.cmdline || p.name}>
                        <span className="font-medium text-text">{p.name}</span>
                        {p.pid ? <span className="ml-1.5 font-mono text-[11px] text-muted">{p.pid}</span> : null}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
                            <div className="h-full rounded-full bg-primary" style={{ width: `${((p.cpu_pct ?? 0) / maxProcCpu) * 100}%` }} />
                          </div>
                          <span className="w-12 text-right tabular-nums text-text2">{fmtPct(p.cpu_pct)}</span>
                        </div>
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums text-text2">{formatBytes(p.memory_bytes ?? 0)}</td>
                      <td className="hidden max-w-[200px] truncate px-4 py-2 text-muted lg:table-cell" title={p.user_name ?? undefined}>{p.user_name || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <EmptyState icon={ListTree} title="No process sample yet" compact />}
        </Panel>
      </div>

      {/* Right column */}
      <div className="space-y-4">
        <Panel
          title="System"
          icon={ServerIcon}
          actions={<LinkButton onClick={() => go('system')}>Details</LinkButton>}
        >
          <KeyValues
            columns={1}
            className="gap-y-2.5"
            items={[
              ['Operating system', `${server.os_name || server.os_type}${server.os_version ? ` ${server.os_version}` : ''}`],
              server.kernel_or_build ? ['Build', server.kernel_or_build] : null,
              ['Processor', server.cpu_model || '—'],
              ['Cores', server.cpu_cores ? `${server.cpu_cores} logical${server.cpu_physical_cores ? ` · ${server.cpu_physical_cores} physical` : ''}` : '—'],
              ['Memory', server.memory_total_bytes ? formatBytes(server.memory_total_bytes) : '—'],
              ['Uptime', server.boot_time ? `${formatUptime(server.boot_time)} · since ${new Date(server.boot_time).toLocaleDateString()}` : '—'],
              server.fqdn ? ['FQDN', server.fqdn] : null,
            ]}
          />
        </Panel>

        <Panel
          title="Agent"
          icon={Bot}
          actions={<LinkButton onClick={() => go('agent')}>Manage</LinkButton>}
        >
          {server.agent_id ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Pill tone={server.agent_status === 'online' ? 'success' : server.agent_status === 'offline' || server.agent_status === 'error' ? 'danger' : 'warning'} pulse={server.agent_status === 'online'}>
                  {server.agent_status ? server.agent_status[0].toUpperCase() + server.agent_status.slice(1) : 'Unknown'}
                </Pill>
                {server.agent_version && <span className="text-xs text-muted">Version {server.agent_version}</span>}
              </div>
              {updateLine && (
                <div className="rounded-lg bg-info/10 px-3 py-2 text-xs text-info">{updateLine}</div>
              )}
              <KeyValues
                columns={1}
                className="gap-y-2.5"
                items={[
                  ['Monitoring', agent?.monitoring_profile ? PROFILE[agent.monitoring_profile] ?? agent.monitoring_profile : '—'],
                  ['Last heartbeat', relativeTime(server.agent_last_heartbeat_at)],
                  ['Last metrics', relativeTime(server.agent_last_metric_at ?? null)],
                  agent ? ['Queue', `${agent.queue_depth} batch${agent.queue_depth === 1 ? '' : 'es'} · ${formatBytes(agent.spool_bytes)}`] : null,
                ]}
              />
            </div>
          ) : (
            <EmptyState icon={Bot} title="No agent installed" hint="Install the ZenPlus agent to monitor this server." compact />
          )}
        </Panel>

        <Panel
          title="Volumes"
          subtitle={filesystems.length ? `${filesystems.length} mounted` : undefined}
          icon={HardDrive}
          actions={<LinkButton onClick={() => go('system', 'storage')}>Storage</LinkButton>}
        >
          {filesystems.length ? (
            <ul className="space-y-3.5">
              {[...filesystems].sort((a, b) => (b.used_pct ?? 0) - (a.used_pct ?? 0)).map((fs) => {
                const f = forecast.get(fs.mount)
                const days = f?.days_until_full
                return (
                  <li key={fs.mount}>
                    <div className="mb-1 flex items-baseline justify-between gap-2 text-[13px]">
                      <span className="truncate font-medium text-text" title={fs.mount}>{fs.mount}</span>
                      <span className={cn('tabular-nums', pctTone(fs.used_pct, 85, 95) === 'danger' ? 'text-danger' : pctTone(fs.used_pct, 85, 95) === 'warning' ? 'text-warning' : 'text-text2')}>
                        {fmtPct(fs.used_pct, 0)}
                      </span>
                    </div>
                    <Meter pct={fs.used_pct} warn={85} crit={95} />
                    <div className="mt-1 flex justify-between gap-2 text-[11px] text-muted">
                      <span>{formatBytes(fs.used_bytes ?? 0)} of {formatBytes(fs.total_bytes ?? 0)}</span>
                      <span className={cn(days != null && days < 7 ? 'text-danger' : days != null && days < 30 ? 'text-warning' : '')}>
                        {days != null ? `full in ~${Math.max(1, Math.round(days))}d` : f?.growth_bytes_per_day != null && f.growth_bytes_per_day <= 0 ? 'not growing' : ''}
                      </span>
                    </div>
                  </li>
                )
              })}
            </ul>
          ) : <EmptyState icon={HardDrive} title="No volumes reported" compact />}
        </Panel>

        {cores.length > 0 && (
          <Panel title="CPU cores" subtitle="Latest minute" icon={Cpu}>
            <div className="grid grid-cols-8 gap-1.5">
              {cores.map((v, i) => {
                const pct = v ?? 0
                return (
                  <div key={i} title={`Core ${i}: ${pct.toFixed(1)}%`} className="flex flex-col items-center gap-1">
                    <div className="relative h-12 w-full overflow-hidden rounded bg-surface2">
                      <div
                        className={cn('absolute inset-x-0 bottom-0 rounded', pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warning' : 'bg-primary')}
                        style={{ height: `${Math.max(2, pct)}%` }}
                      />
                    </div>
                    <span className="text-[10px] tabular-nums text-muted">{i}</span>
                  </div>
                )
              })}
            </div>
          </Panel>
        )}
      </div>
    </div>
  )
}
