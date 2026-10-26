/**
 * Report runs: the pieces shared by a schedule's run history (Schedules page)
 * and Delivered reports (Report library). A run is `running` until it ends as
 * success, partial, failed or skipped; its share link expires or can be
 * revoked; a stored attachment can be downloaded; failed targets can be
 * retried (docs/reports-delivery.md).
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ChevronDown, ChevronRight, Download, ExternalLink, History, Link2Off, Lock, MoreHorizontal, RefreshCw, RotateCw,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatDate, formatDateTime, type Branding } from '@/lib/branding'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { toast } from '@/components/ui/Toast'
import { describeFailure } from '@/lib/access'
import { downloadErrorMessage } from '../viewer/ExportMenu'
import { useReportCatalog } from '../api'
import {
  SCHEDULE_RUNS_LIMIT, downloadRunArtifact, useReportSettings, useRetryRun, useRevokeRun, useScheduleRuns,
  useScheduleStatus,
} from './api'
import {
  canRetryRun, failedTargetLabels, isVisibilityScoped, linkState, relativeTo, retryText, runHealth, runKindLabel,
  runNotes, toInstant, type ScheduleRun,
} from './model'
import { RunStatusBadge, scheduleErrorText } from './ui'

/** What the caller may do with runs: from the catalog `can` flags and the role. */
export function useRunPermissions(): { canManage: boolean; canExport: boolean } {
  const can = useCan()
  const user = useAuth((s) => s.user)
  const catalog = useReportCatalog()
  const scoped = isVisibilityScoped(user)
  return {
    canManage: can('reports.manage') && !scoped && catalog.data?.can?.manage !== false,
    canExport: can('reports.export') && catalog.data?.can?.export !== false,
  }
}

/** Highest automatic retry count: report settings from GET /report-schedules/status, else GET /reports/settings. */
export function useRetryAttempts(): number | null {
  const status = useScheduleStatus()
  const fromStatus = status.data?.settings?.retry_attempts
  const settings = useReportSettings({ enabled: status.isFetched && typeof fromStatus !== 'number' })
  const n = typeof fromStatus === 'number' ? fromStatus : settings.data?.retry_attempts
  return typeof n === 'number' ? n : null
}

export function sharedRunLink(token: string | null | undefined): string | null {
  return token ? `/api/v1/reports/shared/${encodeURIComponent(token)}` : null
}

/* ------------------------------------------------------------------ */
/*  Small pieces                                                       */
/* ------------------------------------------------------------------ */

export function RunKindBadge({ kind }: { kind: string | null | undefined }) {
  const label = runKindLabel(kind)
  if (!label || kind === 'scheduled') return null
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1 py-px text-2xs font-medium',
        kind === 'retry' ? 'border-warning/35 text-warning-fg' : 'border-border text-text2',
      )}
    >
      {kind === 'retry' && <RotateCw className="h-2.5 w-2.5" aria-hidden />}
      {label}
    </span>
  )
}

/** "Link expires 20-Nov-2026", "Link expired or revoked on 21-Oct-2026". */
export function LinkStateText({ run, branding, now }: { run: ScheduleRun; branding: Branding; now: number }) {
  const state = linkState(run, now)
  if (state === 'ended') {
    const at = toInstant(run.expires_at)
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted" title={at ? formatDateTime(at, branding, { zone: true }) : undefined}>
        <Link2Off className="h-3 w-3" aria-hidden /> Link expired or revoked{at ? ` on ${formatDate(at, branding)}` : ''}
      </span>
    )
  }
  if (state === 'active') {
    const at = toInstant(run.expires_at)!
    return <span className="text-xs text-muted" title={formatDateTime(at, branding, { zone: true })}>Link expires {formatDate(at, branding)}</span>
  }
  return null
}

