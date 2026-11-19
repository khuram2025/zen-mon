/* Recommended default rules for the whole appliance (GET /alert-rules/recommended,
 * app.services.recommended_rules; alerting audit plan step 2.1). Nothing is created until an operator
 * accepts one: Accept makes one ordinary rule (pack_key "default:<key>") through the rule API of its family.
 * "Covered" means an enabled, unscoped rule already watches the same condition, so accepting would duplicate
 * its alerts; the covering rule is named. */
import { useState } from 'react'
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
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { usePolicies } from '@/features/escalation/api'
import { effectivePriority, policyForPriority } from '@/features/escalation/model'
import { PriorityBadge } from '@/features/escalation/ui'
import { recommendationRows, type DefaultRecommendation } from '@/components/forms/alertRuleCatalog'

const STATUS_BADGE: Record<DefaultRecommendation['status'], { label: string; variant: 'outline' | 'success' | 'warning' }> = {
  available: { label: 'available', variant: 'outline' },
  accepted: { label: 'accepted', variant: 'success' },
  covered: { label: 'covered', variant: 'warning' },
}

const CHANNEL_TYPE_LABEL: Record<string, string> = {
  email: 'Email', sms: 'SMS', webhook: 'Webhook', slack: 'Slack',
  telegram: 'Telegram', teams: 'Teams', discord: 'Discord', pagerduty: 'PagerDuty',
}

export function RecommendedRulesPanel({ onOpenRule }: { onOpenRule: (ruleId: string) => void }) {
  const can = useCan()
  const canManage = can('alerts.manage')
  const q = useQuery<DefaultRecommendation[]>({
    queryKey: ['alert-rules', 'recommended'],
    queryFn: async () => (await api.get('/alert-rules/recommended')).data?.items || [],
  })
  const [accepting, setAccepting] = useState<DefaultRecommendation | null>(null)
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="recommended rules" />
  const rows = recommendationRows(q.data || [])
  return (
    <div className="space-y-3">
      <p className="max-w-3xl text-xs text-muted">
        Rules most monitoring setups want, beyond the ones ZenPlus ships. Nothing is created or enabled until you
        accept one; an accepted rule is an ordinary rule you can edit. <span className="text-text">Covered</span> means
        one of your rules already watches the same condition for everything, so accepting would send duplicate alerts.
      </p>
      <div className="overflow-x-auto rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/50">
            <Tr><Th>Recommendation</Th><Th>Condition</Th><Th>Severity</Th><Th>Status</Th><Th className="text-right" /></Tr>
          </THead>
          <TBody>
            {rows.map(({ item, condition, timers, family }) => (
              <Tr key={item.key} className="align-top">
                <Td>
                  <div className="font-medium">{item.name}</div>
                  <div className="max-w-md text-xs text-muted">{item.description}</div>
                </Td>
                <Td className="text-xs">
                  <div className="font-medium text-text">{family}</div>
                  <div>{condition}</div>
                  <div className="text-2xs text-muted">{timers}</div>
                </Td>
                <Td>
                  <div className="flex items-center gap-1">
                    <Badge variant={item.severity === 'critical' ? 'danger' : item.severity === 'warning' ? 'warning' : 'info'}>{item.severity}</Badge>
                    <PriorityBadge priority={effectivePriority(item.priority, item.severity)} compact />
                  </div>
                </Td>
                <Td className="text-xs">
                  <Badge variant={STATUS_BADGE[item.status].variant}>{STATUS_BADGE[item.status].label}</Badge>
                  {item.status === 'accepted' && item.rule && (
                    <div className="mt-0.5">
                      <button type="button" className="text-primary hover:underline" onClick={() => onOpenRule(item.rule!.id)}>{item.rule.name}</button>
                      {!item.rule.enabled && <span className="text-muted"> (off)</span>}
                    </div>
                  )}
                  {item.status === 'covered' && item.covered_by && (
                    <div className="mt-0.5 text-muted">
                      by <button type="button" className="text-primary hover:underline" onClick={() => onOpenRule(item.covered_by!.id)}>{item.covered_by.name}</button>
                    </div>
                  )}
                </Td>
                <Td className="text-right">
                  {canManage && item.status !== 'accepted' && (
                    <Button size="sm" variant={item.status === 'covered' ? 'ghost' : 'outline'} onClick={() => setAccepting(item)}
                      title={item.status === 'covered' ? 'Another rule already covers this; accepting adds a second rule' : undefined}>
                      <ThumbsUp className="h-3.5 w-3.5" /> {item.status === 'covered' ? 'Accept anyway' : 'Accept'}
                    </Button>
                  )}
                </Td>
              </Tr>
            ))}
            {q.isLoading && (
              <Tr><Td colSpan={5} className="py-8 text-center text-sm text-muted"><Loader2 className="mr-2 inline h-4 w-4 animate-spin" />Loading…</Td></Tr>
            )}
            {!q.isLoading && rows.length === 0 && (
              <Tr><Td colSpan={5} className="py-8 text-center text-sm text-muted">No recommendations.</Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
      {accepting && <AcceptRecommendationDialog item={accepting} onClose={() => setAccepting(null)} />}
    </div>
  )
}

function AcceptRecommendationDialog({ item, onClose }: { item: DefaultRecommendation; onClose: () => void }) {
  const qc = useQueryClient()
  const [enabled, setEnabled] = useState(true)
  const [channels, setChannels] = useState<string[]>([])
  const [policyId, setPolicyId] = useState('')
  // The alert wizard's loaders and cache keys.
  const channelsQ = useQuery<any>({ queryKey: ['channels', 'list-min'], queryFn: async () => (await api.get('/settings/channels')).data })
  const channelList: any[] = Array.isArray(channelsQ.data) ? channelsQ.data : channelsQ.data?.data || []
  const policiesQ = usePolicies(true)
  const policies = policiesQ.data ?? []
  const priority = effectivePriority(item.priority, item.severity)
  const defaultPolicy = policyForPriority(policies, priority)
  const accept = useMutation({
    mutationFn: async () => (await api.post(`/alert-rules/recommended/${encodeURIComponent(item.key)}/accept`, {
      enabled, notify_channels: channels, escalation_policy_id: policyId || null,
    })).data,
    onSuccess: (r: any) => {
      toast.success('Alert rule created', r?.name || item.name)
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      onClose()
    },
    onError: (e) => toast.error('Not accepted', apiErrorMessage(e)),
  })
  const toggle = (id: string) => setChannels((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))
  const nobody = !channels.length && !policyId && !defaultPolicy && !policiesQ.isLoading
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Accept “{item.name}”</DialogTitle>
          <DialogDescription>{item.description} The rule is created as shown and can be edited like any rule.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {item.status === 'covered' && item.covered_by && (
            <p className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
              {item.covered_by.name} already watches this condition for everything: both rules will raise alerts.
            </p>
          )}
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Enable the rule now" /> Enable the rule now
          </label>
          <FormField label="Notify (optional)" hint="Without a channel the rule still raises alerts in ZenPlus, and its escalation policy may page.">
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
          <FormField label="Escalation">
            <select aria-label="Escalation policy" value={policyId} onChange={(e) => setPolicyId(e.target.value)}
              className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm">
              <option value="">{defaultPolicy ? `${priority} default: ${defaultPolicy.name}` : `${priority} default (no policy is the default)`}</option>
              {policies.map((p) => <option key={p.id} value={p.id}>{p.name}{p.enabled ? '' : ' (disabled)'}</option>)}
            </select>
          </FormField>
          {nobody && <p className="text-xs text-danger">No channel and no escalation policy: this rule&apos;s alerts would notify nobody.</p>}
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
