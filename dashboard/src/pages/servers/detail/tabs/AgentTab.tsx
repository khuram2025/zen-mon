/** Agent tab: the installed agent (health, updates, monitoring profile,
 *  details, on-demand actions and command history) and the server record
 *  (edit, decommission, delete). Merges the former Agent and Settings tabs. */

import { useEffect, useMemo, useState, type ComponentType, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, ArrowUpCircle, Bot, CheckCircle2, Clock3, CloudOff, Download, FileDown,
  Info, KeyRound, Layers, Loader2, Pencil, RefreshCw, ScrollText, Settings2, ShieldCheck, Stethoscope,
  Trash2, Zap,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, formatBytes, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { AGENT_STATUS_META, AuthorizationBadge, TagList } from '@/components/servers/shared'
import { QueryError, TableStateRow } from '@/components/servers/tables'
import type {
  AgentItem, CollectionMode, MonitoringProfile, ServerCommand, ServerItem, UpdateRing,
} from '@/types/servers'
import { useServerAgent } from '../data'
import {
  EmptyState, KeyValues, Panel, Pill, Segmented, StatTile, fmtNum, type Tone,
} from '../ui'

type Icon = ComponentType<{ className?: string }>

/* ── Constants ───────────────────────────────────────────────────── */

const SECTIONS = [
  { key: 'agent', label: 'Agent', icon: Bot },
  { key: 'settings', label: 'Settings', icon: Settings2 },
] as const
type SectionKey = typeof SECTIONS[number]['key']

const MONITORING_PROFILES: { value: MonitoringProfile; label: string; short: string; hint: string }[] = [
  {
    value: 'infrastructure', label: 'Server monitoring only', short: 'Server monitoring',
    hint: 'Host metrics, services, events and inventory. Runs on the smaller server-monitoring package.',
  },
  {
    value: 'combined', label: 'Server monitoring + APM', short: 'Server + APM',
    hint: 'Everything in server monitoring, plus application tracing through the local APM gateway.',
  },
  {
    value: 'apm', label: 'APM only', short: 'APM only',
    hint: 'Application tracing only, for hosts monitored by another tool.',
  },
]

const UPDATE_RINGS: { value: UpdateRing; label: string; hint: string }[] = [
  { value: 'canary', label: 'Canary', hint: 'Receives new versions first' },
  { value: 'beta', label: 'Beta', hint: 'Receives new versions after canary' },
  { value: 'stable', label: 'Stable', hint: 'Receives versions once proven' },
  { value: 'pinned', label: 'Pinned', hint: 'Only upgrades when requested here' },
]

const COLLECTION_MODE_LABEL: Record<CollectionMode, string> = {
  agent: 'Agent',
  agentless_wmi: 'Agentless (WMI)',
  agentless_winrm: 'Agentless (WinRM)',
  snmp: 'SNMP',
  ssh: 'SSH',
  none: 'None',
}

const PROFILE_CAPABILITY = 'monitoring_profile_v1'
/** Clocks off by more than this are flagged. */
const CLOCK_SKEW_WARN_S = 120
/** A successful change stays visible for a day, then the version speaks for itself. */
const UPDATE_SUCCESS_VISIBLE_MS = 24 * 60 * 60 * 1000
/** Commands shown before "Show all". */
const COMMANDS_VISIBLE = 15

interface AgentPackage {
  id?: string
  platform: string
  version: string
  flavor?: string | null
  channel?: string | null
  is_latest?: boolean
  file_size?: number | null
  released_at?: string | null
}

interface BulkResult {
  ok: boolean
  affected: number
  skipped?: { agent_id?: string; hostname: string | null; reason: string }[]
}

/** The agent record carries its advertised capabilities; older type
 *  definitions omit the field. */
type AgentRecord = AgentItem & { capabilities?: string[] }

/* ── Helpers ─────────────────────────────────────────────────────── */

function compareVersionsDesc(a: string, b: string): number {
  const parts = (v: string) => v.replace(/^v/i, '').split('-')[0].split('.').map((n) => Number.parseInt(n, 10) || 0)
  const pa = parts(a)
  const pb = parts(b)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pb[i] ?? 0) - (pa[i] ?? 0)
    if (d !== 0) return d
  }
  return 0
}

/** True when `a` is a newer version than `b`. */
function isNewer(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a) return false
  if (!b) return true
  return compareVersionsDesc(a, b) < 0
}

function profileMeta(p: MonitoringProfile | null | undefined) {
  return MONITORING_PROFILES.find((x) => x.value === p)
}

function variantTone(v: string): Tone {
  if (v === 'success' || v === 'warning' || v === 'danger' || v === 'info') return v
  return 'muted'
}

function parseTs(iso: string | null | undefined): number {
  if (!iso) return NaN
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)
  return Date.parse(hasTz ? iso : `${iso.replace(' ', 'T')}Z`)
}

/** "in 42d" / "3d ago" for a date that may lie in the future. */
function relativeEither(iso: string | null | undefined): string {
  const t = parseTs(iso)
  if (Number.isNaN(t)) return '—'
  const diff = (t - Date.now()) / 1000
  if (diff <= 0) return relativeTime(iso)
  if (diff < 3600) return `in ${Math.max(1, Math.floor(diff / 60))}m`
  if (diff < 86400) return `in ${Math.floor(diff / 3600)}h`
  return `in ${Math.floor(diff / 86400)}d`
}

function fmtDateTime(iso: string | null | undefined): string {
  const t = parseTs(iso)
  return Number.isNaN(t) ? '—' : new Date(t).toLocaleString()
}

function fmtSkew(skew: number): string {
  const abs = Math.abs(skew)
  const sign = skew > 0 ? '+' : '−'
  if (abs < 60) return `${sign}${Math.round(abs)}s`
  if (abs < 3600) return `${sign}${Math.round(abs / 60)} min`
  return `${sign}${(abs / 3600).toFixed(1)} h`
}

function formatUptime(bootTime: string): string {
  const s = (Date.now() - parseTs(bootTime)) / 1000
  if (!Number.isFinite(s) || s <= 0) return '—'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
}

function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return <span className="font-mono text-xs" title={title}>{children}</span>
}

