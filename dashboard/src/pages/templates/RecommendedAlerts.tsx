/* A template's recommended alert rules (migrate-193, app.services.rule_packs): the template's own rule pack
 * (built-ins: interface rules on roles) and "above warn / above crit" for every gauge metric with display
 * thresholds. Nothing is created until an operator accepts one; Accept makes one ordinary alert rule scoped
 * to the template. A later template revision never changes an accepted rule: a different recommendation is
 * shown as "changed" with Apply (update the rule) or Keep (leave it, stop showing the notice). */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Loader2, ThumbsUp } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { QueryFailure } from '@/components/ui/QueryState'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { SeverityBadge } from '@/components/alerts/AlertBits'
import { changeWords, conditionWords, holdWords, roleLabel } from '@/components/alerts/thresholdModel'
import { type Recommendation, type TemplateDetail, recommendationPath, templatesApi } from './api'

const STATUS_BADGE: Record<Recommendation['status'], { label: string; variant: 'outline' | 'success' | 'warning' }> = {
  available: { label: 'available', variant: 'outline' },
  accepted: { label: 'accepted', variant: 'success' },
  changed: { label: 'changed', variant: 'warning' },
}

/** key → metric name and unit over the template's groups (and the parent's, on a customised template). */
function metricNames(t: TemplateDetail): { names: Record<string, string>; units: Record<string, string> } {
  const names: Record<string, string> = {}
  const units: Record<string, string> = {}
  for (const g of [...(t.parent_groups || []), ...(t.oid_groups || [])]) {
    for (const m of g.metrics || []) { names[m.key] = m.name; if (m.unit) units[m.key] = m.unit }
  }
  return { names, units }
}

/** The recommendation's condition in words, with the template metric's unit. */
function itemWords(it: Recommendation, meta: { names: Record<string, string>; units: Record<string, string> }): string {
  const unit = it.metric.startsWith('tpl_') ? meta.units[it.metric.slice(4)] ?? '' : undefined
  return conditionWords({ metric: it.metric, operator: it.operator, threshold: it.threshold, reset_threshold: it.reset_threshold },
    { names: meta.names, unit })
}

export function RecommendedAlertsTab({ t }: { t: TemplateDetail }) {
  const can = useCan()
  const canManage = can('alerts.manage')
  const qc = useQueryClient()
  const key = ['device-template-recommended', t.id, t.revision]
  const q = useQuery({ queryKey: key, queryFn: () => templatesApi.recommended(t.id) })
  const meta = useMemo(() => metricNames(t), [t])
  const [accepting, setAccepting] = useState<Recommendation | null>(null)
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['device-template-recommended', t.id] })
    qc.invalidateQueries({ queryKey: ['alert-rules'] })
  }
  const act = useMutation({
    mutationFn: async ({ item, action }: { item: Recommendation; action: 'apply' | 'keep' }) =>
      (await api.post(recommendationPath(t.id, item.key, action))).data,
    onSuccess: (_d, v) => {
      toast.success(v.action === 'apply' ? 'Rule updated to the recommendation' : 'Rule kept as it is',
        v.action === 'apply' ? v.item.accepted?.name : 'The notice is cleared until the recommendation changes again.')
      refresh()
    },
    onError: (e) => toast.error('Failed', apiErrorMessage(e)),
  })

  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="recommended alerts" />
  const items = q.data || []
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        Nothing is created until you accept a recommendation; a later template revision never changes an accepted rule.
      </p>
      <div className="overflow-x-auto rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60">
            <Tr><Th>Recommendation</Th><Th>Condition</Th><Th>Severity</Th><Th>Source</Th><Th>Status</Th><Th className="text-right" /></Tr>
          </THead>
          <TBody>
            {items.map((it) => {
              const acc = it.accepted
              const inherited = !!acc && !acc.on_this_template
              const busy = act.isPending && act.variables?.item.key === it.key
              return (
                <Tr key={it.key} className="align-top">
                  <Td>
                    <div className="font-medium">{it.name}</div>
                    {it.description && <div className="max-w-md text-xs text-muted">{it.description}</div>}
                  </Td>
                  <Td className="text-sm">
                    {itemWords(it, meta)}
                    {' '}{holdWords(it.min_duration)}
                    {it.if_role && <div className="text-xs text-muted">on {roleLabel(it.if_role)} interfaces</div>}
                  </Td>
                  <Td><SeverityBadge severity={it.severity} />{it.priority && <span className="ml-1 text-xs text-muted">{it.priority}</span>}</Td>
                  <Td className="text-xs text-muted">{it.source === 'template' ? 'template' : 'from threshold'}</Td>
                  <Td>
                    {inherited ? (
                      <span className="text-xs text-muted">Accepted on {acc!.template_name || 'the parent template'}</span>
                    ) : (
                      <>
                        <Badge variant={STATUS_BADGE[it.status].variant}>{STATUS_BADGE[it.status].label}</Badge>
                        {acc && (
                          <div className="mt-0.5 text-xs">
                            <Link to="/alert-rules" className="text-primary hover:underline">{acc.name}</Link>
                            {!acc.enabled && <span className="text-muted"> (off)</span>}
                          </div>
                        )}
                        {it.status === 'changed' && it.changes.length > 0 && (
                          <ul className="mt-1 space-y-0.5 text-xs text-warning-fg" aria-label="What the recommendation changed">
                            {it.changes.map((c) => <li key={c.field}>{changeWords(c)}</li>)}
                          </ul>
                        )}
                      </>
                    )}
                  </Td>
                  <Td className="text-right">
                    {canManage && !inherited && (
                      <div className="flex justify-end gap-1">
                        {it.status === 'available' && (
                          <Button size="sm" variant="outline" onClick={() => setAccepting(it)}><ThumbsUp className="h-3.5 w-3.5" /> Accept</Button>
                        )}
                        {it.status === 'changed' && (
                          <>
                            <Button size="sm" disabled={busy} onClick={() => act.mutate({ item: it, action: 'apply' })}>
                              {busy && act.variables?.action === 'apply' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />} Apply
                            </Button>
                            <Button size="sm" variant="outline" disabled={busy} onClick={() => act.mutate({ item: it, action: 'keep' })}>Keep</Button>
                          </>
                        )}
                      </div>
                    )}
                  </Td>
                </Tr>
              )
            })}
            {!q.isLoading && items.length === 0 && (
              <Tr><Td colSpan={6} className="py-8 text-center text-sm text-muted">
                This template recommends no alert rules: it has no rule pack and no metric with thresholds.
              </Td></Tr>
            )}
            {q.isLoading && (
              <Tr><Td colSpan={6} className="py-8 text-center text-sm text-muted"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />Loading…</Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
      {accepting && <AcceptDialog t={t} item={accepting} meta={meta} onClose={() => setAccepting(null)} onDone={() => { setAccepting(null); refresh() }} />}
    </div>
  )
}

