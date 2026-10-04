/**
 * Shared pieces for the alert surfaces (Alert Center, alert detail, device
 * pages): channel icons, status / severity / entity presentation, delivery
 * outcome, and the triage dialogs (snooze, resolve, assign).
 *
 * Semantics (Alerts review, "Professional UI and UX proposal"):
 *  - red = critical severity / breached targets, amber = warning / degraded
 *    delivery, blue = acknowledged ownership, gray = intentionally suppressed,
 *    green ONLY for a confirmed recovery.
 *  - Workflow status and condition are separate: a snoozed alert is
 *    "Suppressed — condition still active", never resolved.
 *  - Channel icons are configured routing, not proof of delivery.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlarmClockOff, AlertCircle, AlertTriangle, BellOff, CheckCircle2, Cpu, Gauge, Hash, Hourglass,
  Info, Loader2, Mail, MessageSquare, Network, Radar, Search, Send, Server, UserCheck, UserX, Webhook, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { fmtAlertDate, fmtDur } from './format'
import type {
  AlertActionResult, AlertClosure, AlertDeliverySummary, AlertEntity, AlertRow, AlertSeverity, AlertStatus,
  AlertSuppression, Assignee, EntityKind, SnoozePreview,
} from './types'

export * from './format'

/** Focus ring for custom controls (the base Button has none). */
export const focusRing = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface'

/* ── Channels ─────────────────────────────────────────────────────────── */

export type AlertChannel = {
  id: string
  name: string
  type: 'email' | 'sms' | 'webhook' | 'slack' | 'telegram' | string
  enabled: boolean
}

export const CHANNEL_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  email: Mail,
  sms: MessageSquare,
  webhook: Webhook,
  slack: Hash,
  telegram: Send,
}

/**
 * Icons for the channels a rule notifies (configured routing — NOT delivery).
 * Disabled channels render dimmed: an alert whose only channel is switched off
 * is effectively silent, and the operator should see that at a glance.
 */
export function ChannelIcons({ channels, className }: { channels?: AlertChannel[]; className?: string }) {
  if (!channels || channels.length === 0) {
    return (
      <span className={cn('inline-flex items-center gap-1 text-muted/60', className)} title="No notification channels configured">
        <BellOff className="h-3.5 w-3.5" aria-hidden />
        <span className="sr-only">No notification channels configured</span>
      </span>
    )
  }
  return (
    <span className={cn('inline-flex items-center gap-1', className)}>
      {channels.map((c) => {
        const Icon = CHANNEL_ICONS[c.type] || Webhook
        return (
          <span
            key={c.id}
            title={`${c.name} (${c.type})${c.enabled ? '' : ' — disabled'}`}
            className={cn(
              'inline-flex h-5 w-5 items-center justify-center rounded bg-info/10 text-info',
              !c.enabled && 'bg-surface2 text-muted/50',
            )}
          >
            <Icon className="h-3 w-3" aria-hidden />
            <span className="sr-only">{c.name}{c.enabled ? '' : ' (disabled)'}</span>
          </span>
        )
      })}
    </span>
  )
}

/** Legacy alert rows stored the rendered SMS template as the message. Strip
 *  the transport prefix so lists show the actual event. */
export function cleanAlertMessage(message: string | null | undefined): string {
  if (!message) return 'Alert'
  return message.replace(/^\[ZenPlus\s+[A-Z]+\]\s*/, '')
}

/**
 * Notification column: configured routing icons plus the recorded delivery
 * outcome. Without `delivery` the outcome is stated as unknown — icons alone
 * never imply that anyone was notified (A24).
 */
export function NotifyCell({ channels, delivery }: { channels?: AlertChannel[]; delivery?: AlertDeliverySummary | null }) {
  return (
    <div className="flex flex-col items-start gap-1">
      <ChannelIcons channels={channels} />
      <DeliveryBadge delivery={delivery} />
    </div>
  )
}