/** Callout inside a panel. */
function Note({ tone = 'info', icon: IconC = Info, title, children, className }: {
  tone?: 'info' | 'warning' | 'danger' | 'success' | 'muted'
  icon?: Icon
  title?: ReactNode
  children?: ReactNode
  className?: string
}) {
  const styles = {
    info: 'border-info/25 bg-info/5 text-info',
    warning: 'border-warning/30 bg-warning/5 text-warning',
    danger: 'border-danger/30 bg-danger/5 text-danger',
    success: 'border-success/30 bg-success/5 text-success',
    muted: 'border-border bg-surface2/40 text-muted',
  }[tone]
  return (
    <div className={cn('flex items-start gap-2.5 rounded-lg border px-3 py-2.5', styles, className)}>
      <IconC className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div className="min-w-0 text-xs leading-5">
        {title && <div className="font-medium">{title}</div>}
        {children && <div className="text-muted">{children}</div>}
      </div>
    </div>
  )
}

function FieldLabel({ children }: { children: ReactNode }) {
  return <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">{children}</div>
}

/* ── Update status ───────────────────────────────────────────────── */

interface UpdateView {
  phase: 'progress' | 'failed' | 'succeeded'
  title: string
  detail: string
  error?: string | null
}

/** Mirrors the fleet page's update badge: progress, failure or recent success
 *  of an upgrade or a monitoring-profile change. */
function describeUpdate(agent: AgentItem): UpdateView | null {
  const s = agent.update_status
  if (!s?.state || !s.target_version) return null
  const when = s.updated_at ? ` · ${relativeTime(s.updated_at)}` : ''
  const profileChange = s.kind === 'profile'
  const adding = profileChange && s.target_flavor === 'full' && s.target_profile !== 'infrastructure'
  const removing = profileChange && s.target_profile === 'infrastructure'
  const target = profileMeta(s.target_profile)?.short ?? 'profile'
  switch (s.state) {
    case 'requested':
    case 'downloading':
    case 'installing': {
      const step = s.state === 'requested' ? 'Requested' : s.state === 'downloading' ? 'Downloading' : 'Installing'
      return {
        phase: 'progress',
        title: !profileChange
          ? `${s.state === 'requested' ? 'Upgrade to' : step} ${s.target_version}${s.state === 'requested' ? ' requested' : ''}`
          : adding ? 'Adding APM' : removing ? 'Removing APM' : `Switching to ${target}`,
        detail: profileChange
          ? `${step} · monitoring-profile change${when}`
          : `From ${s.from_version || agent.version || 'current'}${when}`,
      }
    }
    case 'failed':
      return {
        phase: 'failed',
        title: !profileChange
          ? `Upgrade to ${s.target_version} failed`
          : adding ? 'Adding APM failed' : removing ? 'Removing APM failed' : 'Profile change failed',
        detail: profileChange ? `Monitoring-profile change${when}` : `From ${s.from_version || agent.version || 'current'}${when}`,
        error: s.error || 'The agent reported a failure without a reason',
      }
    case 'succeeded': {
      const at = parseTs(s.updated_at)
      if (agent.version !== s.target_version || !(Date.now() - at < UPDATE_SUCCESS_VISIBLE_MS)) return null
      return {
        phase: 'succeeded',
        title: !profileChange ? `Upgraded to ${s.target_version}` : adding ? 'APM added' : removing ? 'APM removed' : `Now ${target}`,
        detail: profileChange ? `Monitoring profile changed${when}` : `From ${s.from_version || 'the previous version'}${when}`,
      }
    }
    default:
      return null
  }
}

function UpdateStatusLine({ view }: { view: UpdateView }) {
  const tone = view.phase === 'failed' ? 'danger' : view.phase === 'succeeded' ? 'success' : 'info'
  const styles = {
    info: 'border-info/25 bg-info/5',
    danger: 'border-danger/30 bg-danger/5',
    success: 'border-success/30 bg-success/5',
  }[tone]
  const text = { info: 'text-info', danger: 'text-danger', success: 'text-success' }[tone]
  return (
    <div className={cn('flex items-start gap-2.5 rounded-lg border px-3 py-2.5', styles)} role="status">
      {view.phase === 'progress' ? (
        <Loader2 className={cn('mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin', text)} />
      ) : view.phase === 'failed' ? (
        <AlertTriangle className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', text)} />
      ) : (
        <CheckCircle2 className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', text)} />
      )}
      <div className="min-w-0 text-xs leading-5">
        <div className={cn('font-medium', text)}>{view.title}</div>
        <div className="text-muted">{view.detail}</div>
        {view.error && <div className="mt-1 break-words text-text">{view.error}</div>}
      </div>
    </div>
  )
}

/* ── APM status ──────────────────────────────────────────────────── */

function apmSummary(status: AgentItem['apm_status']): { tone: Tone; label: string; detail: string } {
  const failed = status?.failed ?? 0
  const state = (status?.state || '').toLowerCase()
  const hasProblem = Boolean(status?.last_error)
    || failed > 0
    || state === 'error'
    || state === 'failed'
    || (status?.enabled === true && status.gateway?.managed === true && status.gateway.healthy === false)
  const tone: Tone = status?.enabled === false ? 'muted' : hasProblem ? 'danger' : status?.gateway?.healthy ? 'success' : 'warning'
  const label = status?.enabled === false
    ? 'Disabled'
    : hasProblem
      ? 'Needs attention'
      : status?.gateway?.healthy
        ? 'Ready'
        : status?.gateway?.listening
          ? 'OTLP listening'
          : status?.enabled
            ? 'Starting'
            : 'No status'
  const detail = [
    status?.gateway?.version ? `Gateway ${status.gateway.version}` : null,
    status?.discovered != null || status?.instrumented != null
      ? `${status?.instrumented ?? 0}/${status?.discovered ?? 0} processes instrumented`
      : null,
    failed > 0 ? `${failed} instrumentation failure${failed === 1 ? '' : 's'}` : null,
    status?.spans_forwarded_1m != null ? `${fmtNum(status.spans_forwarded_1m)} spans/min` : null,
  ].filter(Boolean).join(' · ') || 'Reported by the endpoint agent'
  return { tone, label, detail }
}

/* ── Tab ─────────────────────────────────────────────────────────── */

export function AgentTab({ server, sub, setSub, onEdit, onDecommission, onDelete, onDownloadAgent }: {
  server: ServerItem
  sub: string
  setSub: (s: string) => void
  onEdit: () => void
  onDecommission: () => void
  onDelete: () => void
  onDownloadAgent: () => void
}) {
  const active: SectionKey = sub === 'settings' ? 'settings' : 'agent'
  return (
    <div className="space-y-4 text-[13px]">
      <Segmented items={SECTIONS} value={active} onChange={(k) => setSub(k)} />
      {active === 'agent'
        ? <AgentSection server={server} onDownloadAgent={onDownloadAgent} />
        : <SettingsSection server={server} onEdit={onEdit} onDecommission={onDecommission} onDelete={onDelete} />}
    </div>
  )
}

