/* Per-asset exclusions from shared alert rules: the asset page's rule list and switch, the confirmation before
 * turning a shared rule off for one asset, and the rule's list of assets it is off for (Alert Rules page).
 * The rules behind the switch live in ./exclusionModel.ts. */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellOff, ExternalLink, Loader2, RotateCcw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Switch } from '@/components/ui/Switch'
import { Textarea } from '@/components/ui/Textarea'
import { QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'
import { SeverityBadge } from './AlertBits'
import {
  exclusionApi, exclusionAssetLabel, exclusionHref, excludeConsequence, formatWhen, ruleSwitch,
  type AssetKind, type AssetRule, type RuleExclusionsResponse,
} from './exclusionModel'

/** Exclude / re-include mutations for one asset; `onChanged` refreshes the caller's queries. */
export function useRuleExclusion(kind: AssetKind, assetId: string, onChanged?: () => void) {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['alert-rules'] })
    qc.invalidateQueries({ queryKey: ['alerts'] })
    onChanged?.()
  }
  const exclude = useMutation({
    mutationFn: async ({ ruleId, reason }: { ruleId: string; reason?: string }) =>
      (await api.post(exclusionApi.create(ruleId), { asset_kind: kind, asset_id: assetId, ...(reason ? { reason } : {}) })).data,
    onSuccess: (d: any) => {
      refresh()
      const n = Number(d?.resolved_alerts) || 0
      toast.success(`Rule disabled for this ${kind}`,
        n ? `${n} open alert${n === 1 ? '' : 's'} from it here resolved. Other ${kind}s are unaffected.` : `Other ${kind}s are unaffected.`)
    },
    onError: (e) => toast.error('Could not disable the rule here', apiErrorMessage(e)),
  })
  const include = useMutation({
    mutationFn: async (ruleId: string) => (await api.delete(exclusionApi.remove(ruleId, kind, assetId))).data,
    onSuccess: () => { refresh(); toast.success(`Rule enabled for this ${kind}`, 'Evaluation resumes on the next cycle.') },
    onError: (e) => toast.error('Could not enable the rule here', apiErrorMessage(e)),
  })
  return { exclude, include }
}

/** Confirmation before a shared rule is turned off for one asset, with an optional reason. */
export function ExcludeRuleDialog({ open, onOpenChange, rule, kind, assetName, loading, onConfirm }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  rule: { name: string; scope?: { label: string } | null } | null
  kind: AssetKind
  assetName?: string | null
  loading?: boolean
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const close = (v: boolean) => { onOpenChange(v); if (!v) setReason('') }
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <BellOff className="h-5 w-5 text-warning" aria-hidden />
            Disable “{rule?.name}” for {assetName || `this ${kind}`} only?
          </DialogTitle>
          <DialogDescription>
            {rule?.scope?.label ? <>The rule applies to {rule.scope.label.toLowerCase()}. </> : null}
            {excludeConsequence(kind)}
          </DialogDescription>
        </DialogHeader>
        <label className="block text-xs font-medium text-text2" htmlFor="exclude-reason">Reason (optional, shown on the rule)</label>
        <Textarea id="exclude-reason" value={reason} maxLength={500} rows={2} onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. lab server, restarts nightly" />
        <DialogFooter>
          <Button variant="outline" onClick={() => close(false)} disabled={loading}>Cancel</Button>
          <Button onClick={() => onConfirm(reason.trim())} disabled={loading}>
            {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Disable for this {kind}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The rules that apply to one device or server, each switchable for this asset alone. */
export function AssetAlertRules({ kind, assetId, assetName }: { kind: AssetKind; assetId: string; assetName?: string | null }) {
  const can = useCan()
  const qc = useQueryClient()
  const canManage = can('alerts.manage')
  const key = ['alert-rules', 'for-asset', kind, assetId]
  const q = useQuery<{ items: AssetRule[] }>({
    queryKey: key,
    queryFn: async () => (await api.get(exclusionApi.forAsset, { params: { asset_kind: kind, asset_id: assetId } })).data,
  })
  const [pending, setPending] = useState<AssetRule | null>(null)
  const { exclude, include } = useRuleExclusion(kind, assetId, () => qc.invalidateQueries({ queryKey: key }))
  const toggleOwn = useMutation({
    mutationFn: async (r: AssetRule) => (await api.post(`/alert-rules/${r.id}/toggle`, { enabled: !r.enabled })).data,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['alert-rules'] }); toast.success('Rule updated') },
    onError: (e) => toast.error('Could not change the rule', apiErrorMessage(e)),
  })
  const rules = q.data?.items || []

  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} compact what="alert rules" />
  if (q.isLoading) return <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading rules…</div>
  if (!rules.length) return <p className="text-sm text-muted">No alert rule applies to this {kind}.</p>

  return (
    <>
      <ul className="divide-y divide-border/60" aria-label={`Alert rules for this ${kind}`}>
        {rules.map((r) => {
          const sw = ruleSwitch(r, kind, canManage)
          const busy = (exclude.isPending && exclude.variables?.ruleId === r.id)
            || (include.isPending && include.variables === r.id)
            || (toggleOwn.isPending && toggleOwn.variables?.id === r.id)
          return (
            <li key={r.id} className={cn('flex items-start gap-3 py-2', !r.active && 'opacity-70')}>
              <Switch
                className="mt-0.5"
                checked={sw.checked}
                disabled={sw.disabled || busy}
                aria-label={sw.label}
                title={sw.label}
                onCheckedChange={() => {
                  if (sw.action === 'exclude') setPending(r)
                  else if (sw.action === 'include') include.mutate(r.id)
                  else if (sw.action === 'toggle') toggleOwn.mutate(r)
                }}
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="truncate text-sm font-medium" title={r.name}>{r.name}</span>
                  {r.severity && <SeverityBadge severity={r.severity as any} />}
                </div>
                <div className="text-xs text-muted">
                  {r.shared ? (r.scope?.label || 'Shared rule') : `This ${kind} only`}
                </div>
                {sw.note && <div className={cn('text-xs', r.excluded ? 'text-warning' : 'text-muted')}>{sw.note}</div>}
              </div>
            </li>
          )
        })}
      </ul>
      <p className="mt-2 text-2xs text-muted">
        Turning a shared rule off here affects this {kind} only.{' '}
        <Link to="/alert-rules" className="text-primary hover:underline">Manage rules</Link>
      </p>
      <ExcludeRuleDialog
        open={!!pending}
        onOpenChange={(v) => { if (!v) setPending(null) }}
        rule={pending}
        kind={kind}
        assetName={assetName}
        loading={exclude.isPending}
        onConfirm={(reason) => {
          if (!pending) return
          exclude.mutate({ ruleId: pending.id, reason }, { onSuccess: () => setPending(null) })
        }}
      />
    </>
  )
}

