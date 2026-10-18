/**
 * Matrix: the P1–P4 escalation matrix as configured — default policy, initial
 * owner and channels, acknowledgement target, step timeline with delays,
 * review cadence and coverage — beside the field report's recommendation.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, Loader2, ShieldCheck, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { useCoverage, useDirectory, usePolicies, useResponseTargets } from '@/features/escalation/api'
import {
  PRIORITIES, PRIORITY_META, describeTarget, fmtMinutes, policyForPriority, priorityConflicts, priorityCoverageStatus,
  recommendedPayload, recommendedPlan, validateRecommended, type Directory, type EscalationTab, type RecommendedForm,
} from '@/features/escalation/model'
import { PriorityBadge, StepTimeline, TargetChip } from '@/features/escalation/ui'
import type { Coverage, EscalationPolicy, Priority, ResponseTargets } from '@/features/escalation/types'

const TONE_VARIANT = { success: 'success', warning: 'warning', danger: 'danger', muted: 'outline' } as const

export function MatrixTab({ canManage, go }: { canManage: boolean; go: (t: EscalationTab, id?: string | null) => void }) {
  const policies = usePolicies()
  const coverage = useCoverage()
  const targets = useResponseTargets()
  const dir = useDirectory()
  const [wizard, setWizard] = useState(false)

  if (policies.isLoading) {
    return <div className="grid gap-4 xl:grid-cols-2">{PRIORITIES.map((p) => <Skeleton key={p} className="h-64" />)}</div>
  }
  if (policies.isError && !policies.data) {
    return <QueryFailure error={policies.error} what="escalation policies" onRetry={() => void policies.refetch()} retrying={policies.isFetching} />
  }
  return (
    <MatrixView
      policies={policies.data ?? []}
      coverage={coverage.data ?? null}
      coverageError={coverage.isError}
      targets={targets.data ?? null}
      dir={dir}
      canManage={canManage}
      go={go}
      onRecommended={() => setWizard(true)}
      wizard={canManage && wizard ? <RecommendedMatrixDialog dir={dir} existing={policies.data ?? []} onClose={() => setWizard(false)} /> : null}
    />
  )
}

/** The matrix itself (rendered from data; scripts/test-escalation-ui.mjs renders it with seeded queries). */
export function MatrixView({ policies, coverage, coverageError, targets, dir, canManage, go, onRecommended, wizard }: {
  policies: EscalationPolicy[]
  coverage: Coverage | null
  coverageError?: boolean
  targets: ResponseTargets | null
  dir: Directory
  canManage: boolean
  go: (t: EscalationTab, id?: string | null) => void
  onRecommended: () => void
  wizard?: React.ReactNode
}) {
  const conflicts = priorityConflicts(policies)
  const unrouted = PRIORITIES.filter((p) => p !== 'P4' && !policyForPriority(policies, p))
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface2/30 p-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 text-sm">
          <p className="font-medium">Timers start when an incident is confirmed firing. Critical notification never waits for a warning digest.</p>
          <p className="text-xs text-muted">
            A rule&apos;s own policy wins; otherwise the default policy for the incident&apos;s priority applies; otherwise the rule&apos;s legacy tiers. Acknowledging stops step escalation and assigns the owner; unresolved-incident updates continue until resolution.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => go('coverage')}><ShieldCheck className="h-3.5 w-3.5" aria-hidden /> Coverage</Button>
          {canManage && (
            <Button size="sm" onClick={onRecommended}><Sparkles className="h-3.5 w-3.5" aria-hidden /> Create recommended matrix</Button>
          )}
        </div>
      </div>

      {(conflicts.length > 0 || unrouted.length > 0) && (
        <div role="status" className="space-y-1 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-text2">
          {unrouted.length > 0 && (
            <p className="flex items-start gap-1.5"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
              <span><b className="text-text">Unrouted priorities: {unrouted.join(', ')}.</b> Incidents of these priorities escalate only when their rule names a policy or has legacy tiers.</span>
            </p>
          )}
          {conflicts.map((c) => (
            <p key={c.priority} className="flex items-start gap-1.5"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
              <span><b className="text-text">Route conflict for {c.priority}:</b> {c.policies.join(', ')} all claim it; only “{c.policies[0]}” is used.</span>
            </p>
          ))}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {PRIORITIES.map((p) => (
          <PriorityCard key={p} priority={p} policy={policyForPriority(policies, p)} coverage={coverage} coverageError={coverageError} ack={targets ? targets[p] : undefined} dir={dir} go={go} />
        ))}
      </div>
      {wizard}
    </div>
  )
}

