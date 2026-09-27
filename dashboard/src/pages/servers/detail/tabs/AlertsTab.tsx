/** Alerts tab — this server's alerts, silences and host alert rules.
 *
 *  Alerts can be acknowledged, snoozed/muted and resolved; silences can be
 *  lifted; rules (global and server-specific) can be toggled, and
 *  server-specific ones created and deleted. */

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, BellOff, BellRing, Check, CheckCheck, Plus, Search, Settings2, ShieldAlert, Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { QueryError, TableStateRow } from '@/components/servers/tables'
import type { ServerItem } from '@/types/servers'
import { EmptyState, Panel, Pill, Segmented, StatTile, type Tone } from '../ui'

/* ── Types and labels ────────────────────────────────────────────── */

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

export interface HostRule {
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

/** Pre-fills the new-rule dialog, e.g. from a service or process row. */
export type HostRulePrefill = {
  metric?: string
  target?: string
  name?: string
  severity?: string
}

interface Silence { id: string; dedupe: string; until: string | null; forever: boolean }

export const HOST_METRICS = [
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

export function metricLabel(metric: string): string {
  return HOST_METRICS.find((m) => m.value === metric)?.label || metric
}

export function ruleCondition(r: HostRule): string {
  const m = HOST_METRICS.find((x) => x.value === r.metric)
  if (m?.kind === 'svc') return `Service ${r.target || '?'} stopped`
  if (m?.kind === 'proc') return `Process ${r.target || '?'} not running`
  return `${metricLabel(r.metric)} ${OP_SYMBOL[r.operator] || r.operator} ${r.threshold}%`
}

function sevTone(sev: string): Tone {
  return sev === 'critical' ? 'danger' : sev === 'warning' ? 'warning' : 'info'
}

function SevPill({ sev }: { sev: string }) {
  const label = sev ? sev.charAt(0).toUpperCase() + sev.slice(1) : 'Info'
  return <Pill tone={sevTone(sev)}>{label}</Pill>
}

function StatePill({ status }: { status: string }) {
  if (status === 'active') return <Pill tone="danger" pulse>Active</Pill>
  if (status === 'acknowledged') return <Pill tone="muted">Acknowledged</Pill>
  return <Pill tone="success">Resolved</Pill>
}

function fmtDuration(seconds: number): string {
  if (!seconds) return 'immediately'
  if (seconds < 60) return `for ${seconds}s`
  const m = Math.round(seconds / 60)
  return m >= 60 && m % 60 === 0 ? `for ${m / 60}h` : `for ${m} min`
}

type StatusFilter = 'active' | 'acknowledged' | 'resolved' | 'all'

const SNOOZE_OPTIONS = [
  { value: '60', label: '1 hour' },
  { value: '240', label: '4 hours' },
  { value: '1440', label: '24 hours' },
  { value: 'forever', label: 'Mute forever' },
] as const

/* ── Tab ─────────────────────────────────────────────────────────── */

export function AlertsTab({ server }: { server: ServerItem }) {
  const serverId = server.id
  const serverStatus = server.status
  const statusReasons = server.status_reasons ?? []
  const qc = useQueryClient()
  const [status, setStatus] = useState<StatusFilter>('active')
  const [search, setSearch] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [deleting, setDeleting] = useState<HostRule | null>(null)

  const alertsQ = useQuery<{ data: ServerAlert[]; meta: { total: number } }>({
    queryKey: ['servers', serverId, 'alerts', status],
    queryFn: async () => (await api.get('/alerts', {
      params: { server_id: serverId, limit: 100, ...(status !== 'all' ? { status } : {}) },
    })).data,
    refetchInterval: 15_000,
    placeholderData: (prev) => prev,
  })
  const alerts = alertsQ.data?.data || []

  // Open = active + acknowledged, for the tiles and counts whatever the filter.
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

  const rulesQ = useQuery<{ items: HostRule[] }>({
    queryKey: ['servers', serverId, 'host-rules'],
    queryFn: async () => (await api.get('/host-alert-rules', { params: { server_id: serverId } })).data,
  })
  const rules = rulesQ.data?.items || []

  const { data: silencesResp } = useQuery<{ items: Silence[] }>({
    queryKey: ['servers', serverId, 'silences'],
    queryFn: async () => (await api.get('/alerts/silences', { params: { server_id: serverId } })).data,
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
    onSuccess: () => { invalidate(); setDeleting(null); toast.success('Rule deleted') },
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
    open: openAlerts.length,
    active: openAlerts.filter((a) => a.status === 'active').length,
    acknowledged: openAlerts.filter((a) => a.status === 'acknowledged').length,
    critical: openAlerts.filter((a) => a.severity === 'critical').length,
    warning: openAlerts.filter((a) => a.severity === 'warning').length,
  }
  const rulesEnabled = rules.filter((r) => r.enabled).length
  const serverRules = rules.filter((r) => r.server_id).length

  const unhealthy = serverStatus === 'warning' || serverStatus === 'critical'
  const healthOnlyReasons = statusReasons.length > 0 && unhealthy && openAlerts.length === 0

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    const rows = q ? alerts.filter((a) => a.message.toLowerCase().includes(q) || a.severity.includes(q)) : alerts
    // Critical first within the open states; otherwise newest first.
    const rank = (s: string) => (s === 'critical' ? 0 : s === 'warning' ? 1 : 2)
    return [...rows].sort((a, b) =>
      (a.status === 'resolved' || b.status === 'resolved' ? 0 : rank(a.severity) - rank(b.severity))
      || Date.parse(b.triggered_at) - Date.parse(a.triggered_at))
  }, [alerts, search])

  const silenceLabel = (s: Silence) => {
    const rid = s.dedupe.startsWith('rule:') ? s.dedupe.slice(5) : null
    if (rid) return rules.find((r) => r.id === rid)?.name || 'Alert rule'
    if (s.dedupe === 'agent_offline') return 'Agent offline'
    if (s.dedupe.startsWith('health:')) return 'Health threshold'
    return s.dedupe
  }

  const statusItems = [
    { key: 'active' as const, label: 'Active', count: counts.active, tone: 'danger' as const },
    { key: 'acknowledged' as const, label: 'Acknowledged', count: counts.acknowledged, tone: 'muted' as const },
    { key: 'resolved' as const, label: 'Resolved' },
    { key: 'all' as const, label: 'All' },
  ]

  return (
    <div className="space-y-4">
      {/* Summary */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          icon={BellRing}
          label="Active"
          value={counts.active}
          tone={counts.active > 0 ? 'danger' : 'default'}
          sub={counts.open ? `${counts.open} open · ${counts.warning} warning` : 'nothing firing'}
          onClick={() => setStatus('active')}
          active={status === 'active'}
        />
        <StatTile
          icon={ShieldAlert}
          label="Critical"
          value={counts.critical}
          tone={counts.critical > 0 ? 'danger' : 'default'}
          sub="open, active or acknowledged"
        />
        <StatTile
          icon={CheckCheck}
          label="Acknowledged"
          value={counts.acknowledged}
          tone={counts.acknowledged > 0 ? 'warning' : 'default'}
          sub={silences.length ? `${silences.length} snoozed or muted` : 'still open until resolved'}
          onClick={() => setStatus('acknowledged')}
          active={status === 'acknowledged'}
        />
        <StatTile
          icon={Settings2}
          label="Rules enabled"
          value={rulesQ.isLoading ? '—' : <>{rulesEnabled}<span className="text-sm font-medium text-muted">/{rules.length}</span></>}
          sub={`${serverRules} server-specific · ${rules.length - serverRules} global`}
          onClick={() => document.getElementById('alert-rules')?.scrollIntoView({ behavior: 'smooth' })}
        />
      </div>

      {/* Health context */}
      {unhealthy && statusReasons.length > 0 && (
        <div
          className={cn(
            'rounded-xl border p-4',
            serverStatus === 'critical' ? 'border-danger/30 bg-danger/5' : 'border-warning/30 bg-warning/5',
          )}
        >
          <div className="mb-1 flex items-center gap-2 text-[13px] font-semibold text-text">
            <AlertTriangle className={cn('h-4 w-4', serverStatus === 'critical' ? 'text-danger' : 'text-warning')} />
            Server health: {serverStatus}
          </div>
          <p className="mb-2 text-xs text-muted">
            Health status comes from live telemetry (CPU, memory, disk, services). It stays {serverStatus} until
            the underlying issue is fixed — acknowledging alerts does not clear health status.
            {healthOnlyReasons && ' No alert rows exist yet for these conditions — they will appear after the next health evaluation.'}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {statusReasons.map((r) => (
              <Pill key={r} tone={serverStatus === 'critical' ? 'danger' : 'warning'}>{r}</Pill>
            ))}
          </div>
        </div>
      )}

      {/* Alerts */}
      <Panel
        icon={BellRing}
        title="Alerts"
        subtitle={`${counts.active} active · ${counts.acknowledged} acknowledged · ${counts.critical} critical · ${counts.warning} warning`}
        flush
        actions={<Segmented size="sm" items={statusItems} value={status} onChange={setStatus} />}
      >
        {alerts.length > 5 && (
          <div className="border-b border-border/60 px-4 py-2.5">
            <div className="relative w-full sm:w-64">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter alerts…"
                className="h-8 pl-8 text-[13px]"
              />
            </div>
          </div>
        )}
        <Table className="text-[13px]">
          <THead className="bg-surface2/40">
            <Tr className="hover:bg-transparent">
              <Th className="h-9 w-[110px] pl-4">Severity</Th>
              <Th className="h-9">Alert</Th>
              <Th className="h-9 w-[130px]">State</Th>
              <Th className="h-9 w-[120px]">Triggered</Th>
              <Th className="h-9 w-[250px] pr-4 text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {alertsQ.isError ? (
              <TableStateRow colSpan={5}><QueryError error={alertsQ.error} onRetry={() => alertsQ.refetch()} /></TableStateRow>
            ) : alertsQ.isLoading ? (
              <TableStateRow colSpan={5}><div className="py-6"><Skeleton className="h-16 w-full" /></div></TableStateRow>
            ) : shown.length === 0 ? (
              <TableStateRow colSpan={5}>
                <EmptyState
                  compact
                  icon={Check}
                  title={search ? 'No alerts match the filter' : `No ${status === 'all' ? '' : `${status} `}alerts`}
                  hint={
                    search ? undefined
                      : status === 'active' && counts.acknowledged > 0 ? (
                        <>
                          {counts.acknowledged} acknowledged alert{counts.acknowledged === 1 ? '' : 's'} still open —{' '}
                          <button type="button" className="font-medium text-primary hover:underline" onClick={() => setStatus('acknowledged')}>
                            show acknowledged
                          </button>
                        </>
                      ) : status === 'active' ? (
                        <>
                          This server is quiet.{' '}
                          <button type="button" className="font-medium text-primary hover:underline" onClick={() => setStatus('all')}>
                            Show resolved history
                          </button>
                        </>
                      ) : undefined
                  }
                />
              </TableStateRow>
            ) : shown.map((a) => (
              <Tr key={a.id} className="border-border/60 hover:bg-surface2/40">
                <Td className="py-2 pl-4"><SevPill sev={a.severity} /></Td>
                <Td className="max-w-0 py-2">
                  <div className="truncate font-medium text-text" title={a.message}>{a.message}</div>
                  {(a.acknowledged_at || a.resolved_at) && (
                    <div className="truncate text-xs text-muted">
                      {a.resolved_at
                        ? `Resolved ${relativeTime(a.resolved_at)}`
                        : `Acknowledged ${relativeTime(a.acknowledged_at)}`}
                    </div>
                  )}
                </Td>
                <Td className="py-2"><StatePill status={a.status} /></Td>
                <Td className="whitespace-nowrap py-2 text-xs tabular-nums text-muted" title={new Date(a.triggered_at).toLocaleString()}>
                  {relativeTime(a.triggered_at)}
                </Td>
                <Td className="py-2 pr-4">
                  <div className="flex items-center justify-end gap-1.5">
                    {a.status === 'active' && (
                      <Button
                        size="sm" variant="outline" className="h-7 px-2.5"
                        disabled={ack.isPending && ack.variables === a.id}
                        onClick={() => ack.mutate(a.id)}
                      >
                        Ack
                      </Button>
                    )}
                    {a.status !== 'resolved' && a.server_id && (
                      <Select value="" onValueChange={(v) => snooze.mutate({ id: a.id, minutes: v === 'forever' ? null : Number(v) })}>
                        <SelectTrigger className="h-7 w-[96px] px-2.5 text-xs" aria-label="Snooze or mute">
                          <SelectValue placeholder={<span className="inline-flex items-center gap-1"><BellOff className="h-3 w-3" /> Snooze</span>} />
                        </SelectTrigger>
                        <SelectContent align="end">
                          {SNOOZE_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
                        </SelectContent>
                      </Select>
                    )}
                    {a.status !== 'resolved' && (
                      <Button
                        size="sm" variant="outline" className="h-7 px-2.5"
                        disabled={resolve.isPending && resolve.variables === a.id}
                        onClick={() => resolve.mutate(a.id)}
                      >
                        Resolve
                      </Button>
                    )}
                    {a.status === 'resolved' && <span className="text-xs text-muted">—</span>}
                  </div>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
        {(alertsQ.data?.meta?.total ?? 0) > alerts.length && (
          <div className="border-t border-border/60 px-4 py-2 text-xs text-muted">
            Showing the latest {alerts.length} of {alertsQ.data?.meta.total.toLocaleString()} alerts.
          </div>
        )}
      </Panel>

      {/* Snoozed / muted conditions */}
      {silences.length > 0 && (
        <Panel icon={BellOff} title="Snoozed & muted" subtitle={`${silences.length} condition${silences.length === 1 ? '' : 's'}`} flush>
          <ul className="divide-y divide-border/60">
            {silences.map((s) => (
              <li key={s.id} className="flex items-center gap-3 px-4 py-2 text-[13px] hover:bg-surface2/40">
                <Pill tone="muted">{s.forever ? 'Muted' : 'Snoozed'}</Pill>
                <span className="min-w-0 truncate font-medium" title={s.dedupe}>{silenceLabel(s)}</span>
                <span className="shrink-0 text-xs text-muted" title={s.until ? new Date(s.until).toLocaleString() : undefined}>
                  {s.forever ? 'until removed' : `until ${s.until ? new Date(s.until).toLocaleString() : ''}`}
                </span>
                <Button
                  size="sm" variant="ghost" className="ml-auto h-7 px-2.5 text-xs"
                  disabled={removeSilence.isPending && removeSilence.variables === s.id}
                  onClick={() => removeSilence.mutate(s.id)}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {/* Alert rules */}
      <Panel
        id="alert-rules"
        icon={Settings2}
        title="Alert rules"
        subtitle={`Global and server-specific · ${rulesEnabled} of ${rules.length} enabled`}
        flush
        actions={(
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-3.5 w-3.5" /> New rule
          </Button>
        )}
      >
        <Table className="text-[13px]">
          <THead className="bg-surface2/40">
            <Tr className="hover:bg-transparent">
              <Th className="h-9 w-16 pl-4">On</Th>
              <Th className="h-9">Name</Th>
              <Th className="h-9">Condition</Th>
              <Th className="h-9 w-[110px]">Severity</Th>
              <Th className="h-9 w-[120px]">Scope</Th>
              <Th className="h-9 w-[90px] pr-4 text-right">Actions</Th>
            </Tr>
          </THead>
          <TBody>
            {rulesQ.isError ? (
              <TableStateRow colSpan={6}><QueryError error={rulesQ.error} onRetry={() => rulesQ.refetch()} /></TableStateRow>
            ) : rulesQ.isLoading ? (
              <TableStateRow colSpan={6}><div className="py-6"><Skeleton className="h-16 w-full" /></div></TableStateRow>
            ) : rules.length === 0 ? (
              <TableStateRow colSpan={6}>
                <EmptyState
                  compact
                  icon={Settings2}
                  title="No host alert rules"
                  hint="Rules alert on CPU, memory, filesystem or disk thresholds, or on a stopped service or process."
                  action={(
                    <Button size="sm" variant="outline" onClick={() => setCreateOpen(true)}>
                      <Plus className="h-3.5 w-3.5" /> New rule
                    </Button>
                  )}
                />
              </TableStateRow>
            ) : rules.map((r) => (
              <Tr key={r.id} className={cn('border-border/60 hover:bg-surface2/40', !r.enabled && 'opacity-60')}>
                <Td className="py-2 pl-4">
                  <Switch
                    checked={r.enabled}
                    disabled={toggleRule.isPending && toggleRule.variables === r.id}
                    onCheckedChange={() => toggleRule.mutate(r.id)}
                    aria-label={`${r.enabled ? 'Disable' : 'Enable'} ${r.name}`}
                  />
                </Td>
                <Td className="max-w-[240px] truncate py-2 font-medium" title={r.name}>{r.name}</Td>
                <Td className="py-2">
                  <div className="truncate text-text2" title={ruleCondition(r)}>{ruleCondition(r)}</div>
                  {HOST_METRICS.find((m) => m.value === r.metric)?.kind === 'pct' && (
                    <div className="text-xs text-muted">{fmtDuration(r.min_duration)}</div>
                  )}
                </Td>
                <Td className="py-2"><SevPill sev={r.severity} /></Td>
                <Td className="py-2 text-xs text-muted">{r.server_id ? 'This server' : 'All servers'}</Td>
                <Td className="py-2 pr-4 text-right">
                  {r.server_id ? (
                    <Button
                      size="sm" variant="ghost" className="h-7 w-7 p-0 text-muted hover:text-danger"
                      aria-label={`Delete ${r.name}`} title="Delete rule"
                      onClick={() => setDeleting(r)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  ) : (
                    <span className="text-xs text-muted" title="Global rules are managed on the Alerts page">global</span>
                  )}
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </Panel>

      <CreateHostRuleDialog
        serverId={serverId}
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={invalidate}
      />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => { if (!o) setDeleting(null) }}
        title="Delete alert rule?"
        description={deleting ? <>“{deleting.name}” ({ruleCondition(deleting)}) will stop alerting on this server.</> : undefined}
        confirmText="Delete rule"
        destructive
        loading={delRule.isPending}
        onConfirm={() => { if (deleting) delRule.mutate(deleting.id) }}
      />
    </div>
  )
}

/* ── New rule dialog ─────────────────────────────────────────────── */

/** Exported so other tabs (services, processes) can open it pre-filled. */
export function CreateHostRuleDialog({
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

  const reset = () => {
    setName(''); setMetric('host_cpu_pct'); setOperator('gt'); setThreshold('90')
    setSeverity('warning'); setMinutes('5'); setTarget(''); setAllServers(false); setChannels([])
  }

  // Apply the prefill (e.g. from a service row) each time the dialog opens.
  useEffect(() => {
    if (!open) return
    reset()
    if (initial) {
      setName(initial.name || '')
      setMetric(initial.metric || 'host_cpu_pct')
      setTarget(initial.target || '')
      setSeverity(initial.severity || 'warning')
    }
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

  const toggleChannel = (id: string) =>
    setChannels((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))

  const preview = kind === 'svc'
    ? `Service ${target.trim() || '…'} stopped`
    : kind === 'proc'
      ? `Process ${target.trim() || '…'} not running`
      : `${metricLabel(metric)} ${OP_SYMBOL[operator]} ${threshold || '…'}% ${fmtDuration(Math.round(Number(minutes || 0) * 60))}`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>New alert rule</DialogTitle></DialogHeader>
        <form
          className="space-y-3"
          onSubmit={(e) => { e.preventDefault(); create.mutate() }}
        >
          <div className="space-y-1.5">
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={`e.g. ${metricLabel(metric)} alert`} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Metric</Label>
              <Select value={metric} onValueChange={(v) => { setMetric(v); if (v === 'host_filesystem_pct') setThreshold('85') }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {HOST_METRICS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
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
              <div className="space-y-1.5">
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
              <div className="space-y-1.5">
                <Label>Threshold %</Label>
                <Input type="number" min={0} max={100} value={threshold} onChange={(e) => setThreshold(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label>For (min)</Label>
                <Input type="number" min={0} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
              </div>
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label>{kind === 'svc' ? 'Service name' : 'Process name'}</Label>
              <Input
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                placeholder={kind === 'svc' ? 'e.g. MSSQLSERVER' : 'e.g. nginx.exe'}
                required
              />
            </div>
          )}

          <div className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">
            Alerts when <span className="font-medium text-text">{preview}</span> on{' '}
            <span className="font-medium text-text">{allServers ? 'any server' : 'this server'}</span>.
          </div>

          <label className="flex cursor-pointer items-center gap-2">
            <Switch checked={allServers} onCheckedChange={setAllServers} />
            <span className="text-[13px]">Apply to all servers (not just this one)</span>
          </label>

          {allChannels.length > 0 && (
            <div className="space-y-1.5">
              <Label>Notify channels</Label>
              <div className="flex flex-wrap gap-1.5">
                {allChannels.map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => toggleChannel(c.id)}
                    aria-pressed={channels.includes(c.id)}
                    className={cn(
                      'rounded-md border px-2 py-1 text-xs transition-colors',
                      channels.includes(c.id)
                        ? 'border-primary bg-primary/10 text-primary'
                        : 'border-border text-muted hover:text-text',
                    )}
                  >
                    {c.name} <span className="opacity-60">({c.type})</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? 'Creating…' : 'Create rule'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