/** The assets a rule is turned off for: name (linked), who, when, why, and re-enable. */
export function RuleExclusionsDialog({ open, onOpenChange, rule }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  rule: { id: string; name: string } | null
}) {
  const can = useCan()
  const qc = useQueryClient()
  const q = useQuery<RuleExclusionsResponse>({
    queryKey: ['alert-rules', rule?.id, 'exclusions'],
    queryFn: async () => (await api.get(exclusionApi.list(rule!.id))).data,
    enabled: open && !!rule,
    staleTime: 0,
  })
  const include = useMutation({
    mutationFn: async (x: { kind: AssetKind; assetId: string }) =>
      (await api.delete(exclusionApi.remove(rule!.id, x.kind, x.assetId))).data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
      toast.success('Rule enabled again for that asset', 'Evaluation resumes on the next cycle.')
    },
    onError: (e) => toast.error('Could not re-enable', apiErrorMessage(e)),
  })
  const items = q.data?.items || []
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] w-[calc(100vw-2rem)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>“{rule?.name}” is off for</DialogTitle>
          <DialogDescription>
            The rule stays on for every other asset it applies to. Re-enable an asset to evaluate it again.
          </DialogDescription>
        </DialogHeader>
        {q.isError ? (
          <QueryFailure error={q.error} onRetry={() => q.refetch()} compact what="exclusions" />
        ) : q.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
        ) : items.length === 0 ? (
          <p className="text-sm text-muted">This rule is not disabled for any {q.data?.hidden ? 'asset you can see' : 'device or server'}.</p>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border" aria-label="Assets this rule is disabled for">
            {items.map((x) => (
              <li key={x.id} className="flex items-start gap-3 p-3">
                <div className="min-w-0 flex-1">
                  <Link to={exclusionHref(x)} className="inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline"
                    onClick={() => onOpenChange(false)}>
                    {exclusionAssetLabel(x)} <ExternalLink className="h-3 w-3" aria-hidden />
                  </Link>
                  <span className="ml-2 text-2xs uppercase tracking-wide text-muted">{x.asset_kind}</span>
                  {x.asset_ip && x.asset_name && <span className="ml-2 font-mono text-2xs text-muted">{x.asset_ip}</span>}
                  <div className="text-xs text-muted">
                    Disabled{x.created_by_name ? ` by ${x.created_by_name}` : ''}{x.created_at ? ` · ${formatWhen(x.created_at)}` : ''}
                  </div>
                  {x.reason && <div className="text-xs text-text2">“{x.reason}”</div>}
                </div>
                {can('alerts.manage') && (
                  <Button size="sm" variant="outline" disabled={include.isPending && include.variables?.assetId === x.asset_id}
                    onClick={() => include.mutate({ kind: x.asset_kind, assetId: x.asset_id })}>
                    <RotateCcw className="h-3.5 w-3.5" /> Re-enable
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
        {!!q.data?.hidden && (
          <p className="text-xs text-muted">
            {q.data.hidden} more {q.data.hidden === 1 ? 'asset is' : 'assets are'} outside your visibility scope.
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}