function PriorityCard({ priority: p, policy, coverage, coverageError, ack, dir, go }: {
  priority: Priority
  policy: EscalationPolicy | null
  coverage: Coverage | null
  coverageError?: boolean
  ack: number | null | undefined
  dir: Directory
  go: (t: EscalationTab, id?: string | null) => void
}) {
  const meta = PRIORITY_META[p]
  const status = coverageError ? { tone: 'muted' as const, label: 'Coverage unavailable' } : priorityCoverageStatus(p, coverage, policy)
  const gaps = coverage?.priorities[p]?.gaps ?? []
  const steps = policy?.steps ?? []
  const first = steps[0]
  const ackText = ack === undefined ? '…' : ack == null ? 'No target' : `Within ${fmtMinutes(ack)}`
  return (
    <Card className={cn(p === 'P1' && 'border-danger/40')}>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <PriorityBadge priority={p} />
              <h2 className="text-base font-semibold">{meta.name}</h2>
            </div>
            <p className="mt-1 text-xs text-muted">{meta.trigger}</p>
          </div>
          <Badge variant={TONE_VARIANT[status.tone]}>{status.label}</Badge>
        </div>

        <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[140px_minmax(0,1fr)]">
          <dt className="text-xs font-medium uppercase tracking-wider text-muted">Policy</dt>
          <dd className="min-w-0">
            {policy ? (
              <button type="button" onClick={() => go('policies', policy.id)} className="inline-flex items-center gap-1 font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60">
                {policy.name} <ArrowRight className="h-3 w-3" aria-hidden />
              </button>
            ) : (
              <span className="text-muted">{p === 'P4' ? 'None — no paging escalation' : 'No default policy'}</span>
            )}
          </dd>

          <dt className="text-xs font-medium uppercase tracking-wider text-muted">Owner &amp; channel</dt>
          <dd className="min-w-0">
            {first?.targets.length ? (
              <div className="flex flex-wrap gap-1">{first.targets.map((t) => <TargetChip key={`${t.type}:${t.id}`} target={t} dir={dir} />)}</div>
            ) : (
              <span className="text-xs text-muted">Recommended: {meta.owner}</span>
            )}
          </dd>

          <dt className="text-xs font-medium uppercase tracking-wider text-muted">Acknowledge</dt>
          <dd>
            <span className="font-medium">{ackText}</span>
            {policy?.ack_target_minutes != null && ack != null && policy.ack_target_minutes !== ack && (
              <span className="ml-1 text-xs text-warning">(policy says {fmtMinutes(policy.ack_target_minutes)})</span>
            )}
          </dd>

          <dt className="text-xs font-medium uppercase tracking-wider text-muted">Escalation</dt>
          <dd className="min-w-0">
            {policy ? (
              <StepTimeline
                ackMinutes={ack ?? null}
                steps={steps.map((s, i) => ({
                  delay: s.delay_minutes,
                  label: s.name || `Step ${i + 1}`,
                  detail: s.targets.map((t) => describeTarget(t, dir).label).join(', ') || 'No targets',
                  warn: !s.targets.length,
                }))}
              />
            ) : (
              <span className="text-xs text-muted">Recommended: {meta.escalation}</span>
            )}
          </dd>

          <dt className="text-xs font-medium uppercase tracking-wider text-muted">Review cadence</dt>
          <dd className="text-sm">
            {policy?.review_minutes ? `Updates every ${fmtMinutes(policy.review_minutes)} while unresolved` : policy ? <span className="text-muted">No unresolved-incident updates</span> : <span className="text-xs text-muted">Recommended: {meta.review}</span>}
            {policy?.repeat_minutes ? <div className="text-xs text-muted">Re-notifies the current step every {fmtMinutes(policy.repeat_minutes)}, up to {policy.max_repeats}×</div> : null}
            {policy?.ack_expiry_minutes ? <div className="text-xs text-muted">Acknowledgement expires after {fmtMinutes(policy.ack_expiry_minutes)}</div> : null}
          </dd>
        </dl>

        {gaps.length > 0 && (
          <ul className="space-y-0.5 rounded-md border border-warning/40 bg-warning/5 p-2">
            {gaps.map((g) => <li key={g} className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />{g}</li>)}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

/* ── Create recommended matrix ────────────────────────────────────────── */

const claimed = (existing: EscalationPolicy[]) => PRIORITIES.filter((p) => existing.some((x) => x.enabled && x.default_priorities.includes(p)))

function RecommendedMatrixDialog({ dir, existing, onClose }: { dir: Directory; existing: EscalationPolicy[]; onClose: () => void }) {
  const qc = useQueryClient()
  const [f, setF] = useState<RecommendedForm>({ primary_schedule_id: '', backup_schedule_id: '', lead_user_id: '', noc_channel_ids: [], team_channel_ids: [], replace_defaults: false })
  const error = validateRecommended(f) ?? (claimed(existing).length && !f.replace_defaults ? 'Confirm moving the existing defaults, or remove them first' : null)
  const names = (t: { type: 'schedule' | 'user' | 'channel'; id: string }) => describeTarget(t, dir).label
  const plan = recommendedPlan(f, names)
  const claimedNow = claimed(existing)

  const create = useMutation({
    mutationFn: async () => (await api.post('/escalation-policies/recommended', recommendedPayload(f))).data,
    onSuccess: () => {
      toast.success('Recommended matrix created', 'Four policies, one per priority. Review them on the Policies tab.')
      qc.invalidateQueries({ queryKey: ['escalation'] })
      onClose()
    },
    onError: (e) => toast.error('Could not create the matrix', apiErrorMessage(e)),
  })

  const toggle = (key: 'noc_channel_ids' | 'team_channel_ids', id: string) =>
    setF((s) => ({ ...s, [key]: s[key].includes(id) ? s[key].filter((x) => x !== id) : [...s[key], id] }))

  const select = 'h-9 w-full rounded-md border border-border bg-surface px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40'

  return (
    <Dialog open onOpenChange={(o) => { if (!o && !create.isPending) onClose() }}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-3xl">
        <DialogHeader>
          <DialogTitle>Create the recommended matrix</DialogTitle>
          <DialogDescription>
            Creates four escalation policies (P1–P4) from the field-report matrix, each the default for its priority. Values stay editable afterwards.
          </DialogDescription>
        </DialogHeader>
        {dir.schedules.length === 0 && (
          <p role="alert" className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
            Create an on-call schedule first (On-call tab) — the matrix pages the primary on-call.
          </p>
        )}
        {claimedNow.length > 0 && (
          <label className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-text2">
            <input type="checkbox" className="mt-0.5 h-4 w-4" checked={f.replace_defaults} onChange={(e) => setF({ ...f, replace_defaults: e.target.checked })} />
            <span>
              Existing policies are already the default for {claimedNow.join(', ')} ({PRIORITIES.filter((p) => claimedNow.includes(p)).map((p) => existing.find((x) => x.enabled && x.default_priorities.includes(p))?.name).filter(Boolean).join(', ')}).
              <b className="text-text"> Move those defaults to the new policies.</b> The existing policies are kept; rules that name them are unchanged.
            </span>
          </label>
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <FormField label="Primary on-call" required>
            <select className={select} aria-label="Primary on-call schedule" value={f.primary_schedule_id} onChange={(e) => setF({ ...f, primary_schedule_id: e.target.value })}>
              <option value="">Choose a schedule…</option>
              {dir.schedules.map((s) => <option key={s.id} value={s.id}>{s.name}{s.members === 0 ? ' (no members)' : ''}</option>)}
            </select>
          </FormField>
          <FormField label="Backup on-call">
            <select className={select} aria-label="Backup on-call schedule" value={f.backup_schedule_id} onChange={(e) => setF({ ...f, backup_schedule_id: e.target.value })}>
              <option value="">None</option>
              {dir.schedules.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </FormField>
          <FormField label="Incident / team lead">
            <select className={select} aria-label="Incident or team lead" value={f.lead_user_id} onChange={(e) => setF({ ...f, lead_user_id: e.target.value })}>
              <option value="">None</option>
              {dir.users.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
            </select>
          </FormField>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {(['noc_channel_ids', 'team_channel_ids'] as const).map((key) => (
            <fieldset key={key} className="space-y-1.5">
              <legend className="text-xs font-medium uppercase tracking-wider text-muted">{key === 'noc_channel_ids' ? 'NOC channels (P1, P2)' : 'Team channels (P2, P3)'}</legend>
              {dir.channels.length === 0 ? (
                <p className="text-xs text-muted">No channels exist yet.</p>
              ) : (
                <div className="max-h-40 space-y-1 overflow-y-auto">
                  {dir.channels.map((c) => (
                    <label key={c.id} className={cn('flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1.5 text-sm', f[key].includes(c.id) ? 'border-primary/50 bg-primary/5' : 'border-border')}>
                      <input type="checkbox" className="h-4 w-4" checked={f[key].includes(c.id)} onChange={() => toggle(key, c.id)} />
                      <span className="min-w-0 flex-1 truncate">{c.name}</span>
                      <span className="text-[11px] uppercase text-muted">{c.type}</span>
                    </label>
                  ))}
                </div>
              )}
            </fieldset>
          ))}
        </div>
        <section aria-label="What will be created" className="rounded-md border border-border bg-surface2/30 p-3">
          <h3 className="mb-2 text-sm font-semibold">What will be created</h3>
          <div className="grid gap-3 sm:grid-cols-2">
            {plan.map((row) => (
              <div key={row.priority} className="space-y-1">
                <div className="flex items-center gap-2 text-xs">
                  <PriorityBadge priority={row.priority} />
                  <span className="text-muted">Ack {row.ack ? fmtMinutes(row.ack) : 'no target'}{row.review ? ` · updates every ${fmtMinutes(row.review)}` : ''}</span>
                </div>
                {row.steps.length ? (
                  <StepTimeline steps={row.steps.map((s) => ({ delay: s.delay, label: s.who || '—' }))} />
                ) : (
                  <p className="text-xs text-muted">No paging escalation; included in digests.</p>
                )}
              </div>
            ))}
          </div>
        </section>
        <DialogFooter>
          {error && <p className="mr-auto self-center text-xs text-muted">{error}</p>}
          <Button variant="outline" onClick={onClose} disabled={create.isPending}>Cancel</Button>
          <Button onClick={() => create.mutate()} disabled={!!error || create.isPending}>
            {create.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />} Create four policies
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
