/**
 * Alert detail — the investigation page for one incident.
 *
 * Alerts development review A03, A06, A09, A18–A24, A26; F01, F02, F04, F05,
 * F07, F08:
 *  - Workflow status and condition are shown separately; a suppressed alert
 *    is "Suppressed — condition still active", only an evaluator recovery is
 *    green, and durations are "time to recover" only for real recoveries.
 *  - Ages use the appliance clock (response server_time), never Date.now().
 *  - Cached content stays on screen when a background refresh fails.
 *  - Triage and policy controls follow the user's permissions; policy changes
 *    live in their own card with impact confirmation.
 */
import { useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowLeft, Check, Clock, Eye, Loader2, MessageSquare, RefreshCw, Repeat, ShieldAlert, Timer, TriangleAlert,
  UserCheck, UserPlus, Waves, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import {
  AssignDialog, ENTITY_ICON, ENTITY_KIND_LABEL, EntityCell, ResolveDialog, SeverityBadge, SnoozeMenu, StatusChip,
  cleanAlertMessage, entityOf, focusRing, isOpen, severityMeta, statusKey, useAlertActions,
} from '@/components/alerts/AlertBits'
import { AlertTimeline } from '@/components/alerts/AlertTimeline'
import { PolicyCard } from '@/components/alerts/PolicyCard'
import { RoutingCard } from '@/components/alerts/RoutingCard'
import {
  ageOf, fmtAgo, fmtAlertDate, fmtClock, fmtDur, parseTs, spanSeconds, useApplianceNow,
} from '@/components/alerts/format'
import type { AlertDetail, AlertRow, RelatedAlert } from '@/components/alerts/types'
import { alertFlapState } from '@/components/devices/deviceStatus'

export function AlertDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const can = useCan()
  const me = useAuth((s) => s.user)
  const commentRef = useRef<HTMLTextAreaElement>(null)
  const [dialog, setDialog] = useState<'resolve' | 'assign' | null>(null)

  const q = useQuery<AlertDetail>({
    queryKey: ['alerts', 'detail', id],
    queryFn: async () => (await api.get(`/alerts/${id}`)).data,
    enabled: !!id,
    refetchInterval: 15_000,
    retry: 1,
  })
  const alert = q.data
  const now = useApplianceNow(alert?.server_time ?? null)
  const actions = useAlertActions()

  const goBack = () => {
    if (window.history.length > 1) navigate(-1)
    else navigate('/alerts')
  }

  if (q.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-9 w-2/3" />
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-20" />)}</div>
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (!alert) {
    const notFound = (q.error as { response?: { status?: number } } | null)?.response?.status === 404
    return (
      <Card>
        <CardContent className="space-y-3 p-6">
          <button onClick={goBack} className={cn('inline-flex items-center gap-1 text-sm text-muted hover:text-text', focusRing)}>
            <ArrowLeft className="h-4 w-4" /> Back
          </button>
          <p className="text-sm text-danger">
            {notFound ? 'This alert does not exist or is outside your visible scope.' : `Failed to load alert: ${apiErrorMessage(q.error)}`}
          </p>
          {!notFound && (
            <Button size="sm" variant="outline" className={focusRing} onClick={() => q.refetch()} disabled={q.isFetching}>
              {q.isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Retry
            </Button>
          )}
        </CardContent>
      </Card>
    )
  }

  const perms = alert.permissions
  const canTriage = can('alerts.acknowledge') && (perms?.can_triage ?? true)
  const open = isOpen(alert)
  const suppressed = !!alert.suppression || alert.snoozed
  const msg = cleanAlertMessage(alert.message)
  const entity = entityOf(alert)
  const key = statusKey(alert)
  const staleSince = q.isError ? q.dataUpdatedAt : null
  const flap = alertFlapState(alert, (ms) => fmtAlertDate(new Date(ms).toISOString()))

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="space-y-3">
        <button onClick={goBack} className={cn('inline-flex items-center gap-1 text-sm text-muted hover:text-text', focusRing)}>
          <ArrowLeft className="h-4 w-4" /> Back
        </button>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 space-y-2">
            <h1 className="break-words text-xl font-semibold tracking-tight sm:text-2xl">{msg}</h1>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <SeverityBadge severity={alert.severity} />
              <StatusChip alert={alert} />
              <EntityCell entity={entity} compact className="max-w-[280px]" />
              <span className="text-muted" title={fmtAlertDate(alert.triggered_at, { seconds: true, year: 'always' })}>
                Opened {fmtAgo(alert.triggered_at, now)}
              </span>
              <span className="text-muted">·</span>
              <span className={cn(!alert.assigned_to && open && alert.severity === 'critical' ? 'font-medium text-danger' : 'text-text2')}>
                Owner: {alert.assigned_to?.name ?? 'Unassigned'}
              </span>
            </div>
          </div>
          {canTriage && (
            <div className="flex flex-wrap gap-2 lg:justify-end">
              {alert.status === 'active' && (
                <Button
                  size="sm" className={focusRing}
                  onClick={() => actions.acknowledge.mutate({ id: alert.id })}
                  disabled={actions.acknowledge.isPending}
                  aria-label={`Acknowledge alert: ${msg}`}
                >
                  {actions.acknowledge.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />} Acknowledge
                </Button>
              )}
              {open && me && alert.assigned_to?.id !== me.id && (
                <Button size="sm" variant="outline" className={focusRing} onClick={() => actions.assign.mutate({ id: alert.id, user_id: me.id })} disabled={actions.assign.isPending}>
                  <UserCheck className="h-3.5 w-3.5" /> Assign to me
                </Button>
              )}
              {open && (
                <Button size="sm" variant="outline" className={focusRing} onClick={() => setDialog('assign')}>
                  <UserPlus className="h-3.5 w-3.5" /> Assign…
                </Button>
              )}
              {(open || suppressed) && !alert.is_notice && (
                <SnoozeMenu alertId={alert.id} snoozed={suppressed} alertLabel={msg} />
              )}
              {open && (
                <Button size="sm" variant="outline" className={focusRing} onClick={() => setDialog('resolve')} aria-label={`Close incident: ${msg}`}>
                  <XCircle className="h-3.5 w-3.5" /> Close…
                </Button>
              )}
              <Button
                size="sm" variant="ghost" className={focusRing}
                onClick={() => { commentRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }); commentRef.current?.focus({ preventScroll: true }) }}
              >
                <MessageSquare className="h-3.5 w-3.5" /> Comment
              </Button>
            </div>
          )}
        </div>
      </div>

      {staleSince != null && (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
          <TriangleAlert className="h-4 w-4" aria-hidden />
          Showing data from {fmtClock(staleSince)} — refresh failed ({apiErrorMessage(q.error)}).
          <Button size="sm" variant="outline" className={cn('ml-auto h-7', focusRing)} onClick={() => q.refetch()} disabled={q.isFetching}>
            {q.isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Retry
          </Button>
        </div>
      )}

      {suppressed && open && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-muted/40 bg-muted/10 px-3 py-2 text-sm text-text2">
          Notifications for this condition are suppressed
          {alert.suppression?.forever ? ' indefinitely' : (alert.suppression?.until ?? alert.snoozed_until) ? ` until ${fmtAlertDate(alert.suppression?.until ?? alert.snoozed_until)}` : ''}
          {alert.suppression?.by ? ` by ${alert.suppression.by.name}` : ''}
          {alert.suppression?.reason ? ` — “${alert.suppression.reason}”` : ''}.
          <span className="text-muted">The incident is not resolved and the condition is still active.</span>
        </div>
      )}

      {flap?.kind === 'held' && (
        <div role="status" className="flex flex-wrap items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-text2">
          <Waves className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
          <span>
            <span className="font-medium text-text">Held: device flapping.</span>{' '}
            {entity.href ? <Link className="text-primary hover:underline" to={entity.href}>{entity.name}</Link> : entity.name} keeps changing status, so this
            notification waits{flap.until ? ` until ${fmtAlertDate(flap.until)}` : ' for the flap stability time'}. It is sent if the device is still in this status
            then; otherwise it closes without a notification. A rule set to notify immediately even while the device is flapping skips this hold.
          </span>
        </div>
      )}
      {flap?.kind === 'notice' && (
        <div role="note" className="flex flex-wrap items-start gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-sm text-text2">
          <Waves className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
          <span>
            <span className="font-medium text-text">{flap.label}.</span>{' '}
            {flap.detail}
          </span>
        </div>
      )}

      <SummaryTiles alert={alert} now={now} statusKeyValue={key} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0 space-y-4">
          <AlertTimeline ref={commentRef} alert={alert} now={now} canComment={canTriage} />
          <EvidenceCard alert={alert} />
          <RelatedCard alert={alert} now={now} />
          <RecurrenceCard alert={alert} now={now} />
        </div>
        <div className="min-w-0 space-y-4">
          <EntityCard alert={alert} />
          <PolicyCard rule={alert.rule} snapshot={alert.policy_snapshot} />
          <RoutingCard alert={alert} now={now} />
          {alert.rule && <MessagePreviewCard ruleId={alert.rule.id} />}
        </div>
      </div>

      <ResolveDialog
        open={dialog === 'resolve'}
        onOpenChange={(v) => { if (!v) setDialog(null) }}
        alertLabel={msg}
        pending={actions.resolve.isPending}
        onConfirm={(reason) => actions.resolve.mutate({ id: alert.id, reason }, { onSettled: () => setDialog(null) })}
      />
      <AssignDialog
        open={dialog === 'assign'}
        onOpenChange={(v) => { if (!v) setDialog(null) }}
        current={alert.assigned_to}
        pending={actions.assign.isPending}
        onAssign={(uid) => actions.assign.mutate({ id: alert.id, user_id: uid }, { onSettled: () => setDialog(null) })}
      />
    </div>
  )
}