/* ── Agent section ───────────────────────────────────────────────── */

function AgentSection({ server, onDownloadAgent }: { server: ServerItem; onDownloadAgent: () => void }) {
  const { data, isLoading, isError, error, refetch } = useServerAgent(server.id)
  const agent = data as AgentRecord | null | undefined

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-[92px] rounded-xl" />)}
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <Skeleton className="h-64 rounded-xl" />
          <Skeleton className="h-64 rounded-xl" />
        </div>
      </div>
    )
  }

  if (isError) {
    return (
      <Panel title="Agent" icon={Bot}>
        <QueryError error={error} onRetry={() => refetch()} />
      </Panel>
    )
  }

  if (!agent) {
    return (
      <Panel title="Agent" subtitle="Not enrolled" icon={Bot}>
        <EmptyState
          icon={Bot}
          title="No agent enrolled on this server"
          hint={
            <>
              Install the agent with this appliance&apos;s controller address, then approve its pending
              authorization request in Agent Fleet. No endpoint token is required.
            </>
          }
          action={(
            <Button size="sm" className="mt-2" onClick={onDownloadAgent}>
              <Download className="h-3.5 w-3.5" /> Download agent
            </Button>
          )}
        />
      </Panel>
    )
  }

  return <AgentBody server={server} agent={agent} onDownloadAgent={onDownloadAgent} />
}

function AgentBody({ server, agent, onDownloadAgent }: {
  server: ServerItem
  agent: AgentRecord
  onDownloadAgent: () => void
}) {
  const qc = useQueryClient()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['servers', server.id, 'agent'] })
    qc.invalidateQueries({ queryKey: ['servers', server.id, 'commands'] })
    qc.invalidateQueries({ queryKey: ['servers', server.id] })
    qc.invalidateQueries({ queryKey: ['agent-fleet'] })
  }

  // Versions this appliance can serve to the agent, newest first.
  const { data: packageList, isLoading: packagesLoading } = useQuery<{ items: AgentPackage[] }>({
    queryKey: ['agent-packages'],
    queryFn: async () => (await api.get('/agent-fleet/packages')).data,
    staleTime: 60_000,
  })
  const platform = (agent.platform || 'windows').toLowerCase()
  const platformPackages = useMemo(
    () => (packageList?.items ?? []).filter(
      (p) => (p.platform || '').toLowerCase() === platform && (!p.channel || p.channel === 'stable'),
    ),
    [packageList, platform],
  )
  const publishedVersions = useMemo(
    () => Array.from(new Set(platformPackages.map((p) => p.version))).sort(compareVersionsDesc),
    [platformPackages],
  )
  const latest = publishedVersions[0] ?? null
  const updateAvailable = latest && agent.version && isNewer(latest, agent.version) ? latest : null

  const capabilities = agent.capabilities?.length ? agent.capabilities : server.agent_capabilities ?? []
  const view = describeUpdate(agent)

  // Bulk endpoint, scoped to this one agent; skipped agents come back with a reason.
  const bulk = useMutation({
    mutationFn: async (body: Record<string, unknown>) =>
      (await api.post<BulkResult>('/agent-fleet/bulk', { agent_ids: [agent.id], ...body })).data,
    onError: (e) => toast.error('Request failed', apiErrorMessage(e)),
  })
  const runBulk = (body: Record<string, unknown>, success: [string, string]) =>
    bulk.mutateAsync(body).then((r) => {
      const skipped = r?.skipped ?? []
      if (skipped.length > 0) {
        toast.error('Not applied', skipped.map((s) => s.reason).join(' · '))
      } else {
        toast.success(success[0], success[1])
      }
      invalidate()
      return skipped.length === 0
    }).catch(() => false)

  return (
    <div className="space-y-4">
      <AgentAlerts agent={agent} />
      <AgentTiles agent={agent} latest={latest} updateAvailable={updateAvailable} view={view} />

      <div className="grid gap-4 lg:grid-cols-2">
        <UpdatesPanel
          agent={agent}
          versions={publishedVersions}
          latest={latest}
          loading={packagesLoading}
          view={view}
          busy={bulk.isPending}
          onUpgrade={(v) => runBulk(
            { action: 'trigger_upgrade', target_version: v },
            ['Upgrade requested', `The agent downloads ${v} on its next check-in; progress shows here`],
          )}
          onRingChanged={invalidate}
        />
        <ProfilePanel
          agent={agent}
          capabilities={capabilities}
          view={view}
          busy={bulk.isPending}
          onApply={(profile, removeInstrumentation) => runBulk(
            {
              action: 'change_monitoring_profile',
              monitoring_profile: profile,
              remove_instrumentation: profile === 'infrastructure' && removeInstrumentation,
            },
            ['Monitoring profile change requested', 'The agent applies it on its next check-in; progress shows here'],
          )}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <DetailsPanel agent={agent} capabilities={capabilities} className="lg:col-span-2" />
        <ActionsPanel server={server} agent={agent} onDownloadAgent={onDownloadAgent} onDone={invalidate} />
      </div>

      <CommandHistory serverId={server.id} />
    </div>
  )
}

/** Conditions that need an operator, above everything else. */
function AgentAlerts({ agent }: { agent: AgentItem }) {
  const items: ReactNode[] = []
  if (agent.registration_conflict) {
    const who = [agent.registration_conflict_hostname, agent.registration_conflict_ip].filter(Boolean).join(', ')
    items.push(
      <Note key="conflict" tone="danger" icon={AlertTriangle} title="Registration conflict">
        A different installation{who ? ` (${who})` : ''} is claiming this agent&apos;s identity, usually after a
        VM was cloned. Resolve it in{' '}
        <Link to="/server-agents" className="font-medium text-primary hover:underline">Agent Fleet</Link>.
      </Note>,
    )
  }
  if (agent.authorization_state === 'pending') {
    items.push(
      <Note key="pending" tone="warning" icon={ShieldCheck} title="Awaiting authorization">
        The agent has registered but receives no API key until it is authorized in{' '}
        <Link to="/server-agents" className="font-medium text-primary hover:underline">Agent Fleet</Link>.
      </Note>,
    )
  } else if (agent.authorization_state === 'revoked') {
    items.push(
      <Note key="revoked" tone="danger" icon={ShieldCheck} title="Authorization revoked">
        The agent&apos;s API key no longer authenticates{agent.revoked_at ? ` (revoked ${relativeTime(agent.revoked_at)})` : ''}.
        Re-authorize it in{' '}
        <Link to="/server-agents" className="font-medium text-primary hover:underline">Agent Fleet</Link>.
      </Note>,
    )
  }
  if (agent.config_apply_error) {
    items.push(
      <Note key="config" tone="danger" icon={AlertTriangle} title="The agent could not apply its configuration">
        <span className="break-words">{agent.config_apply_error}</span>
      </Note>,
    )
  }
  if (!items.length) return null
  return <div className="space-y-2">{items}</div>
}

