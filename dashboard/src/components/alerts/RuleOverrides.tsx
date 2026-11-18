/* Per-asset overrides of a shared alert rule (migrate-193, docs/alert-rule-overrides.md): the ordered list
 * on the rule, opened from the Alert Rules page. A row names a device, a server or a group (optionally only
 * instances matching a pattern) and changes the threshold, clear level, holds or priority for them.
 * Device and server rows are tried before group rows; the first matching, unexpired row applies.
 * Turning a rule off for one asset stays an exclusion (RuleExclusions.tsx). */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Loader2, Pencil, Plus, SlidersHorizontal, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { formatWhen } from './exclusionModel'
import {
  type Condition, fromLocalInput, metricWords, overrideAssetKind, overrideChangeWords, ruleHasInstances, toLocalInput,
} from './thresholdModel'

export type RuleOverride = {
  id: string
  rule_id: string
  position: number
  device_id: string | null
  server_id: string | null
  group_id: string | null
  target_kind: 'device' | 'server' | 'group'
  target_name: string | null
  instance_pattern: string | null
  metric: string | null
  threshold: number | null
  reset_threshold: number | null
  min_duration: number | null
  clear_duration: number | null
  priority: string | null
  reason: string
  expires_at: string | null
  expired: boolean
  created_at: string | null
  updated_at: string | null
  created_by: string | null
}

type OverrideRule = {
  id: string
  name: string
  metric: string
  operator?: string | null
  threshold?: number | null
  conditions?: Condition[] | null
  min_duration?: number | null
  clear_duration?: number | null
  priority?: string | null
}

export const overrideApi = {
  list: (ruleId: string) => `/alert-rules/${ruleId}/overrides`,
  one: (ruleId: string, id: string) => `/alert-rules/${ruleId}/overrides/${id}`,
  reorder: (ruleId: string) => `/alert-rules/${ruleId}/overrides/reorder`,
}

function targetHref(o: RuleOverride): string {
  if (o.device_id) return `/devices/${o.device_id}`
  if (o.server_id) return `/servers/${o.server_id}`
  return `/device-groups/${o.group_id}`
}