/* ── Summary tiles ────────────────────────────────────────────────────── */

function Tile({ label, value, sub, icon: Icon, tone = 'neutral' }: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  icon: typeof Clock
  tone?: 'danger' | 'warning' | 'info' | 'success' | 'muted' | 'neutral'
}) {
  const cls = {
    danger: 'bg-danger/10 text-danger', warning: 'bg-warning/10 text-warning', info: 'bg-primary/10 text-primary',
    success: 'bg-success/10 text-success', muted: 'bg-muted/10 text-text2', neutral: 'bg-surface2 text-text2',
  }[tone]
  return (
    <Card>
      <CardContent className="flex items-start gap-3 p-3">
        <span className={cn('flex h-9 w-9 shrink-0 items-center justify-center rounded-md', cls)}><Icon className="h-4 w-4" aria-hidden /></span>
        <div className="min-w-0">
          <div className="text-xs font-medium text-muted">{label}</div>
          <div className="text-base font-semibold">{value}</div>
          {sub && <div className="text-xs text-muted">{sub}</div>}
        </div>
      </CardContent>
    </Card>
  )
}

function SummaryTiles({ alert, now, statusKeyValue }: { alert: AlertDetail; now: number; statusKeyValue: string }) {
  const open = isOpen(alert)
  const recovered = alert.closure?.reason === 'recovered'
  const sev = severityMeta(alert.severity)

  const condition = alert.status === 'pending'
    ? { text: alertFlapState(alert)?.kind === 'held' ? 'Held while the device is flapping' : 'Breaching — within hold time', tone: 'warning' as const }
    : statusKeyValue === 'suppressed'
      ? { text: 'Still active (suppressed)', tone: 'muted' as const }
      : open
        ? { text: 'Still firing (not recovered)', tone: 'danger' as const }
        : recovered
          ? { text: 'Recovered', tone: 'success' as const }
          : { text: 'Unknown — closed without recovery', tone: 'neutral' as const }

  const age = ageOf(alert.triggered_at, now)
  const span = spanSeconds(alert.triggered_at, alert.resolved_at)
  const durLabel = open ? 'Open for' : recovered ? 'Time to recover' : 'Time to close'
  const durValue = open ? (age.future ? 'Future timestamp' : fmtDur(age.seconds)) : fmtDur(span)

  let target: { value: string; sub?: string; tone: 'danger' | 'warning' | 'neutral' | 'info' }
  const ackSpan = spanSeconds(alert.triggered_at, alert.acknowledged_at)
  if (alert.acknowledged_at) {
    target = { value: `Acknowledged in ${fmtDur(ackSpan)}`, sub: alert.acknowledged_by ? `by ${alert.acknowledged_by.name}` : undefined, tone: 'info' }
  } else if (!alert.ack_due_at) {
    target = { value: open ? 'No target' : 'Not acknowledged', sub: open ? 'No response target for this severity' : undefined, tone: 'neutral' }
  } else {
    const due = parseTs(alert.ack_due_at) ?? 0
    const delta = Math.abs(due - now) / 1000
    target = alert.ack_overdue || due < now
      ? { value: `Overdue by ${fmtDur(delta)}`, sub: `Ack was due ${fmtAlertDate(alert.ack_due_at)}`, tone: 'danger' }
      : { value: `Ack due in ${fmtDur(delta)}`, sub: fmtAlertDate(alert.ack_due_at), tone: 'warning' }
  }

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <Tile label="Severity" value={sev.label} icon={ShieldAlert} tone={alert.severity === 'critical' ? 'danger' : alert.severity === 'warning' ? 'warning' : 'info'} />
      <Tile
        label="Status · Condition"
        value={<StatusChip alert={alert} />}
        sub={<span className={cn(condition.tone === 'success' && 'text-success', condition.tone === 'danger' && 'text-danger')}>{condition.text}</span>}
        icon={Waves}
        tone={condition.tone === 'success' ? 'success' : condition.tone === 'muted' ? 'muted' : open ? 'danger' : 'neutral'}
      />
      <Tile
        label={durLabel}
        value={durValue}
        sub={open ? `since ${fmtAlertDate(alert.triggered_at)}` : `closed ${fmtAlertDate(alert.resolved_at)}${alert.closure ? ` · ${alert.closure.label}` : ''}`}
        icon={Clock}
        tone={age.future && open ? 'warning' : 'neutral'}
      />
      <Tile
        label="Owner"
        value={alert.assigned_to?.name ?? 'Unassigned'}
        sub={alert.assigned_at ? `since ${fmtAlertDate(alert.assigned_at)}` : open ? 'Nobody owns this yet' : undefined}
        icon={UserCheck}
        tone={!alert.assigned_to && open && alert.severity === 'critical' ? 'danger' : alert.assigned_to ? 'info' : 'neutral'}
      />
      <Tile label="Response target" value={target.value} sub={target.sub} icon={Timer} tone={target.tone} />
    </div>
  )
}