function AgentTiles({ agent, latest, updateAvailable, view }: {
  agent: AgentItem
  latest: string | null
  updateAvailable: string | null
  view: UpdateView | null
}) {
  const meta = AGENT_STATUS_META[agent.status] ?? AGENT_STATUS_META.error
  const skew = agent.clock_skew_s ?? 0
  const skewBad = Math.abs(skew) > CLOCK_SKEW_WARN_S
  const profile = profileMeta(agent.monitoring_profile)
  const apm = agent.monitoring_profile && agent.monitoring_profile !== 'infrastructure' ? apmSummary(agent.apm_status) : null
  const upgrading = view?.phase === 'progress'

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
      <StatTile
        icon={Activity}
        label="Status"
        value={meta.label}
        tone={variantTone(meta.variant)}
        sub={<span title={fmtDateTime(agent.last_heartbeat_at)}>Heartbeat {relativeTime(agent.last_heartbeat_at)}</span>}
      />
      <StatTile
        icon={Bot}
        label="Version"
        value={<span className="truncate">{agent.version || '—'}</span>}
        sub={
          upgrading
            ? <span className="text-info">{view?.title}</span>
            : updateAvailable
              ? <span className="text-info">Update available: {updateAvailable}</span>
              : !agent.version ? 'Not reported' : latest ? 'Up to date' : 'No package published here'
        }
      />
      <StatTile
        icon={Layers}
        label="Monitoring"
        value={<span className="truncate text-lg">{profile?.short ?? 'Not reported'}</span>}
        tone={profile ? 'default' : 'muted'}
        sub={
          apm
            ? <span>APM: <span className={cn(apm.tone === 'danger' && 'text-danger', apm.tone === 'success' && 'text-success', apm.tone === 'warning' && 'text-warning')}>{apm.label}</span></span>
            : agent.package_flavor === 'full' ? 'Full package (APM installed)' : agent.package_flavor === 'core' ? 'Server-monitoring package' : 'Agent 1.14.2+ reports this'
        }
      />
      <StatTile
        icon={ScrollText}
        label="Upload queue"
        value={agent.queue_depth.toLocaleString()}
        tone={agent.queue_depth > 1000 ? 'warning' : 'default'}
        sub={`${formatBytes(agent.spool_bytes)} spooled · metrics ${relativeTime(agent.last_metric_at)}`}
      />
      <StatTile
        icon={Clock3}
        label="Clock skew"
        value={Math.abs(skew) < 60 ? 'In sync' : fmtSkew(skew)}
        tone={skewBad ? 'warning' : Math.abs(skew) < 60 ? 'success' : 'default'}
        sub={skewBad ? 'Timestamps corrected at ingest; fix NTP' : `Offset ${fmtSkew(skew)} from the appliance`}
      />
    </div>
  )
}

/* ── Updates ─────────────────────────────────────────────────────── */