/** The overrides of one rule: ordered rows with add, edit, delete and move. */
export function RuleOverridesDialog({ open, onOpenChange, rule }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  rule: OverrideRule | null
}) {
  const can = useCan()
  const canManage = can('alerts.manage')
  const qc = useQueryClient()
  const key = ['alert-rules', rule?.id, 'overrides']
  const q = useQuery<{ items: RuleOverride[]; hidden: number }>({
    queryKey: key,
    queryFn: async () => (await api.get(overrideApi.list(rule!.id))).data,
    enabled: open && !!rule,
    staleTime: 0,
  })
  const [editing, setEditing] = useState<RuleOverride | 'new' | null>(null)
  const [deleting, setDeleting] = useState<RuleOverride | null>(null)
  const refresh = () => {
    qc.invalidateQueries({ queryKey: key })
    qc.invalidateQueries({ queryKey: ['alert-rules', 'effective'] })
  }
  const del = useMutation({
    mutationFn: async (o: RuleOverride) => api.delete(overrideApi.one(rule!.id, o.id)),
    onSuccess: () => { toast.success('Override deleted', 'The rule’s own values apply again on the next evaluation.'); setDeleting(null); refresh() },
    onError: (e) => toast.error('Could not delete the override', apiErrorMessage(e)),
  })
  const reorder = useMutation({
    mutationFn: async (ids: string[]) => (await api.post(overrideApi.reorder(rule!.id), { ids })).data,
    onSuccess: () => refresh(),
    onError: (e) => toast.error('Could not reorder', apiErrorMessage(e)),
  })
  const items = q.data?.items || []
  // Moves stay inside a block: device/server rows are always tried before group rows.
  const move = (idx: number, dir: -1 | 1) => {
    const j = idx + dir
    if (j < 0 || j >= items.length) return
    if ((items[idx].target_kind === 'group') !== (items[j].target_kind === 'group')) return
    const ids = items.map((x) => x.id)
    ;[ids[idx], ids[j]] = [ids[j], ids[idx]]
    reorder.mutate(ids)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-5 w-5 text-primary" aria-hidden /> Overrides of “{rule?.name}”
          </DialogTitle>
          <DialogDescription>
            Device and server rows are tried before group rows; the first matching row applies. To turn a rule off
            for one asset use an exclusion.
          </DialogDescription>
        </DialogHeader>
        {q.isError ? (
          <QueryFailure error={q.error} onRetry={() => q.refetch()} compact what="overrides" />
        ) : q.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted">No overrides: every {overrideAssetKind(rule || { metric: '' })} the rule covers uses its own values.</p>
        ) : (
          <ol className="divide-y divide-border rounded-md border border-border" aria-label="Overrides in evaluation order">
            {items.map((o, i) => (
              <li key={o.id} className={cn('flex items-start gap-3 p-3', o.expired && 'opacity-70')}>
                <span className="mt-0.5 w-5 shrink-0 text-right text-xs tabular-nums text-muted">{i + 1}</span>
                <div className="min-w-0 flex-1 space-y-0.5">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="text-2xs uppercase tracking-wide text-muted">{o.target_kind}</span>
                    <Link to={targetHref(o)} className="truncate text-sm font-medium text-primary hover:underline" onClick={() => onOpenChange(false)}>
                      {o.target_name || o.device_id || o.server_id || o.group_id}
                    </Link>
                    {o.instance_pattern && <span className="font-mono text-xs text-text2">{o.instance_pattern}</span>}
                    {o.expired && <Badge variant="warning">expired</Badge>}
                  </div>
                  <div className="text-sm">{overrideChangeWords(o)}</div>
                  <div className="text-xs text-text2">“{o.reason}”</div>
                  <div className="text-xs text-muted">
                    {o.created_by ? `By ${o.created_by}` : 'Added'}{o.updated_at || o.created_at ? ` · ${formatWhen((o.updated_at || o.created_at)!)}` : ''}
                    {o.expires_at && ` · ${o.expired ? 'expired' : 'expires'} ${formatWhen(o.expires_at)}`}
                  </div>
                </div>
                {canManage && (
                  <div className="flex shrink-0 items-center gap-0.5">
                    <Button variant="ghost" size="icon" className="h-7 w-7" title="Move up" aria-label="Move up"
                      disabled={reorder.isPending || i === 0 || (items[i - 1].target_kind === 'group') !== (o.target_kind === 'group')}
                      onClick={() => move(i, -1)}><ArrowUp className="h-3.5 w-3.5" /></Button>
                    <Button variant="ghost" size="icon" className="h-7 w-7" title="Move down" aria-label="Move down"
                      disabled={reorder.isPending || i === items.length - 1 || (items[i + 1].target_kind === 'group') !== (o.target_kind === 'group')}
                      onClick={() => move(i, 1)}><ArrowDown className="h-3.5 w-3.5" /></Button>
                    <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" aria-label="Edit override" onClick={() => setEditing(o)}>
                      <Pencil className="h-3.5 w-3.5" /></Button>
                    <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title="Delete" aria-label="Delete override"
                      onClick={() => setDeleting(o)}><Trash2 className="h-3.5 w-3.5" /></Button>
                  </div>
                )}
              </li>
            ))}
          </ol>
        )}
        {!!q.data?.hidden && (
          <p className="text-xs text-muted">
            {q.data.hidden} more {q.data.hidden === 1 ? 'override is' : 'overrides are'} on assets outside your visibility scope.
          </p>
        )}
        {canManage && rule && (
          <DialogFooter>
            <Button onClick={() => setEditing('new')}><Plus className="h-4 w-4" /> Add override</Button>
          </DialogFooter>
        )}
        {editing && rule && (
          <OverrideForm rule={rule} override={editing === 'new' ? null : editing} nextPosition={(items.length + 1) * 10}
            onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh() }} />
        )}
        <ConfirmDialog open={!!deleting} onOpenChange={(v) => !v && setDeleting(null)} destructive confirmText="Delete"
          title="Delete this override?" loading={del.isPending} onConfirm={() => { if (deleting) del.mutate(deleting) }}
          description={deleting ? `${deleting.target_name || deleting.target_kind}: ${overrideChangeWords(deleting)}. The rule's own values apply there again.` : ''} />
      </DialogContent>
    </Dialog>
  )
}

type Draft = {
  target_kind: 'asset' | 'group'
  asset_id: string
  group_id: string
  instance_pattern: string
  metric: string
  threshold: string
  reset_threshold: string
  min_duration: string
  clear_duration: string
  priority: string
  reason: string
  expires_at: string
}

const numOrNull = (v: string): number | null => (v.trim() === '' ? null : Number(v))