/* ── Evidence (F05) ───────────────────────────────────────────────────── */

function EvidenceCard({ alert }: { alert: AlertDetail }) {
  const ev = alert.evidence ?? []
  return (
    <Card>
      <CardContent className="p-4">
        <h2 className="mb-2 text-base font-semibold">Evidence</h2>
        {ev.length ? (
          <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
            {ev.map((e, i) => (
              <div key={`${e.label}-${i}`} className="min-w-0 rounded-md border border-border/60 bg-surface2/30 px-3 py-2">
                <dt className="text-xs font-medium text-muted">{e.label}</dt>
                <dd className="break-words text-sm font-medium">{e.value == null || e.value === '' ? <span className="text-muted">Unknown</span> : String(e.value)}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-sm text-muted">No structured readings were recorded with this alert.</p>
        )}
        <dl className="mt-3 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-2">
          <div><dt className="inline text-muted">Triggered: </dt><dd className="inline">{fmtAlertDate(alert.triggered_at, { seconds: true, year: 'always' })}</dd></div>
          <div><dt className="inline text-muted">Alert ID: </dt><dd className="inline break-all font-mono text-xs">{alert.id}</dd></div>
        </dl>
        <details className="mt-3 rounded-md border border-border">
          <summary className={cn('cursor-pointer px-3 py-2 text-sm text-muted hover:text-text', focusRing)}>Raw metadata (JSON)</summary>
          <pre className="max-h-72 overflow-auto border-t border-border bg-bg p-3 text-xs text-text2">
            {JSON.stringify(alert.metadata || {}, null, 2)}
          </pre>
        </details>
      </CardContent>
    </Card>
  )
}

/* ── Related (A03) ────────────────────────────────────────────────────── */

const RELATION_FALLBACK: Record<string, string> = {
  same_entity: 'Other alerts on the same entity',
  same_rule_window: 'Same rule fired on other targets around the same time',
  dependency_parent: 'Upstream dependency alert',
}

function RelatedCard({ alert, now }: { alert: AlertDetail; now: number }) {
  const related: RelatedAlert[] = alert.related?.length
    ? alert.related
    : (alert.related_alerts ?? []).map((r) => ({ ...r, relation: 'same_entity', relation_label: RELATION_FALLBACK.same_entity }))
  const groups = new Map<string, { label: string; items: RelatedAlert[] }>()
  for (const r of related) {
    const g = groups.get(r.relation) ?? { label: r.relation_label || RELATION_FALLBACK[r.relation] || r.relation, items: [] }
    g.items.push(r)
    groups.set(r.relation, g)
  }
  return (
    <Card>
      <CardContent className="p-4">
        <h2 className="mb-2 text-base font-semibold">Related incidents</h2>
        {groups.size === 0 ? (
          <p className="rounded-md border border-dashed border-border p-4 text-center text-sm text-muted">No related incidents found.</p>
        ) : (
          <div className="space-y-4">
            {[...groups.entries()].map(([rel, g]) => (
              <section key={rel} aria-label={g.label}>
                <h3 className="mb-1.5 text-sm font-semibold text-text2">{g.label} <span className="font-normal text-muted">({g.items.length})</span></h3>
                <ul className="space-y-1.5">
                  {g.items.map((r) => <RelatedItem key={r.id} r={r} now={now} />)}
                </ul>
              </section>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function RelatedItem({ r, now }: { r: AlertRow; now: number }) {
  const e = entityOf(r)
  const Icon = ENTITY_ICON[e.kind]
  return (
    <li>
      <Link
        to={`/alerts/${r.id}`}
        className={cn('flex flex-wrap items-start gap-2 rounded-md border border-border bg-surface2/30 p-2 transition-colors hover:border-primary/50 hover:bg-primary/5', focusRing)}
      >
        <SeverityBadge severity={r.severity} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium" title={cleanAlertMessage(r.message)}>{cleanAlertMessage(r.message)}</div>
          <div className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted">
            {Icon && <Icon className="h-3 w-3" aria-hidden />}
            <span>{ENTITY_KIND_LABEL[e.kind]}: {e.name}</span>
            <span>· {fmtAgo(r.triggered_at, now)}</span>
          </div>
        </div>
        <StatusChip alert={r} />
      </Link>
    </li>
  )
}

/* ── Recurrence (F02) ─────────────────────────────────────────────────── */

function RecurrenceCard({ alert, now }: { alert: AlertDetail; now: number }) {
  const rc = alert.recurrence
  if (!rc) return null
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold">Recurrence</h2>
          {rc.flapping && (
            <span className="inline-flex items-center gap-1 rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-xs font-medium text-warning">
              <Repeat className="h-3.5 w-3.5" aria-hidden /> Flapping
            </span>
          )}
        </div>
        <p className="text-sm">
          Same condition fired <b>{rc.same_condition_24h.toLocaleString()}</b> time{rc.same_condition_24h === 1 ? '' : 's'} in 24 h and{' '}
          <b>{rc.same_condition_7d.toLocaleString()}</b> in 7 days
          {rc.flapping ? ' — it keeps opening and clearing; consider a hold time or reset threshold rather than closing it.' : '.'}
        </p>
        {rc.previous?.length > 0 && (
          <ul className="mt-2 space-y-1">
            {rc.previous.map((p) => (
              <li key={p.id} className="flex flex-wrap items-center gap-2 text-sm">
                <Link to={`/alerts/${p.id}`} className={cn('text-primary hover:underline', focusRing)}>{fmtAlertDate(p.triggered_at)}</Link>
                <span className="text-muted">({fmtAgo(p.triggered_at, now)})</span>
                <StatusChip alert={{ status: p.status }} />
                {p.resolved_at && <span className="text-xs text-muted">lasted {fmtDur(spanSeconds(p.triggered_at, p.resolved_at))}</span>}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

/* ── Affected entity (A09) ────────────────────────────────────────────── */

function EntityCard({ alert }: { alert: AlertDetail }) {
  const e = entityOf(alert)
  const Icon = ENTITY_ICON[e.kind]
  const facts: Array<[string, string | null]> = e.kind === 'device'
    ? [['Hostname', alert.device_hostname], ['IP address', alert.device_ip]]
    : e.kind === 'server' ? [['Hostname', alert.server_hostname]]
      : e.kind === 'service_check' ? [['Service check', alert.service_check_name]]
        : e.kind === 'sensor' ? [['Sensor', alert.sensor_name]]
          : []
  const body = (
    <>
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary"><Icon className="h-4 w-4" aria-hidden /></span>
      <span className="min-w-0">
        <span className="block truncate text-sm font-semibold">{e.name}</span>
        <span className="block truncate text-xs text-muted">{ENTITY_KIND_LABEL[e.kind]}{e.subtitle ? ` · ${e.subtitle}` : ''}</span>
      </span>
    </>
  )
  return (
    <Card>
      <CardContent className="p-4">
        <h2 className="mb-2 text-base font-semibold">Affected entity</h2>
        {e.href ? (
          <Link to={e.href} className={cn('flex items-center gap-3 rounded-md border border-border bg-surface2/30 p-3 hover:border-primary/50 hover:bg-primary/5', focusRing)}>{body}</Link>
        ) : (
          <div className="flex items-center gap-3 rounded-md border border-border bg-surface2/30 p-3">{body}</div>
        )}
        {facts.some(([, v]) => v) && (
          <dl className="mt-2 space-y-0.5 text-sm">
            {facts.filter(([, v]) => v).map(([k, v]) => (
              <div key={k} className="flex gap-2"><dt className="w-24 shrink-0 text-muted">{k}</dt><dd className="min-w-0 break-all font-mono text-xs leading-5">{v}</dd></div>
            ))}
          </dl>
        )}
        {!e.href && e.kind === 'system' && <p className="mt-2 text-xs text-muted">System-level alert — not tied to a monitored entity.</p>}
      </CardContent>
    </Card>
  )
}

/* ── Message preview ──────────────────────────────────────────────────── */

function MessagePreviewCard({ ruleId }: { ruleId: string }) {
  const [show, setShow] = useState(false)
  const [tab, setTab] = useState<'email' | 'sms'>('email')

  const { data, isFetching, error } = useQuery<{
    alert: { subject: string; email_body: string; sms_body: string }
    recovery: { subject: string; email_body: string; sms_body: string } | null
  }>({
    queryKey: ['alert-rules', ruleId, 'preview'],
    queryFn: async () => (await api.post(`/alert-rules/${ruleId}/preview`)).data,
    enabled: show,
    staleTime: 60_000,
  })

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-base font-semibold">Message preview</h2>
          {show && (
            <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5" role="group" aria-label="Preview format">
              {(['email', 'sms'] as const).map((t) => (
                <button
                  key={t}
                  aria-pressed={tab === t}
                  onClick={() => setTab(t)}
                  className={cn('rounded px-2 py-0.5 text-xs font-semibold uppercase', focusRing, tab === t ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text')}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
        </div>
        {!show ? (
          <Button size="sm" variant="outline" className={focusRing} onClick={() => setShow(true)}>
            <Eye className="h-3.5 w-3.5" /> Preview notification
          </Button>
        ) : isFetching ? (
          <div className="py-4 text-center text-muted"><Loader2 className="mx-auto h-4 w-4 animate-spin" /></div>
        ) : error != null ? (
          <div className="text-sm text-danger">Preview failed: {apiErrorMessage(error)}</div>
        ) : data ? (
          tab === 'email' ? (
            <div className="space-y-2">
              <div className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-sm">
                <span className="mr-1.5 text-xs font-semibold text-muted">Subject</span>
                {data.alert.subject}
              </div>
              <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-md bg-bg p-3 text-xs leading-relaxed text-text/90">{data.alert.email_body}</pre>
              {data.recovery && (
                <details className="text-sm">
                  <summary className={cn('cursor-pointer text-muted hover:text-text', focusRing)}>Recovery message</summary>
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-bg p-3 text-xs text-text/90">{data.recovery.email_body}</pre>
                </details>
              )}
            </div>
          ) : (
            <div className="rounded-md border border-border bg-surface2/40 p-3 font-mono text-xs leading-relaxed">{data.alert.sms_body}</div>
          )
        ) : null}
        <p className="mt-2 text-xs text-muted">Rendered with sample values — edit templates on the rule in Alert Rules.</p>
      </CardContent>
    </Card>
  )
}