function UpdatesPanel({ agent, versions, latest, loading, view, busy, onUpgrade, onRingChanged }: {
  agent: AgentItem
  versions: string[]
  latest: string | null
  loading: boolean
  view: UpdateView | null
  busy: boolean
  onUpgrade: (version: string) => Promise<boolean>
  onRingChanged: () => void
}) {
  const [target, setTarget] = useState('')
  const [confirm, setConfirm] = useState(false)

  // Default to the newest version; keep the operator's choice while it stays published.
  useEffect(() => {
    if (versions.length && !versions.includes(target)) setTarget(versions[0])
  }, [versions.join(','), target])

  const setRing = useMutation({
    // The endpoint reads the ring from the query string; the body is kept
    // for older controllers that read it from there.
    mutationFn: async (ring: UpdateRing) =>
      (await api.post(`/agent-fleet/${agent.id}/set-update-ring`, { update_ring: ring }, { params: { ring } })).data,
    onSuccess: () => {
      toast.success('Update ring changed')
      onRingChanged()
    },
    onError: (e) => toast.error('Could not change update ring', apiErrorMessage(e)),
  })

  const inProgress = view?.phase === 'progress'
  const same = Boolean(target) && target === agent.version
  const older = Boolean(target && agent.version) && isNewer(agent.version, target)
  const disabledReason = !versions.length
    ? null
    : agent.status === 'disabled'
      ? 'The agent is disabled; enable it before upgrading.'
      : agent.authorization_state !== 'authorized'
        ? 'The agent must be authorized before it can be upgraded.'
        : inProgress
          ? 'A change is already in progress.'
          : same
            ? `${target} is already installed.`
            : null
  const ring = UPDATE_RINGS.find((r) => r.value === agent.update_ring)

  return (
    <Panel
      title="Updates"
      subtitle="Agent version and rollout ring"
      icon={ArrowUpCircle}
    >
      <div className="space-y-4">
        <KeyValues
          items={[
            ['Installed', <Mono key="v">{agent.version || '—'}</Mono>],
            ['Latest published', loading ? <Skeleton key="l" className="h-4 w-16" /> : latest
              ? (
                <span key="l" className="inline-flex items-center gap-1.5">
                  <Mono>{latest}</Mono>
                  {isNewer(latest, agent.version) ? <Pill tone="info">newer</Pill> : <Pill tone="success">current</Pill>}
                </span>
              )
              : <span key="l" className="text-muted">None for {agent.platform}</span>],
            agent.desired_version && agent.desired_version !== agent.version
              ? ['Requested', <Mono key="d">{agent.desired_version}</Mono>]
              : null,
          ]}
        />

        <div>
          <FieldLabel>Update ring</FieldLabel>
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={agent.update_ring}
              onValueChange={(v) => setRing.mutate(v as UpdateRing)}
              disabled={setRing.isPending}
            >
              <SelectTrigger className="h-8 w-[150px] text-xs" aria-label="Update ring"><SelectValue /></SelectTrigger>
              <SelectContent>
                {UPDATE_RINGS.map((r) => <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>)}
              </SelectContent>
            </Select>
            {setRing.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
            <span className="text-xs text-muted">{ring?.hint}</span>
          </div>
        </div>

        {view && <UpdateStatusLine view={view} />}

        <div className="border-t border-border/70 pt-4">
          <FieldLabel>Upgrade now</FieldLabel>
          {versions.length === 0 ? (
            <p className="text-xs text-muted">
              {loading ? 'Loading published versions…' : `No ${agent.platform} agent package is published on this appliance. Copy a release to the artifact store to offer upgrades.`}
            </p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Select value={target} onValueChange={setTarget}>
                  <SelectTrigger className="h-8 w-[170px] text-xs" aria-label="Target version" title="Versions published on this appliance">
                    <SelectValue placeholder="Version" />
                  </SelectTrigger>
                  <SelectContent>
                    {versions.map((v, i) => (
                      <SelectItem key={v} value={v}>
                        {v}{i === 0 ? ' (latest)' : ''}{v === agent.version ? ' (installed)' : ''}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button size="sm" disabled={!target || Boolean(disabledReason) || busy} onClick={() => setConfirm(true)}>
                  <ArrowUpCircle className="h-3.5 w-3.5" />
                  {older ? `Downgrade to ${target}` : `Upgrade to ${target || '…'}`}
                </Button>
              </div>
              <p className="mt-1.5 text-xs text-muted">
                {disabledReason ?? 'The agent downloads the package from this appliance and restarts once. Monitoring resumes automatically.'}
              </p>
            </>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={older ? 'Downgrade agent' : 'Upgrade agent'}
        description={
          <span className="block space-y-2">
            <span className="block">
              {older ? 'Downgrade' : 'Upgrade'} the agent on <span className="font-semibold text-text">{agent.hostname || agent.agent_uid}</span> from{' '}
              <span className="font-mono text-text">{agent.version || 'unknown'}</span> to{' '}
              <span className="font-mono text-text">{target}</span>?
            </span>
            <span className="block">
              It picks up the request on its next check-in, downloads the package from this appliance and restarts
              once. Progress appears in this panel.
            </span>
            {older && <span className="block text-warning">Older versions may lack features this appliance uses.</span>}
          </span>
        }
        confirmText={older ? 'Downgrade' : 'Upgrade'}
        loading={busy}
        onConfirm={async () => {
          if (await onUpgrade(target)) setConfirm(false)
        }}
      />
    </Panel>
  )
}

/* ── Monitoring profile ──────────────────────────────────────────── */

function ProfilePanel({ agent, capabilities, view, busy, onApply }: {
  agent: AgentItem
  capabilities: string[]
  view: UpdateView | null
  busy: boolean
  onApply: (profile: MonitoringProfile, removeInstrumentation: boolean) => Promise<boolean>
}) {
  const current = agent.monitoring_profile ?? null
  const [target, setTarget] = useState<MonitoringProfile>(current ?? 'infrastructure')
  const [removeInstr, setRemoveInstr] = useState(false)
  const [confirm, setConfirm] = useState(false)

  // Follow the agent when its reported profile changes.
  useEffect(() => {
    if (current) setTarget(current)
  }, [current])

  const supported = capabilities.includes(PROFILE_CAPABILITY)
  const isWindows = (agent.platform || '').toLowerCase() === 'windows'
  const inProgress = view?.phase === 'progress'
  const flavor = agent.package_flavor ?? null
  const needsFull = target !== 'infrastructure'
  const unchanged = target === current
  const apm = current && current !== 'infrastructure' ? apmSummary(agent.apm_status) : null
  const unusedApm = current === 'infrastructure' && flavor === 'full'

  const blocked = !isWindows
    ? 'Only Windows agents support monitoring-profile changes.'
    : !supported
      ? 'Upgrade the agent to 1.14.2 or later first. Earlier agents (and current-user installations) cannot change their profile remotely.'
      : agent.status === 'disabled'
        ? 'The agent is disabled; enable it before changing its profile.'
        : null

  const explanation: ReactNode = unchanged
    ? unusedApm
      ? 'The APM components are installed but unused. Applying Server monitoring only swaps to the smaller server-monitoring package.'
      : 'This is the agent’s current profile.'
    : needsFull && flavor !== 'full'
      ? 'The agent downloads the APM package (about 90 MB) from this appliance and installs it. Monitoring continues throughout; the agent restarts once. Instrument applications afterwards from APM.'
      : needsFull
        ? 'The APM components are already installed, so the agent switches profile without a package change.'
        : flavor === 'full'
          ? 'The agent replaces its package with the server-monitoring package, which removes the APM components. Monitoring continues throughout; the agent restarts once.'
          : 'The agent stops collecting application telemetry.'

  const canApply = !blocked && !inProgress && !busy && (!unchanged || unusedApm)

  return (
    <Panel title="Monitoring profile" subtitle="What this agent collects" icon={Layers}>
      <div className="space-y-4">
        <KeyValues
          items={[
            ['Current profile', current
              ? <span key="p">{profileMeta(current)?.label ?? current}</span>
              : <span key="p" className="text-muted">Not reported</span>],
            ['Installed package', flavor === 'full'
              ? <span key="f">Full <span className="text-muted">(with APM)</span></span>
              : flavor === 'core'
                ? <span key="f">Core <span className="text-muted">(server monitoring)</span></span>
                : <span key="f" className="text-muted">Not reported</span>],
            ['APM', apm
              ? <span key="a" title={agent.apm_status?.last_error || apm.detail}><Pill tone={apm.tone}>{apm.label}</Pill></span>
              : <span key="a" className="text-muted">{unusedApm ? 'Installed, unused' : 'Not in use'}</span>],
            apm ? ['APM detail', <span key="d" className="text-xs text-muted" title={apm.detail}>{apm.detail}</span>] : null,
          ]}
        />
        {apm && agent.apm_status?.last_error && (
          <Note tone="danger" icon={AlertTriangle} title="APM error">
            <span className="break-words">{agent.apm_status.last_error}</span>
          </Note>
        )}

        <div className="border-t border-border/70 pt-4">
          <FieldLabel>Change profile</FieldLabel>
          <div className="flex flex-wrap items-center gap-2">
            <Select value={target} onValueChange={(v) => setTarget(v as MonitoringProfile)} disabled={Boolean(blocked)}>
              <SelectTrigger className="h-8 w-[220px] text-xs" aria-label="Monitoring profile"><SelectValue /></SelectTrigger>
              <SelectContent>
                {MONITORING_PROFILES.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}{p.value === current ? ' (current)' : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button size="sm" disabled={!canApply} onClick={() => setConfirm(true)}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />} Apply
            </Button>
          </div>

          {blocked ? (
            <Note tone="warning" icon={Info} className="mt-2.5">{blocked}</Note>
          ) : (
            <div className="mt-2.5 space-y-2">
              <p className="text-xs text-muted">{profileMeta(target)?.hint}</p>
              <Note tone={unchanged && !unusedApm ? 'muted' : 'info'} icon={Info}>{explanation}</Note>
              {target === 'infrastructure' && (!unchanged || unusedApm) && (
                <label
                  className="flex cursor-pointer items-start gap-2 text-xs text-text"
                  title="Applications ZenPlus instrumented are restored to their original settings, which restarts them"
                >
                  <input
                    type="checkbox"
                    className="mt-0.5 h-3.5 w-3.5 cursor-pointer accent-primary"
                    checked={removeInstr}
                    onChange={(e) => setRemoveInstr(e.target.checked)}
                  />
                  <span>
                    Also remove APM instrumentation (restarts those apps)
                    <span className="block text-muted">
                      {removeInstr
                        ? 'Instrumented applications are restored to their original settings and restarted.'
                        : 'If any application is still instrumented, the agent leaves it unchanged and reports why.'}
                    </span>
                  </span>
                </label>
              )}
              {inProgress && <p className="text-xs text-muted">A change is already in progress; wait for it to finish.</p>}
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Change monitoring profile"
        description={
          <span className="block space-y-2">
            <span className="block">
              Switch <span className="font-semibold text-text">{agent.hostname || agent.agent_uid}</span> to{' '}
              <span className="font-semibold text-text">{profileMeta(target)?.label}</span>?
            </span>
            <span className="block">{explanation}</span>
            {target === 'infrastructure' && (
              <span className="block">
                {removeInstr
                  ? 'Applications instrumented by ZenPlus APM are restored to their original settings and restarted.'
                  : 'Applications instrumented by ZenPlus APM are left unchanged.'}
              </span>
            )}
          </span>
        }
        confirmText="Change profile"
        loading={busy}
        onConfirm={async () => {
          if (await onApply(target, removeInstr)) setConfirm(false)
        }}
      />
    </Panel>
  )
}

/* ── Details ─────────────────────────────────────────────────────── */

function DetailsPanel({ agent, capabilities, className }: {
  agent: AgentItem
  capabilities: string[]
  className?: string
}) {
  const certMs = parseTs(agent.certificate_expires_at)
  const certDays = Number.isNaN(certMs) ? null : (certMs - Date.now()) / 86_400_000
  const certTone = certDays == null ? 'muted' : certDays < 0 ? 'danger' : certDays < 30 ? 'warning' : 'default'

  return (
    <Panel title="Details" subtitle="Identity, enrollment and configuration" icon={Info} className={className}>
      <KeyValues
        columns={3}
        items={[
          ['Agent ID', <Mono key="id" title={agent.id}>{agent.id}</Mono>],
          ['Agent UID', <Mono key="uid" title={agent.agent_uid}>{agent.agent_uid}</Mono>],
          ['Hostname', agent.hostname || '—'],
          ['Authorization', (
            <span key="auth" className="inline-flex items-center gap-1.5">
              <AuthorizationBadge state={agent.authorization_state} source={agent.authorization_source} />
              {agent.authorized_at && agent.authorization_state === 'authorized' && (
                <span className="text-xs text-muted">{relativeTime(agent.authorized_at)}</span>
              )}
            </span>
          )],
          ['Platform', <span key="pl" className="capitalize">{agent.platform || '—'}</span>],
          ['Policy', agent.policy_name || 'Platform default'],
          ['Site', agent.site_name || '—'],
          ['Last IP', agent.last_ip ? <Mono key="ip">{agent.last_ip}</Mono> : '—'],
          ['API key', agent.api_key_prefix ? <Mono key="k">{agent.api_key_prefix}…</Mono> : '—'],
          ['Enrolled', <span key="en" title={fmtDateTime(agent.created_at)}>{relativeTime(agent.created_at)}</span>],
          ['Last heartbeat', <span key="hb" title={fmtDateTime(agent.last_heartbeat_at)}>{relativeTime(agent.last_heartbeat_at)}</span>],
          ['Last metrics', <span key="lm" title={fmtDateTime(agent.last_metric_at)}>{relativeTime(agent.last_metric_at)}</span>],
          ['Certificate expiry', agent.certificate_expires_at
            ? (
              <span key="cert" title={fmtDateTime(agent.certificate_expires_at)} className={cn(
                certTone === 'danger' && 'text-danger', certTone === 'warning' && 'text-warning',
              )}>
                {new Date(certMs).toLocaleDateString()} <span className="text-muted">· {certDays != null && certDays < 0 ? `expired ${relativeTime(agent.certificate_expires_at)}` : relativeEither(agent.certificate_expires_at)}</span>
              </span>
            )
            : '—'],
          ['Config hash', agent.last_config_hash
            ? <Mono key="h" title={agent.last_config_hash}>{agent.last_config_hash.slice(0, 16)}{agent.last_config_hash.length > 16 ? '…' : ''}</Mono>
            : '—'],
          ['Config apply', agent.config_apply_error
            ? <span key="ce" className="text-danger" title={agent.config_apply_error}>Error: {agent.config_apply_error}</span>
            : <span key="ce" className="text-success">Applied</span>],
        ]}
      />
      <div className="mt-4 border-t border-border/70 pt-3">
        <FieldLabel>Capabilities</FieldLabel>
        {capabilities.length ? (
          <div className="flex flex-wrap gap-1.5">
            {[...capabilities].sort().map((c) => (
              <span key={c} className="rounded-md border border-border bg-surface2/60 px-1.5 py-0.5 font-mono text-[11px] text-text2">
                {c}
              </span>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted">This agent does not advertise capabilities (versions before 1.2).</p>
        )}
      </div>
    </Panel>
  )
}

/* ── Actions ─────────────────────────────────────────────────────── */

function ActionsPanel({ server, agent, onDownloadAgent, onDone }: {
  server: ServerItem
  agent: AgentItem
  onDownloadAgent: () => void
  onDone: () => void
}) {
  const [confirmRotate, setConfirmRotate] = useState(false)
  const [pending, setPending] = useState<string | null>(null)

  const act = useMutation({
    mutationFn: async ({ url }: { key: string; url: string; label: string; detail: string }) => (await api.post(url)).data,
    onMutate: (v) => setPending(v.key),
    onSuccess: (d, v) => {
      // The command endpoint reports when an identical command is already queued.
      const r = d as { detail?: string; queued?: boolean } | undefined
      toast.success(r?.queued === false ? 'Already queued' : `${v.label} requested`, r?.detail ?? v.detail)
      onDone()
    },
    onError: (e, v) => toast.error(`${v.label} failed`, apiErrorMessage(e)),
    onSettled: () => setPending(null),
  })

  const usable = agent.status !== 'disabled' && agent.authorization_state === 'authorized'
  const base = `/agent-fleet/${agent.id}`
  const rows: {
    key: string; icon: Icon; title: string; hint: string; button: string; run: () => void; needsAgent?: boolean
  }[] = [
    {
      key: 'collect', icon: Zap, title: 'Collect now', button: 'Collect',
      hint: 'Collect and upload metrics immediately instead of waiting for the interval.',
      run: () => act.mutate({ key: 'collect', url: `${base}/commands/collect_now`, label: 'Collect now', detail: 'The agent picks it up on its next poll' }),
      needsAgent: true,
    },
    {
      key: 'refresh', icon: RefreshCw, title: 'Refresh configuration', button: 'Refresh',
      hint: 'Re-pull the policy from the appliance and apply it.',
      run: () => act.mutate({ key: 'refresh', url: `${base}/commands/refresh_config`, label: 'Config refresh', detail: 'The agent picks it up on its next poll' }),
      needsAgent: true,
    },
    {
      key: 'verify', icon: Stethoscope, title: 'Verify agent', button: 'Verify',
      hint: 'Request a status report to confirm the agent is responsive and healthy.',
      run: () => act.mutate({ key: 'verify', url: `${base}/commands/status`, label: 'Status report', detail: 'The result appears in the command history' }),
      needsAgent: true,
    },
    {
      key: 'diag', icon: FileDown, title: 'Request diagnostics', button: 'Request',
      hint: 'The agent uploads a support bundle (logs and configuration) on its next check-in.',
      run: () => act.mutate({ key: 'diag', url: `${base}/request-diagnostics`, label: 'Diagnostics upload', detail: 'The agent uploads a support bundle on its next check-in' }),
      needsAgent: true,
    },
    {
      key: 'cert', icon: ShieldCheck, title: 'Rotate certificate', button: 'Rotate',
      hint: 'Issue a new client certificate; the agent swaps to it on its next check-in.',
      run: () => setConfirmRotate(true),
      needsAgent: true,
    },
    {
      key: 'key', icon: KeyRound, title: 'Enrollment key / installer', button: 'Open',
      hint: `Download the agent or generate a new enrollment key to reinstall on ${server.display_name}.`,
      run: onDownloadAgent,
    },
  ]

  return (
    <Panel title="Actions" subtitle="Queued for the agent's next poll (≤30s)" icon={Settings2} bodyClassName="p-0">
      <ul className="divide-y divide-border/70">
        {rows.map((r) => {
          const disabled = (r.needsAgent && !usable) || act.isPending
          return (
            <li key={r.key} className="flex items-center gap-3 px-4 py-2.5">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface2 text-muted">
                <r.icon className="h-3.5 w-3.5" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium text-text">{r.title}</div>
                <div className="text-xs leading-4 text-muted">{r.hint}</div>
              </div>
              <Button variant="outline" size="sm" className="h-7 shrink-0 px-2.5" disabled={disabled} onClick={r.run}>
                {pending === r.key && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {r.button}
              </Button>
            </li>
          )
        })}
      </ul>
      {!usable && (
        <div className="border-t border-border/70 px-4 py-2.5 text-xs text-muted">
          {agent.status === 'disabled'
            ? 'The agent is disabled; enable it in Agent Fleet to send commands.'
            : 'The agent must be authorized before it accepts commands.'}
        </div>
      )}

      <ConfirmDialog
        open={confirmRotate}
        onOpenChange={setConfirmRotate}
        title="Rotate certificate"
        description={
          <>
            Issue a new client certificate for <span className="font-semibold text-text">{agent.hostname || agent.agent_uid}</span>?
            The agent switches to it on its next check-in; the current certificate keeps working until then.
          </>
        }
        confirmText="Rotate"
        loading={act.isPending && pending === 'cert'}
        onConfirm={() => {
          act.mutate({ key: 'cert', url: `${base}/rotate-certificate`, label: 'Certificate rotation', detail: 'The agent picks it up on its next check-in' })
          setConfirmRotate(false)
        }}
      />
    </Panel>
  )
}

/* ── Command history ─────────────────────────────────────────────── */

function commandTone(status: string): { tone: Tone; label: string; pulse?: boolean } {
  switch (status) {
    case 'succeeded': return { tone: 'success', label: 'Succeeded' }
    case 'failed': return { tone: 'danger', label: 'Failed' }
    case 'queued': return { tone: 'info', label: 'Queued' }
    case 'sent': return { tone: 'warning', label: 'Sent', pulse: true }
    case 'running': return { tone: 'warning', label: 'Running', pulse: true }
    case 'expired': return { tone: 'muted', label: 'Expired' }
    default: return { tone: 'muted', label: status.charAt(0).toUpperCase() + status.slice(1) }
  }
}

function commandParams(c: ServerCommand): string | null {
  const p = c.params
  if (!p || typeof p !== 'object' || !Object.keys(p).length) return null
  if (typeof p.version === 'string') return `→ ${p.version}`
  if (typeof p.monitoring_profile === 'string') return `→ ${profileMeta(p.monitoring_profile as MonitoringProfile)?.short ?? p.monitoring_profile}`
  const s = JSON.stringify(p)
  return s.length > 60 ? `${s.slice(0, 57)}…` : s
}

function CommandHistory({ serverId }: { serverId: string }) {
  const [showAll, setShowAll] = useState(false)
  const { data, isLoading, isError, error, refetch } = useQuery<{ items: ServerCommand[] }>({
    queryKey: ['servers', serverId, 'commands'],
    queryFn: async () => (await api.get(`/servers/${serverId}/commands`)).data,
    refetchInterval: 15_000,
  })
  const items = data?.items ?? []
  const visible = showAll ? items : items.slice(0, COMMANDS_VISIBLE)
  const failed = items.filter((c) => c.status === 'failed').length
  const open = items.filter((c) => ['queued', 'sent', 'running'].includes(c.status)).length

  return (
    <Panel
      title="Command history"
      subtitle={
        items.length
          ? `${items.length} command${items.length === 1 ? '' : 's'}${open ? ` · ${open} pending` : ''}${failed ? ` · ${failed} failed` : ''}`
          : 'Commands sent to this agent'
      }
      icon={ScrollText}
      flush
      actions={items.length > COMMANDS_VISIBLE && (
        <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show recent' : `Show all ${items.length}`}
        </Button>
      )}
    >
      <div className="overflow-x-auto">
        <Table>
          <THead className="bg-surface2/40">
            <Tr>
              <Th className="pl-4">Command</Th>
              <Th>Status</Th>
              <Th>Requested by</Th>
              <Th>Created</Th>
              <Th className="pr-4">Completed</Th>
            </Tr>
          </THead>
          <TBody>
            {isLoading && Array.from({ length: 3 }).map((_, i) => (
              <TableStateRow key={i} colSpan={5}><Skeleton className="h-5 w-full" /></TableStateRow>
            ))}
            {!isLoading && isError && (
              <TableStateRow colSpan={5}><QueryError error={error} onRetry={() => refetch()} /></TableStateRow>
            )}
            {!isLoading && !isError && items.length === 0 && (
              <TableStateRow colSpan={5}>
                <div className="py-8 text-center text-xs text-muted">
                  No commands sent yet. Actions above queue commands the agent picks up on its next poll (≤30s).
                </div>
              </TableStateRow>
            )}
            {visible.map((c) => {
              const st = commandTone(c.status)
              const params = commandParams(c)
              return (
                <Tr key={c.id}>
                  <Td className="py-2 pl-4 align-top">
                    <div className="font-mono text-xs text-text">{c.command}</div>
                    {params && <div className="font-mono text-[11px] text-muted">{params}</div>}
                  </Td>
                  <Td className="py-2 align-top">
                    <Pill tone={st.tone} pulse={st.pulse}>{st.label}</Pill>
                    {c.status === 'failed' && c.error_message && (
                      <div className="mt-1 max-w-[360px] break-words text-xs text-danger">{c.error_message}</div>
                    )}
                  </Td>
                  <Td className="py-2 align-top text-xs text-muted">{c.requested_by_name || 'system'}</Td>
                  <Td className="py-2 align-top text-xs tabular-nums text-muted" title={fmtDateTime(c.created_at)}>
                    {relativeTime(c.created_at)}
                  </Td>
                  <Td className="py-2 pr-4 align-top text-xs tabular-nums text-muted" title={c.completed_at ? fmtDateTime(c.completed_at) : undefined}>
                    {c.completed_at ? relativeTime(c.completed_at) : '—'}
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      </div>
    </Panel>
  )
}

/* ── Settings section ────────────────────────────────────────────── */

function SettingsSection({ server, onEdit, onDecommission, onDelete }: {
  server: ServerItem
  onEdit: () => void
  onDecommission: () => void
  onDelete: () => void
}) {
  const os = [server.os_name || server.os_type, server.os_version].filter(Boolean).join(' ')
  const decommissioned = server.status === 'disabled'

  return (
    <div className="space-y-4">
      <Panel
        title="Server record"
        subtitle="How this server is identified and organised"
        icon={Settings2}
        actions={(
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
        )}
      >
        <KeyValues
          columns={3}
          items={[
            ['Display name', server.display_name],
            ['Environment', server.environment || '—'],
            ['Owner', server.owner || '—'],
            ['Site', server.site_name || '—'],
            ['Collection mode', COLLECTION_MODE_LABEL[server.collection_mode] ?? server.collection_mode],
            ['Registered', <span key="reg" title={fmtDateTime(server.created_at)}>{new Date(parseTs(server.created_at)).toLocaleDateString()}</span>],
            ['Hostname', server.hostname || '—'],
            ['FQDN', server.fqdn || '—'],
            ['Primary IP', server.primary_ip ? <Mono key="ip">{server.primary_ip}</Mono> : '—'],
            ['Operating system', os || '—'],
            ['Build', server.kernel_or_build || '—'],
            ['Architecture', server.architecture || '—'],
            ['Uptime', server.boot_time
              ? <span key="up" title={`Booted ${fmtDateTime(server.boot_time)}`}>{formatUptime(server.boot_time)}</span>
              : '—'],
          ]}
        />
        <div className="mt-4 grid gap-4 border-t border-border/70 pt-3 md:grid-cols-3">
          <div className="min-w-0">
            <FieldLabel>Tags</FieldLabel>
            {server.tags?.length ? <TagList tags={server.tags} max={12} /> : <span className="text-muted">No tags</span>}
          </div>
          <div className="min-w-0 md:col-span-2">
            <FieldLabel>Description</FieldLabel>
            {server.description
              ? <p className="whitespace-pre-wrap break-words text-[13px] text-text">{server.description}</p>
              : <span className="text-muted">No description</span>}
          </div>
        </div>
      </Panel>

      <Panel
        title="Danger zone"
        subtitle="These actions affect monitoring and history"
        icon={AlertTriangle}
        className="border-danger/30"
        bodyClassName="p-0"
      >
        <div className="divide-y divide-border/70">
          <div className="flex flex-wrap items-center gap-3 px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-text">Decommission server</div>
              <p className="text-xs text-muted">
                {decommissioned
                  ? 'This server is already decommissioned. Monitoring and alerting are off; its history is kept.'
                  : 'Stops monitoring and alerting for this server but keeps the record, inventory and history for reference.'}
              </p>
            </div>
            <Button
              variant="outline" size="sm"
              className="border-danger/40 text-danger hover:bg-danger/10 hover:text-danger"
              onClick={onDecommission}
              disabled={decommissioned}
            >
              <CloudOff className="h-3.5 w-3.5" /> Decommission
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-3 px-4 py-3.5">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-text">Delete server</div>
              <p className="text-xs text-muted">
                Permanently removes the server, its inventory and its alert history. This cannot be undone.
              </p>
            </div>
            <Button variant="destructive" size="sm" onClick={onDelete}>
              <Trash2 className="h-3.5 w-3.5" /> Delete server
            </Button>
          </div>
        </div>
      </Panel>
    </div>
  )
}
