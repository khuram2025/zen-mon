/**
 * Coverage: every way an incident could reach nobody — priorities without a
 * policy, policy steps without a responder, schedules without members, people
 * without a contact method — each with a link to fix it. Priorities that are
 * intentionally not paged (no policy or no steps, and no response target) are
 * "Not paged (by design)", not gaps. Built-in events (rule-less alerts such as
 * agent offline) are listed with their routing and a per-family switch.
 */
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, Cpu, MinusCircle, ShieldCheck } from 'lucide-react'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useAuth, useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { fmtAlertDate } from '@/components/alerts/format'
import { useBuiltinRouting, useCoverage, usePolicies, useResponseTargets, useSetBuiltinRouting } from '@/features/escalation/api'
import {
  NOT_PAGED_LABEL, PRIORITIES, PRIORITY_META, builtinNobodyPaged, builtinSourceState, coverageIssues, policyForPriority, priorityCoverageStatus,
  type CoverageStatus, type EscalationTab,
} from '@/features/escalation/model'
import { PriorityBadge } from '@/features/escalation/ui'
import type { BuiltinSource, Coverage, Priority } from '@/features/escalation/types'

const TONE_VARIANT = { success: 'success', warning: 'warning', danger: 'danger', muted: 'outline' } as const

function StatusIcon({ status }: { status: CoverageStatus }) {
  if (status.tone === 'success') return <CheckCircle2 className="h-4 w-4 text-success" aria-label={status.label} />
  if (status.tone === 'muted') return <MinusCircle className="h-4 w-4 text-muted" aria-label={status.label} />
  return <AlertTriangle className={cn('h-4 w-4', status.tone === 'danger' ? 'text-danger' : 'text-warning')} aria-label={status.label} />
}

function statusDetail(p: Priority, c: Coverage, status: CoverageStatus): string {
  const e = c.priorities[p]
  if (status.label === 'Covered') return 'Every step reaches someone now'
  if (status.label === NOT_PAGED_LABEL) {
    return e?.reason === 'no_steps' || (e?.policy_id && e.reason !== 'no_policy')
      ? 'Its policy has no paging steps and there is no acknowledgement target; rule channels and digests only.'
      : 'No default policy and no acknowledgement target; rule channels and digests only.'
  }
  if (status.label === 'Nobody paged') return 'Its default policy has no steps, so nobody is paged.'
  if (status.label === 'Unrouted') return PRIORITY_META[p].escalation
  if (e?.gaps.length) return `${e.gaps.length} gap${e.gaps.length === 1 ? '' : 's'}`
  return status.label
}