export function DeliveryBadge({ delivery, className }: { delivery?: AlertDeliverySummary | null; className?: string }) {
  if (!delivery) {
    return <span className={cn('whitespace-nowrap text-xs text-muted', className)} title="No delivery record for this alert">Delivery unknown</span>
  }
  if (delivery.attempts === 0) {
    return <span className={cn('text-xs text-muted', className)}>No attempts</span>
  }
  if (delivery.failed > 0) {
    const allFailed = delivery.accepted === 0
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-px text-xs font-medium',
          allFailed ? 'border-danger/40 bg-danger/10 text-danger' : 'border-warning/40 bg-warning/10 text-warning',
          className,
        )}
        title={`${delivery.failed} of ${delivery.attempts} delivery attempts failed${delivery.last_at ? ` · last ${fmtAlertDate(delivery.last_at)}` : ''}`}
      >
        <XCircle className="h-3 w-3" aria-hidden />
        {delivery.failed} failed
      </span>
    )
  }
  return (
    <span
      className={cn('text-xs text-muted', className)}
      title={`${delivery.accepted} of ${delivery.attempts} attempts accepted by the provider${delivery.last_at ? ` · last ${fmtAlertDate(delivery.last_at)}` : ''}`}
    >
      {delivery.accepted} accepted
    </span>
  )
}

/* ── Severity ─────────────────────────────────────────────────────────── */

const SEV: Record<AlertSeverity, { label: string; cls: string; icon: typeof AlertCircle }> = {
  critical: { label: 'Critical', cls: 'border-danger/35 bg-danger/10 text-danger', icon: AlertCircle },
  warning: { label: 'Warning', cls: 'border-warning/40 bg-warning/10 text-warning', icon: AlertTriangle },
  info: { label: 'Info', cls: 'border-info/35 bg-info/10 text-info', icon: Info },
}

export function severityMeta(s: string | null | undefined) {
  return SEV[(s as AlertSeverity)] ?? SEV.info
}

export function SeverityBadge({ severity, className }: { severity: AlertSeverity | string; className?: string }) {
  const m = severityMeta(severity)
  const Icon = m.icon
  return (
    <span className={cn('inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-semibold', m.cls, className)}>
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {m.label}
    </span>
  )
}

export const SEVERITY_DOT: Record<string, string> = {
  critical: 'bg-danger',
  warning: 'bg-warning',
  info: 'bg-info',
}

/* ── Status (workflow vs condition) ───────────────────────────────────── */

export type StatusKey = 'pending' | 'active' | 'acknowledged' | 'suppressed' | 'recovered' | 'closed'

type StatusInput = {
  status: AlertStatus
  severity?: AlertSeverity | string
  suppression?: AlertSuppression | null
  snoozed?: boolean
  snoozed_until?: string | null
  closure?: AlertClosure | null
  is_notice?: boolean
  notice_kind?: string | null
}

export function statusKey(a: StatusInput): StatusKey {
  if (a.status === 'resolved') return a.closure?.reason === 'recovered' ? 'recovered' : 'closed'
  if (a.suppression || a.snoozed) return 'suppressed'
  return a.status
}

export function isOpen(a: { status: AlertStatus }): boolean {
  return a.status !== 'resolved'
}

function suppressionTitle(a: StatusInput): string {
  const s = a.suppression
  const until = s?.until ?? a.snoozed_until
  const forever = s?.forever ?? (!until && !!a.snoozed)
  const parts = ['Notifications suppressed; the incident is not resolved']
  parts.push(forever ? 'indefinitely' : until ? `until ${fmtAlertDate(until)}` : '')
  if (s?.by) parts.push(`by ${s.by.name}`)
  if (s?.reason) parts.push(`— ${s.reason}`)
  return parts.filter(Boolean).join(' ')
}

