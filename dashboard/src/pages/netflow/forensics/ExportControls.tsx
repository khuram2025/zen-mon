import { useState } from 'react'
import { Ban, Download, FileDown, FileStack, Loader2, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/DropdownMenu'
import { toast } from '@/components/ui/Toast'
import { cn } from '@/lib/utils'
import { formatBytes, formatCountExact, formatDateTime } from '@/lib/netflow/format'
import {
  blobErrorText,
  downloadExportJob,
  downloadStreamedExport,
  errorText,
  useExportJobMutations,
  useExportJobs,
} from './api'
import { exportMode, jobIsActive, type ExportJob } from './model'

export interface ExportControlsProps {
  /** Rows on screen (client-side CSV). */
  displayedCount: number
  onExportDisplayed: () => void
  /** Complete result size from the search (null = unknown). */
  total: number | null
  exact: boolean
  allowed: boolean
  streamMax: number
  jobMax: number
  /** API parameters of the applied search, pinned to the searched window. */
  params: URLSearchParams
  jobsOpen: boolean
  onJobsOpenChange: (open: boolean) => void
}

/**
 * Export scope is always explicit (NF-10): "displayed rows" exports exactly the rows on screen;
 * "all matching rows" is the complete result, streamed when small enough and otherwise produced
 * by a background export job — both need the netflow.export permission.
 */
export function ExportControls(props: ExportControlsProps) {
  const { displayedCount, onExportDisplayed, total, exact, allowed, streamMax, jobMax, params, jobsOpen, onJobsOpenChange } = props
  const [streaming, setStreaming] = useState(false)
  const jobs = useExportJobMutations()
  const mode = exportMode({ allowed, total, exact, streamMax, jobMax })

  const exportAll = async () => {
    if (mode === 'stream') {
      setStreaming(true)
      try {
        const rows = await downloadStreamedExport(params)
        toast.success('Export downloaded', rows !== null ? `${formatCountExact(rows)} records (all matching rows).` : undefined)
      } catch (err) {
        toast.error('The export failed', await blobErrorText(err))
      } finally {
        setStreaming(false)
      }
    } else if (mode === 'job') {
      jobs.create.mutate(params, {
        onSuccess: () => {
          onJobsOpenChange(true)
          toast.success('Export job queued', 'It runs in the background; download it from Export jobs when it is ready.')
        },
        onError: (err) => toast.error('The export job was not created', errorText(err)),
      })
    }
  }

  const allLabel = (() => {
    switch (mode) {
      case 'stream':
        return `All ${formatCountExact(total ?? 0)} matching rows (CSV)`
      case 'job':
        return `All ${exact ? '' : 'at least '}${formatCountExact(total ?? 0)} matching rows (export job)`
      case 'too_large':
        return `All matching rows: over the ${formatCountExact(jobMax)}-row limit`
      case 'forbidden':
        return 'All matching rows: needs the netflow.export permission'
      case 'unknown_total':
        return 'All matching rows: total unknown'
      default:
        return 'All matching rows: nothing to export'
    }
  })()

  return (
    <div className="flex items-center gap-1.5">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="sm" className="gap-1.5" disabled={displayedCount === 0 && !total}>
            {streaming || jobs.create.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Download aria-hidden />}
            Export
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent className="w-80">
          <DropdownMenuLabel>Export as CSV</DropdownMenuLabel>
          <DropdownMenuItem disabled={displayedCount === 0} onSelect={onExportDisplayed}>
            <FileDown aria-hidden />
            <span>
              Displayed rows only ({formatCountExact(displayedCount)})
              <span className="block text-[11px] text-text2">Exactly what is loaded on this page, in the chosen columns</span>
            </span>
          </DropdownMenuItem>
          <DropdownMenuItem disabled={mode !== 'stream' && mode !== 'job'} onSelect={() => void exportAll()}>
            <FileStack aria-hidden />
            <span>
              {allLabel}
              <span className="block text-[11px] text-text2">
                {mode === 'too_large'
                  ? 'Narrow the time range or add filters'
                  : mode === 'forbidden'
                    ? 'Ask an administrator for export rights'
                    : 'Every column, the complete result for the searched period'}
              </span>
            </span>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => onJobsOpenChange(!jobsOpen)}>
            <FileStack aria-hidden />
            {jobsOpen ? 'Hide export jobs' : 'Show export jobs'}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

const STATUS_TEXT: Record<ExportJob['status'], string> = {
  queued: 'Queued',
  running: 'Running',
  done: 'Ready',
  failed: 'Failed',
  cancelled: 'Cancelled',
  expired: 'Expired',
}

const STATUS_TONE: Record<ExportJob['status'], string> = {
  queued: 'text-text2',
  running: 'text-info-fg',
  done: 'text-success-fg',
  failed: 'text-danger-fg',
  cancelled: 'text-text2',
  expired: 'text-text2',
}

/** The caller's export jobs with progress, expiry, download and cancel/remove. */
export function ExportJobsPanel({ tz, onClose }: { tz: string; onClose: () => void }) {
  const q = useExportJobs(true)
  const { remove } = useExportJobMutations()
  const [downloading, setDownloading] = useState<string | null>(null)
  const items = q.data?.items ?? []

  const download = async (job: ExportJob) => {
    setDownloading(job.id)
    try {
      await downloadExportJob(job)
    } catch (err) {
      toast.error('The download failed', await blobErrorText(err))
    } finally {
      setDownloading(null)
    }
  }

  return (
    <section aria-label="Export jobs" className="rounded-md border border-border bg-surface2/40 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-text">Export jobs</h3>
        <button type="button" onClick={onClose} aria-label="Close export jobs" className="rounded p-1 text-text2 hover:bg-surface3 hover:text-text">
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>
      {q.isPending ? (
        <p className="text-[11px] text-text2" role="status">Loading export jobs…</p>
      ) : q.isError ? (
        <p className="text-[11px] text-danger-fg" role="alert">Could not load export jobs: {errorText(q.error)}</p>
      ) : items.length === 0 ? (
        <p className="text-[11px] text-text2">
          No export jobs. Complete exports larger than {formatCountExact(q.data?.limits.stream_max_rows ?? 0)} rows run here;
          files are kept for {q.data?.limits.retention_hours ?? 24} hours.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((j) => (
            <li key={j.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-[11px]">
              <span className={cn('w-16 font-semibold', STATUS_TONE[j.status])}>{STATUS_TEXT[j.status]}</span>
              <span className="min-w-0 flex-1 text-text">
                {formatCountExact(j.row_count ?? 0)}
                {j.total !== null ? ` of ${formatCountExact(j.total)}` : ''} rows
                {j.progress_pct !== null && jobIsActive(j) ? ` · ${j.progress_pct.toFixed(0)}%` : ''}
                {j.byte_size ? ` · ${formatBytes(j.byte_size)} (gzip)` : ''}
                <span className="block text-text2">
                  {j.query.window ? `${formatDateTime(j.query.window.from, tz, { zone: 'none', seconds: false })} → ${formatDateTime(j.query.window.to, tz, { seconds: false })}` : ''}
                  {j.expires_at && j.status === 'done' ? ` · expires ${formatDateTime(j.expires_at, tz, { seconds: false })}` : ''}
                  {j.error ? ` · ${j.error}` : ''}
                </span>
              </span>
              {jobIsActive(j) && (
                <span className="h-1.5 w-24 overflow-hidden rounded-full bg-surface3" role="progressbar" aria-label="Export progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(j.progress_pct ?? 0)}>
                  <span className="block h-full bg-info" style={{ width: `${Math.max(3, j.progress_pct ?? 0)}%` }} />
                </span>
              )}
              {j.download_ready && (
                <Button type="button" size="sm" variant="outline" className="h-7 gap-1 px-2 text-[11px]" onClick={() => void download(j)} disabled={downloading === j.id}>
                  {downloading === j.id ? <Loader2 className="animate-spin" aria-hidden /> : <Download aria-hidden />}
                  Download
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-7 gap-1 px-2 text-[11px]"
                disabled={remove.isPending && remove.variables === j.id}
                onClick={() =>
                  remove.mutate(j.id, { onError: (err) => toast.error('Could not update the export job', errorText(err)) })
                }
                aria-label={jobIsActive(j) ? `Cancel export job from ${j.created_at ?? ''}` : `Remove export job from ${j.created_at ?? ''}`}
              >
                {jobIsActive(j) ? <Ban aria-hidden /> : <Trash2 aria-hidden />}
                {jobIsActive(j) ? 'Cancel' : 'Remove'}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
