/**
 * Policies: reusable escalation policies (steps with delays and targets,
 * repeat / review / acknowledgement-expiry timers, fallback route, the
 * priorities each one is the default for) with a dry-run preview.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronsUp, Eye, Loader2, Pencil, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useDirtyGuard } from '@/hooks/useDirtyGuard'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { fmtAlertDate } from '@/components/alerts/format'
import { useDirectory, useDraftDryRun, usePolicies, usePolicyPreview } from '@/features/escalation/api'
import {
  DEFAULT_CONFLICT_REFUSAL, MAX_STEPS, PRIORITIES, PRIORITY_META, addTarget, draftFromPolicy, draftOutline, emptyPolicyDraft, fmtMinutes, fmtOffset,
  hasErrors, newStep, policyDirty, policyPayload, removeTarget, reviewText, stepSummary, validatePolicy,
  type Directory, type PolicyErrors,
} from '@/features/escalation/model'
import { MinutesField, PreviewSteps, PriorityBadge, RecipientList, TargetChip, TargetPicker } from '@/features/escalation/ui'
import type { EscalationPolicy, PolicyDraft, Priority } from '@/features/escalation/types'

export function PoliciesTab({ canManage, focusId }: { canManage: boolean; focusId: string | null }) {
  const qc = useQueryClient()
  const policies = usePolicies()
  const [editing, setEditing] = useState<EscalationPolicy | 'new' | null>(null)
  const [deleting, setDeleting] = useState<EscalationPolicy | null>(null)

  // A link from the Matrix, Coverage or an incident opens that policy once it has loaded — once per link, so a
  // refetch after saving does not open it again.
  const opened = useRef<string | null>(null)
  useEffect(() => {
    if (!focusId || !policies.data || opened.current === focusId) return
    const p = policies.data.find((x) => x.id === focusId)
    if (p) { opened.current = focusId; setEditing(p) }
  }, [focusId, policies.data])

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/escalation-policies/${id}`),
    onSuccess: () => {
      toast.success('Policy deleted')
      setDeleting(null)
      qc.invalidateQueries({ queryKey: ['escalation'] })
      qc.invalidateQueries({ queryKey: ['alert-rules'] })
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const list = policies.data ?? []
  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex flex-col gap-2 border-b border-border p-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-sm font-semibold">Escalation policies</h2>
            <p className="text-xs text-muted">A rule can name a policy; otherwise its own legacy tiers apply; otherwise the policy that is the default for the incident&apos;s priority.</p>
          </div>
          {canManage && (
            <Button size="sm" onClick={() => setEditing('new')}><Plus className="h-3.5 w-3.5" aria-hidden /> New policy</Button>
          )}
        </div>
        {policies.isLoading ? (
          <div className="p-4"><SkeletonTable rows={4} cols={6} /></div>
        ) : policies.isError && !policies.data ? (
          <div className="p-4"><QueryFailure error={policies.error} what="escalation policies" onRetry={() => void policies.refetch()} retrying={policies.isFetching} /></div>
        ) : list.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-10 text-center">
            <ChevronsUp className="h-6 w-6 text-muted" aria-hidden />
            <p className="text-sm font-medium">No escalation policies yet</p>
            <p className="max-w-md text-xs text-muted">Create one, or use “Create recommended matrix” on the Matrix tab for the four report policies. Rules keep their legacy escalation tiers until a policy applies.</p>
          </div>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>Policy</Th>
                <Th>Default for</Th>
                <Th>Steps</Th>
                <Th>Ack target</Th>
                <Th>Updates</Th>
                <Th className="text-right">Rules</Th>
                <Th className="w-[96px]"><span className="sr-only">Actions</span></Th>
              </Tr>
            </THead>
            <TBody>
              {list.map((p) => (
                <Tr key={p.id} className={cn(!p.enabled && 'opacity-60')}>
                  <Td>
                    <button type="button" onClick={() => setEditing(p)} className="text-left font-medium hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60">
                      {p.name}
                    </button>
                    {!p.enabled && <Badge variant="outline" className="ml-2">Disabled</Badge>}
                    {p.description && <div className="max-w-[320px] truncate text-xs text-muted" title={p.description}>{p.description}</div>}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {p.default_priorities.length ? p.default_priorities.map((x) => <PriorityBadge key={x} priority={x} compact />) : <span className="text-xs text-muted">—</span>}
                    </div>
                  </Td>
                  <Td className="text-xs text-text2">{stepSummary(p.steps)}</Td>
                  <Td className="text-xs">{fmtMinutes(p.ack_target_minutes)}</Td>
                  <Td className="text-xs">{p.review_minutes ? `every ${fmtMinutes(p.review_minutes)}` : '—'}</Td>
                  <Td className="text-right tabular-nums">{p.used_by_rules}</Td>
                  <Td>
                    <div className="flex justify-end gap-1">
                      <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => setEditing(p)} aria-label={`${canManage ? 'Edit' : 'View'} policy ${p.name}`}>
                        {canManage ? <Pencil className="h-3.5 w-3.5" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
                      </Button>
                      {canManage && (
                        <Button size="icon" variant="ghost" className="h-7 w-7 text-muted hover:text-danger" onClick={() => setDeleting(p)} aria-label={`Delete policy ${p.name}`}>
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
      {editing && (
        <PolicyEditorDialog
          key={editing === 'new' ? 'new' : editing.id}
          policy={editing === 'new' ? null : editing}
          others={list.filter((x) => editing === 'new' || x.id !== editing.id)}
          readOnly={!canManage}
          onClose={() => setEditing(null)}
        />
      )}
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => { if (!o) setDeleting(null) }}
        title={`Delete “${deleting?.name ?? ''}”?`}
        description={deleting?.used_by_rules
          ? `${deleting.used_by_rules} rule${deleting.used_by_rules === 1 ? '' : 's'} name this policy. They fall back to their own legacy tiers, otherwise the priority default policy, otherwise no escalation.`
          : 'Incidents of the priorities it is the default for will no longer escalate through it.'}
        confirmText="Delete policy"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </Card>
  )
}

/* ── Editor ───────────────────────────────────────────────────────────── */

