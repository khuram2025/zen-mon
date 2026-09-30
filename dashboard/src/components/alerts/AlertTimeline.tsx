/**
 * Lifecycle timeline (A06, A23, F01, F07): typed events oldest-first with
 * actor, source, reason and status transition, including escalations and
 * comments, plus the comment box for users who can triage.
 */
import { forwardRef, useState } from 'react'
import {
  AlarmClockOff, AlertTriangle, ArrowRight, BellOff, CheckCircle2, CircleDot, Hourglass, Loader2, MessageSquare,
  Megaphone, UserCheck, UserMinus, UserPlus, XCircle,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Textarea } from '@/components/ui/Textarea'
import { StatusChip, focusRing, useAlertActions } from './AlertBits'
import { fmtAgo, fmtAlertDate, parseTs } from './format'
import type { AlertDetail, AlertEvent, AlertStatus } from './types'

const ICON: Record<string, { icon: typeof CircleDot; cls: string }> = {
  triggered: { icon: AlertTriangle, cls: 'bg-danger/10 text-danger' },
  held: { icon: Hourglass, cls: 'bg-surface2 text-muted' },
  confirmed: { icon: AlertTriangle, cls: 'bg-danger/10 text-danger' },
  acknowledged: { icon: UserCheck, cls: 'bg-primary/10 text-primary' },
  assigned: { icon: UserPlus, cls: 'bg-primary/10 text-primary' },
  unassigned: { icon: UserMinus, cls: 'bg-surface2 text-muted' },
  comment: { icon: MessageSquare, cls: 'bg-surface2 text-text2' },
  snoozed: { icon: BellOff, cls: 'bg-muted/15 text-text2' },
  unsnoozed: { icon: AlarmClockOff, cls: 'bg-surface2 text-text2' },
  suppression_expired: { icon: AlarmClockOff, cls: 'bg-surface2 text-muted' },
  escalated: { icon: Megaphone, cls: 'bg-warning/10 text-warning' },
  resolved: { icon: XCircle, cls: 'bg-surface2 text-text2' },
  recovered: { icon: CheckCircle2, cls: 'bg-success/10 text-success' },
  superseded: { icon: XCircle, cls: 'bg-surface2 text-muted' },
  hold_cancelled: { icon: XCircle, cls: 'bg-surface2 text-muted' },
  closed: { icon: XCircle, cls: 'bg-surface2 text-text2' },
}

const SOURCE: Record<string, string> = { user: 'User', system: 'System', evaluator: 'Evaluator' }

/** Legacy payloads without `events`: build the minimum from timestamps. */
function synthesize(a: AlertDetail): AlertEvent[] {
  const ev: AlertEvent[] = [{ at: a.triggered_at, kind: 'triggered', label: 'Triggered', actor: null, source: 'evaluator', reason: null, old_status: null, new_status: 'active', detail: null }]
  if (a.acknowledged_at) ev.push({ at: a.acknowledged_at, kind: 'acknowledged', label: 'Acknowledged', actor: a.acknowledged_by, source: 'user', reason: null, old_status: 'active', new_status: 'acknowledged', detail: null })
  if (a.resolved_at) {
    const rec = a.closure?.reason === 'recovered'
    ev.push({ at: a.resolved_at, kind: rec ? 'recovered' : 'resolved', label: a.closure?.label ?? 'Resolved', actor: a.closure?.by ?? null, source: rec ? 'evaluator' : 'user', reason: a.closure?.note ?? null, old_status: null, new_status: 'resolved', detail: null })
  }
  return ev
}

function Transition({ from, to }: { from: AlertStatus | null; to: AlertStatus | null }) {
  if (!to || from === to) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {from && <StatusChip alert={{ status: from }} />}
      {from && <ArrowRight className="h-3 w-3 text-muted" aria-label="to" />}
      <StatusChip alert={{ status: to }} />
    </span>
  )
}

