// Discovery rules phase 2: run the active rules (all, or the ones chosen) on devices and servers already in inventory.
// Preview first (writes nothing), then apply exactly that preview: the digest makes the API refuse when the inventory
// or the rules changed since (409 preview_stale), and the page previews again.
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowLeft, CheckCircle2, Loader2, Lock, Play, Wand2, X } from 'lucide-react'
import { useCan } from '@/stores/auth'
import { useBreadcrumbDetail } from '@/components/layout/breadcrumbDetail'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, cn } from '@/lib/utils'
import { rulesApi } from './api'
import type {
  AssetKind,
  ChangeOutcome,
  DiscoveryRule,
  InventoryApplyResult,
  InventoryPreview,
  InventoryRunRequest,
  PreviewStaleDetail,
} from './types'
import {
  applyConfirmText,
  ASSET_KIND_ORDER,
  ASSET_KIND_SHORT,
  CHANGE_OUTCOME_LABEL,
  CHANGE_OUTCOME_ORDER,
  buildTargets,
  dependencyIds,
  isSubnet,
  parseList,
  parseRunTargets,
  plural,
  sentenceCase,
  settingLabel,
  summarizeApplyResult,
  summarizeInventoryPreview,
} from './ruleModel'
import { ChangesTable, useRuleCatalog, useRuleLookups, useWithDeviceNames } from './ruleUi'

const MAX_ROWS = 500