function PolicyEditorDialog({ policy, others, readOnly, onClose }: { policy: EscalationPolicy | null; others: EscalationPolicy[]; readOnly: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const dir = useDirectory()
  const [saved] = useState<PolicyDraft>(() => (policy ? draftFromPolicy(policy) : emptyPolicyDraft()))
  const [draft, setDraft] = useState<PolicyDraft>(saved)
  const [showErrors, setShowErrors] = useState(false)
  const dirty = !readOnly && policyDirty(draft, saved)
  const errors = validatePolicy(draft)

  const save = useMutation({
    mutationFn: async () => {
      const body = policyPayload(draft)
      return policy ? (await api.put(`/escalation-policies/${policy.id}`, body)).data : (await api.post('/escalation-policies', body)).data
    },
    onSuccess: () => {
      toast.success(policy ? 'Policy saved' : 'Policy created')
      qc.invalidateQueries({ queryKey: ['escalation'] })
      qc.invalidateQueries({ queryKey: ['alerts', 'route'] })
      onClose()
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const guard = useDirtyGuard(dirty, save.isPending, (o) => { if (!o) onClose() })

  // Leaving the page with unsaved edits asks first, like closing the dialog does.
  useEffect(() => {
    if (!dirty) return
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [dirty])

  const submit = () => {
    if (hasErrors(errors)) { setShowErrors(true); return }
    save.mutate()
  }

  return (
    <Dialog open onOpenChange={guard.onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-6xl">
        <DialogHeader>
          <DialogTitle>{readOnly ? policy?.name ?? 'Policy' : policy ? `Edit “${policy.name}”` : 'New escalation policy'}</DialogTitle>
          <DialogDescription>
            Step 1 at 0 min pages in addition to the rule&apos;s own channels; later steps fire while the incident is active and unacknowledged.
            {policy?.used_by_rules ? ` ${policy.used_by_rules} rule${policy.used_by_rules === 1 ? '' : 's'} name this policy.` : ''}
          </DialogDescription>
        </DialogHeader>
        <PolicyEditorBody
          draft={draft}
          setDraft={setDraft}
          errors={showErrors ? errors : {}}
          dir={dir}
          readOnly={readOnly}
          others={others}
          policyId={policy?.id ?? null}
          revision={policy?.updated_at ?? policy?.id ?? null}
          dirty={dirty}
        />
        <DialogFooter>
          {dirty && <span className="mr-auto self-center text-xs text-warning">Unsaved changes</span>}
          <Button variant="outline" onClick={() => guard.onOpenChange(false)} disabled={save.isPending}>{readOnly ? 'Close' : 'Cancel'}</Button>
          {!readOnly && (
            <Button onClick={submit} disabled={save.isPending || (!!policy && !dirty)}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />} {policy ? 'Save policy' : 'Create policy'}
            </Button>
          )}
        </DialogFooter>
        {guard.confirm}
      </DialogContent>
    </Dialog>
  )
}

/** The editor form and its preview panel (no dialog; scripts/test-escalation-ui.mjs renders it). */
export function PolicyEditorBody({ draft, setDraft, errors, dir, readOnly, others, policyId, revision, dirty }: {
  draft: PolicyDraft
  setDraft: (fn: (d: PolicyDraft) => PolicyDraft) => void
  errors: PolicyErrors
  dir: Directory
  readOnly: boolean
  others: EscalationPolicy[]
  policyId: string | null
  revision: string | null
  dirty: boolean
}) {
  const set = (patch: Partial<PolicyDraft>) => setDraft((d) => ({ ...d, ...patch }))
  const setStep = (key: string, patch: Partial<PolicyDraft['steps'][number]>) =>
    setDraft((d) => ({ ...d, steps: d.steps.map((s) => (s.key === key ? { ...s, ...patch } : s)) }))
  const togglePriority = (p: Priority) =>
    setDraft((d) => ({ ...d, default_priorities: d.default_priorities.includes(p) ? d.default_priorities.filter((x) => x !== p) : [...d.default_priorities, p] }))
  const claimedBy = (p: Priority) => others.filter((o) => o.enabled && o.default_priorities.includes(p)).map((o) => o.name)
  // Rows keep the order they were added in while typing (no jumping focus); they are saved in delay order.
  const delays = draft.steps.map((s) => Number(s.delay_minutes))
  const outOfOrder = delays.some((d, i) => i > 0 && Number.isFinite(d) && Number.isFinite(delays[i - 1]) && d < delays[i - 1])

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
      <div className="min-w-0 space-y-4">
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
          <FormField label="Name" required error={errors.name}>
            <Input value={draft.name} disabled={readOnly} onChange={(e) => set({ name: e.target.value })} placeholder="e.g. P1 Critical — major outage" maxLength={255} />
          </FormField>
          <div className="flex items-end gap-2 pb-1">
            <Switch checked={draft.enabled} disabled={readOnly} onCheckedChange={(v) => set({ enabled: v })} aria-label="Policy enabled" />
            <span className="text-sm">{draft.enabled ? 'Enabled' : 'Disabled'}</span>
          </div>
        </div>
        <FormField label="Description">
          <Textarea rows={2} value={draft.description} disabled={readOnly} onChange={(e) => set({ description: e.target.value })} placeholder="Who this policy is for and when it applies" />
        </FormField>

        <fieldset className="space-y-1.5">
          <legend className="text-xs font-medium uppercase tracking-wider text-muted">Default policy for priorities</legend>
          <div className="flex flex-wrap gap-2">
            {PRIORITIES.map((p) => {
              const on = draft.default_priorities.includes(p)
              const taken = claimedBy(p)
              return (
                <button
                  key={p}
                  type="button"
                  disabled={readOnly}
                  aria-pressed={on}
                  onClick={() => togglePriority(p)}
                  title={taken.length ? `Also claimed by ${taken.join(', ')}` : PRIORITY_META[p].trigger}
                  className={cn('rounded-md border px-2.5 py-1 text-xs font-semibold disabled:opacity-60', on ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text')}
                >
                  {PRIORITY_META[p].label}{taken.length > 0 && on ? ' ⚠' : ''}
                </button>
              )
            })}
          </div>
          {draft.default_priorities.some((p) => claimedBy(p).length) && (
            <p className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />
              {draft.default_priorities.filter((p) => claimedBy(p).length).map((p) => `${p} is also the default in ${claimedBy(p).join(', ')}`).join('; ')}
              {draft.enabled ? `: ${DEFAULT_CONFLICT_REFUSAL}.` : `. This policy is disabled, so it can be saved, but enabling it later will be refused unless the other policy drops it.`}
            </p>
          )}
          <p className="text-xs text-muted">Used for incidents whose rule names no policy and has no legacy tiers. Leave all off for a policy that rules choose explicitly.</p>
        </fieldset>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <FormField label="Acknowledge within" error={errors.ack_target_minutes} hint="Policy acknowledgement target">
            <MinutesField value={draft.ack_target_minutes} disabled={readOnly} onChange={(v) => set({ ack_target_minutes: v })} placeholder="none" invalid={!!errors.ack_target_minutes} />
          </FormField>
          <FormField label="Repeat step every" error={errors.repeat_minutes} hint="Re-notify the current step while unacknowledged">
            <MinutesField value={draft.repeat_minutes} max={1440} disabled={readOnly} onChange={(v) => set({ repeat_minutes: v })} placeholder="never" invalid={!!errors.repeat_minutes} />
          </FormField>
          <FormField label="Max repeats" error={errors.max_repeats} hint="Per step">
            <Input type="number" min={0} max={20} className="w-24" value={draft.max_repeats} disabled={readOnly} onChange={(e) => set({ max_repeats: e.target.value })} />
          </FormField>
          <FormField label="Unresolved updates every" error={errors.review_minutes} hint="Continue after acknowledgement, until resolved">
            <MinutesField value={draft.review_minutes} disabled={readOnly} onChange={(v) => set({ review_minutes: v })} placeholder="none" invalid={!!errors.review_minutes} />
          </FormField>
          <FormField label="Acknowledgement expires after" error={errors.ack_expiry_minutes} hint="Re-opens escalation from the current step" className="sm:col-span-2">
            <MinutesField value={draft.ack_expiry_minutes} disabled={readOnly} onChange={(v) => set({ ack_expiry_minutes: v })} placeholder="never" invalid={!!errors.ack_expiry_minutes} />
          </FormField>
        </div>

        <section aria-labelledby="policy-steps-h" className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 id="policy-steps-h" className="text-sm font-semibold">Steps</h3>
            {errors.steps && <span className="text-xs text-danger">{errors.steps}</span>}
          </div>
          <ol className="space-y-2">
            {draft.steps.map((s, i) => {
              const err = errors.stepErrors?.[s.key]
              return (
                <li key={s.key} className={cn('space-y-2 rounded-lg border p-3', err ? 'border-danger/50' : 'border-border bg-surface2/30')}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold uppercase tracking-wider text-primary">Step {i + 1}</span>
                    <span className="text-xs text-muted">{Number(s.delay_minutes) > 0 ? 'if still unacknowledged after' : 'at'}</span>
                    <MinutesField
                      value={s.delay_minutes}
                      min={0}
                      ariaLabel={`Step ${i + 1} delay in minutes`}
                      disabled={readOnly}
                      invalid={!!err && err.startsWith('Delay')}
                      onChange={(v) => setStep(s.key, { delay_minutes: v })}
                    />
                    <Input
                      className="h-8 min-w-[160px] flex-1"
                      aria-label={`Step ${i + 1} name`}
                      placeholder={i === 0 ? 'Initial page' : 'e.g. Backup on-call'}
                      value={s.name}
                      disabled={readOnly}
                      maxLength={100}
                      onChange={(e) => setStep(s.key, { name: e.target.value })}
                    />
                    {!readOnly && (
                      <Button type="button" size="icon" variant="ghost" className="h-7 w-7 text-muted hover:text-danger" aria-label={`Remove step ${i + 1}`}
                        onClick={() => setDraft((d) => ({ ...d, steps: d.steps.filter((x) => x.key !== s.key) }))}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {s.targets.map((t) => (
                      <TargetChip key={`${t.type}:${t.id}`} target={t} dir={dir} onRemove={readOnly ? undefined : () => setStep(s.key, { targets: removeTarget(s.targets, t) })} />
                    ))}
                    {!readOnly && <TargetPicker dir={dir} value={s.targets} label={`Add a target to step ${i + 1}`} onAdd={(t) => setStep(s.key, { targets: addTarget(s.targets, t) })} />}
                    {readOnly && !s.targets.length && <span className="text-xs text-muted">No targets</span>}
                  </div>
                  {err && <p className="text-xs text-danger">{err}</p>}
                </li>
              )
            })}
          </ol>
          {outOfOrder && <p className="text-xs text-muted">Steps are saved and run in delay order.</p>}
          {!readOnly && draft.steps.length < MAX_STEPS && (
            <Button type="button" variant="outline" size="sm" onClick={() => setDraft((d) => ({ ...d, steps: [...d.steps, newStep(d)] }))}>
              <Plus className="h-3.5 w-3.5" aria-hidden /> Add step
            </Button>
          )}
        </section>

        <section aria-labelledby="policy-fallback-h" className="space-y-1.5 rounded-lg border border-border p-3">
          <h3 id="policy-fallback-h" className="text-sm font-semibold">Fallback route</h3>
          <p className="text-xs text-muted">Used when a step resolves to nobody, or every delivery of the step failed. A coverage-gap or route-failed event is recorded on the incident.</p>
          <div className="flex flex-wrap items-center gap-1.5">
            {draft.fallback_targets.map((t) => (
              <TargetChip key={`${t.type}:${t.id}`} target={t} dir={dir} onRemove={readOnly ? undefined : () => set({ fallback_targets: removeTarget(draft.fallback_targets, t) })} />
            ))}
            {!readOnly && <TargetPicker dir={dir} value={draft.fallback_targets} label="Add a fallback target" onAdd={(t) => set({ fallback_targets: addTarget(draft.fallback_targets, t) })} />}
            {readOnly && !draft.fallback_targets.length && <span className="text-xs text-muted">None</span>}
          </div>
        </section>
      </div>

      <PreviewPanel draft={draft} dir={dir} policyId={policyId} revision={revision} dirty={dirty} />
    </div>
  )
}

/** Live outline of the draft plus the server dry run (saved policy, or the unsaved draft). Never sends anything. */
function PreviewPanel({ draft, dir, policyId, revision, dirty }: { draft: PolicyDraft; dir: Directory; policyId: string | null; revision: string | null; dirty: boolean }) {
  const [priority, setPriority] = useState<Priority>(() => draft.default_priorities[0] ?? 'P3')
  const outline = useMemo(() => draftOutline(draft, dir), [draft, dir])
  // A saved, unchanged policy is previewed by id; a new or edited one as a draft, once it is valid and the
  // typing has paused (the debounced body is the query key).
  const useDraft = !policyId || dirty
  const valid = !hasErrors(validatePolicy(draft))
  const body = useDebounced(valid ? policyPayload(draft) : null, 500)
  const saved = usePolicyPreview(policyId, priority, revision, !useDraft)
  const draftRun = useDraftDryRun(body, priority, useDraft)
  const preview = useDraft ? draftRun : saved
  const fmt = (iso: string) => fmtAlertDate(iso)
  const review = preview.data ? reviewText(preview.data.review) : null
  return (
    <aside aria-label="Policy preview" className="space-y-3 lg:sticky lg:top-0 lg:self-start">
      <div className="rounded-lg border border-border bg-surface2/30 p-3">
        <h3 className="mb-2 text-sm font-semibold">Draft outline</h3>
        <ol className="space-y-1.5">
          {outline.steps.map((s) => (
            <li key={s.key} className="text-sm">
              <div className="flex items-baseline gap-2">
                <span className="w-20 shrink-0 text-xs font-semibold tabular-nums text-text2">{fmtOffset(s.delay)}</span>
                <span className="min-w-0 font-medium">{s.name}</span>
              </div>
              <div className="ml-[88px] text-xs text-muted">{s.targets.map((t) => t.label).join(', ') || '—'}</div>
              {s.gaps.map((g) => <div key={g} className="ml-[88px] flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />{g}</div>)}
            </li>
          ))}
        </ol>
        {(minutes(draft.review_minutes) || minutes(draft.ack_expiry_minutes)) && (
          <p className="mt-2 text-xs text-text2">
            {minutes(draft.review_minutes) ? `Unresolved updates every ${fmtMinutes(minutes(draft.review_minutes))}. ` : ''}
            {minutes(draft.ack_expiry_minutes) ? `An acknowledgement older than ${fmtMinutes(minutes(draft.ack_expiry_minutes))} re-opens escalation.` : ''}
          </p>
        )}
        {outline.warnings.length > 0 && (
          <ul className="mt-2 space-y-0.5 border-t border-border pt-2">
            {outline.warnings.map((w) => <li key={w} className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />{w}</li>)}
          </ul>
        )}
      </div>

      <div className="rounded-lg border border-border p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">Dry run — who would be notified</h3>
          <label className="flex items-center gap-1.5 text-xs text-muted">
            For
            <select aria-label="Preview priority" value={priority} onChange={(e) => setPriority(e.target.value as Priority)} className="h-7 rounded-md border border-border bg-surface px-1.5 text-xs text-text">
              {PRIORITIES.map((p) => <option key={p} value={p}>{PRIORITY_META[p].label}</option>)}
            </select>
          </label>
        </div>
        {useDraft && !valid ? (
          <p className="text-xs text-muted">Complete the policy (a name, and targets on every step) to resolve who would be notified. The dry run never sends anything.</p>
        ) : (
          <>
            {useDraft && <p className="mb-2 text-xs text-muted">Previewing your unsaved changes.</p>}
            {preview.isLoading || (useDraft && !body) ? (
              <div className="flex items-center gap-2 py-3 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Resolving recipients…</div>
            ) : preview.isError ? (
              <QueryFailure compact error={preview.error} what="the dry run" onRetry={() => void preview.refetch()} retrying={preview.isFetching} />
            ) : preview.data ? (
              <div className="space-y-2">
                <PreviewSteps steps={preview.data.steps} fmt={fmt} />
                {review && <p className="text-xs text-text2">{review}</p>}
                {(preview.data.fallback?.length ?? 0) > 0 && (
                  <div>
                    <div className="text-xs font-medium text-muted">Fallback route</div>
                    <RecipientList recipients={preview.data.fallback ?? []} />
                  </div>
                )}
                {(preview.data.fallback_gaps?.length ?? 0) > 0 && (
                  <ul className="space-y-0.5">
                    {preview.data.fallback_gaps!.map((g) => <li key={g} className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />Fallback: {g}</li>)}
                  </ul>
                )}
                {(preview.data.warnings?.length ?? 0) > 0 && (
                  <ul className="space-y-0.5 border-t border-border pt-2">
                    {preview.data.warnings!.map((w) => <li key={w} className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />{w}</li>)}
                  </ul>
                )}
                <p className="text-xs text-muted">Resolved for now: on-call overrides win, then the highest layer on duty. Duplicate addresses across targets are sent once.</p>
              </div>
            ) : null}
          </>
        )}
      </div>
    </aside>
  )
}

/** `value` once it has stopped changing for `ms` (compared by JSON). */
function useDebounced<T>(value: T, ms: number): T {
  const json = JSON.stringify(value)
  const [out, setOut] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setOut(JSON.parse(json ?? 'null')), ms)
    return () => clearTimeout(t)
  }, [json, ms])
  return out
}

function minutes(v: string): number | null {
  const n = Number(v)
  return v.trim() !== '' && Number.isFinite(n) && n > 0 ? Math.round(n) : null
}
