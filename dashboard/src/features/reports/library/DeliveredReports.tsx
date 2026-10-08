/**
 * Delivered reports: recent runs from GET /reports/runs. A run's link opens
 * the stored copy (/api/v1/reports/shared/{token}); the token is withheld
 * from accounts that may not share it, which then see "Restricted".
 */
import { Link } from 'react-router-dom'
import { ExternalLink, Inbox, Lock, SearchX } from 'lucide-react'
import { formatDateTime, zoneLabel, type Branding } from '@/lib/branding'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { useReportRuns } from '../api'
import { filterRuns, periodLabel, plural, runTone, sharedRunLink } from './model'
import { EmptyState, RunStatusChip } from './parts'

export const RUNS_LIMIT = 50

export function DeliveredReports({ q, branding, canSchedule, onClearSearch }: {
  q: string
  branding: Branding
  canSchedule: boolean
  onClearSearch: () => void
}) {
  const runs = useReportRuns(RUNS_LIMIT)

  if (runs.isLoading) {
    return (
      <div className="rounded-lg border border-border bg-surface p-4 shadow-card dark:shadow-card-dark" aria-busy="true" aria-label="Loading delivered reports">
        <SkeletonTable rows={6} cols={6} />
      </div>
    )
  }
  if (runs.isError && !runs.data) {
    return <QueryFailure error={runs.error} what="delivered reports" onRetry={() => void runs.refetch()} retrying={runs.isFetching} />
  }

  const all = runs.data ?? []
  const shown = filterRuns(all, q)

  if (all.length === 0) {
    return (
      <EmptyState
        icon={<Inbox className="h-5 w-5" />}
        title="No reports delivered yet"
        action={canSchedule ? (
          <Button asChild size="sm" variant="outline"><Link to="/reports/schedules">Go to schedules</Link></Button>
        ) : undefined}
      >
        Scheduled reports appear here each time they are generated, with a link to the stored copy.
      </EmptyState>
    )
  }

  return (
    <div className="space-y-2">
      {runs.isError && (
        <QueryFailure compact error={runs.error} what="delivered reports" onRetry={() => void runs.refetch()} retrying={runs.isFetching} />
      )}
      {shown.length === 0 ? (
        <EmptyState
          icon={<SearchX className="h-5 w-5" />}
          title={`No delivered reports match “${q.trim()}”`}
          action={<Button size="sm" variant="outline" onClick={onClearSearch}>Clear search</Button>}
        >
          Search covers the report title, schedule name, period and status.
        </EmptyState>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-card dark:shadow-card-dark">
          <Table className="text-[13px]">
            <caption className="sr-only">Delivered reports, newest first</caption>
            <THead className="bg-surface2/50">
              <Tr className="hover:bg-transparent">
                <Th>Report</Th>
                <Th className="hidden md:table-cell">Schedule</Th>
                <Th>Status</Th>
                <Th className="hidden xl:table-cell">Delivered to</Th>
                <Th>Generated <span className="normal-case tracking-normal">({zoneLabel(branding.timezone)})</span></Th>
                <Th className="text-right"><span className="sr-only">Open</span></Th>
              </Tr>
            </THead>
            <TBody>
              {shown.map((r) => {
                const href = sharedRunLink(r.token)
                const to = r.delivered_to || []
                return (
                  <Tr key={r.id}>
                    <Td className="py-2.5">
                      <p className="font-medium text-text">{r.title || r.report_type}</p>
                      <p className="text-[11px] text-muted">{periodLabel(r.period) || r.period}</p>
                    </Td>
                    <Td className="hidden py-2.5 text-xs text-text2 md:table-cell">
                      {r.schedule_name || <span className="text-muted">Deleted schedule</span>}
                    </Td>
                    <Td className="py-2.5"><RunStatusChip status={r.status} tone={runTone(r.status)} /></Td>
                    <Td className="hidden py-2.5 text-xs text-text2 xl:table-cell" title={to.join(', ') || undefined}>
                      {to.length ? plural(to.length, 'recipient') : <span className="text-muted">Not sent</span>}
                    </Td>
                    <Td className="whitespace-nowrap py-2.5 text-xs tabular-nums text-text2">
                      {r.generated_at ? formatDateTime(r.generated_at, branding) : '—'}
                    </Td>
                    <Td className="py-2.5 text-right">
                      {href ? (
                        <Button asChild size="sm" variant="ghost" className="h-7 gap-1 px-2 text-xs [&_svg]:size-3.5">
                          <a href={href} target="_blank" rel="noopener noreferrer" aria-label={`Open ${r.title || 'report'} (opens in a new tab)`}>
                            Open <ExternalLink aria-hidden />
                          </a>
                        </Button>
                      ) : (
                        <span
                          className="inline-flex items-center gap-1 text-[11px] text-muted"
                          title="Only the schedule owner and report managers can open stored copies."
                        >
                          <Lock className="h-3 w-3" aria-hidden /> Restricted
                        </span>
                      )}
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        </div>
      )}
      {all.length >= RUNS_LIMIT && (
        <p className="text-[11px] text-muted">Showing the {RUNS_LIMIT} most recent runs. Each schedule's full history is on the Schedules page.</p>
      )}
    </div>
  )
}
