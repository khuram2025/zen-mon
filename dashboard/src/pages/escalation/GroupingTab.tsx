/**
 * Grouping (field report NOTIFY 02): P3/P4 trigger notifications for related
 * events wait a short group wait and go out as one summary; a per-recipient
 * hourly budget moves overflow into a digest instead of dropping it. Every
 * held message records why and when it is released.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Eye, Layers, Loader2, Save, Send, ShieldCheck, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton, SkeletonTable } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { SeverityBadge } from '@/components/alerts/AlertBits'
import { fmtAlertDate } from '@/components/alerts/format'
import { useDigest, useDigests, useGrouping } from '@/features/escalation/api'
import { DIGEST_STATUS, GROUP_BY_OPTIONS, groupingSentence, holdReasonLabel, validateGrouping } from '@/features/escalation/model'
import type { Digest, GroupingSettings } from '@/features/escalation/types'

const TONE = { info: 'info', success: 'success', danger: 'danger', warning: 'warning', muted: 'outline' } as const

export function GroupingTab({ canManage, focusId }: { canManage: boolean; focusId?: string | null }) {
  return (
    <div className="space-y-4">
      <GroupingSettingsCard canManage={canManage} />
      <DigestsCard canManage={canManage} focusId={focusId ?? null} />
    </div>
  )
}

function GroupingSettingsCard({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useGrouping()
  const [edited, setEdited] = useState<GroupingSettings | null>(null)
  const form = edited ?? q.data?.settings ?? null

  const save = useMutation({
    mutationFn: async (g: GroupingSettings) => (await api.put('/alerts/notification-grouping', g)).data,
    onSuccess: (_d, g) => {
      toast.success(g.enabled ? 'Grouping saved' : 'Grouping turned off')
      setEdited(null)
      qc.invalidateQueries({ queryKey: ['alerts', 'notification-grouping'] })
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (q.isLoading) return <Skeleton className="h-64 w-full" />
  if (q.isError && !q.data) return <QueryFailure error={q.error} what="grouping settings" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  if (!form || !q.data) return null
  const errors = validateGrouping(form)
  const invalid = Object.keys(errors).length > 0
  const dirty = JSON.stringify(form) !== JSON.stringify(q.data.settings)
  const set = (patch: Partial<GroupingSettings>) => setEdited({ ...form, ...patch })
  const num = (v: string) => (v.trim() === '' ? NaN : Number(v))
  const ro = !canManage

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold"><Layers className="h-4 w-4 text-primary" aria-hidden /> Warning grouping and delivery budget</h2>
            <p className="text-xs text-muted">{groupingSentence(form)}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Switch checked={form.enabled} disabled={ro} onCheckedChange={(v) => set({ enabled: v })} aria-label="Group warning notifications" />
            <span className="text-sm">{form.enabled ? 'On' : 'Off'}</span>
          </div>
        </div>

        <div className="flex items-start gap-2 rounded-md border border-success/30 bg-success/5 px-3 py-2 text-xs text-text2" role="note">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" aria-hidden />
          <span><b className="text-text">Critical reserve.</b> P1/P2 and critical notifications, recoveries, escalations and unresolved-incident updates are never grouped, delayed or counted against the budget{form.critical_reserve ? '; capacity is reserved for them' : ''}.</span>
        </div>

        <fieldset className="space-y-1.5" disabled={ro}>
          <legend className="text-xs font-medium uppercase tracking-wider text-muted">Group by</legend>
          <div className="flex flex-wrap gap-2">
            {GROUP_BY_OPTIONS.map((o) => {
              const on = form.group_by.includes(o.value)
              return (
                <button key={o.value} type="button" aria-pressed={on} title={o.hint} disabled={ro}
                  onClick={() => set({ group_by: on ? form.group_by.filter((x) => x !== o.value) : [...form.group_by, o.value] })}
                  className={cn('rounded-md border px-2.5 py-1 text-xs font-medium disabled:opacity-60', on ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text')}>
                  {o.label}
                </button>
              )
            })}
          </div>
          {errors.group_by && <p className="text-[11px] text-danger">{errors.group_by}</p>}
          <p className="text-[11px] text-muted">Recipients with different access are never merged: a digest is per group, channel and recipient.</p>
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <FormField label="Group wait (seconds)" error={errors.group_wait_seconds} hint="From the first held event">
            <Input type="number" min={0} max={3600} disabled={ro} value={Number.isFinite(form.group_wait_seconds) ? form.group_wait_seconds : ''} onChange={(e) => set({ group_wait_seconds: num(e.target.value) })} />
          </FormField>
          <FormField label="Group interval (seconds)" error={errors.group_interval_seconds} hint="Later events join the open digest">
            <Input type="number" min={0} max={86400} disabled={ro} value={Number.isFinite(form.group_interval_seconds) ? form.group_interval_seconds : ''} onChange={(e) => set({ group_interval_seconds: num(e.target.value) })} />
          </FormField>
          <FormField label="Max incidents per digest" error={errors.max_items}>
            <Input type="number" min={1} max={500} disabled={ro} value={Number.isFinite(form.max_items) ? form.max_items : ''} onChange={(e) => set({ max_items: num(e.target.value) })} />
          </FormField>
          <FormField label="Budget per recipient / hour" error={errors.budget_per_recipient_per_hour} hint="0 = no budget">
            <Input type="number" min={0} max={1000} disabled={ro} value={Number.isFinite(form.budget_per_recipient_per_hour) ? form.budget_per_recipient_per_hour : ''} onChange={(e) => set({ budget_per_recipient_per_hour: num(e.target.value) })} />
          </FormField>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={form.critical_reserve} disabled={ro} onCheckedChange={(v) => set({ critical_reserve: v })} aria-label="Reserve capacity for critical notifications" />
          Reserve capacity for critical notifications
        </label>

        {canManage && (
          <div className="flex flex-wrap justify-end gap-2">
            {!q.data.settings.enabled && (
              <Button variant="outline" size="sm" disabled={save.isPending} onClick={() => save.mutate({ ...q.data!.recommended, enabled: true })}>
                <Sparkles className="h-3.5 w-3.5" aria-hidden /> Enable recommended
              </Button>
            )}
            {dirty && <Button variant="ghost" size="sm" onClick={() => setEdited(null)}>Discard</Button>}
            <Button size="sm" onClick={() => save.mutate(form)} disabled={!dirty || invalid || save.isPending}>
              {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Save className="h-3.5 w-3.5" aria-hidden />} Save
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

const STATUS_FILTERS = [
  { key: 'open', label: 'Holding' },
  { key: 'sent', label: 'Sent' },
  { key: 'failed', label: 'Failed' },
  { key: 'cancelled', label: 'Cancelled' },
  { key: '', label: 'All' },
]

function DigestsCard({ canManage, focusId }: { canManage: boolean; focusId: string | null }) {
  const qc = useQueryClient()
  const [status, setStatus] = useState('open')
  // A "View digest" link from an incident opens that digest.
  const [viewing, setViewing] = useState<string | null>(focusId)
  const [releasing, setReleasing] = useState<Digest | null>(null)
  const q = useDigests(status)
  const release = useMutation({
    mutationFn: async (id: string) => (await api.post(`/alerts/digests/${id}/release`)).data,
    onSuccess: () => { toast.success('Digest released', 'The summary is being sent now.'); setReleasing(null); qc.invalidateQueries({ queryKey: ['alerts'] }) },
    onError: (e) => toast.error('Release failed', apiErrorMessage(e)),
  })
  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex flex-col gap-2 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-semibold">Digests</h2>
            <p className="text-xs text-muted">Held notifications, why they were held and when they go out. Nothing is dropped.</p>
          </div>
          <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5" role="group" aria-label="Digest status">
            {STATUS_FILTERS.map((f) => (
              <button key={f.key || 'all'} type="button" aria-pressed={status === f.key} onClick={() => setStatus(f.key)}
                className={cn('rounded px-2 py-0.5 text-xs font-medium', status === f.key ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text')}>
                {f.label}
              </button>
            ))}
          </div>
        </div>
        {q.isLoading ? <div className="p-4"><SkeletonTable rows={3} cols={6} /></div>
          : q.isError && !q.data ? <div className="p-4"><QueryFailure error={q.error} what="digests" onRetry={() => void q.refetch()} retrying={q.isFetching} /></div>
            : (q.data ?? []).length === 0 ? <p className="p-6 text-center text-sm text-muted">No {status ? STATUS_FILTERS.find((f) => f.key === status)?.label.toLowerCase() : ''} digests.</p>
              : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>Group</Th>
                      <Th>Recipient</Th>
                      <Th>Status</Th>
                      <Th>Reason · release condition</Th>
                      <Th>Release</Th>
                      <Th className="text-right">Items</Th>
                      <Th className="w-[120px]"><span className="sr-only">Actions</span></Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {(q.data ?? []).map((d) => {
                      const st = DIGEST_STATUS[d.status] ?? { label: d.status, tone: 'muted' as const }
                      return (
                        <Tr key={d.id}>
                          <Td className="max-w-[220px]"><div className="truncate font-medium" title={d.group_key}>{d.group_label || d.group_key}</div></Td>
                          <Td className="max-w-[200px] text-xs"><div className="truncate">{d.recipient || '—'}</div>{d.channel_name && <div className="truncate text-muted">{d.channel_name}</div>}</Td>
                          <Td><Badge variant={TONE[st.tone]}>{st.label}</Badge></Td>
                          <Td className="max-w-[280px] text-xs"><div className="font-medium">{holdReasonLabel(d.reason)}</div><div className="text-muted">{d.release_condition || '—'}</div>{d.error && <div className="text-danger">{d.error}</div>}</Td>
                          <Td className="whitespace-nowrap text-xs">{d.sent_at ? `Sent ${fmtAlertDate(d.sent_at)}` : fmtAlertDate(d.release_at)}</Td>
                          <Td className="text-right tabular-nums">{d.item_count}</Td>
                          <Td>
                            <div className="flex justify-end gap-1">
                              <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => setViewing(d.id)} aria-label={`View digest ${d.group_label || d.group_key}`}><Eye className="h-3.5 w-3.5" aria-hidden /></Button>
                              {canManage && (d.status === 'open' || d.status === 'failed') && (
                                <Button size="sm" variant="outline" className="h-7 px-2" onClick={() => setReleasing(d)} aria-label={`Release digest ${d.group_label || d.group_key} now`}>
                                  <Send className="h-3.5 w-3.5" aria-hidden /> Release
                                </Button>
                              )}
                            </div>
                          </Td>
                        </Tr>
                      )
                    })}
                  </TBody>
                </Table>
              )}
      </CardContent>
      {viewing && <DigestDialog id={viewing} onClose={() => setViewing(null)} canManage={canManage} onRelease={(d) => { setViewing(null); setReleasing(d) }} />}
      <ConfirmDialog
        open={!!releasing}
        onOpenChange={(o) => { if (!o) setReleasing(null) }}
        title="Send this digest now?"
        description={releasing ? `${releasing.item_count} held notification${releasing.item_count === 1 ? '' : 's'} for ${releasing.recipient || 'this recipient'} go out as one summary immediately, instead of ${releasing.release_at ? `at ${fmtAlertDate(releasing.release_at)}` : 'at the release time'}.` : undefined}
        confirmText="Release now"
        loading={release.isPending}
        onConfirm={() => { if (releasing) release.mutate(releasing.id) }}
      />
    </Card>
  )
}

function DigestDialog({ id, onClose, canManage, onRelease }: { id: string; onClose: () => void; canManage: boolean; onRelease: (d: Digest) => void }) {
  const q = useDigest(id)
  const d = q.data
  const st = d ? DIGEST_STATUS[d.status] ?? { label: d.status, tone: 'muted' as const } : null
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-2xl">
        <DialogHeader>
          <DialogTitle>{d?.group_label || d?.group_key || 'Digest'}</DialogTitle>
          <DialogDescription>{d ? `${holdReasonLabel(d.reason)} — ${d.release_condition || 'released at the release time'}` : 'Loading…'}</DialogDescription>
        </DialogHeader>
        {q.isLoading ? <Skeleton className="h-40 w-full" /> : q.isError ? <QueryFailure error={q.error} what="the digest" onRetry={() => void q.refetch()} /> : d ? (
          <div className="space-y-3">
            <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-2">
              <div><dt className="inline text-muted">Status: </dt><dd className="inline">{st && <Badge variant={TONE[st.tone]}>{st.label}</Badge>}</dd></div>
              <div><dt className="inline text-muted">Recipient: </dt><dd className="inline break-all">{d.recipient || '—'}{d.channel_name ? ` (${d.channel_name})` : ''}</dd></div>
              <div><dt className="inline text-muted">Opened: </dt><dd className="inline">{fmtAlertDate(d.opened_at)}</dd></div>
              <div><dt className="inline text-muted">{d.sent_at ? 'Sent' : 'Release'}: </dt><dd className="inline">{fmtAlertDate(d.sent_at ?? d.release_at)}</dd></div>
            </dl>
            {d.error && <p className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{d.error}</p>}
            <ul className="max-h-80 divide-y divide-border overflow-y-auto rounded-md border border-border">
              {d.items.length === 0 && <li className="p-3 text-sm text-muted">No items.</li>}
              {d.items.map((it) => (
                <li key={it.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                  {it.severity && <SeverityBadge severity={it.severity} />}
                  {it.alert_id ? (
                    <Link to={`/alerts/${it.alert_id}`} onClick={onClose} className="min-w-0 flex-1 truncate font-medium text-primary hover:underline">{it.message || `Incident ${it.alert_id.slice(0, 8)}`}</Link>
                  ) : <span className="min-w-0 flex-1 truncate">{it.message || it.event}</span>}
                  {it.priority && <span className="text-xs font-semibold text-text2">{it.priority}</span>}
                  {it.already_recovered && <Badge variant="success">Already recovered</Badge>}
                  <span className="text-xs text-muted">{fmtAlertDate(it.added_at)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Close</Button>
          {canManage && (d?.status === 'open' || d?.status === 'failed') && <Button onClick={() => onRelease(d)}><Send className="h-4 w-4" aria-hidden /> Release now</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