/** Error, notes, failed targets and retry state; long text collapses to one line. */
export function RunDetails({ run, branding, retryAttempts }: { run: ScheduleRun; branding: Branding; retryAttempts: number | null }) {
  const [open, setOpen] = useState(false)
  const notes = runNotes(run.notes)
  const failed = failedTargetLabels(run.failed_targets)
  const retry = retryText(run, retryAttempts, branding)
  const health = runHealth(run.status)
  const lines: { text: string; tone: 'danger' | 'warning' | 'muted' }[] = []
  if (run.error) lines.push({ text: run.error, tone: health === 'failed' ? 'danger' : 'warning' })
  for (const n of notes) lines.push({ text: n, tone: health === 'failed' ? 'danger' : 'warning' })
  if (failed.length) lines.push({ text: `Not delivered to ${failed.join(', ')}`, tone: 'danger' })
  if (health === 'skipped' && !run.error && !notes.length) lines.push({ text: 'The report had no data, so nothing was sent.', tone: 'muted' })
  if (!lines.length && !retry) return null
  const long = lines.length > 1 || lines.some((l) => l.text.length > 140)
  const tone = { danger: 'text-danger-fg', warning: 'text-warning-fg', muted: 'text-muted' }
  return (
    <div className="space-y-0.5 text-xs">
      {retry && (
        <div className="inline-flex items-center gap-1 text-warning-fg">
          <RotateCw className="h-3 w-3" aria-hidden /> {retry}
        </div>
      )}
      {lines.length > 0 && (
        long ? (
          <div>
            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              className={cn(
                'flex max-w-full items-start gap-1 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
                tone[lines[0].tone],
              )}
            >
              {open ? <ChevronDown className="mt-px h-3 w-3 shrink-0" aria-hidden /> : <ChevronRight className="mt-px h-3 w-3 shrink-0" aria-hidden />}
              <span className={cn('break-words', !open && 'line-clamp-1')}>{lines[0].text}</span>
              {!open && lines.length > 1 && <span className="shrink-0 text-muted">+{lines.length - 1}</span>}
            </button>
            {open && lines.slice(1).map((l, i) => (
              <div key={i} className={cn('break-words pl-4', tone[l.tone])}>{l.text}</div>
            ))}
          </div>
        ) : (
          <div className={cn('break-words', tone[lines[0].tone])}>{lines[0].text}</div>
        )
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Actions                                                            */
/* ------------------------------------------------------------------ */

/** Open the stored copy, plus Download attachment / Revoke link / Retry failed in a menu. */
export function RunActions({ run, now, canManage, canExport }: { run: ScheduleRun; now: number; canManage: boolean; canExport: boolean }) {
  const revoke = useRevokeRun()
  const retry = useRetryRun()
  const [confirmRevoke, setConfirmRevoke] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const state = linkState(run, now)
  const href = state === 'active' || state === 'never_expires' ? sharedRunLink(run.token) : null
  // The token is only given to the schedule's owner and report managers, who may revoke it.
  const mayRevoke = !!run.token && (state === 'active' || state === 'never_expires')
  const mayDownload = !!run.has_artifact && canExport && !run.token_withheld
  const mayRetry = canManage && canRetryRun(run)
  const title = run.title || 'report'

  const download = async () => {
    setDownloading(true)
    try {
      const name = await downloadRunArtifact(run.id, run.artifact_name)
      toast.success('Download started', name)
    } catch (e) {
      toast.error('The attachment could not be downloaded', await downloadErrorMessage(e))
    } finally {
      setDownloading(false)
    }
  }
  const doRetry = () => {
    retry.mutateAsync(run.id).then(
      (r) => {
        const st = runHealth(r?.status)
        if (st === 'failed') toast.error('Retry failed', r?.errors?.join('; ') || 'No target accepted the report.')
        else if (st === 'partial') toast.warning('Some targets still failed', r?.errors?.join('; ') || undefined)
        else if (r?.remaining) toast.warning('Some targets still failed', r?.errors?.join('; ') || `${r.remaining} still to deliver.`)
        else toast.success('Report resent', r?.delivered?.length ? `Delivered to ${r.delivered.join(', ')}.` : undefined)
      },
      (e) => {
        // 409: a retry of this run is already in progress (or nothing is left to retry).
        const f = describeFailure(e)
        if (f.kind === 'conflict') toast.info('Not retried', f.message || 'A retry of this run is already in progress.')
        else toast.error('The report was not resent', scheduleErrorText(e))
      },
    )
  }

  const menu = mayDownload || mayRevoke || mayRetry
  return (
    <div className="flex shrink-0 items-center justify-end gap-1">
      {href ? (
        <Button asChild size="sm" variant="ghost" className="h-7 gap-1 px-2 text-xs [&_svg]:size-3.5">
          <a href={href} target="_blank" rel="noopener noreferrer" aria-label={`Open ${title} (opens in a new tab)`}>
            Open <ExternalLink aria-hidden />
          </a>
        </Button>
      ) : run.token_withheld && state !== 'ended' ? (
        <span
          className="inline-flex items-center gap-1 px-2 text-xs text-muted"
          title="Report links are shown to the schedule's owner and to people who manage reports"
        >
          <Lock className="h-3 w-3" aria-hidden /> Restricted
          <span className="sr-only">: report links are shown to the schedule&apos;s owner and to people who manage reports</span>
        </span>
      ) : null}
      {menu && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon" variant="ghost" className="h-7 w-7" aria-label={`More actions for ${title}`}>
              {downloading || retry.isPending ? <RefreshCw className="animate-spin" aria-hidden /> : <MoreHorizontal aria-hidden />}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            {mayDownload && (
              <DropdownMenuItem onSelect={() => void download()} disabled={downloading}>
                <Download aria-hidden /> Download attachment{run.artifact_name ? ` (${run.artifact_name})` : ''}
              </DropdownMenuItem>
            )}
            {mayRetry && (
              <DropdownMenuItem onSelect={doRetry} disabled={retry.isPending}>
                <RotateCw aria-hidden /> Retry failed targets now
              </DropdownMenuItem>
            )}
            {mayRevoke && (
              <>
                {(mayDownload || mayRetry) && <DropdownMenuSeparator />}
                <DropdownMenuItem destructive onSelect={() => setConfirmRevoke(true)}>
                  <Link2Off aria-hidden /> Revoke link
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      <ConfirmDialog
        open={confirmRevoke}
        onOpenChange={setConfirmRevoke}
        title="Revoke report link"
        description="The link in the emails for this run stops working now, for everyone. The run stays in the history. This cannot be undone."
        confirmText="Revoke link"
        destructive
        loading={revoke.isPending}
        onConfirm={() => {
          revoke.mutateAsync(run.id).then(
            () => { toast.success('Link revoked', title); setConfirmRevoke(false) },
            (e) => toast.error('The link was not revoked', scheduleErrorText(e)),
          )
        }}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  A schedule's run history                                           */
/* ------------------------------------------------------------------ */

export function RunHistory({ scheduleId, branding, now }: { scheduleId: string; branding: Branding; now: number }) {
  const q = useScheduleRuns(scheduleId)
  const { canManage, canExport } = useRunPermissions()
  const retryAttempts = useRetryAttempts()
  if (q.isLoading) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label="Loading run history">
        {[0, 1, 2].map((i) => <Skeleton key={i} className="h-11 w-full" />)}
      </div>
    )
  }
  if (q.isError && !q.data) {
    return <QueryFailure compact error={q.error} what="the run history" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  }
  const runs = q.data?.runs ?? []
  if (!runs.length) {
    return <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted">No runs yet. Runs appear here after the first scheduled or manual run.</p>
  }
  const total = q.data?.total ?? null
  const more = total != null ? total > runs.length : runs.length >= SCHEDULE_RUNS_LIMIT
  return (
    <div className="space-y-2">
      <ol className="divide-y divide-border overflow-hidden rounded-md border border-border bg-surface" aria-label="Recent runs">
        {runs.map((run) => {
          const at = run.generated_at
          const to = run.delivered_to ?? []
          const count = run.delivered_count ?? to.length
          return (
            <li key={run.id} className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <RunStatusBadge status={run.status} />
                  <RunKindBadge kind={run.kind} />
                  {at ? (
                    <span className="text-sm tabular-nums text-text">
                      {formatDateTime(at, branding, { zone: true })}
                      <span className="ml-1.5 text-xs text-muted">{relativeTo(at, now)}</span>
                    </span>
                  ) : (
                    <span className="text-sm text-muted">Time not recorded</span>
                  )}
                </div>
                {to.length > 0 ? (
                  <div className="text-xs text-text2">
                    <span className="text-muted">Delivered to </span>
                    {to.join(', ')}
                  </div>
                ) : count > 0 ? (
                  <div className="text-xs text-text2">Delivered to {count} recipient{count === 1 ? '' : 's'}</div>
                ) : runHealth(run.status) !== 'running' && runHealth(run.status) !== 'skipped' ? (
                  <div className="text-xs text-muted">Not delivered to anyone</div>
                ) : null}
                <RunDetails run={run} branding={branding} retryAttempts={retryAttempts} />
                <LinkStateText run={run} branding={branding} now={now} />
              </div>
              <RunActions run={run} now={now} canManage={canManage} canExport={canExport} />
            </li>
          )
        })}
      </ol>
      <p className="text-xs text-muted">
        {more && <>Showing the {runs.length} most recent runs.{' '}</>}
        <Link
          to={`/reports?view=delivered&schedule=${encodeURIComponent(scheduleId)}`}
          className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
        >
          <History className="h-3 w-3" aria-hidden /> View all{total != null ? ` ${total}` : ''} in Delivered reports
        </Link>
      </p>
    </div>
  )
}