export function RuleRunPage() {
  const navigate = useNavigate()
  const can = useCan()
  const canManage = can('discovery.manage_rules')
  useBreadcrumbDetail('Run on inventory')

  return (
    <div className="space-y-5">
      <div>
        <button
          onClick={() => navigate('/discovery/rules')}
          className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
        >
          <ArrowLeft className="h-3 w-3" /> Back to rules
        </button>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Wand2 className="h-6 w-6 text-primary" />
          Run rules on inventory
        </h1>
        <p className="mt-1 text-xs text-muted">
          Apply the active discovery rules to devices, servers and controller-managed switches and access points that
          are already monitored. Preview first: nothing changes until you apply. Rules set values and add tags; they
          never delete anything or turn monitoring off. A rule set to revert puts back what it set on assets that no
          longer match it.
        </p>
      </div>
      {canManage ? (
        <RunForm />
      ) : (
        <Card>
          <CardContent className="flex items-start gap-3 py-6 text-sm">
            <Lock className="mt-0.5 h-5 w-5 shrink-0 text-muted" />
            <div>
              <div className="font-medium">Managing discovery rules is needed</div>
              <p className="mt-1 text-xs text-muted">
                Running rules on inventory changes devices and servers, so it needs the Discovery “Manage rules”
                permission (and the device or server management permission for what the rules set). Ask an
                administrator, or review the rules and their history on the{' '}
                <Link to="/discovery/rules" className="text-primary hover:underline">
                  Rules
                </Link>{' '}
                page.
              </p>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function RunForm() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const pre = useMemo(() => parseRunTargets(params), [params])
  const { data: catalog } = useRuleCatalog()
  const { lookups: baseLookups, options } = useRuleLookups()
  const { data: rules = [], isLoading: rulesLoading } = useQuery<DiscoveryRule[]>({
    queryKey: ['discovery', 'rules', 'list'],
    queryFn: rulesApi.list,
  })
  const active = rules.filter((r) => r.mode === 'active')

  const [ruleChoice, setRuleChoice] = useState<'all' | 'pick'>('all')
  const [picked, setPicked] = useState<string[]>([])
  const [kinds, setKinds] = useState<AssetKind[]>(pre.kinds)
  const [groupIds, setGroupIds] = useState<string[]>([])
  const [tagsText, setTagsText] = useState('')
  const [subnet, setSubnet] = useState('')
  const [query, setQuery] = useState('')

  const [preview, setPreview] = useState<InventoryPreview | null>(null)
  const [previewBody, setPreviewBody] = useState<string | null>(null)
  const [stale, setStale] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [applied, setApplied] = useState<InventoryApplyResult | null>(null)
  const [show, setShow] = useState<'all' | ChangeOutcome>('all')

  const subnetBad = subnet.trim() !== '' && !isSubnet(subnet)
  const preselected = pre.device_ids.length + pre.server_ids.length + pre.child_ids.length > 0
  const body: InventoryRunRequest = useMemo(() => {
    const targets = buildTargets({
      kinds,
      device_ids: pre.device_ids,
      server_ids: pre.server_ids,
      child_ids: pre.child_ids,
      group_ids: kinds.includes('device') || kinds.includes('child') ? groupIds : [],
      tags: parseList(tagsText),
      subnet,
      query,
    })
    return ruleChoice === 'pick' ? { rule_ids: picked, targets } : { targets }
  }, [kinds, pre, groupIds, tagsText, subnet, query, ruleChoice, picked])
  const bodyKey = JSON.stringify(body)
  const outdated = !!preview && previewBody !== bodyKey

  const blockers: string[] = []
  if (!kinds.length) blockers.push('Choose devices, servers or managed switches / APs')
  if (ruleChoice === 'pick' && !picked.length) blockers.push('Pick at least one rule')
  if (!rulesLoading && active.length === 0) blockers.push('There are no active rules')
  if (subnetBad) blockers.push('The subnet must look like 10.0.0.0/16')

  const runPreview = useMutation({
    mutationFn: (b: InventoryRunRequest) => rulesApi.inventoryPreview(b),
    onSuccess: (r, b) => {
      setPreview(r)
      setPreviewBody(JSON.stringify(b))
      setApplied(null)
      setShow('all')
    },
    onError: (e: any) => toast.error(e?.response?.status === 403 ? 'Not allowed' : 'Preview failed', apiErrorMessage(e)),
  })

  const apply = useMutation({
    mutationFn: () => rulesApi.inventoryApply({ ...JSON.parse(previewBody!), digest: preview!.digest }),
    onSuccess: (r) => {
      setConfirming(false)
      setApplied(r)
      setPreview(null)
      setPreviewBody(null)
      setStale(null)
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'history'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'conflicts'] })
      qc.invalidateQueries({ queryKey: ['discovery', 'rules', 'asset'] })
      toast.success('Rules applied', summarizeApplyResult(r))
    },
    onError: (e: any) => {
      setConfirming(false)
      const detail = e?.response?.data?.detail as PreviewStaleDetail | undefined
      if (e?.response?.status === 409 && detail && typeof detail === 'object' && detail.code === 'preview_stale') {
        setStale(detail.message || 'The inventory or the rules changed since the preview; preview again.')
        // The 409 carries only the new counts: preview again so the table matches what an apply would do.
        if (previewBody) runPreview.mutate(JSON.parse(previewBody))
        return
      }
      toast.error(e?.response?.status === 403 ? 'Not allowed' : 'Could not apply', apiErrorMessage(e))
    },
  })

  const changes = preview?.changes ?? []
  const lookups = useWithDeviceNames(baseLookups, useMemo(() => dependencyIds(changes), [changes]))
  const rows = changes.filter((c) => show === 'all' || c.outcome === show)
  const shown = rows.slice(0, MAX_ROWS)
  const summary = preview?.summary
  const writes = (summary?.changes ?? 0) + (summary?.reverts ?? 0)
  const canApply = !!preview && !outdated && writes > 0 && !apply.isPending

  const toggleKind = (k: AssetKind, on: boolean) =>
    setKinds(on ? Array.from(new Set([...kinds, k])) : kinds.filter((x) => x !== k))

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Rules</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input type="radio" checked={ruleChoice === 'all'} onChange={() => setRuleChoice('all')} />
              Every active rule, in order ({active.length})
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" checked={ruleChoice === 'pick'} onChange={() => setRuleChoice('pick')} />
              Only these active rules
            </label>
          </div>
          {ruleChoice === 'pick' && (
            <div className="flex flex-wrap gap-1.5 rounded-md border border-border bg-surface2/30 p-2">
              {active.length === 0 && <span className="text-xs text-muted">No active rules.</span>}
              {active.map((r) => (
                <Chip
                  key={r.id}
                  active={picked.includes(r.id)}
                  onClick={() => setPicked(picked.includes(r.id) ? picked.filter((x) => x !== r.id) : [...picked, r.id])}
                >
                  {r.name}
                </Chip>
              ))}
            </div>
          )}
          <p className="text-xs text-muted">
            Only active rules run on inventory, whatever they are set to run on automatically. Rules keep their order:
            the first matching rule sets each value. Draft and simulating rules are previewed from the rule editor.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Devices and servers</CardTitle>
          <p className="mt-1 text-xs text-muted">Every filter narrows the selection; leave them empty to check everything.</p>
        </CardHeader>
        <CardContent className="space-y-3">
          {preselected && (
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm">
              <span className="font-medium">
                {[
                  pre.device_ids.length ? plural(pre.device_ids.length, 'selected device') : '',
                  pre.server_ids.length ? plural(pre.server_ids.length, 'selected server') : '',
                  pre.child_ids.length ? plural(pre.child_ids.length, 'selected managed switch / AP', 'selected managed switches / APs') : '',
                ]
                  .filter(Boolean)
                  .join(' and ')}
              </span>
              <span className="text-xs text-muted">Only these are checked.</span>
              <button
                type="button"
                className="ml-auto inline-flex items-center gap-1 text-xs text-muted hover:text-text"
                onClick={() => {
                  const next = new URLSearchParams(params)
                  for (const k of ['device', 'devices', 'server', 'servers', 'child', 'children']) next.delete(k)
                  setParams(next, { replace: true })
                  setKinds(['device', 'server'])
                }}
              >
                <X className="h-3.5 w-3.5" /> Clear the selection
              </button>
            </div>
          )}
          <div className="flex flex-wrap gap-4 text-sm">
            {ASSET_KIND_ORDER.map((k) => (
              <label key={k} className="flex items-center gap-2" title={k === 'child' ? 'FortiLink switches and FortiAPs read through their FortiGate' : undefined}>
                <input type="checkbox" checked={kinds.includes(k)} onChange={(e) => toggleKind(k, e.target.checked)} />
                {ASSET_KIND_SHORT[k]}
              </label>
            ))}
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <FormField label="Tags" hint="Comma separated; an asset with any of them">
              <Input value={tagsText} placeholder="datacenter, firewall" onChange={(e) => setTagsText(e.target.value)} />
            </FormField>
            <FormField label="Subnet" hint="IP address in this network" error={subnetBad ? 'Not a subnet' : undefined}>
              <Input value={subnet} placeholder="10.0.0.0/16" className="font-mono text-xs" onChange={(e) => setSubnet(e.target.value)} />
            </FormField>
            <FormField label="Search" hint="Name or IP address contains">
              <Input value={query} placeholder="fw-, 10.51." onChange={(e) => setQuery(e.target.value)} />
            </FormField>
          </div>
          {(kinds.includes('device') || kinds.includes('child')) && options.device_groups.length > 0 && (
            <div className="space-y-1">
              <div className="text-xs font-medium">Device groups {groupIds.length ? `(${groupIds.length})` : ''}</div>
              <div className="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto rounded-md border border-border bg-surface2/30 p-2">
                {options.device_groups.map((g) => (
                  <Chip
                    key={g.id}
                    active={groupIds.includes(g.id)}
                    onClick={() => setGroupIds(groupIds.includes(g.id) ? groupIds.filter((x) => x !== g.id) : [...groupIds, g.id])}
                  >
                    {g.name}
                  </Chip>
                ))}
              </div>
              {groupIds.length > 0 && kinds.includes('server') && (
                <div className="text-2xs text-muted">Groups apply to network devices and managed switches / APs; servers are not in device groups.</div>
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            <Button onClick={() => { setStale(null); runPreview.mutate(body) }} disabled={blockers.length > 0 || runPreview.isPending}>
              {runPreview.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              Preview
            </Button>
            {blockers.length > 0 && <span className="text-xs text-warning-fg">{blockers[0]}</span>}
          </div>
        </CardContent>
      </Card>

      {applied && (
        <Card className="border-success/40">
          <CardContent className="flex flex-wrap items-start gap-3 p-4">
            <CheckCircle2 className="mt-0.5 h-5 w-5 text-success-fg" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold">Applied ({applied.status})</div>
              <div className="text-xs text-muted">{summarizeApplyResult(applied)}</div>
              {applied.skipped_changed > 0 && (
                <div className="mt-1 text-xs text-warning-fg">
                  Values skipped had changed between the preview and the apply; they are left as they are.
                </div>
              )}
            </div>
            <Link to={`/discovery/rules?run=${applied.engine_run_id}`} className="text-sm font-medium text-primary hover:underline">
              Open this run in the history (undo is there)
            </Link>
          </CardContent>
        </Card>
      )}

      {stale && (
        <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning-fg">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            {stale}
            <div className="text-xs">{runPreview.isPending ? 'Previewing again…' : 'The preview below is the new one: check it and apply again.'}</div>
          </div>
        </div>
      )}

      {preview && summary && (
        <Card>
          <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3 pb-3">
            <div className="min-w-0">
              <CardTitle className="text-base">{sentenceCase(summarizeInventoryPreview(summary, JSON.parse(previewBody || '{}')?.targets?.kinds))}</CardTitle>
              <p className="mt-1 text-xs text-muted">
                {plural(summary.assets, 'asset')} checked
                {preview.truncated ? ' — more matched the filter than one run covers: narrow it and run again for the rest' : ''}
                {Object.keys(summary.by_action || {}).length > 0 &&
                  ` · ${Object.entries(summary.by_action)
                    .map(([k, n]) => `${settingLabel(k, catalog).toLowerCase()} ${n}`)
                    .join(', ')}`}
              </p>
              {outdated && (
                <p className="mt-1 text-xs text-warning-fg">The filters changed since this preview: preview again to apply.</p>
              )}
            </div>
            <Button onClick={() => setConfirming(true)} disabled={!canApply}>
              <CheckCircle2 className="h-4 w-4" /> Apply {writes ? plural(writes, 'change') : ''}
            </Button>
          </CardHeader>
          <CardContent className="space-y-2">
            {changes.length > 0 && (
              <div className="flex flex-wrap items-center gap-1">
                {(['all', ...CHANGE_OUTCOME_ORDER] as const).map((k) => {
                  const n = k === 'all' ? changes.length : changes.filter((c) => c.outcome === k).length
                  if (k !== 'all' && n === 0) return null
                  return (
                    <button
                      key={k}
                      type="button"
                      onClick={() => setShow(k)}
                      className={cn(
                        'whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium',
                        show === k ? 'bg-primary/10 text-text' : 'text-muted hover:bg-surface2 hover:text-text',
                      )}
                    >
                      {k === 'all' ? 'All' : CHANGE_OUTCOME_LABEL[k]} ({n})
                    </button>
                  )
                })}
                {rows.length > shown.length && <span className="ml-auto text-xs text-muted">first {shown.length} shown</span>}
              </div>
            )}
            {shown.length === 0 ? (
              <div className="py-6 text-center text-sm text-muted">
                {summary.matched ? 'Everything the rules set is already in place.' : 'No device or server matches these rules.'}
              </div>
            ) : (
              <ChangesTable changes={shown} catalog={catalog} lookups={lookups} />
            )}
          </CardContent>
        </Card>
      )}

      <Dialog open={confirming} onOpenChange={(o) => !apply.isPending && setConfirming(o)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Apply these changes?</DialogTitle>
          </DialogHeader>
          {summary && (
            <div className="space-y-2 text-sm">
              <p>{applyConfirmText(summary)}</p>
              {summary.kept_manual_edit > 0 && (
                <p className="text-muted">{sentenceCase(plural(summary.kept_manual_edit, 'hand edit'))} stay as they are (set-once rules).</p>
              )}
              {summary.not_reversible > 0 && <p className="text-muted">{summary.not_reversible} left alone (not reversible).</p>}
              <p className="text-xs text-muted">
                If anything changed since the preview, nothing is applied and the preview is refreshed. The run can be
                undone from the history.
              </p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(false)} disabled={apply.isPending}>
              Cancel
            </Button>
            <Button onClick={() => apply.mutate()} disabled={apply.isPending}>
              {apply.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Apply {summary ? plural(writes, 'change') : ''}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors',
        active ? 'border-primary bg-primary/10 text-text' : 'border-border text-muted hover:border-primary/40 hover:text-text',
      )}
    >
      {children}
    </button>
  )
}
