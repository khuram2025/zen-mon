/**
 * Coverage: every way an incident could reach nobody — priorities without a
 * policy, policy steps without a responder, schedules without members, people
 * without a contact method — each with a link to fix it.
 */
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, ShieldCheck } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { fmtAlertDate } from '@/components/alerts/format'
import { useCoverage } from '@/features/escalation/api'
import { PRIORITIES, PRIORITY_META, coverageIssues, type EscalationTab } from '@/features/escalation/model'
import { PriorityBadge } from '@/features/escalation/ui'

export function CoverageTab({ go }: { go: (t: EscalationTab, id?: string | null) => void }) {
  const q = useCoverage()
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
          const unrouted = c.unrouted_priorities.includes(p)
          const ok = !!e && e.steps_ok && !e.gaps.length && !unrouted
          return (
            <Card key={p}>
              <CardContent className="space-y-1 p-3">
                <div className="flex items-center justify-between"><PriorityBadge priority={p} />
                  {ok ? <CheckCircle2 className="h-4 w-4 text-success" aria-label="Covered" /> : <AlertTriangle className={cn('h-4 w-4', p === 'P4' && unrouted ? 'text-muted' : 'text-warning')} aria-label="Not fully covered" />}
                </div>
                <div className="text-sm font-medium">{e?.policy_name ?? (unrouted ? (p === 'P4' ? 'No paging (by design)' : 'No policy') : '—')}</div>
                <div className="text-xs text-muted">{ok ? 'Every step reaches someone now' : e?.gaps.length ? `${e.gaps.length} gap${e.gaps.length === 1 ? '' : 's'}` : unrouted ? PRIORITY_META[p].escalation : 'Not checked'}</div>
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
          <p className="mt-2 text-xs text-muted">Checked against who is on call now. A gap at send time uses the policy&apos;s fallback route and records a coverage-gap event on the incident.</p>
        </CardContent>
      </Card>
    </div>
  )
}
