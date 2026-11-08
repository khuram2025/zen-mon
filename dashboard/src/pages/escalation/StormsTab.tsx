/**
 * Alert storms (correlation policy): when more than N assets report the same
 * kind of event inside a window, every channel gets ONE combined notification
 * instead of one per asset, then updates and a closing summary. Off by
 * default. Logic: app.services.alert_storm; model: features/escalation/storm.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CloudLightning, Info, Loader2, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton, SkeletonTable } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { fmtAlertDate } from '@/components/alerts/format'
import {
  STORM_FAMILIES, STORM_SCOPES, stormExample, stormFromApi, stormPolicyFromApi, stormSentence,
  validateStormPolicy, type Storm, type StormPolicy,
} from '@/features/escalation/storm'

export function StormsTab({ canManage, focusId }: { canManage: boolean; focusId?: string | null }) {
  return (
    <div className="space-y-4">
      <StormPolicyCard canManage={canManage} />
      <StormHistoryCard focusId={focusId ?? null} />
    </div>
  )
}

function StormPolicyCard({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({
    queryKey: ['alerts', 'storm-policy'],
    queryFn: async () => stormPolicyFromApi((await api.get('/alerts/storm-policy')).data),
  })
  const [edited, setEdited] = useState<StormPolicy | null>(null)
  const form = edited ?? q.data ?? null
  const save = useMutation({
    mutationFn: async (p: StormPolicy) => (await api.put('/alerts/storm-policy', p)).data,
    onSuccess: (_d, p) => {
      toast.success(p.enabled ? 'Alert storm policy saved' : 'Alert storm policy turned off')
      setEdited(null)
      qc.invalidateQueries({ queryKey: ['alerts', 'storm-policy'] })
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  if (q.isLoading) return <Skeleton className="h-64 w-full" />
  if (q.isError && !q.data) return <QueryFailure error={q.error} what="alert storm policy" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  if (!form || !q.data) return null
  const errors = validateStormPolicy(form)
  const invalid = Object.keys(errors).length > 0
  const dirty = JSON.stringify(form) !== JSON.stringify(q.data)
  const set = (patch: Partial<StormPolicy>) => setEdited({ ...form, ...patch })
  const num = (v: string) => (v.trim() === '' ? NaN : Number(v))
  const ro = !canManage

  return (
    <Card>
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold"><CloudLightning className="h-4 w-4 text-primary" aria-hidden /> Alert storm policy</h2>
            <p className="text-xs text-muted">{stormSentence(form)}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Switch checked={form.enabled} disabled={ro} onCheckedChange={(v) => set({ enabled: v })} aria-label="Combine alert storms" />
            <span className="text-sm">{form.enabled ? 'On' : 'Off'}</span>
          </div>
        </div>

        <div className="flex items-start gap-2 rounded-md border border-border bg-surface2/50 px-3 py-2 text-xs text-text2" role="note">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
          <span>
            Applies to every channel (email, SMS, webhooks, chat, PagerDuty, on-call pages) and to every priority, including critical.
            While a storm runs it replaces the individual notifications of its assets — their alerts, recoveries
            {form.hold_escalations ? ' and escalations' : ''} — with one message per channel and recipient. Incidents
            themselves are unchanged and stay in the Alert Center. Example with these settings: 40 assets down → {stormExample(form, 40)}.
          </span>
        </div>

        <fieldset className="space-y-1.5" disabled={ro}>
          <legend className="text-xs font-medium uppercase tracking-wider text-muted">Event types to combine</legend>
          <div className="flex flex-wrap gap-2">
            {STORM_FAMILIES.map((o) => {
              const on = form.families.includes(o.value)
              return (
                <button key={o.value} type="button" aria-pressed={on} title={o.hint} disabled={ro}
                  onClick={() => set({ families: on ? form.families.filter((x) => x !== o.value) : [...form.families, o.value] })}
                  className={cn('rounded-md border px-2.5 py-1 text-xs font-medium disabled:opacity-60', on ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text')}>
                  {o.label}
                </button>
              )
            })}
          </div>
          {errors.families && <p className="text-xs text-danger">{errors.families}</p>}
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <FormField label="Storm when more than (assets)" error={errors.threshold} hint="Up to this many are notified individually">
            <Input type="number" min={2} max={1000} disabled={ro} value={Number.isFinite(form.threshold) ? form.threshold : ''} onChange={(e) => set({ threshold: num(e.target.value) })} />
          </FormField>
          <FormField label="Within (minutes)" error={errors.window_minutes} hint="Also: storm ends after this long without a new event">
            <Input type="number" min={1} max={120} disabled={ro} value={Number.isFinite(form.window_minutes) ? form.window_minutes : ''} onChange={(e) => set({ window_minutes: num(e.target.value) })} />
          </FormField>
          <FormField label="Update every (minutes)" error={errors.update_minutes} hint="While the storm continues">
            <Input type="number" min={5} max={240} disabled={ro} value={Number.isFinite(form.update_minutes) ? form.update_minutes : ''} onChange={(e) => set({ update_minutes: num(e.target.value) })} />
          </FormField>
          <FormField label="Count assets" hint={STORM_SCOPES.find((s) => s.value === form.scope)?.hint}>
            <select className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm disabled:opacity-60" disabled={ro}
              value={form.scope} onChange={(e) => set({ scope: e.target.value as StormPolicy['scope'] })} aria-label="Count assets">
              {STORM_SCOPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </FormField>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={form.hold_escalations} disabled={ro} onCheckedChange={(v) => set({ hold_escalations: v })} aria-label="Fold escalations into the storm" />
          Fold escalation steps and reminders of storm incidents into the storm updates
        </label>

        {canManage && (
          <div className="flex flex-wrap justify-end gap-2">
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

function StormHistoryCard({ focusId }: { focusId: string | null }) {
  const [viewing, setViewing] = useState<string | null>(focusId)
  const q = useQuery({
    queryKey: ['alerts', 'storms'],
    queryFn: async () => ((await api.get('/alerts/storms')).data?.items ?? []).map(stormFromApi) as Storm[],
    refetchInterval: 30_000,
  })
  return (
    <Card>
      <CardContent className="p-0">
        <div className="border-b border-border p-4">
          <h2 className="text-sm font-semibold">Storms</h2>
          <p className="text-xs text-muted">Recent alert storms, the assets they affected and how many have recovered.</p>
        </div>
        {q.isLoading ? <div className="p-4"><SkeletonTable rows={3} cols={5} /></div>
          : q.isError && !q.data ? <div className="p-4"><QueryFailure error={q.error} what="alert storms" onRetry={() => void q.refetch()} retrying={q.isFetching} /></div>
            : (q.data ?? []).length === 0 ? <p className="p-6 text-center text-sm text-muted">No alert storms so far.</p>
              : (
                <Table>
                  <THead>
                    <Tr>
                      <Th>Event</Th>
                      <Th>Scope</Th>
                      <Th>Status</Th>
                      <Th>Started</Th>
                      <Th className="text-right">Assets</Th>
                      <Th className="text-right">Recovered</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {(q.data ?? []).map((s) => (
                      <Tr key={s.id} className="cursor-pointer hover:bg-surface2/50" onClick={() => setViewing(s.id)}>
                        <Td className="font-medium">{s.family_label}</Td>
                        <Td>{s.scope_label || 'All assets'}</Td>
                        <Td>
                          <Badge variant={s.status === 'active' ? 'danger' : 'outline'}>{s.status === 'active' ? 'Ongoing' : 'Over'}</Badge>
                        </Td>
                        <Td className="whitespace-nowrap text-xs">{fmtAlertDate(s.started_at)}{s.ended_at ? ` → ${fmtAlertDate(s.ended_at)}` : ''}</Td>
                        <Td className="text-right tabular-nums">{s.member_count}</Td>
                        <Td className="text-right tabular-nums">{s.recovered_count}</Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
      </CardContent>
      <StormDialog id={viewing} onClose={() => setViewing(null)} />
    </Card>
  )
}

function StormDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ['alerts', 'storms', id],
    enabled: !!id,
    queryFn: async () => stormFromApi((await api.get(`/alerts/storms/${id}`)).data),
  })
  const s = q.data
  return (
    <Dialog open={!!id} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{s ? `${s.family_label}${s.scope_label ? ` — ${s.scope_label}` : ''}` : 'Alert storm'}</DialogTitle>
          <DialogDescription>
            {s ? `${s.member_count} assets since ${fmtAlertDate(s.started_at)}; ${s.recovered_count} recovered. `
              + (s.status === 'active' ? 'Ongoing: individual notifications for these assets are paused.' : `Over since ${fmtAlertDate(s.ended_at)}.`) : ''}
          </DialogDescription>
        </DialogHeader>
        {q.isLoading ? <SkeletonTable rows={4} cols={3} />
          : q.isError ? <QueryFailure error={q.error} what="storm" onRetry={() => void q.refetch()} retrying={q.isFetching} />
            : (
              <div className="max-h-[60vh] overflow-auto">
                <Table>
                  <THead><Tr><Th>Asset</Th><Th>Joined</Th><Th>Notified</Th><Th>State</Th></Tr></THead>
                  <TBody>
                    {(s?.members ?? []).map((m) => (
                      <Tr key={m.alert_id ?? m.asset}>
                        <Td className="font-medium">{m.alert_id ? <Link className="text-primary hover:underline" to={`/alerts/${m.alert_id}`}>{m.asset}</Link> : m.asset}</Td>
                        <Td className="whitespace-nowrap text-xs">{fmtAlertDate(m.joined_at)}</Td>
                        <Td className="text-xs">{m.held ? 'In the storm summary' : 'Individually (before the storm)'}</Td>
                        <Td><Badge variant={m.recovered ? 'success' : 'warning'}>{m.recovered ? 'Recovered' : 'Affected'}</Badge></Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
      </DialogContent>
    </Dialog>
  )
}