export function CoverageTab({ go }: { go: (t: EscalationTab, id?: string | null) => void }) {
  const q = useCoverage()
  const policies = usePolicies()
  const targets = useResponseTargets()
  const me = useAuth((s) => s.user)
  const can = useCan()
  if (q.isLoading) return <Skeleton className="h-48 w-full" />
  if (q.isError && !q.data) return <QueryFailure error={q.error} what="coverage" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  const c = q.data!
  const issues = coverageIssues(c, (iso) => fmtAlertDate(iso))
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {PRIORITIES.map((p) => {
          const e = c.priorities[p]
          const policy = policies.data ? policyForPriority(policies.data, p) : null
          const status = priorityCoverageStatus(p, c, policy, targets.data ? targets.data[p] : undefined)
          const notPaged = status.label === NOT_PAGED_LABEL
          return (
            <Card key={p}>
              <CardContent className="space-y-1 p-3">
                <div className="flex items-center justify-between gap-2">
                  <PriorityBadge priority={p} />
                  <span className="flex items-center gap-1.5">
                    <Badge variant={TONE_VARIANT[status.tone]} className="text-[10px]">{status.label}</Badge>
                    <StatusIcon status={status} />
                  </span>
                </div>
                <div className="text-sm font-medium">{e?.policy_name ?? policy?.name ?? (notPaged ? 'No paging policy' : 'No policy')}</div>
                <div className="text-xs text-muted">{statusDetail(p, c, status)}</div>
              </CardContent>
            </Card>
          )
        })}
      </div>
      <Card>
        <CardContent className="p-4">
          <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="h-4 w-4 text-primary" aria-hidden /> Coverage gaps</h2>
          {issues.length === 0 ? (
            <p className="flex items-center gap-2 rounded-md border border-success/30 bg-success/5 px-3 py-2 text-sm text-success"><CheckCircle2 className="h-4 w-4" aria-hidden /> No gaps: every routed priority reaches a responder with a contact method.</p>
          ) : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {issues.map((i) => (
                <li key={i.key} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                  <AlertTriangle className={cn('h-4 w-4 shrink-0', i.severity === 'danger' ? 'text-danger' : 'text-warning')} aria-hidden />
                  <span className="min-w-0 flex-1">{i.text}</span>
                  {i.fix.tab === 'profile' ? (
                    i.fix.id && me?.id === i.fix.id ? (
                      <Button asChild size="sm" variant="outline"><Link to="/settings/general?tab=profile">{i.fix.label}</Link></Button>
                    ) : can('users.manage') ? (
                      <Button asChild size="sm" variant="outline"><Link to="/settings/general?tab=users">Open Users</Link></Button>
                    ) : <span className="text-xs text-muted">The user adds one in My Profile</span>
                  ) : (
                    <Button size="sm" variant="outline" onClick={() => go(i.fix.tab as EscalationTab, i.fix.id)}>{i.fix.label}</Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-muted">Checked against who is on call now. A gap at send time uses the policy&apos;s fallback route and records a coverage-gap event on the incident.</p>
        </CardContent>
      </Card>
      <BuiltinEventsCard sources={c.builtin_sources} coverage={c} canManage={can('alerts.manage')} />
    </div>
  )
}

/** Built-in (rule-less) event families: priority, routing, paging, and a switch per family for alerts.manage. */
export function BuiltinEventsCard({ sources, coverage = null, canManage }: { sources: BuiltinSource[] | null; coverage?: Coverage | null; canManage: boolean }) {
  const routing = useBuiltinRouting(canManage && sources != null)
  const setRouting = useSetBuiltinRouting()
  if (sources == null) {
    return (
      <Card>
        <CardContent className="p-4 text-xs text-muted">
          <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold text-text"><Cpu className="h-4 w-4 text-primary" aria-hidden /> Built-in events</h2>
          This appliance version does not report how built-in events (agent offline, sensor offline, config backups) are routed.
        </CardContent>
      </Card>
    )
  }
  const enabledKeys = routing.data?.enabled_families ?? sources.filter((b) => b.enabled).map((b) => b.key)
  const unroutedUrgent = sources.filter((b) => b.enabled && !b.routed && b.priority !== 'P4')
  const nobodyPaged = sources.filter((b) => builtinNobodyPaged(b, coverage))
  const toggle = (b: BuiltinSource, on: boolean) => setRouting.mutate({ enabled: enabledKeys, key: b.key, on }, {
    onSuccess: () => toast.success(on ? `${b.label}: routing on` : `${b.label}: routing off`),
    onError: (e) => toast.error('Could not change built-in routing', apiErrorMessage(e)),
  })
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold"><Cpu className="h-4 w-4 text-primary" aria-hidden /> Built-in events</h2>
          <p className="text-xs text-muted">
            Alerts the appliance raises without a rule. When routing is on, each escalates through the default policy of its priority (never a rule policy or legacy tiers).
          </p>
        </div>
        {unroutedUrgent.length > 0 && (
          <p role="alert" className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-text2">
            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
            <span><b className="text-text">No routing policy:</b> {unroutedUrgent.map((b) => `${b.label} (${b.priority})`).join(', ')} {unroutedUrgent.length === 1 ? 'is' : 'are'} switched on, but {unroutedUrgent.length === 1 ? 'its priority has' : 'their priorities have'} no default policy, so nobody is paged.</span>
          </p>
        )}
        {nobodyPaged.length > 0 && (
          <p role="alert" className="flex items-start gap-1.5 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-text2">
            <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
            <span><b className="text-text">Nobody paged:</b> {nobodyPaged.map((b) => `${b.label} (${b.priority})`).join(', ')} {nobodyPaged.length === 1 ? 'is' : 'are'} routed to a policy with no steps, so nobody is paged.</span>
          </p>
        )}
        {sources.length === 0 ? (
          <p className="text-xs text-muted">No built-in event families are reported.</p>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border">
            {sources.map((b) => {
              const state = builtinSourceState(b, coverage)
              const on = enabledKeys.includes(b.key)
              return (
                <li key={b.key} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                  <PriorityBadge priority={b.priority} compact />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium" title={b.description ?? undefined}>{b.label}</div>
                    {(b.note || (b.routed && b.policy_name)) && (
                      <div className="text-[11px] text-muted">{b.note ?? `Policy: ${b.policy_name}`}</div>
                    )}
                  </div>
                  <Badge variant={TONE_VARIANT[state.tone]} className="text-[10px]">{state.label}</Badge>
                  {canManage ? (
                    <Switch checked={on} disabled={setRouting.isPending} onCheckedChange={(v) => toggle(b, v)} aria-label={`Route ${b.label} events`} />
                  ) : (
                    <span className="w-10 text-right text-[11px] text-muted">{b.enabled ? 'On' : 'Off'}</span>
                  )}
                </li>
              )
            })}
          </ul>
        )}
        {!canManage && <p className="text-[11px] text-muted">Changing built-in routing needs the alerts.manage permission.</p>}
      </CardContent>
    </Card>
  )
}
