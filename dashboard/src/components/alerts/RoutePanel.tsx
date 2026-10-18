/**
 * Escalation route for one incident (GET /alerts/{id}/route), the notices for
 * notifications held in a digest, and the explicit acknowledgement
 * confirmation behind the email "Acknowledge" link (/alerts/<id>?ack=1).
 *
 * The link is a GET that mail scanners may follow, so it never mutates: it
 * only opens AckConfirmDialog, and the POST is sent by its confirm button.
 */
import { Link } from 'react-router-dom'
import { AlertTriangle, Check, ChevronsUp, Clock, Inbox, Loader2, ShieldAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { useAlertRoute } from '@/features/escalation/api'
import { ROUTE_SOURCE_LABEL, ackDialogState, currentStep, holdReasonLabel, holdsFromApi, nextEscalationText, reviewText, type AckDialogState } from '@/features/escalation/model'
import { PreviewSteps, PriorityBadge } from '@/features/escalation/ui'
import { fmtAgo, fmtAlertDate } from './format'
import { focusRing } from './AlertBits'
import type { AlertDetail } from './types'

export function RoutePanel({ alert, now }: { alert: AlertDetail; now: number }) {
  const q = useAlertRoute(alert.id)
  const fmt = (iso: string) => fmtAlertDate(iso)
  const route = q.data
  const notFound = (q.error as { response?: { status?: number } } | null)?.response?.status === 404
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 text-base font-semibold"><ChevronsUp className="h-4 w-4 text-primary" aria-hidden /> Escalation route</h2>
          <PriorityBadge priority={route?.priority ?? alert.priority} />
        </div>
        {q.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Explaining the route…</div>
        ) : q.isError || !route ? (
          <p className="text-sm text-muted">{notFound ? 'This appliance does not explain escalation routes yet.' : 'The route explanation could not be loaded.'}</p>
        ) : (
          <>
            <dl className="space-y-1 text-sm">
              <div className="flex flex-wrap gap-x-2">
                <dt className="text-muted">Policy</dt>
                <dd className="min-w-0 font-medium">
                  {route.policy?.id && route.policy.source !== 'legacy_levels' ? (
                    <Link to={`/escalation?tab=policies&id=${encodeURIComponent(route.policy.id)}`} className={cn('text-primary hover:underline', focusRing)}>{route.policy.name ?? 'Escalation policy'}</Link>
                  ) : route.policy?.source === 'legacy_levels' ? 'Rule escalation tiers' : route.policy?.name ?? 'None'}
                  <span className="ml-1 text-xs font-normal text-muted">· {ROUTE_SOURCE_LABEL[route.policy?.source ?? 'none']}</span>
                </dd>
              </div>
              {route.state && (
                <div className="flex flex-wrap gap-x-2">
                  <dt className="text-muted">State</dt>
                  <dd>
                    {(() => {
                      const cur = currentStep(route)
                      if (cur) return route.policy?.source === 'legacy_levels' ? cur.name ?? `Level ${cur.position}` : `Step ${cur.position + 1}${cur.name ? ` · ${cur.name}` : ''}`
                      return 'Not escalated yet'
                    })()}
                    {route.state.repeats ? ` · repeated ${route.state.repeats}×` : ''}
                    {route.state.acknowledged ? <span className="ml-1 inline-flex items-center gap-0.5 text-success"><Check className="h-3.5 w-3.5" aria-hidden />acknowledged</span> : <span className="ml-1 text-warning">unacknowledged</span>}
                  </dd>
                </div>
              )}
            </dl>
            <p className="flex items-start gap-1.5 rounded-md border border-border bg-surface2/40 px-2.5 py-2 text-sm" role="status">
              <Clock className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
              <span>
                {nextEscalationText(route, fmt)}
                {route.state?.next_at && <span className="text-muted"> ({fmtAgo(route.state.next_at, now)})</span>}
              </span>
            </p>
            {route.steps.length > 0 && <PreviewSteps steps={route.steps} fmt={fmt} />}
            {reviewText(route.review ?? null) && <p className="text-xs text-text2">{reviewText(route.review ?? null)}</p>}
            {route.warnings.length > 0 && (
              <ul className="space-y-0.5">
                {route.warnings.map((w) => <li key={w} className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />{w}</li>)}
              </ul>
            )}
            <p className="text-[11px] text-muted">Recipients are resolved from who is on call now. Accepted by the mail server is not proof that someone read it.</p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

/** "Held in a digest" notices: why each notification waits and when it is released. */
export function NotificationHolds({ alert }: { alert: AlertDetail }) {
  const holds = holdsFromApi(alert.notification_holds).filter((h) => h.status === 'open' || h.status === 'sending')
  if (!holds.length) return null
  return (
    <div role="status" className="space-y-1 rounded-md border border-info/40 bg-info/10 px-3 py-2 text-sm text-text2">
      {holds.map((h, i) => (
        <div key={`${h.digest_id}-${i}`} className="flex flex-wrap items-start gap-2">
          <Inbox className="mt-0.5 h-4 w-4 shrink-0 text-info" aria-hidden />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-text">Notification held: {holdReasonLabel(h.reason)}.</span>{' '}
            {h.release_condition || 'It is sent with the next digest'}
            {h.release_at ? ` — release ${fmtAlertDate(h.release_at)}` : ''}. Nothing is dropped.
          </span>
          {h.digest_id && (
            <Link to={`/escalation?tab=grouping&id=${encodeURIComponent(h.digest_id)}`} className={cn('text-xs text-primary hover:underline', focusRing)}>View digest</Link>
          )}
        </div>
      ))}
    </div>
  )
}

const ACK_COPY: Record<AckDialogState, { title: string; body: string }> = {
  confirm: { title: 'Acknowledge this incident?', body: 'You become the owner, escalation to further steps stops, and unresolved-incident updates continue until it is resolved. The incident stays open.' },
  already: { title: 'Already acknowledged', body: 'Someone has acknowledged this incident; nothing more is needed.' },
  closed: { title: 'Incident already closed', body: 'This incident is resolved or closed, so there is nothing to acknowledge.' },
  pending: { title: 'Not confirmed yet', body: 'This incident is still within its hold time and cannot be acknowledged until it is confirmed.' },
  forbidden: { title: 'You cannot acknowledge alerts', body: 'Your role does not include the alerts.acknowledge permission. Ask an operator to acknowledge it.' },
}

/** The dialog body; rendering it never acknowledges anything. */
export function AckConfirmBody({ state, message, acknowledgedBy, pending, onConfirm, onCancel }: {
  state: AckDialogState
  message: string
  acknowledgedBy?: string | null
  pending?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const copy = ACK_COPY[state]
  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          {state === 'confirm' ? <ShieldAlert className="h-5 w-5 text-warning" aria-hidden /> : <Check className="h-5 w-5 text-muted" aria-hidden />}
          {copy.title}
        </DialogTitle>
        <DialogDescription>{copy.body}{state === 'already' && acknowledgedBy ? ` Acknowledged by ${acknowledgedBy}.` : ''}</DialogDescription>
      </DialogHeader>
      <p className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-sm font-medium">{message}</p>
      <DialogFooter>
        <Button variant="outline" onClick={onCancel} disabled={pending}>{state === 'confirm' ? 'Cancel' : 'Close'}</Button>
        {state === 'confirm' && (
          <Button onClick={onConfirm} disabled={pending} aria-label={`Acknowledge this incident: ${message}`}>
            {pending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Check className="h-4 w-4" aria-hidden />} Acknowledge this incident
          </Button>
        )}
      </DialogFooter>
    </>
  )
}

export function AckConfirmDialog({ open, alert, canAcknowledge, message, pending, onConfirm, onClose }: {
  open: boolean
  alert: AlertDetail
  canAcknowledge: boolean
  message: string
  pending: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const state = ackDialogState(alert, canAcknowledge)
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && !pending) onClose() }}>
      <DialogContent className="max-w-md">
        <AckConfirmBody state={state} message={message} acknowledgedBy={alert.acknowledged_by?.name} pending={pending} onConfirm={onConfirm} onCancel={onClose} />
      </DialogContent>
    </Dialog>
  )
}