const CHANNEL_TYPE_LABEL: Record<string, string> = {
  email: 'Email', sms: 'SMS', webhook: 'Webhook', slack: 'Slack',
  telegram: 'Telegram', teams: 'Teams', discord: 'Discord', pagerduty: 'PagerDuty',
}

function AcceptDialog({ t, item, meta, onClose, onDone }: {
  t: TemplateDetail; item: Recommendation; meta: { names: Record<string, string>; units: Record<string, string> }
  onClose: () => void; onDone: () => void
}) {
  const [enabled, setEnabled] = useState(true)
  const [channels, setChannels] = useState<string[]>([])
  const [groupId, setGroupId] = useState('')
  // The alert wizard's loaders and cache keys.
  const channelsQ = useQuery<any>({ queryKey: ['channels', 'list-min'], queryFn: async () => (await api.get('/settings/channels')).data })
  const groupsQ = useQuery<{ id: string; name: string; kind: string }[]>({
    queryKey: ['groups-tree'], queryFn: async () => (await api.get('/device-groups')).data.data,
  })
  const channelList: any[] = Array.isArray(channelsQ.data) ? channelsQ.data : channelsQ.data?.data || []
  const accept = useMutation({
    mutationFn: async () => (await api.post(recommendationPath(t.id, item.key, 'accept'), {
      enabled, notify_channels: channels, group_id: groupId || null,
    })).data,
    onSuccess: (r) => { toast.success('Alert rule created', r?.name || item.name); onDone() },
    onError: (e) => toast.error('Not accepted', apiErrorMessage(e)),
  })
  const toggle = (id: string) => setChannels((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Accept “{item.name}”</DialogTitle>
          <DialogDescription>
            Creates one alert rule for devices on {t.name} (and templates customised from it):{' '}
            {itemWords(item, meta)}{' '}
            {holdWords(item.min_duration)}{item.if_role ? `, on ${roleLabel(item.if_role)} interfaces` : ''}. You can edit it like any rule.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Enable the rule now" /> Enable the rule now
          </label>
          <FormField label="Notify (optional)" hint="Without a channel the rule still raises alerts in ZenPlus.">
            <div className="grid grid-cols-2 gap-2">
              {channelList.length === 0 && <p className="col-span-2 text-xs text-muted">No notification channels.</p>}
              {channelList.map((c) => {
                const on = channels.includes(c.id)
                return (
                  <button key={c.id} type="button" onClick={() => toggle(c.id)} aria-pressed={on}
                    className={cn('flex items-center justify-between rounded-md border px-2.5 py-2 text-left text-xs', on ? 'border-primary bg-primary/10' : 'border-border')}>
                    <span><span className="font-medium">{c.name}</span> <span className="text-muted">{CHANNEL_TYPE_LABEL[c.type] || c.type}</span></span>
                    {on && <Check className="h-3.5 w-3.5 text-primary" />}
                  </button>
                )
              })}
            </div>
          </FormField>
          <FormField label="Only devices in group (optional)" hint="Narrows the rule to members of one group that are also on this template.">
            <Select value={groupId || '__all__'} onValueChange={(v) => setGroupId(v === '__all__' ? '' : v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">Every device on the template</SelectItem>
                {(groupsQ.data || []).map((g) => <SelectItem key={g.id} value={g.id}>{g.name}{g.kind === 'dynamic' ? ' (dynamic)' : ''}</SelectItem>)}
              </SelectContent>
            </Select>
          </FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={accept.isPending}>Cancel</Button>
          <Button onClick={() => accept.mutate()} disabled={accept.isPending}>
            {accept.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Create rule
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