export function StatusChip({ alert, className }: { alert: StatusInput; className?: string }) {
  const key = statusKey(alert)
  const base = 'inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-medium'
  let chip: ReactNode
  switch (key) {
    case 'pending':
      chip = (
        <span className={cn(base, 'border-dashed border-border-strong bg-surface text-text2')} title="Held for the rule's hold time — not yet announced">
          <Hourglass className="h-3.5 w-3.5" aria-hidden /> Pending (held)
        </span>
      )
      break
    case 'active':
      chip = (
        <span className={cn(base, 'border-border bg-surface2 text-text')} title="Firing and needs attention">
          <span className={cn('h-2 w-2 rounded-full', SEVERITY_DOT[alert.severity || 'info'] ?? 'bg-danger')} aria-hidden />
          Active
        </span>
      )
      break
    case 'acknowledged':
      chip = (
        <span className={cn(base, 'border-primary/35 bg-primary/10 text-primary')} title="A person owns this incident; the condition may still be firing">
          <UserCheck className="h-3.5 w-3.5" aria-hidden /> Acknowledged
        </span>
      )
      break
    case 'suppressed':
      chip = (
        <span className={cn(base, 'items-start whitespace-normal border-muted/40 bg-muted/10 text-text2')} title={suppressionTitle(alert)}>
          <BellOff className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
          <span className="leading-tight">
            Suppressed<span className="sr-only"> — </span>
            <span className="block text-xs font-normal text-muted">condition still active</span>
          </span>
        </span>
      )
      break
    case 'recovered':
      chip = (
        <span className={cn(base, 'border-success/35 bg-success/10 text-success')} title={closureTitle(alert.closure)}>
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> {alert.closure?.label || 'Recovered'}
        </span>
      )
      break
    default:
      chip = (
        <span className={cn(base, 'border-border bg-surface2 text-text2')} title={closureTitle(alert.closure)}>
          <XCircle className="h-3.5 w-3.5 text-muted" aria-hidden /> {alert.closure?.label || 'Resolved'}
        </span>
      )
  }
  return (
    <span className={cn('inline-flex flex-wrap items-center gap-1', className)}>
      {chip}
      {alert.is_notice && (
        <span className={cn(base, 'border-border text-muted')} title="Informational notice — excluded from incident metrics">
          Notice{alert.notice_kind ? ` · ${alert.notice_kind.replace('_', ' ')}` : ''}
        </span>
      )}
    </span>
  )
}

function closureTitle(c: AlertClosure | null | undefined): string {
  if (!c) return 'Resolved'
  return [c.label, c.by ? `by ${c.by.name}` : null, c.note ? `— ${c.note}` : null].filter(Boolean).join(' ')
}

/* ── Entity ───────────────────────────────────────────────────────────── */

export const ENTITY_KIND_LABEL: Record<EntityKind, string> = {
  device: 'Device',
  server: 'Server',
  service_check: 'Service check',
  sensor: 'Sensor',
  apm: 'APM',
  system: 'System',
}

export const ENTITY_ICON: Record<EntityKind, typeof Server> = {
  device: Network,
  server: Server,
  service_check: Activity,
  sensor: Radar,
  apm: Gauge,
  system: Cpu,
}

/** Typed entity for a row; legacy rows without `entity` are derived (A09). */
export function entityOf(a: Partial<AlertRow>): AlertEntity {
  if (a.entity && a.entity.kind) return a.entity
  if (a.server_id) return { kind: 'server', id: a.server_id, name: a.server_hostname || 'Server', subtitle: 'server', href: `/servers/${a.server_id}` }
  if (a.sensor_id) return { kind: 'sensor', id: a.sensor_id, name: a.sensor_name || 'Sensor', subtitle: 'sensor', href: `/sensors/${a.sensor_id}` }
  if (a.service_check_id) return { kind: 'service_check', id: a.service_check_id, name: a.service_check_name || 'Service check', subtitle: 'service check', href: `/services/${a.service_check_id}` }
  if (a.device_id) return { kind: 'device', id: a.device_id, name: a.device_hostname || a.device_ip || 'Device', subtitle: a.device_ip ?? null, href: `/devices/${a.device_id}` }
  return { kind: 'system', id: null, name: 'System', subtitle: null, href: null }
}

