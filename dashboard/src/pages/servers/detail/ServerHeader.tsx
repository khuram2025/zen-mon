/** Identity, health and live resources of one server.
 *
 *  Answers "what is this host and is it OK?" before any tab is opened: name
 *  and health first, the facts an operator looks up most (address, OS,
 *  agent), then six live tiles with the last hour's trend that open the tab
 *  where each number is explained. */

import { Link } from 'react-router-dom'
import {
  AlertTriangle, ArrowLeft, Bot, Clock3, Cpu, Download, HardDrive, ListTree, MemoryStick,
  MoreHorizontal, Network as NetworkIcon, Pencil, Power, RefreshCw, Trash2, Wrench,
} from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { cn, formatBps, formatBytes, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { OsIcon, TagList } from '@/components/servers/shared'
import type {
  AgentItem, ServerFilesystem, ServerItem, ServerLiveMetrics, ServerProcess, ServerService,
} from '@/types/servers'
import { agentProfile, type MetricDetail, type TabKey } from './data'
import { C, Pill, StatTile, fmtPct, pctTone, type Tone } from './ui'

const HEALTH: Record<ServerItem['status'], { label: string; tone: Tone }> = {
  healthy: { label: 'Healthy', tone: 'success' },
  warning: { label: 'Warning', tone: 'warning' },
  critical: { label: 'Critical', tone: 'danger' },
  stale: { label: 'Stale', tone: 'muted' },
  unknown: { label: 'Unknown', tone: 'muted' },
  disabled: { label: 'Disabled', tone: 'muted' },
}

const AGENT: Record<string, { label: string; tone: Tone }> = {
  online: { label: 'Online', tone: 'success' },
  stale: { label: 'Stale', tone: 'warning' },
  offline: { label: 'Offline', tone: 'danger' },
  enrolling: { label: 'Enrolling', tone: 'info' },
  updating: { label: 'Updating', tone: 'info' },
  error: { label: 'Error', tone: 'danger' },
  disabled: { label: 'Disabled', tone: 'muted' },
}

const PROFILE: Record<string, string> = {
  infrastructure: 'Server monitoring',
  combined: 'Server + APM',
  apm: 'APM only',
}

/** Compact uptime from a boot timestamp, e.g. "12d 4h" / "3h 20m". */
export function formatUptime(bootTime: string): string {
  const secs = Math.max(0, (Date.now() - Date.parse(bootTime)) / 1000)
  const d = Math.floor(secs / 86400)
  const h = Math.floor((secs % 86400) / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-muted">{label}</span>
      <span className="text-text2">{children}</span>
    </span>
  )
}

export function ServerHeader({
  server, live, detail, agent, processes, services, filesystems, activeTab, go,
  onEdit, onDownloadAgent, onDecommission, onDelete,
}: {
  server: ServerItem
  live: ServerLiveMetrics
  detail?: MetricDetail
  agent?: AgentItem | null
  processes?: ServerProcess[]
  services?: ServerService[]
  filesystems: ServerFilesystem[]
  activeTab: TabKey
  go: (t: TabKey, sub?: string) => void
  onEdit: () => void
  onDownloadAgent: () => void
  onDecommission: () => void
  onDelete: () => void
}) {
  const qc = useQueryClient()
  const health = HEALTH[server.status] ?? HEALTH.unknown
  const agentMeta = server.agent_status ? AGENT[server.agent_status] ?? AGENT.error : null
  const isLive = server.status !== 'stale' && server.status !== 'disabled' && server.status !== 'unknown'

  // Last hour's trend for the tiles.
  const cpuSpark = detail?.cpu.map((p) => p.total) ?? []
  const memSpark = detail?.memory.map((p) => p.used_pct) ?? []
  const netSpark = (() => {
    if (!detail) return []
    const byTs = new Map<string, number>()
    for (const nic of detail.interfaces) {
      for (const p of nic.points) byTs.set(p.ts, (byTs.get(p.ts) ?? 0) + ((p.rx_bytes_ps ?? 0) + (p.tx_bytes_ps ?? 0)) * 8)
    }
    return [...byTs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, v]) => v)
  })()
  const diskSpark = (() => {
    if (!detail) return []
    const byTs = new Map<string, number>()
    for (const fs of detail.filesystems) {
      for (const p of fs.points) byTs.set(p.ts, Math.max(byTs.get(p.ts) ?? 0, p.used_pct ?? 0))
    }
    return [...byTs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, v]) => v)
  })()

  const proc = processes ?? []
  const running = proc.filter((p) => p.running !== false).length
  const watchedMissing = proc.filter((p) => p.running === false && p.watchlisted).length
  const svc = services ?? []
  const svcRunning = svc.filter((s) => (s.state || '').toLowerCase() === 'running').length
  const svcAutoStopped = svc.filter((s) => {
    const st = (s.state || '').toLowerCase()
    return /auto|enabled/i.test(s.start_mode || '') && st !== '' && st !== 'running' && st !== 'not_found'
  }).length
  const soonestFull = detail?.filesystems
    .map((f) => f.days_until_full)
    .filter((d): d is number => d != null)
    .sort((a, b) => a - b)[0]

  const os = `${server.os_name || server.os_type}${server.os_version ? ` ${server.os_version}` : ''}`

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-border bg-surface px-4 py-3.5 shadow-card dark:shadow-card-dark sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 items-start gap-3">
            <Link
              to="/servers/inventory"
              aria-label="Back to servers"
              title="Back to servers"
              className="mt-1.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border text-muted transition-colors hover:bg-surface2 hover:text-text"
            >
              <ArrowLeft className="h-4 w-4" />
            </Link>
            <span
              className={cn(
                'relative flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border bg-surface2/60',
                health.tone === 'danger' ? 'border-danger/40' : health.tone === 'warning' ? 'border-warning/40' : 'border-border',
              )}
            >
              <OsIcon os={server.os_type} className="h-6 w-6" />
              <span
                className={cn(
                  'absolute -bottom-0.5 -right-0.5 h-3.5 w-3.5 rounded-full border-2 border-surface',
                  health.tone === 'success' ? 'bg-success' : health.tone === 'warning' ? 'bg-warning' : health.tone === 'danger' ? 'bg-danger' : 'bg-muted',
                )}
              />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                <h1 className="truncate text-2xl font-semibold leading-8 tracking-tight">{server.display_name}</h1>
                <Pill tone={health.tone} title={server.status_reasons.join('\n') || undefined}>{health.label}</Pill>
                {server.environment && (
                  <span className="rounded-md border border-border px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide text-muted">
                    {server.environment}
                  </span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                {server.primary_ip && <Fact label="IP"><span className="font-mono">{server.primary_ip}</span></Fact>}
                {server.hostname && server.hostname !== server.display_name && <Fact label="Host">{server.hostname}</Fact>}
                <Fact label="OS">{os}{server.architecture ? ` · ${server.architecture}` : ''}</Fact>
                {server.site_name && <Fact label="Site">{server.site_name}</Fact>}
                {server.owner && <Fact label="Owner">{server.owner}</Fact>}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                <button
                  type="button"
                  onClick={() => go('agent')}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface2/40 px-2 py-1 text-text2 transition-colors hover:border-border-strong hover:text-text"
                  title="Open agent details"
                >
                  <Bot className="h-3.5 w-3.5 text-muted" />
                  {agentMeta ? (
                    <>
                      <span className={cn('h-1.5 w-1.5 rounded-full', agentMeta.tone === 'success' ? 'bg-success' : agentMeta.tone === 'warning' ? 'bg-warning' : agentMeta.tone === 'danger' ? 'bg-danger' : 'bg-muted')} />
                      Agent {agentMeta.label.toLowerCase()}
                      {server.agent_version && <span className="text-muted">v{server.agent_version}</span>}
                      {agentProfile(agent) && <span className="text-muted">· {PROFILE[agentProfile(agent)!] ?? agentProfile(agent)}</span>}
                    </>
                  ) : 'No agent'}
                </button>
                <span className="inline-flex items-center gap-1.5 text-muted" title={server.boot_time ? `Booted ${new Date(server.boot_time).toLocaleString()}` : undefined}>
                  <Clock3 className="h-3.5 w-3.5" />
                  {server.boot_time ? `Up ${formatUptime(server.boot_time)}` : 'Uptime unknown'}
                </span>
                <span className="inline-flex items-center gap-1.5 text-muted" title={server.last_seen ? new Date(server.last_seen).toLocaleString() : undefined}>
                  <RefreshCw className="h-3.5 w-3.5" />
                  Seen {relativeTime(server.last_seen)}
                </span>
                {server.tags.length > 0 && <TagList tags={server.tags} max={6} />}
              </div>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="ghost" size="sm" title="Refresh now"
              onClick={() => qc.invalidateQueries({ queryKey: ['servers', server.id] })}
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
            <Button variant="outline" size="sm" onClick={onEdit}>
              <Pencil className="h-3.5 w-3.5" /> Edit
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" aria-label="More actions">
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuItem onSelect={() => go('agent')}><Bot /> Manage agent</DropdownMenuItem>
                <DropdownMenuItem onSelect={onDownloadAgent}><Download /> Download agent installer</DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={onDecommission}><Power /> Decommission…</DropdownMenuItem>
                <DropdownMenuItem destructive onSelect={onDelete}><Trash2 /> Delete server…</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        {server.status_reasons.length > 0 && (
          <div
            className={cn(
              'mt-3 flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-xs',
              server.status === 'critical' ? 'bg-danger/10 text-danger' : 'bg-warning/10 text-warning',
            )}
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            {server.status_reasons.join(' · ')}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatTile
          icon={Cpu} label="CPU" active={activeTab === 'performance'}
          value={isLive ? fmtPct(live.cpu_pct) : '—'}
          tone={pctTone(live.cpu_pct)}
          pct={isLive ? live.cpu_pct ?? null : null}
          sub={server.cpu_cores ? `${server.cpu_cores} logical cores` : 'processor'}
          spark={cpuSpark} sparkColor={C.blue}
          onClick={() => go('performance')}
        />
        <StatTile
          icon={MemoryStick} label="Memory"
          value={isLive ? fmtPct(live.memory_pct) : '—'}
          tone={pctTone(live.memory_pct)}
          pct={isLive ? live.memory_pct ?? null : null}
          sub={server.memory_total_bytes ? `of ${formatBytes(server.memory_total_bytes)}` : 'physical memory'}
          spark={memSpark} sparkColor={C.violet}
          onClick={() => go('performance')}
        />
        <StatTile
          icon={HardDrive} label="Disk (fullest)"
          value={isLive ? fmtPct(live.disk_max_pct) : '—'}
          tone={pctTone(live.disk_max_pct, 85, 95)}
          pct={isLive ? live.disk_max_pct ?? null : null}
          sub={soonestFull != null && soonestFull < 60
            ? `full in ~${Math.max(1, Math.round(soonestFull))} days`
            : `${filesystems.length} volume${filesystems.length === 1 ? '' : 's'}`}
          spark={diskSpark} sparkColor={C.teal}
          onClick={() => go('system', 'storage')}
        />
        <StatTile
          icon={NetworkIcon} label="Network"
          value={isLive && live.net_bps != null ? formatBps(live.net_bps * 8) : '—'}
          sub="received + sent"
          spark={netSpark} sparkColor={C.sky}
          onClick={() => go('performance')}
        />
        <StatTile
          icon={ListTree} label="Processes" active={activeTab === 'processes'}
          value={processes ? running : '—'}
          tone={watchedMissing ? 'warning' : 'default'}
          sub={watchedMissing ? `${watchedMissing} watched not running` : 'top by CPU and memory'}
          onClick={() => go('processes')}
        />
        <StatTile
          icon={Wrench} label="Services" active={activeTab === 'services'}
          value={svc.length ? <>{svcRunning}<span className="text-base font-medium text-muted">/{svc.length}</span></> : '—'}
          tone={svcAutoStopped ? 'warning' : 'default'}
          sub={svcAutoStopped ? `${svcAutoStopped} auto-start stopped` : svc.length ? 'running' : 'not reported'}
          onClick={() => go('services')}
        />
      </div>
    </div>
  )
}