function detailText(e: AlertEvent): string | null {
  const d = e.detail
  if (!d) return null
  if (e.kind === 'escalated') {
    const parts: string[] = []
    if (d.level != null) parts.push(`Level ${d.level}`)
    const names = (d.channel_names ?? d.channels) as unknown
    if (Array.isArray(names) && names.length) parts.push(`notified ${names.join(', ')}`)
    if (d.repeat != null) parts.push(`repeat ${d.repeat}`)
    return parts.join(' · ') || null
  }
  if (e.kind === 'snoozed') {
    if (d.forever) return 'Indefinitely'
    if (typeof d.until === 'string') return `Until ${fmtAlertDate(d.until)}`
    if (typeof d.minutes === 'number') return `For ${d.minutes} min`
  }
  if (e.kind === 'assigned' && typeof d.assignee_name === 'string') return `To ${d.assignee_name}`
  if (e.kind === 'comment' && typeof d.text === 'string') return d.text
  return null
}

export const AlertTimeline = forwardRef<HTMLTextAreaElement, { alert: AlertDetail; now: number; canComment: boolean }>(
  function AlertTimeline({ alert, now, canComment }, commentRef) {
    const { comment } = useAlertActions()
    const [text, setText] = useState('')
    const events = (alert.events?.length ? [...alert.events] : synthesize(alert))
      .sort((a, b) => (parseTs(a.at) ?? 0) - (parseTs(b.at) ?? 0))
    const trimmed = text.trim()

    return (
      <Card>
        <CardContent className="p-4">
          <h2 className="mb-3 text-base font-semibold">Timeline</h2>
          {!alert.events?.length && (
            <p className="mb-2 text-xs text-muted">Detailed lifecycle events were not recorded for this alert; showing its timestamps.</p>
          )}
          <ol className="relative space-y-3 border-l border-border pl-5">
            {events.map((e, i) => {
              const m = ICON[e.kind] ?? { icon: CircleDot, cls: 'bg-surface2 text-muted' }
              const Icon = m.icon
              const extra = detailText(e)
              const who = e.actor?.name ?? SOURCE[e.source] ?? e.source
              return (
                <li key={`${e.at}-${i}`} className="relative">
                  <span className={cn('absolute -left-[31px] top-0 flex h-6 w-6 items-center justify-center rounded-full ring-4 ring-surface', m.cls)}>
                    <Icon className="h-3.5 w-3.5" aria-hidden />
                  </span>
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="text-sm font-semibold">{e.label}</span>
                    <span className="text-sm text-text2">
                      {e.kind === 'comment' ? '' : 'by '}{who}
                      {e.actor && <span className="ml-1 rounded bg-surface2 px-1 text-xs text-muted">{SOURCE[e.source] ?? e.source}</span>}
                    </span>
                    <span className="ml-auto text-xs text-muted" title={fmtAlertDate(e.at, { seconds: true, year: 'always' })}>
                      {fmtAlertDate(e.at, { seconds: true })} · {fmtAgo(e.at, now)}
                    </span>
                  </div>
                  {(e.old_status || e.new_status) && e.kind !== 'comment' && (
                    <div className="mt-1"><Transition from={e.old_status} to={e.new_status} /></div>
                  )}
                  {extra && e.kind !== 'comment' && <div className="mt-1 text-sm text-text2">{extra}</div>}
                  {(e.reason || (e.kind === 'comment' && extra)) && (
                    <blockquote className="mt-1 whitespace-pre-wrap break-words rounded-md border-l-2 border-border-strong bg-surface2/40 px-2.5 py-1.5 text-sm">
                      {e.reason ?? extra}
                    </blockquote>
                  )}
                </li>
              )
            })}
          </ol>

          {canComment && (
            <form
              className="mt-4 border-t border-border pt-3"
              onSubmit={(ev) => {
                ev.preventDefault()
                if (!trimmed || trimmed.length > 2000) return
                comment.mutate({ id: alert.id, text: trimmed }, { onSuccess: () => setText('') })
              }}
            >
              <label htmlFor="alert-comment" className="text-sm font-medium">Add a comment</label>
              <Textarea
                id="alert-comment"
                ref={commentRef}
                className="mt-1"
                maxLength={2000}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Findings, handover notes, ticket links…"
              />
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <span className="text-xs text-muted">{text.length}/2000 · visible to everyone who can see this alert</span>
                <Button type="submit" size="sm" className={focusRing} disabled={!trimmed || comment.isPending}>
                  {comment.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <MessageSquare className="h-3.5 w-3.5" />} Comment
                </Button>
              </div>
            </form>
          )}
        </CardContent>
      </Card>
    )
  },
)
