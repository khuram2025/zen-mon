/**
 * Delivered reports: every run from GET /reports/runs, newest first, 50 at a
 * time with "Load more", filtered by status and schedule (?schedule= and
 * ?status= in the URL, so a schedule's run history can link here). A run's
 * link opens the stored copy (/api/v1/reports/shared/{token}) until it
 * expires or is revoked; the token is withheld from accounts that may not
 * share it, which then see "Restricted". The stored attachment can be
 * downloaded, and failed deliveries retried.
 */
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Inbox, Loader2, SearchX, X } from 'lucide-react'
import { formatDateTime, zoneLabel, type Branding } from '@/lib/branding'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { useReportSchedules, useRunsPages } from '../schedules/api'
import { RUN_STATUSES, periodLabel, searchRuns } from '../schedules/model'
import { LinkStateText, RunActions, RunDetails, RunKindBadge, useRetryAttempts, useRunPermissions } from '../schedules/runs'
import { RunStatusBadge, useNow } from '../schedules/ui'
import { plural } from './model'
import { EmptyState } from './parts'

export const RUNS_LIMIT = 50

const ANY = '__any'

export function DeliveredReports({ q, branding, canSchedule, onClearSearch }: {
  q: string
  branding: Branding
  canSchedule: boolean
  onClearSearch: () => void
}) {
  const [params, setParams] = useSearchParams()
  const [status, setStatusState] = useState<string>(() => params.get('status') || '')
  const [scheduleId, setScheduleState] = useState<string>(() => params.get('schedule') || '')
  const runs = useRunsPages({ status: status || null, scheduleId: scheduleId || null })
  const schedules = useReportSchedules()
  const { canManage, canExport } = useRunPermissions()
  const retryAttempts = useRetryAttempts()
  const now = useNow(30_000)

  // Keep the filters in the URL next to the library's own parameters.
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }
  const setStatus = (v: string) => { setStatusState(v); setParam('status', v) }
  const setSchedule = (v: string) => { setScheduleState(v); setParam('schedule', v) }
  // The library rewrites the URL from its own filters (search, view): put ours back.
  useEffect(() => {
    if ((params.get('status') || '') === status && (params.get('schedule') || '') === scheduleId) return
    const next = new URLSearchParams(params)
    if (status) next.set('status', status)
    else next.delete('status')
    if (scheduleId) next.set('schedule', scheduleId)
    else next.delete('schedule')
    setParams(next, { replace: true })
  }, [params, status, scheduleId, setParams])

  // Offset paging: runs that arrive between pages shift later pages, so a run
  // can come back twice. Keep the first copy.
  const all = useMemo(() => {
    const seen = new Set<string>()
    return (runs.data?.pages.flatMap((p) => p.data) ?? []).filter((r) => !seen.has(r.id) && !!seen.add(r.id))
  }, [runs.data])
  const total = runs.data?.pages[0]?.total ?? 0
  const shown = searchRuns(all, q)
  const filtered = !!status || !!scheduleId
  const scheduleOptions = useMemo(() => {
    const list = [...(schedules.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)).map((s) => ({ id: s.id, name: s.name }))
    if (scheduleId && !list.some((s) => s.id === scheduleId)) {
      const name = all.find((r) => r.schedule_id === scheduleId)?.schedule_name
      list.unshift({ id: scheduleId, name: name || 'Deleted schedule' })
    }
    return list
  }, [schedules.data, scheduleId, all])

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

  if (all.length === 0 && !filtered) {
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
      <div className="flex flex-wrap items-center gap-2">
        <Select value={status || ANY} onValueChange={(v) => setStatus(v === ANY ? '' : v)}>
          <SelectTrigger className="h-8 w-40 text-xs" aria-label="Filter by status"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Any status</SelectItem>
            {RUN_STATUSES.map((s) => <SelectItem key={s.id} value={s.id}>{s.label}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={scheduleId || ANY} onValueChange={(v) => setSchedule(v === ANY ? '' : v)}>
          <SelectTrigger className="h-8 w-full text-xs sm:w-64" aria-label="Filter by schedule"><SelectValue /></SelectTrigger>
          <SelectContent className="max-h-72">
            <SelectItem value={ANY}>All schedules</SelectItem>
            {scheduleOptions.map((s) => <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>)}
          </SelectContent>
        </Select>
        {filtered && (
          <Button size="sm" variant="ghost" className="h-8 text-xs" onClick={() => { setStatus(''); setSchedule('') }}>
            <X className="h-3.5 w-3.5" aria-hidden /> Clear filters
          </Button>
        )}
        <span className="ml-auto text-[11px] text-muted" aria-live="polite">
          {q.trim() ? `${shown.length} of ${all.length} loaded` : `${all.length} of ${total}`} run{total === 1 ? '' : 's'}
        </span>
      </div>

      {runs.isError && (
        <QueryFailure compact error={runs.error} what="delivered reports" onRetry={() => void runs.refetch()} retrying={runs.isFetching} />
      )}
      {shown.length === 0 ? (
        q.trim() ? (
          <EmptyState
            icon={<SearchX className="h-5 w-5" />}
            title={`No delivered reports match “${q.trim()}”`}
            action={<Button size="sm" variant="outline" onClick={onClearSearch}>Clear search</Button>}
          >
            Search covers the report title, schedule name, period and status of the runs loaded so far.
          </EmptyState>
        ) : (
          <EmptyState
            icon={<SearchX className="h-5 w-5" />}
            title="No runs match these filters"
            action={<Button size="sm" variant="outline" onClick={() => { setStatus(''); setSchedule('') }}>Clear filters</Button>}
          />
        )
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
                <Th className="text-right"><span className="sr-only">Actions</span></Th>
              </Tr>
            </THead>
            <TBody>
              {shown.map((r) => {
                const to = r.delivered_to || []
                const count = r.delivered_count ?? to.length
                return (
                  <Tr key={r.id} className="align-top">
                    <Td className="max-w-[360px] py-2.5">
                      <p className="font-medium text-text">{r.title || r.report_type}</p>
                      <p className="text-[11px] text-muted">{periodLabel(r.period)}</p>
                      <div className="mt-1 space-y-0.5">
                        <RunDetails run={r} branding={branding} retryAttempts={retryAttempts} />
                      </div>
                    </Td>
                    <Td className="hidden py-2.5 text-xs text-text2 md:table-cell">
                      {r.schedule_name || (r.schedule_id ? <span className="text-muted">Deleted schedule</span> : <span className="text-muted">One-off</span>)}
                    </Td>
                    <Td className="py-2.5">
                      <div className="flex flex-wrap items-center gap-1">
                        <RunStatusBadge status={r.status} />
                        <RunKindBadge kind={r.kind} />
                      </div>
                    </Td>
                    <Td className="hidden py-2.5 text-xs text-text2 xl:table-cell" title={to.join(', ') || undefined}>
                      {count ? plural(count, 'recipient') : <span className="text-muted">Not sent</span>}
                    </Td>
                    <Td className="whitespace-nowrap py-2.5 text-xs tabular-nums text-text2">
                      {r.generated_at ? formatDateTime(r.generated_at, branding) : '—'}
                    </Td>
                    <Td className="py-2.5 text-right">
                      <RunActions run={r} now={now} canManage={canManage} canExport={canExport} />
                      <div className="mt-0.5"><LinkStateText run={r} branding={branding} now={now} /></div>
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        </div>
      )}
      {runs.hasNextPage && (
        <div className="flex justify-center">
          <Button size="sm" variant="outline" onClick={() => void runs.fetchNextPage()} disabled={runs.isFetchingNextPage}>
            {runs.isFetchingNextPage && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />}
            Load {Math.max(1, Math.min(RUNS_LIMIT, total - all.length))} more
          </Button>
        </div>
      )}
    </div>
  )
}
