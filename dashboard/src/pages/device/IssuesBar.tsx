import { forwardRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, ChevronRight, CircleSlash, Info, XCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { repeatPhrase } from './eventsModel'
import { formatAbsolute, formatRelative } from './format'
import type { DeviceIssue, DeviceSummary } from './types'

/* What is wrong right now, worst first then oldest: the confirmed status when
 * it is not Up, open alerts, template groups needing attention (tunnel down,
 * APs offline, PSU, fans, temperature), unexpectedly down interfaces, flapping
 * and maintenance. Each item opens the tab that shows it. Repeats of one alert
 * condition from one rule are one item ("Degraded (packet loss) ×5 in 3 days").
 * When nothing is open the bar collapses to one line. */

const ICON = { critical: XCircle, warning: AlertTriangle, info: Info } as const
const TONE = {
  critical: 'text-danger',
  warning: 'text-warning',
  info: 'text-info',
} as const

export const IssuesBar = forwardRef<HTMLElement, {
  summary?: DeviceSummary
  loading: boolean
  onOpen: (issue: DeviceIssue) => void
}>(function IssuesBar({ summary, loading, onOpen }, ref) {
  const [expanded, setExpanded] = useState(false)
  if (!summary) {
    return <section ref={ref} aria-label="Open issues" className="rounded-xl border border-border bg-surface px-4 py-2.5 text-sm text-muted">{loading ? 'Checking for open issues…' : 'Open issues are temporarily unavailable.'}</section>
  }
  const issues = summary.issues
  const counted = issues.filter((i) => i.severity !== 'info')
  const lastChange = summary.status_changed_at
  if (!counted.length) {
    const info = issues.filter((i) => i.severity === 'info')
    return (
      <section ref={ref} aria-label="Open issues" className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-border bg-surface px-4 py-2.5 text-sm">
        <span className="inline-flex items-center gap-1.5 font-medium text-success"><CheckCircle2 className="h-4 w-4" aria-hidden />No open issues</span>
        {lastChange && <span className="text-muted" title={formatAbsolute(lastChange)}>Last status change {formatRelative(lastChange)}</span>}
        {info.map((i, n) => <IssueButton key={n} issue={i} onOpen={onOpen} />)}
        <NotApplicable summary={summary} />
      </section>
    )
  }
  const limit = 4
  const shown = expanded ? issues : issues.slice(0, limit)
  const worst = counted.some((i) => i.severity === 'critical') ? 'border-l-danger' : 'border-l-warning'
  return (
    <section ref={ref} aria-label="Open issues" className={cn('rounded-xl border border-border border-l-4 bg-surface px-4 py-2.5', worst)}>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
        <h2 className="text-sm font-semibold">{counted.length} open issue{counted.length === 1 ? '' : 's'}</h2>
        {shown.map((issue, n) => <IssueButton key={`${issue.source}-${n}`} issue={issue} onOpen={onOpen} />)}
        {issues.length > limit && (
          <button type="button" className="text-sm font-medium text-primary hover:underline" onClick={() => setExpanded((v) => !v)}>
            {expanded ? 'Show fewer' : `Show all ${issues.length}`}
          </button>
        )}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-muted">
        <NotApplicable summary={summary} />
        <span>
          {summary.alerts ? `${summary.alerts.active} active alert${summary.alerts.active === 1 ? '' : 's'}` : 'Alerts not shown (no alert access)'}
          {lastChange ? ` · last status change ${formatAbsolute(lastChange)}` : ''}
        </span>
      </div>
    </section>
  )
})

function IssueButton({ issue, onOpen }: { issue: DeviceIssue; onOpen: (issue: DeviceIssue) => void }) {
  const Icon = ICON[issue.severity] || Info
  const first = issue.since ? Date.parse(issue.since) : NaN
  const last = issue.last_at ? Date.parse(issue.last_at) : NaN
  const repeated = (issue.count ?? 1) > 1 && Number.isFinite(first) && Number.isFinite(last) ? repeatPhrase(issue.count!, first, last) : ''
  return (
    <button type="button" onClick={() => onOpen(issue)}
      className="group inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md py-0.5 text-left text-sm hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
      <Icon className={cn('h-4 w-4 shrink-0', TONE[issue.severity])} aria-label={issue.severity} />
      <span className="min-w-0 break-words font-medium">{issue.title}{repeated ? ` ${repeated}` : ''}</span>
      {issue.detail && <span className="min-w-0 break-words text-muted">· {issue.detail}</span>}
      {issue.since && <span className="shrink-0 text-muted" title={formatAbsolute(issue.since)}>· {formatRelative(issue.since)}</span>}
      {issue.until && <span className="shrink-0 text-muted">· until {formatAbsolute(issue.until)}</span>}
      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted opacity-0 transition-opacity group-hover:opacity-100" aria-hidden />
    </button>
  )
}

function NotApplicable({ summary }: { summary: DeviceSummary }) {
  if (!summary.not_applicable.length) return null
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted">
      <CircleSlash className="h-3.5 w-3.5" aria-hidden />
      Not applicable: {summary.not_applicable.map((g) => g.name).join(', ')}
    </span>
  )
}