export function EntityCell({ entity, className, compact }: { entity: AlertEntity; className?: string; compact?: boolean }) {
  const Icon = ENTITY_ICON[entity.kind] ?? Cpu
  const kind = ENTITY_KIND_LABEL[entity.kind] ?? entity.kind
  const body = (
    <>
      <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded bg-surface2 text-muted" title={kind}>
        <Icon className="h-3.5 w-3.5" aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-medium">{entity.name || kind}</span>
        {!compact && (
          <span className="block truncate text-xs text-muted">
            <span className="sr-only">{kind}: </span>
            {entity.subtitle || kind}
          </span>
        )}
      </span>
    </>
  )
  const cls = cn('flex min-w-0 max-w-[240px] items-start gap-2', className)
  if (entity.href) {
    return (
      <Link to={entity.href} className={cn(cls, 'rounded hover:text-primary', focusRing)} aria-label={`${kind}: ${entity.name}`}>
        {body}
      </Link>
    )
  }
  return <span className={cls}>{body}</span>
}

/* ── Actions (shared by list + detail) ────────────────────────────────── */

type Conflictable = { response?: { status?: number } }

/** Build a readable explanation for a 409 by re-reading the alert. */
async function explainConflict(id: string, fallback: string): Promise<string> {
  try {
    const a: AlertRow = (await api.get(`/alerts/${id}`)).data
    if (a.status === 'resolved') {
      const who = a.closure?.by?.name ?? (a.closure?.reason === 'recovered' ? 'the evaluator' : 'the system')
      return `Already resolved by ${who} at ${fmtAlertDate(a.resolved_at, { seconds: true })}${a.closure?.label ? ` (${a.closure.label})` : ''}.`
    }
    if (a.status === 'pending') return 'This alert is still held (pending) and cannot be acknowledged yet.'
    if (a.status === 'acknowledged' && a.acknowledged_by) {
      return `Already acknowledged by ${a.acknowledged_by.name} at ${fmtAlertDate(a.acknowledged_at, { seconds: true })}.`
    }
  } catch { /* fall through */ }
  return fallback
}

/**
 * Per-alert triage mutations with consistent toasts, 409 handling
 * (the alert changed under you → explain + refetch) and cache invalidation.
 */
export function useAlertActions(onSettled?: () => void) {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['alerts'] })
    onSettled?.()
  }
  const fail = (title: string) => async (e: unknown, vars: { id: string }) => {
    if ((e as Conflictable)?.response?.status === 409) {
      const msg = await explainConflict(vars.id, apiErrorMessage(e))
      toast.info('Alert already changed', msg)
      refresh()
      return
    }
    toast.error(title, apiErrorMessage(e))
  }

  const acknowledge = useMutation({
    mutationFn: async ({ id, note }: { id: string; note?: string }) =>
      (await api.post<AlertActionResult>(`/alerts/${id}/acknowledge`, note ? { note } : {})).data,
    onSuccess: (d) => {
      toast.success(d?.changed === false ? 'Already acknowledged' : 'Alert acknowledged')
      refresh()
    },
    onError: fail('Acknowledge failed'),
  })

  const resolve = useMutation({
    mutationFn: async ({ id, reason }: { id: string; reason?: string }) =>
      (await api.post<AlertActionResult>(`/alerts/${id}/resolve`, reason ? { reason } : {})).data,
    onSuccess: (d) => {
      toast.success(d?.changed === false ? 'Already resolved' : 'Incident closed', d?.changed === false ? undefined : 'Recorded as “Closed by operator”, not as a recovery.')
      refresh()
    },
    onError: fail('Resolve failed'),
  })

  const assign = useMutation({
    mutationFn: async ({ id, user_id, note }: { id: string; user_id: string | null; note?: string }) =>
      (await api.post<AlertActionResult>(`/alerts/${id}/assign`, { user_id, ...(note ? { note } : {}) })).data,
    onSuccess: (d, v) => {
      toast.success(v.user_id ? `Assigned to ${d?.assigned_to?.name ?? 'user'}` : 'Owner removed')
      refresh()
    },
    onError: fail('Assign failed'),
  })

  const unsnooze = useMutation({
    mutationFn: async ({ id }: { id: string }) =>
      (await api.post<{ removed?: number }>(`/alerts/${id}/unsnooze`)).data,
    onSuccess: (d) => {
      toast.success('Suppression removed', d?.removed != null ? `${d.removed} silence${d.removed === 1 ? '' : 's'} removed — the condition will notify again.` : 'The condition will notify again.')
      refresh()
    },
    onError: fail('Unsnooze failed'),
  })

  const comment = useMutation({
    mutationFn: async ({ id, text }: { id: string; text: string }) =>
      (await api.post(`/alerts/${id}/comments`, { text })).data,
    onSuccess: () => {
      toast.success('Comment added')
      refresh()
    },
    onError: fail('Comment failed'),
  })

  return { acknowledge, resolve, assign, unsnooze, comment }
}