function draftOf(o: RuleOverride | null): Draft {
  return {
    target_kind: o?.group_id ? 'group' : 'asset',
    asset_id: o?.device_id || o?.server_id || '',
    group_id: o?.group_id || '',
    instance_pattern: o?.instance_pattern || '',
    metric: o?.metric || '',
    threshold: o?.threshold != null ? String(o.threshold) : '',
    reset_threshold: o?.reset_threshold != null ? String(o.reset_threshold) : '',
    min_duration: o?.min_duration != null ? String(o.min_duration) : '',
    clear_duration: o?.clear_duration != null ? String(o.clear_duration) : '',
    priority: o?.priority || '',
    reason: o?.reason || '',
    expires_at: toLocalInput(o?.expires_at),
  }
}

/** Add or edit one override row. */
function OverrideForm({ rule, override, nextPosition, onClose, onSaved }: {
  rule: OverrideRule
  override: RuleOverride | null
  nextPosition: number
  onClose: () => void
  onSaved: () => void
}) {
  const kind = overrideAssetKind(rule)
  const [d, setD] = useState<Draft>(() => draftOf(override))
  const set = (patch: Partial<Draft>) => setD((x) => ({ ...x, ...patch }))
  const [search, setSearch] = useState('')
  const conditions: Condition[] = useMemo(() => (Array.isArray(rule.conditions) && rule.conditions.length
    ? rule.conditions
    : [{ metric: rule.metric, operator: rule.operator || '>', threshold: rule.threshold ?? null }]), [rule])
  const assets = useQuery<{ id: string; name: string; sub?: string | null }[]>({
    queryKey: ['override-asset-search', kind, search],
    queryFn: async () => {
      if (kind === 'server') {
        const r = (await api.get('/servers', { params: { page_size: 50, sort: 'display_name', ...(search ? { q: search } : {}) } })).data
        return (r?.items || []).map((x: any) => ({ id: x.id, name: x.display_name || x.hostname, sub: x.primary_ip }))
      }
      const r = (await api.get('/devices', { params: { limit: 50, ...(search ? { search } : {}) } })).data
      return (r?.data || []).map((x: any) => ({ id: x.id, name: x.hostname, sub: x.ip_address }))
    },
    enabled: d.target_kind === 'asset',
  })
  const groups = useQuery<{ id: string; name: string; kind: string }[]>({
    queryKey: ['groups-tree'],
    queryFn: async () => (await api.get('/device-groups')).data.data,
    enabled: d.target_kind === 'group',
  })
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        device_id: d.target_kind === 'asset' && kind === 'device' ? d.asset_id || null : null,
        server_id: d.target_kind === 'asset' && kind === 'server' ? d.asset_id || null : null,
        group_id: d.target_kind === 'group' ? d.group_id || null : null,
        instance_pattern: d.instance_pattern.trim() || null,
        metric: d.metric || null,
        threshold: numOrNull(d.threshold),
        reset_threshold: numOrNull(d.reset_threshold),
        min_duration: numOrNull(d.min_duration),
        clear_duration: numOrNull(d.clear_duration),
        priority: d.priority || null,
        reason: d.reason.trim(),
        expires_at: fromLocalInput(d.expires_at),
        position: override ? override.position : nextPosition,
      }
      return override
        ? (await api.put(overrideApi.one(rule.id, override.id), body)).data
        : (await api.post(overrideApi.list(rule.id), body)).data
    },
    onSuccess: () => { toast.success(override ? 'Override saved' : 'Override added', 'It applies from the next evaluation.'); onSaved() },
    onError: (e) => toast.error('Override not saved', apiErrorMessage(e)),
  })
  const target = d.target_kind === 'group' ? d.group_id : d.asset_id
  const changes = [d.threshold, d.reset_threshold, d.min_duration, d.clear_duration, d.priority].some((v) => v !== '')
  const badNumber = [d.threshold, d.reset_threshold, d.min_duration, d.clear_duration].some((v) => v.trim() !== '' && !Number.isFinite(Number(v)))
  const problem = !target ? `Choose a ${d.target_kind === 'group' ? 'group' : kind}`
    : !changes ? 'Change at least the threshold, clear level, a hold or the priority'
      : badNumber ? 'Numbers only' : !d.reason.trim() ? 'Give a reason' : null
  const chosenCond = conditions.find((c) => c.metric === d.metric) || conditions[0]
  const assetOptions = assets.data || []
  const currentAsset = override && d.asset_id && !assetOptions.some((a) => a.id === d.asset_id)
    ? [{ id: d.asset_id, name: override.target_name || d.asset_id, sub: null }] : []

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{override ? 'Edit override' : 'Add override'}</DialogTitle>
          <DialogDescription>
            Rule: {rule.name}. Fields left empty keep the rule’s value.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <FormField label="Applies to">
            <Select value={d.target_kind} onValueChange={(v) => set({ target_kind: v as Draft['target_kind'] })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="asset">One {kind}</SelectItem>
                <SelectItem value="group">Members of a group</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {d.target_kind === 'asset' ? (
            <div className="grid gap-2 sm:grid-cols-2">
              <FormField label={`Find a ${kind}`}>
                <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Name or IP" />
              </FormField>
              <FormField label={kind === 'server' ? 'Server' : 'Device'} required>
                <Select value={d.asset_id || '__none__'} onValueChange={(v) => set({ asset_id: v === '__none__' ? '' : v })}>
                  <SelectTrigger><SelectValue placeholder={`Choose a ${kind}`} /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__" disabled>Choose a {kind}</SelectItem>
                    {[...currentAsset, ...assetOptions].map((a) => (
                      <SelectItem key={a.id} value={a.id}>{a.name}{a.sub ? ` · ${a.sub}` : ''}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            </div>
          ) : (
            <FormField label="Group" required hint="Static or dynamic; members of its subgroups are included. Group rows need a user without a tag scope.">
              <Select value={d.group_id || '__none__'} onValueChange={(v) => set({ group_id: v === '__none__' ? '' : v })}>
                <SelectTrigger><SelectValue placeholder="Choose a group" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__" disabled>Choose a group</SelectItem>
                  {(groups.data || []).map((g) => <SelectItem key={g.id} value={g.id}>{g.name}{g.kind === 'dynamic' ? ' (dynamic)' : ''}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
          )}
          {ruleHasInstances(rule) && (
            <FormField label="Only instances named (optional)" hint="glob, e.g. port* or wan?  Empty = every interface, filesystem or row.">
              <Input className="font-mono text-xs" value={d.instance_pattern} maxLength={200} onChange={(e) => set({ instance_pattern: e.target.value })} />
            </FormField>
          )}
          {conditions.length > 1 && (
            <FormField label="Condition" hint="The threshold and clear level change this condition.">
              <Select value={d.metric || conditions[0].metric} onValueChange={(v) => set({ metric: v === conditions[0].metric ? '' : v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {conditions.map((c) => <SelectItem key={c.metric} value={c.metric}>{metricWords(c.metric)} {c.operator} {c.threshold}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
          )}
          <div className="grid gap-2 sm:grid-cols-2">
            <FormField label="Threshold" hint={chosenCond ? `Rule: ${chosenCond.operator} ${chosenCond.threshold ?? '?'}` : undefined}>
              <Input type="number" step="any" value={d.threshold} onChange={(e) => set({ threshold: e.target.value })} />
            </FormField>
            <FormField label="Clear level" hint="On the healthy side of the threshold">
              <Input type="number" step="any" value={d.reset_threshold} onChange={(e) => set({ reset_threshold: e.target.value })} />
            </FormField>
            <FormField label="Breach hold (seconds)" hint={`Rule: ${rule.min_duration ?? 0} s`}>
              <Input type="number" min={0} max={86400} value={d.min_duration} onChange={(e) => set({ min_duration: e.target.value })} />
            </FormField>
            <FormField label="Recovery hold (seconds)" hint={`Rule: ${rule.clear_duration ?? 'default 300'} s`}>
              <Input type="number" min={0} max={86400} value={d.clear_duration} onChange={(e) => set({ clear_duration: e.target.value })} />
            </FormField>
            <FormField label="Priority">
              <Select value={d.priority || '__rule__'} onValueChange={(v) => set({ priority: v === '__rule__' ? '' : v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__rule__">As the rule{rule.priority ? ` (${rule.priority})` : ''}</SelectItem>
                  {['P1', 'P2', 'P3', 'P4'].map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Expires (optional)" hint="Stops applying at this time">
              <Input type="datetime-local" value={d.expires_at} onChange={(e) => set({ expires_at: e.target.value })} />
            </FormField>
          </div>
          <FormField label="Reason" required>
            <Textarea rows={2} maxLength={500} value={d.reason} onChange={(e) => set({ reason: e.target.value })}
              placeholder="e.g. branch firewalls run hot; vendor confirmed 95 % is normal" />
          </FormField>
          {problem && <p className="text-xs text-muted">{problem}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!!problem || save.isPending}>
            {save.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />} {override ? 'Save' : 'Add override'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
