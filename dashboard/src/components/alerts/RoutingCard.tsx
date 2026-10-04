/**
 * Routing & delivery (A10, A19, A24, F04, F07).
 *
 * "Configured routing" is today's rule policy; "Delivery attempts" are what
 * was actually recorded for this alert. The two are never conflated: an empty
 * attempts list says nothing was recorded, not that delivery failed.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Pencil } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { ChannelIcons, focusRing } from './AlertBits'
import { fmtAgo, fmtAlertDate } from './format'
import type { AlertChannelRef, AlertDelivery, AlertDetail, AlertRuleDetail } from './types'

const OUTCOME: Record<string, { label: string; cls: string }> = {
  accepted: { label: 'Accepted', cls: 'border-border bg-surface2 text-text2' },
  failed: { label: 'Failed', cls: 'border-danger/40 bg-danger/10 text-danger' },
  skipped: { label: 'Skipped', cls: 'border-dashed border-border text-muted' },
}

const KIND_LABEL: Record<string, string> = {
  trigger: 'Alert', recovery: 'Recovery', escalation: 'Escalation', all_clear: 'All clear', reminder: 'Reminder', closure: 'Closure',
}

export function RoutingCard({ alert, now }: { alert: AlertDetail; now: number }) {
  const can = useCan()
  const [editOpen, setEditOpen] = useState(false)
  const rule = alert.rule
  const channels = alert.channels ?? []
  const deliveries = [...(alert.deliveries ?? [])].sort((a, b) => (a.at < b.at ? 1 : -1))
  const canRoute = !!rule && rule.editable && can('alerts.manage')
  const failed = deliveries.filter((d) => d.outcome === 'failed').length
  const esc = alert.escalation_state

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <h2 className="text-base font-semibold">Routing &amp; delivery</h2>

        <section aria-labelledby="routing-h">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <h3 id="routing-h" className="text-sm font-semibold text-text2">Configured routing <span className="font-normal text-muted">(current policy)</span></h3>
            {canRoute && (
              <Button size="sm" variant="ghost" className={cn('h-7 px-2', focusRing)} onClick={() => setEditOpen(true)} aria-label="Edit notification routing for this rule">
                <Pencil className="h-3.5 w-3.5" /> Edit
              </Button>
            )}
          </div>
          {!rule ? (
            <p className="text-sm text-muted">No rule linked — any routing came from the alert's producer and is not shown here.</p>
          ) : channels.length === 0 ? (
            <div className="rounded-md border border-dashed border-warning/40 bg-warning/5 p-2.5 text-sm text-warning">
              No channels configured — this rule records alerts but notifies nobody.
              {can('alerts.manage') && <> <Link to="/channels" className={cn('underline', focusRing)}>Manage channels</Link></>}
            </div>
          ) : (
            <ul className="space-y-1">
              {channels.map((c) => (
                <li key={c.id} className={cn('flex items-center gap-2 rounded-md border border-border/60 bg-surface2/30 px-2.5 py-1.5', !c.enabled && 'opacity-60')}>
                  <ChannelIcons channels={[c]} />
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">{c.name}</span>
                  <span className="text-xs uppercase tracking-wide text-muted">{c.type}</span>
                  {!c.enabled && <span className="rounded border border-border px-1 text-xs text-muted">disabled</span>}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-1 text-xs text-muted">What the rule notifies today — not proof that anyone was notified.</p>
        </section>

        <section aria-labelledby="delivery-h">
          <h3 id="delivery-h" className="mb-1.5 text-sm font-semibold text-text2">
            Delivery attempts
            {deliveries.length > 0 && (
              <span className="ml-1 font-normal text-muted">
                · {deliveries.length} recorded{failed ? <>, <span className="text-danger">{failed} failed</span></> : ''}
              </span>
            )}
          </h3>
          {deliveries.length === 0 ? (
            <p className="rounded-md border border-dashed border-border p-2.5 text-sm text-muted">No delivery attempts recorded for this alert.</p>
          ) : (
            <ul className="max-h-72 space-y-1 overflow-y-auto">
              {deliveries.map((d, i) => <DeliveryRow key={`${d.at}-${i}`} d={d} now={now} />)}
            </ul>
          )}
        </section>

        {esc && (
          <section aria-labelledby="esc-h" className="rounded-md border border-border bg-surface2/30 p-2.5 text-sm">
            <h3 id="esc-h" className="mb-0.5 text-sm font-semibold text-text2">Escalation</h3>
            <p>
              Level <b>{esc.level}</b>
              {esc.last_at ? <> · last escalated {fmtAgo(esc.last_at, now)} <span className="text-muted">({fmtAlertDate(esc.last_at)})</span></> : null}
              {esc.repeats ? <> · {esc.repeats} repeat{esc.repeats === 1 ? '' : 's'}</> : null}
              {esc.closed ? <span className="text-muted"> · closed</span> : null}
            </p>
          </section>
        )}
      </CardContent>
      {canRoute && editOpen && rule && <ChannelsEditDialog rule={rule} onClose={() => setEditOpen(false)} />}
    </Card>
  )
}

function DeliveryRow({ d, now }: { d: AlertDelivery; now: number }) {
  const o = OUTCOME[d.outcome] ?? OUTCOME.skipped
  const ch: AlertChannelRef[] = d.channel_type ? [{ id: d.channel_id ?? d.at, name: d.channel_name ?? d.channel_type, type: d.channel_type, enabled: true }] : []
  return (
    <li className="rounded-md border border-border/60 px-2.5 py-1.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {ch.length > 0 && <ChannelIcons channels={ch} />}
        <span className="min-w-0 flex-1 truncate font-medium">{d.channel_name ?? 'Unknown channel'}</span>
        <span className="text-xs text-muted">{KIND_LABEL[d.kind] ?? d.kind}</span>
        <span className={cn('rounded border px-1.5 py-px text-xs font-medium', o.cls)}>{o.label}</span>
      </div>
      <div className="mt-0.5 text-xs text-muted" title={fmtAlertDate(d.at, { seconds: true, year: 'always' })}>
        {fmtAgo(d.at, now)}{d.latency_ms != null ? ` · ${d.latency_ms.toLocaleString()} ms` : ''}
      </div>
      {d.error && <div className="mt-0.5 break-words text-xs text-danger">{d.error}</div>}
    </li>
  )
}

/** Routing editor — only for alerts.manage; seeded once per open (mounted only while open). */
function ChannelsEditDialog({ rule, onClose }: { rule: AlertRuleDetail; onClose: () => void }) {
  const qc = useQueryClient()
  const [initial] = useState(() => new Set(rule.notify_channels || []))
  const [selected, setSelected] = useState<Set<string>>(() => new Set(rule.notify_channels || []))
  const { data, error, isLoading, refetch } = useQuery<{ data: AlertChannelRef[] }>({
    queryKey: ['alerts', 'channel-directory'],
    queryFn: async () => (await api.get('/alerts/channel-directory')).data,
  })
  const all = data?.data ?? []
  const dirty = selected.size !== initial.size || [...selected].some((id) => !initial.has(id))

  const save = useMutation({
    mutationFn: async () => (await api.put(`/alert-rules/${rule.id}`, { notify_channels: [...selected] })).data,
    onSuccess: () => {
      toast.success('Notification routing updated')
      qc.invalidateQueries({ queryKey: ['alerts'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onClose()
    },
    onError: (e) => toast.error('Update failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-2rem)] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Notification routing</DialogTitle>
          <DialogDescription>
            Rule “{rule.name}”
            {rule.affected?.shared ? ` — shared by ${rule.affected.count?.toLocaleString() ?? 'several'} ${rule.affected.noun}; routing changes apply to all of them.` : '.'}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5 text-sm">
          {isLoading && <div className="flex items-center gap-2 py-3 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading channels…</div>}
          {error != null && (
            <div className="rounded-md border border-danger/30 bg-danger/5 p-3 text-danger">
              Could not load channels: {apiErrorMessage(error)}{' '}
              <button className="underline" onClick={() => refetch()}>Retry</button>
            </div>
          )}
          {!isLoading && !error && all.length === 0 && (
            <p className="text-muted">
              No notification channels exist yet — create one in{' '}
              <Link to="/channels" className={cn('text-primary hover:underline', focusRing)} onClick={onClose}>Channels</Link>.
            </p>
          )}
          {all.map((c) => (
            <label key={c.id} className={cn('flex cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2', selected.has(c.id) ? 'border-primary/50 bg-primary/5' : 'border-border bg-surface2/30')}>
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={selected.has(c.id)}
                onChange={(e) => {
                  const next = new Set(selected)
                  if (e.target.checked) next.add(c.id); else next.delete(c.id)
                  setSelected(next)
                }}
              />
              <ChannelIcons channels={[c]} />
              <span className="min-w-0 flex-1 truncate font-medium">{c.name}</span>
              <span className="text-xs uppercase tracking-wide text-muted">{c.type}</span>
              {!c.enabled && <span className="rounded border border-border px-1 text-xs text-muted">disabled</span>}
            </label>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || !!error || !dirty}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />} Save routing
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