export function useAssignees(enabled = true) {
  return useQuery<Assignee[]>({
    queryKey: ['alerts', 'assignees'],
    queryFn: async () => {
      const r = (await api.get('/alerts/assignees')).data
      return Array.isArray(r) ? r : r?.items ?? []
    },
    enabled,
    staleTime: 5 * 60_000,
  })
}

/* ── Snooze ───────────────────────────────────────────────────────────── */

const SNOOZE_CHOICES = [
  { key: '15', label: '15 min', minutes: 15 },
  { key: '60', label: '1 hour', minutes: 60 },
  { key: '360', label: '6 hours', minutes: 360 },
  { key: '1440', label: '24 hours', minutes: 1440 },
  { key: '10080', label: '7 days', minutes: 10080 },
  { key: 'custom', label: 'Custom', minutes: null },
  { key: 'forever', label: 'Indefinite', minutes: null },
] as const

const MIN_SNOOZE = 5
const MAX_SNOOZE = 43200

export type SnoozeChoice = { minutes: number | null; reason: string }

/**
 * Snooze (suppress notifications for a condition). Single-alert mode loads
 * `GET /alerts/{id}/snooze-preview` and states the condition, entity, rule,
 * open alerts covered and channels muted. Bulk mode (`onSubmit`) has no
 * preview and offers finite durations only.
 */
export function SnoozeDialog({
  open, onOpenChange, alertId, alertLabel, onSubmit, count, onDone,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  /** Single-alert mode. */
  alertId?: string
  alertLabel?: string
  /** Bulk mode: caller performs the request. */
  onSubmit?: (v: SnoozeChoice) => Promise<unknown>
  count?: number
  onDone?: () => void
}) {
  const qc = useQueryClient()
  const can = useCan()
  const bulk = !!onSubmit
  const [choice, setChoice] = useState<string>('60')
  const [custom, setCustom] = useState('120')
  const [reason, setReason] = useState('')

  // Reset only when the dialog opens.
  useEffect(() => {
    if (open) { setChoice('60'); setCustom('120'); setReason('') }
  }, [open])

  const preview = useQuery<SnoozePreview>({
    queryKey: ['alerts', 'snooze-preview', alertId],
    queryFn: async () => (await api.get(`/alerts/${alertId}/snooze-preview`)).data,
    enabled: open && !!alertId && !bulk,
    staleTime: 0,
  })

  const canForever = !bulk && can('alerts.manage') && (preview.data?.can_forever ?? true)
  const forever = choice === 'forever'
  const customMin = Number(custom)
  const customValid = Number.isInteger(customMin) && customMin >= MIN_SNOOZE && customMin <= MAX_SNOOZE
  const minutes: number | null = forever ? null : choice === 'custom' ? customMin : Number(choice)
  const reasonValid = forever ? reason.trim().length >= 3 : true
  const valid = (choice !== 'custom' || customValid) && reasonValid && reason.length <= 500 && (!forever || canForever)

  const single = useMutation({
    mutationFn: async () =>
      (await api.post(`/alerts/${alertId}/snooze`, { minutes, ...(reason.trim() ? { reason: reason.trim() } : {}) })).data,
    onSuccess: () => {
      toast.success(
        forever ? 'Suppressed indefinitely' : `Suppressed for ${fmtDur((minutes ?? 0) * 60)}`,
        'Notifications are muted; the incident stays open until the condition recovers or someone closes it.',
      )
      qc.invalidateQueries({ queryKey: ['alerts'] })
      onOpenChange(false)
      onDone?.()
    },
    onError: (e) => toast.error('Snooze failed', apiErrorMessage(e)),
  })
  const [bulkPending, setBulkPending] = useState(false)

  const submit = async () => {
    if (!valid) return
    if (bulk) {
      setBulkPending(true)
      try {
        await onSubmit!({ minutes, reason: reason.trim() })
        onOpenChange(false)
        onDone?.()
      } finally {
        setBulkPending(false)
      }
    } else {
      single.mutate()
    }
  }
  const pending = single.isPending || bulkPending

  const p = preview.data
  const ruleName = p ? (typeof p.rule === 'string' ? p.rule : p.rule?.name) : null
  const choices = SNOOZE_CHOICES.filter((c) => c.key !== 'forever' || canForever)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-2rem)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <BellOff className="h-4 w-4 text-muted" aria-hidden />
            {bulk ? `Snooze ${count ?? 0} alert${count === 1 ? '' : 's'}` : 'Snooze condition'}
          </DialogTitle>
          <DialogDescription>
            Snoozing <strong className="text-text">suppresses notifications</strong> for the condition.
            It <strong className="text-text">does not resolve the incident</strong>: open alerts stay open and are
            shown as “Suppressed — condition still active” until the condition recovers or someone closes them.
          </DialogDescription>
        </DialogHeader>

        {!bulk && (
          <div className="rounded-md border border-border bg-surface2/40 p-3 text-sm">
            {preview.isLoading ? (
              <div className="flex items-center gap-2 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading what will be muted…</div>
            ) : preview.isError ? (
              <div className="text-danger">Could not load the snooze preview: {apiErrorMessage(preview.error)}</div>
            ) : p ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5">
                <dt className="text-muted">Condition</dt>
                <dd className="font-medium">{p.condition_label || alertLabel || '—'}</dd>
                <dt className="text-muted">Entity</dt>
                <dd>{p.entity?.name ?? '—'}{p.entity?.kind ? <span className="text-muted"> · {ENTITY_KIND_LABEL[p.entity.kind as EntityKind] ?? p.entity.kind}</span> : null}</dd>
                <dt className="text-muted">Rule</dt>
                <dd>{ruleName || '—'}</dd>
                <dt className="text-muted">Covers</dt>
                <dd>
                  <strong>{p.open_alerts.toLocaleString()}</strong> open alert{p.open_alerts === 1 ? '' : 's'} for this condition (they stay open)
                </dd>
                <dt className="text-muted">Muted</dt>
                <dd>
                  {p.channels?.length ? (
                    <span className="inline-flex flex-wrap items-center gap-1.5">
                      <ChannelIcons channels={p.channels} />
                      <span className="text-text2">{p.channels.map((c) => c.name).join(', ')}</span>
                    </span>
                  ) : (
                    <span className="text-muted">No channels configured</span>
                  )}
                </dd>
                {p.existing && (
                  <>
                    <dt className="text-muted">Existing</dt>
                    <dd className="text-warning">
                      Already suppressed {p.existing.forever ? 'indefinitely' : p.existing.until ? `until ${fmtAlertDate(p.existing.until)}` : ''} — this replaces it.
                    </dd>
                  </>
                )}
              </dl>
            ) : null}
          </div>
        )}

        <fieldset>
          <legend className="mb-2 text-sm font-medium">Duration</legend>
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Snooze duration">
            {choices.map((c) => (
              <button
                key={c.key}
                type="button"
                role="radio"
                aria-checked={choice === c.key}
                onClick={() => setChoice(c.key)}
                className={cn(
                  'rounded-md border px-2.5 py-1.5 text-sm',
                  focusRing,
                  choice === c.key ? 'border-primary bg-primary/10 font-medium text-primary' : 'border-border bg-surface hover:bg-surface2',
                  c.key === 'forever' && choice !== c.key && 'border-dashed',
                )}
              >
                {c.label}
              </button>
            ))}
          </div>
          {choice === 'custom' && (
            <div className="mt-2">
              <label htmlFor="snooze-custom" className="text-sm text-text2">Minutes ({MIN_SNOOZE}–{MAX_SNOOZE.toLocaleString()})</label>
              <div className="mt-1 flex items-center gap-2">
                <input
                  id="snooze-custom"
                  type="number"
                  inputMode="numeric"
                  min={MIN_SNOOZE}
                  max={MAX_SNOOZE}
                  step={1}
                  value={custom}
                  onChange={(e) => setCustom(e.target.value)}
                  aria-invalid={!customValid}
                  className={cn('h-9 w-32 rounded-md border bg-surface px-3 text-sm', focusRing, customValid ? 'border-border' : 'border-danger')}
                />
                <span className="text-sm text-muted">{customValid ? `= ${fmtDur(customMin * 60)}` : ''}</span>
              </div>
              {!customValid && <p className="mt-1 text-sm text-danger">Enter a whole number of minutes between {MIN_SNOOZE} and {MAX_SNOOZE.toLocaleString()} (30 days).</p>}
            </div>
          )}
          {forever && (
            <p className="mt-2 rounded-md border border-warning/40 bg-warning/10 p-2 text-sm text-warning">
              Indefinite suppression stays until someone removes it in the Silences workspace. A reason is required and is recorded with your name.
            </p>
          )}
        </fieldset>

        <div>
          <label htmlFor="snooze-reason" className="text-sm font-medium">
            Reason {forever ? <span className="text-danger">(required)</span> : <span className="font-normal text-muted">(optional)</span>}
          </label>
          <Textarea
            id="snooze-reason"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
            placeholder={forever ? 'Why is this condition muted indefinitely? e.g. decommissioned link, ticket #' : 'e.g. planned maintenance, known issue ticket #'}
            className="mt-1"
          />
          {forever && !reasonValid && reason.length > 0 && <p className="mt-1 text-sm text-danger">At least 3 characters.</p>}
        </div>

        <DialogFooter className="flex-wrap">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>Cancel</Button>
          <Button onClick={submit} disabled={!valid || pending || (!bulk && preview.isLoading)}>
            {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <BellOff className="h-4 w-4" />}
            {forever ? 'Suppress indefinitely' : 'Suppress notifications'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Snooze button (opens SnoozeDialog) or Unsnooze button when the condition is
 * already suppressed. Kept API-compatible with the old dropdown.
 */
export function SnoozeMenu({
  alertId, snoozed, size = 'sm', onDone, alertLabel, className,
}: {
  alertId: string
  snoozed?: boolean
  size?: 'sm' | 'default'
  onDone?: () => void
  alertLabel?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const { unsnooze } = useAlertActions(onDone)
  const ctx = alertLabel ? `: ${alertLabel}` : ''
  if (snoozed) {
    return (
      <Button
        size={size}
        variant="outline"
        className={cn(focusRing, className)}
        onClick={() => unsnooze.mutate({ id: alertId })}
        disabled={unsnooze.isPending}
        aria-label={`Remove suppression${ctx}`}
        title="Condition is suppressed — remove the silence so it notifies again"
      >
        {unsnooze.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <AlarmClockOff className="h-3.5 w-3.5" />}
        Unsnooze
      </Button>
    )
  }
  return (
    <>
      <Button
        size={size}
        variant="outline"
        className={cn(focusRing, className)}
        onClick={() => setOpen(true)}
        aria-label={`Snooze notifications${ctx}`}
        title="Suppress notifications for this condition (does not resolve)"
      >
        <BellOff className="h-3.5 w-3.5" /> Snooze
      </Button>
      <SnoozeDialog open={open} onOpenChange={setOpen} alertId={alertId} alertLabel={alertLabel} onDone={onDone} />
    </>
  )
}

/* ── Resolve (manual close) ───────────────────────────────────────────── */

export function ResolveDialog({
  open, onOpenChange, onConfirm, pending, count = 1, alertLabel,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onConfirm: (reason: string) => void
  pending?: boolean
  count?: number
  alertLabel?: string
}) {
  const [reason, setReason] = useState('')
  useEffect(() => { if (open) setReason('') }, [open])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-2rem)] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{count > 1 ? `Close ${count} incidents` : 'Close incident'}</DialogTitle>
          <DialogDescription>
            {alertLabel && <span className="mb-1 block font-medium text-text">{alertLabel}</span>}
            Closing records the incident as <strong className="text-text">“Closed by operator”</strong>. It does not
            state that the condition recovered — if it is still breaching, the evaluator can raise a new alert.
            Consider Snooze if you only want to stop notifications.
          </DialogDescription>
        </DialogHeader>
        <div>
          <label htmlFor="resolve-reason" className="text-sm font-medium">Reason <span className="font-normal text-muted">(optional, recorded in the timeline)</span></label>
          <Textarea id="resolve-reason" className="mt-1" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. fixed by replacing the PSU, ticket #1234" />
          <div className="mt-1 text-right text-xs text-muted">{reason.length}/500</div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>Cancel</Button>
          <Button onClick={() => onConfirm(reason.trim())} disabled={pending}>
            {pending && <Loader2 className="h-4 w-4 animate-spin" />}
            {count > 1 ? `Close ${count}` : 'Close incident'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Assign ───────────────────────────────────────────────────────────── */

export function AssignDialog({
  open, onOpenChange, current, onAssign, pending, count = 1,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  current?: { id: string; name: string } | null
  onAssign: (userId: string | null) => void
  pending?: boolean
  count?: number
}) {
  const me = useAuth((s) => s.user)
  const { data: users = [], isLoading, isError, error, refetch } = useAssignees(open)
  const [q, setQ] = useState('')
  useEffect(() => { if (open) setQ('') }, [open])
  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    return (s ? users.filter((u) => `${u.name} ${u.username}`.toLowerCase().includes(s)) : users).slice(0, 100)
  }, [users, q])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-2rem)] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{count > 1 ? `Assign ${count} alerts` : 'Assign owner'}</DialogTitle>
          <DialogDescription>
            {current ? <>Currently owned by <strong className="text-text">{current.name}</strong>.</> : 'Nobody owns this yet.'}{' '}
            The owner is accountable for driving the incident to closure.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap gap-2">
          {me && (
            <Button size="sm" onClick={() => onAssign(me.id)} disabled={pending || current?.id === me.id} className={focusRing}>
              <UserCheck className="h-3.5 w-3.5" /> Assign to me
            </Button>
          )}
          {current && count === 1 && (
            <Button size="sm" variant="outline" onClick={() => onAssign(null)} disabled={pending} className={focusRing}>
              <UserX className="h-3.5 w-3.5" /> Unassign
            </Button>
          )}
        </div>
        <div>
          <label htmlFor="assignee-search" className="sr-only">Search users</label>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
            <input
              id="assignee-search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search people…"
              className={cn('h-9 w-full rounded-md border border-border bg-surface pl-8 pr-3 text-sm', focusRing)}
            />
          </div>
          <div className="mt-2 max-h-64 overflow-y-auto rounded-md border border-border" role="listbox" aria-label="People">
            {isLoading ? (
              <div className="flex items-center gap-2 p-3 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading people…</div>
            ) : isError ? (
              <div className="p-3 text-sm text-danger">
                Could not load people: {apiErrorMessage(error)}{' '}
                <button className="underline" onClick={() => refetch()}>Retry</button>
              </div>
            ) : filtered.length === 0 ? (
              <div className="p-3 text-sm text-muted">No matching active users.</div>
            ) : filtered.map((u) => (
              <button
                key={u.id}
                role="option"
                aria-selected={current?.id === u.id}
                disabled={pending}
                onClick={() => onAssign(u.id)}
                className={cn(
                  'flex w-full items-center justify-between gap-2 border-b border-border/60 px-3 py-2 text-left text-sm last:border-0 hover:bg-surface2',
                  focusRing,
                  current?.id === u.id && 'bg-primary/5',
                )}
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">{u.name}{me?.id === u.id ? ' (you)' : ''}</span>
                  <span className="block truncate text-xs text-muted">{u.username}</span>
                </span>
                {current?.id === u.id && <UserCheck className="h-4 w-4 text-primary" aria-label="Current owner" />}
              </button>
            ))}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
