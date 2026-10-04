/**
 * NCCM operations: durable backup jobs, their attempts, and notification
 * deliveries. Fleet views (JobsWorkspace, DeliveriesList without a device) and
 * the per-device "Jobs & attempts" tab (JobList, DeliveriesList with a device).
 *
 * An error is always shown as an error (never as "no jobs"), and every status
 * is text + icon, never colour alone.
 */
import { Fragment, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, ChevronDown, ChevronRight, RefreshCw, RotateCcw, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { cn, formatDuration } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { absoluteUtc, categoryLabel, ncmError, TONE_BADGE, type Tone } from './model'
import { Ago, EmptyState, Kpi, Pagination, SectionError, ToneIcon } from './ui'

// --------------------------------------------------------------------------- //
// Shared vocabulary
// --------------------------------------------------------------------------- //
const JOB_STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: 'Queued', tone: 'info' },
  running: { label: 'Running', tone: 'info' },
  success: { label: 'Succeeded', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
  interrupted: { label: 'Interrupted', tone: 'warning' },
}

const DELIVERY_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: 'Pending', tone: 'info' },
  delivered: { label: 'Delivered', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  disabled: { label: 'Disabled', tone: 'neutral' },
}

const TRIGGERS: Record<string, string> = { manual: 'Manual', schedule: 'Schedule', event: 'Event' }

/** Always name the configuration type explicitly, never "running · failed". */
const TYPE_LABEL: Record<string, string> = { running: 'Running configuration', startup: 'Startup configuration' }
const TYPE_SHORT: Record<string, string> = { running: 'Running config', startup: 'Startup config' }
const typeLabel = (t?: string | null) => (t ? TYPE_LABEL[t] || `${t} configuration` : 'Configuration')

function StatusBadge({ status, map }: { status?: string | null; map: Record<string, { label: string; tone: Tone }> }) {
  const m = (status && map[status]) || { label: status || 'Unknown', tone: 'warning' as Tone }
  return (
    <Badge variant={TONE_BADGE[m.tone]}>
      <ToneIcon tone={m.tone} className={m.tone === 'neutral' ? '' : 'text-current'} />
      {m.label}
    </Badge>
  )
}

function runtimeTone(state?: string | null): Tone {
  return state === 'active' ? 'success' : state === 'inactive' ? 'danger' : 'warning'
}
function runtimeText(state?: string | null) {
  return state === 'active' ? 'Active' : state === 'inactive' ? 'Inactive' : 'Unknown'
}

function duration(sec?: number | null) {
  if (sec == null) return '—'
  return sec < 1 ? '<1s' : formatDuration(sec)
}

function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** Multi-select filter chip row (aria-pressed). */
function ChipFilter({ label, options, value, onChange }: {
  label: string; options: [string, string][]; value: string[]; onChange: (v: string[]) => void
}) {
  const toggle = (k: string) => onChange(value.includes(k) ? value.filter((x) => x !== k) : [...value, k])
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-muted">{label}:</span>
      {options.map(([k, text]) => {
        const on = value.includes(k)
        return (
          <button key={k} type="button" aria-pressed={on} onClick={() => toggle(k)}
            className={cn('rounded-full border px-2.5 py-0.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
              on ? 'border-primary bg-primary/10 text-text' : 'border-border text-muted hover:bg-surface2')}>
            {text}
          </button>
        )
      })}
      {value.length > 0 && (
        <button type="button" onClick={() => onChange([])} className="text-xs text-primary underline-offset-2 hover:underline">Clear</button>
      )}
    </div>
  )
}

function Loading({ what }: { what: string }) {
  return <p role="status" className="px-1 py-6 text-center text-sm text-muted">Loading {what}…</p>
}

// --------------------------------------------------------------------------- //
// Attempt timeline
// --------------------------------------------------------------------------- //
function Attempt({ run, index, final }: { run: any; index?: number; final?: boolean }) {
  const failed = run.status === 'failed' || run.status === 'interrupted'
  return (
    <li className="min-w-0 rounded-md border border-border bg-surface p-3">
      <div className="flex flex-wrap items-center gap-2">
        {index != null && <span className="text-xs text-muted">Attempt {index + 1}</span>}
        <span className="text-sm font-medium">{typeLabel(run.config_type)}</span>
        <span className="text-xs text-muted">status</span>
        <StatusBadge status={run.status} map={JOB_STATUS} />
        {run.stage_label && <span className="text-xs text-muted">Stage: <span className="text-text">{run.stage_label}</span></span>}
        {run.validated && <Badge variant="success"><ToneIcon tone="success" className="text-current" />Validated</Badge>}
        {run.status === 'success' && run.is_change != null && (
          <span className="text-xs text-muted">{run.is_change ? 'Configuration changed' : 'No change'}</span>
        )}
      </div>
      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-muted">Started</dt>
          <dd className="break-words">{absoluteUtc(run.started_at)} {run.started_at && <span className="text-muted">(<Ago iso={run.started_at} />)</span>}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-muted">Finished</dt>
          <dd className="break-words">{run.finished_at ? <>{absoluteUtc(run.finished_at)} <span className="text-muted">(<Ago iso={run.finished_at} />)</span></> : <span className="text-muted">not finished</span>}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-muted">Duration</dt>
          <dd>{duration(run.duration_seconds)}{run.platform && <span className="text-muted"> · driver {run.platform}</span>}</dd>
        </div>
      </dl>
      {failed && (
        <div className="mt-2 space-y-1 text-xs">
          {run.failure_category && <div><span className="text-muted">Failure: </span><span className="font-medium text-danger">{categoryLabel(run.failure_category)}</span></div>}
          {run.message && <div className="break-words"><span className="text-muted">Message: </span>{run.message}</div>}
          {run.recommended_action && <div className="break-words"><span className="text-muted">Recommended action: </span>{run.recommended_action}</div>}
          {run.retryable != null && (
            <div className={run.retryable ? 'text-info' : 'text-warning'}>
              <ToneIcon tone={run.retryable ? 'info' : 'warning'} className="mr-1 inline text-current" />
              {run.retryable ? (final === undefined ? 'Transient failure (retryable)' : final ? 'Transient failure: retry budget used up' : 'Transient failure: retried') : 'Not retried: fix the cause first'}
            </div>
          )}
        </div>
      )}
      {run.version_id && run.status === 'success' && <div className="mt-1 text-xs text-muted">Stored version {String(run.version_id).slice(0, 8)}</div>}
    </li>
  )
}

function AttemptTimeline({ job }: { job: any }) {
  const runs: any[] = job.runs || []
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>Job {String(job.id).slice(0, 8)}</span>
        <span>Requested by {job.actor_name || 'system'}</span>
        <span>Created {absoluteUtc(job.created_at)}</span>
        {job.started_at && <span>Started {absoluteUtc(job.started_at)}</span>}
        {job.finished_at && <span>Finished {absoluteUtc(job.finished_at)}</span>}
        {job.cancel_requested && <span className="text-warning">Cancellation requested</span>}
      </div>
      {job.status === 'failed' && job.recommended_action && (
        <p className="text-xs"><span className="text-muted">Recommended action: </span>{job.recommended_action}</p>
      )}
      {runs.length
        ? <ol aria-label="Attempt timeline" className="space-y-2">{runs.map((r, i) => <Attempt key={r.id} run={r} index={i} final={i === runs.length - 1 && job.status !== 'queued' && job.status !== 'running'} />)}</ol>
        : <p className="text-xs text-muted">{job.status === 'queued' ? 'No attempt yet: the job is waiting for the backup worker.' : 'No attempts were recorded for this job.'}</p>}
    </div>
  )
}

function outcome(job: any) {
  if (job.status === 'failed') {
    return (
      <div className="min-w-0 max-w-xs">
        <div className="font-medium text-danger">{categoryLabel(job.failure_category) || 'Failed'}</div>
        {job.error_code && <div className="truncate text-xs text-muted" title={job.error_code}>{job.error_code}</div>}
      </div>
    )
  }
  if (job.status === 'success') return <span className="text-xs text-muted">Completed</span>
  if (job.status === 'cancelled') return <span className="text-xs text-muted">Cancelled</span>
  if (job.cancel_requested) return <span className="text-xs text-warning">Cancellation requested</span>
  if (job.status === 'queued') return <span className="text-xs text-muted">Waiting for worker</span>
  if (job.status === 'running') return <span className="text-xs text-muted">In progress</span>
  return <span className="text-xs text-muted">—</span>
}

function JobsTable({ jobs, showDevice, onCancel, canCancel }: {
  jobs: any[]; showDevice?: boolean; onCancel?: (job: any) => void; canCancel?: boolean
}) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (id: string) => setOpen((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const cols = 7 + (showDevice ? 1 : 0) + (onCancel ? 1 : 0)
  return (
    <div className="overflow-x-auto">
      <Table>
        <THead>
          <Tr>
            <Th className="w-10"><span className="sr-only">Expand</span></Th>
            {showDevice && <Th>Device</Th>}
            <Th>Created</Th>
            <Th>Trigger</Th>
            <Th>Types</Th>
            <Th>Status</Th>
            <Th>Attempts</Th>
            <Th>Outcome</Th>
            {onCancel && <Th><span className="sr-only">Actions</span></Th>}
          </Tr>
        </THead>
        <TBody>
          {jobs.map((j) => {
            const expanded = open.has(j.id)
            const active = j.status === 'queued' || j.status === 'running'
            return (
              <Fragment key={j.id}>
                <Tr>
                  <Td className="p-1.5">
                    <Button variant="ghost" size="icon" className="h-8 w-8" aria-expanded={expanded}
                      aria-label={`${expanded ? 'Hide' : 'Show'} attempts for job created ${absoluteUtc(j.created_at)}`}
                      onClick={() => toggle(j.id)}>
                      {expanded ? <ChevronDown /> : <ChevronRight />}
                    </Button>
                  </Td>
                  {showDevice && (
                    <Td className="max-w-[12rem]">
                      <Link to={`/ncm/${j.device_id}?tab=jobs`} className="block truncate font-medium text-primary hover:underline">{j.hostname || String(j.device_id).slice(0, 8)}</Link>
                    </Td>
                  )}
                  <Td className="whitespace-nowrap text-xs"><Ago iso={j.created_at} /></Td>
                  <Td className="text-xs">{TRIGGERS[j.trigger] || j.trigger || '—'}</Td>
                  <Td className="text-xs">{(j.config_types || []).map((t: string) => TYPE_SHORT[t] || t).join(', ') || '—'}</Td>
                  <Td><StatusBadge status={j.status} map={JOB_STATUS} /></Td>
                  <Td className="whitespace-nowrap text-xs tabular-nums">{j.attempts ?? 0} of {j.max_attempts ?? 3}</Td>
                  <Td>{outcome(j)}</Td>
                  {onCancel && (
                    <Td className="text-right">
                      {active && canCancel && (
                        <Button size="sm" variant="outline" disabled={j.cancel_requested} onClick={() => onCancel(j)}>
                          <Ban /> {j.cancel_requested ? 'Cancelling' : 'Cancel'}
                        </Button>
                      )}
                    </Td>
                  )}
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={cols} className="bg-surface2/40">
                      <AttemptTimeline job={j} />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Fleet jobs workspace
// --------------------------------------------------------------------------- //
const JOB_PAGE = 25

export function JobsWorkspace() {
  const [statuses, setStatuses] = useState<string[]>([])
  const [trigger, setTrigger] = useState('all')
  const [search, setSearch] = useState('')
  const q = useDebounced(search.trim())
  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1) }, [statuses, trigger, q])

  const params = {
    status: statuses.length ? statuses.join(',') : undefined,
    trigger: trigger === 'all' ? undefined : trigger,
    q: q || undefined,
    limit: JOB_PAGE,
    offset: (page - 1) * JOB_PAGE,
  }
  const jobs = useQuery<any>({
    queryKey: ['ncm', 'jobs', 'fleet', params],
    queryFn: async () => (await api.get('/ncm/jobs', { params })).data,
    placeholderData: keepPreviousData,
    refetchInterval: 15000,
  })
  const data = jobs.data
  const queue = data?.queue
  const runtime = data?.runtime
  const rows: any[] = data?.data || []
  const total: number = data?.total || 0
  const pageCount = Math.max(1, Math.ceil(total / JOB_PAGE))
  const filtered = statuses.length > 0 || trigger !== 'all' || !!q

  return (
    <div className="min-w-0 space-y-4">
      {jobs.isError && !data ? (
        <SectionError what="Job queue and worker state" onRetry={() => jobs.refetch()} />
      ) : (
        <section aria-label="Queue and worker state" className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
          <Kpi label="Queued" value={queue ? queue.queued : '…'} tone={queue?.queued ? 'info' : 'neutral'}
            onClick={() => setStatuses(['queued'])} active={statuses.length === 1 && statuses[0] === 'queued'} />
          <Kpi label="Running" value={queue ? queue.running : '…'} tone={queue?.running ? 'info' : 'neutral'}
            onClick={() => setStatuses(['running'])} active={statuses.length === 1 && statuses[0] === 'running'} />
          <Kpi label="Failed (24h)" value={queue ? queue.failed_24h : '…'} tone={queue?.failed_24h ? 'danger' : 'neutral'}
            onClick={() => setStatuses(['failed'])} active={statuses.length === 1 && statuses[0] === 'failed'} />
          <Kpi label="Succeeded (24h)" value={queue ? queue.success_24h : '…'} tone={queue?.success_24h ? 'success' : 'neutral'}
            onClick={() => setStatuses(['success'])} active={statuses.length === 1 && statuses[0] === 'success'} />
          <Kpi label="Oldest queued" value={queue ? (queue.oldest_queued ? <Ago iso={queue.oldest_queued} className="text-lg" /> : <span className="text-lg text-muted">none</span>) : '…'} />
          {(['worker', 'scheduler', 'delivery'] as const).map((k) => (
            <Kpi key={k} label={k === 'worker' ? 'Backup worker' : k === 'scheduler' ? 'Scheduler' : 'Delivery'}
              value={runtime ? <span className="text-lg">{runtimeText(runtime[k])}</span> : '…'}
              tone={runtime ? runtimeTone(runtime[k]) : 'neutral'}
              sub={k === 'scheduler' && runtime?.time_sync ? `Clock ${runtime.time_sync}` : undefined} />
          ))}
        </section>
      )}

      <Card className="min-w-0">
        <CardContent className="space-y-3 pt-4">
          <div className="flex flex-wrap items-center gap-3">
            <ChipFilter label="Status" value={statuses} onChange={setStatuses}
              options={[['queued', 'Queued'], ['running', 'Running'], ['failed', 'Failed'], ['success', 'Succeeded'], ['cancelled', 'Cancelled']]} />
            <div className="w-40">
              <Select value={trigger} onValueChange={setTrigger}>
                <SelectTrigger aria-label="Trigger" className="h-8 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All triggers</SelectItem>
                  {Object.entries(TRIGGERS).map(([k, v]) => <SelectItem key={k} value={k}>{v}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="relative w-full sm:w-56">
              <Search aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input aria-label="Search hostname or IP" placeholder="Hostname or IP" value={search} onChange={(e) => setSearch(e.target.value)} className="h-8 pl-8 text-xs" />
            </div>
            <Button size="sm" variant="outline" className="ml-auto" onClick={() => jobs.refetch()} disabled={jobs.isFetching}>
              <RefreshCw className={cn(jobs.isFetching && 'animate-spin')} /> Refresh
            </Button>
          </div>

          {jobs.isError && data && (
            <p role="alert" className="text-xs text-warning">Could not refresh jobs: {ncmError(jobs.error)}. Showing the last loaded page.</p>
          )}
          {jobs.isPending ? (
            <Loading what="jobs" />
          ) : jobs.isError && !data ? (
            <SectionError what="Backup jobs" onRetry={() => jobs.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState>{filtered ? 'No jobs match these filters.' : 'No backup jobs have been queued yet.'}</EmptyState>
          ) : (
            <>
              <JobsTable jobs={rows} showDevice />
              <Pagination page={page} pageCount={pageCount} onPage={setPage} total={total} pageSize={JOB_PAGE} label="jobs" />
            </>
          )}
        </CardContent>
      </Card>

      <section aria-labelledby="ncm-deliveries-heading" className="min-w-0 space-y-2">
        <h2 id="ncm-deliveries-heading" className="text-base font-semibold">Notification deliveries</h2>
        <DeliveriesList />
      </section>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Per-device jobs
// --------------------------------------------------------------------------- //
const DEVICE_JOB_PAGE = 10
const RUN_PAGE = 10
const RUN_FETCH = 200

export function JobList({ deviceId }: { deviceId: string }) {
  const can = useCan()
  const qc = useQueryClient()
  const [page, setPage] = useState(1)
  const [cancelJob, setCancelJob] = useState<any>(null)
  const [unlinkedOpen, setUnlinkedOpen] = useState(false)
  const [runPage, setRunPage] = useState(1)

  const params = { limit: DEVICE_JOB_PAGE, offset: (page - 1) * DEVICE_JOB_PAGE }
  const jobs = useQuery<any>({
    queryKey: ['ncm', 'jobs', 'device', deviceId, params],
    queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-jobs`, { params })).data,
    placeholderData: keepPreviousData,
    refetchInterval: 10000,
  })
  const runs = useQuery<any>({
    queryKey: ['ncm', 'runs', deviceId, RUN_FETCH],
    queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-runs`, { params: { limit: RUN_FETCH } })).data,
    enabled: unlinkedOpen,
  })

  const cancel = useMutation({
    mutationFn: async (job: any) => (await api.post(`/devices/${deviceId}/ncm-jobs/${job.id}/cancel`)).data,
    onSuccess: (r: any) => {
      toast.success(r?.status === 'cancelled' ? 'Job cancelled' : 'Cancellation requested', r?.message)
      setCancelJob(null)
      qc.invalidateQueries({ queryKey: ['ncm'] })
    },
    onError: (e: any) => toast.error('Could not cancel the job', ncmError(e)),
  })

  const rows: any[] = jobs.data?.data || []
  const total: number = jobs.data?.total || 0
  const unlinked = useMemo(() => (runs.data?.data || []).filter((r: any) => !r.job_id), [runs.data])
  const runPageCount = Math.max(1, Math.ceil(unlinked.length / RUN_PAGE))
  const runRows = unlinked.slice((runPage - 1) * RUN_PAGE, runPage * RUN_PAGE)

  return (
    <div className="min-w-0 space-y-3">
      {jobs.isError && jobs.data && (
        <p role="alert" className="text-xs text-warning">Could not refresh jobs: {ncmError(jobs.error)}. Showing the last loaded page.</p>
      )}
      {jobs.isPending ? (
        <Loading what="jobs" />
      ) : jobs.isError && !jobs.data ? (
        <SectionError what="Backup jobs" onRetry={() => jobs.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState>No backup jobs have been queued for this device.</EmptyState>
      ) : (
        <>
          <JobsTable jobs={rows} onCancel={setCancelJob} canCancel={can('ncm.manage')} />
          <Pagination page={page} pageCount={Math.max(1, Math.ceil(total / DEVICE_JOB_PAGE))} onPage={setPage} total={total} pageSize={DEVICE_JOB_PAGE} label="jobs" />
        </>
      )}

      <div className="min-w-0 rounded-md border border-border">
        <button type="button" aria-expanded={unlinkedOpen} aria-controls={`ncm-unlinked-${deviceId}`}
          onClick={() => setUnlinkedOpen((o) => !o)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-medium hover:bg-surface2/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50">
          {unlinkedOpen ? <ChevronDown aria-hidden className="h-4 w-4" /> : <ChevronRight aria-hidden className="h-4 w-4" />}
          Attempts not linked to a job
          <span className="text-xs font-normal text-muted">(older history and direct captures)</span>
        </button>
        {unlinkedOpen && (
          <div id={`ncm-unlinked-${deviceId}`} className="space-y-2 border-t border-border p-3">
            {runs.isPending ? (
              <Loading what="attempts" />
            ) : runs.isError ? (
              <SectionError what="Capture attempts" onRetry={() => runs.refetch()} />
            ) : unlinked.length === 0 ? (
              <EmptyState>Every recorded attempt belongs to a job listed above.</EmptyState>
            ) : (
              <>
                {(runs.data?.total || 0) > RUN_FETCH && (
                  <p className="text-xs text-muted">Showing unlinked attempts among the {RUN_FETCH} most recent of {runs.data.total} attempts.</p>
                )}
                <ol aria-label="Attempts not linked to a job" className="space-y-2">
                  {runRows.map((r: any) => <Attempt key={r.id} run={r} />)}
                </ol>
                <Pagination page={runPage} pageCount={runPageCount} onPage={setRunPage} total={unlinked.length} pageSize={RUN_PAGE} label="attempts" />
              </>
            )}
          </div>
        )}
      </div>


      <ConfirmDialog
        open={!!cancelJob}
        onOpenChange={(o) => { if (!o) setCancelJob(null) }}
        title="Cancel backup job?"
        description={cancelJob?.status === 'running'
          ? 'The job is running. Cancellation takes effect before the next configuration capture; a capture already in progress completes.'
          : 'The queued job will be cancelled and will not run.'}
        confirmText="Cancel job"
        cancelText="Keep job"
        destructive
        loading={cancel.isPending}
        onConfirm={() => cancelJob && cancel.mutate(cancelJob)}
      />
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Notification deliveries
// --------------------------------------------------------------------------- //
const DELIVERY_PAGE = 25
const DEVICE_DELIVERY_PAGE = 10

export function DeliveriesList({ deviceId }: { deviceId?: string }) {
  const can = useCan()
  const qc = useQueryClient()
  const fleet = !deviceId
  const [statuses, setStatuses] = useState<string[]>([])
  const [page, setPage] = useState(1)
  const [replay, setReplay] = useState<any>(null)
  useEffect(() => { setPage(1) }, [statuses, deviceId])

  const pageSize = fleet ? DELIVERY_PAGE : DEVICE_DELIVERY_PAGE
  const params = fleet
    ? { status: statuses.length ? statuses.join(',') : undefined, limit: DELIVERY_PAGE, offset: (page - 1) * DELIVERY_PAGE }
    : undefined
  const q = useQuery<any>({
    queryKey: fleet ? ['ncm', 'deliveries', 'fleet', params] : ['ncm', 'deliveries', 'device', deviceId],
    queryFn: async () => (await api.get(fleet ? '/ncm/deliveries' : `/devices/${deviceId}/ncm-deliveries`, { params })).data,
    placeholderData: fleet ? keepPreviousData : undefined,
    refetchInterval: 30000,
  })

  const retry = useMutation({
    mutationFn: async (d: any) => (await api.post(`/ncm/deliveries/${d.id}/retry`)).data,
    onSuccess: () => {
      toast.success('Delivery queued for replay', 'It will be sent on the next delivery run.')
      setReplay(null)
      qc.invalidateQueries({ queryKey: ['ncm', 'deliveries'] })
    },
    onError: (e: any) => toast.error('Could not replay the delivery', ncmError(e)),
  })

  const all: any[] = q.data?.data || []
  const total: number = fleet ? q.data?.total || 0 : all.length
  const rows = fleet ? all : all.slice((page - 1) * pageSize, page * pageSize)
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const canManage = can('ncm.manage')

  return (
    <Card className="min-w-0">
      <CardContent className="space-y-3 pt-4">
        {fleet && (
          <div className="flex flex-wrap items-center gap-3">
            <ChipFilter label="Status" value={statuses} onChange={setStatuses}
              options={[['pending', 'Pending'], ['delivered', 'Delivered'], ['failed', 'Failed'], ['disabled', 'Disabled']]} />
          </div>
        )}
        {q.isError && q.data && (
          <p role="alert" className="text-xs text-warning">Could not refresh deliveries: {ncmError(q.error)}. Showing the last loaded page.</p>
        )}
        {q.isPending ? (
          <Loading what="deliveries" />
        ) : q.isError && !q.data ? (
          <SectionError what="Notification deliveries" onRetry={() => q.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState>
            {fleet && statuses.length ? 'No deliveries match these filters.' : 'No configuration notifications have been generated yet.'}
          </EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <Tr>
                    <Th>Created</Th>
                    {fleet && <Th>Device</Th>}
                    <Th>Event</Th>
                    <Th>Channel</Th>
                    <Th>Status</Th>
                    <Th>Attempts</Th>
                    <Th>Delivered / next</Th>
                    <Th>Last error</Th>
                    {canManage && <Th><span className="sr-only">Actions</span></Th>}
                  </Tr>
                </THead>
                <TBody>
                  {rows.map((d) => (
                    <Tr key={d.id}>
                      <Td className="whitespace-nowrap text-xs"><Ago iso={d.created_at} /></Td>
                      {fleet && (
                        <Td className="max-w-[10rem]">
                          <Link to={`/ncm/${d.device_id}?tab=jobs`} className="block truncate text-primary hover:underline">{d.hostname || String(d.device_id).slice(0, 8)}</Link>
                        </Td>
                      )}
                      <Td className="max-w-[16rem]">
                        <div className="truncate text-sm" title={d.subject || undefined}>{d.subject || d.event_type || d.event_key}</div>
                        {d.event_type && <div className="truncate text-xs text-muted">{d.event_type}</div>}
                      </Td>
                      <Td className="max-w-[10rem]">
                        <div className="truncate text-sm" title={d.channel_name}>{d.channel_name}</div>
                        {d.channel_type && <div className="text-xs text-muted">{d.channel_type}</div>}
                      </Td>
                      <Td><StatusBadge status={d.status} map={DELIVERY_STATUS} /></Td>
                      <Td className="text-xs tabular-nums">{d.attempts ?? 0}</Td>
                      <Td className="whitespace-nowrap text-xs">
                        {d.delivered_at ? <Ago iso={d.delivered_at} prefix="delivered " />
                          : d.status === 'pending' && d.next_attempt_at ? <span>next <Ago iso={d.next_attempt_at} /></span>
                          : <span className="text-muted">—</span>}
                      </Td>
                      <Td className="max-w-[14rem]">
                        {d.last_error ? <div className="truncate text-xs text-danger" title={d.last_error}>{d.last_error}</div> : <span className="text-xs text-muted">—</span>}
                      </Td>
                      {canManage && (
                        <Td className="text-right">
                          {(d.status === 'failed' || d.status === 'disabled') && (
                            <Button size="sm" variant="outline" onClick={() => setReplay(d)} aria-label={`Replay delivery ${d.subject || d.event_key} to ${d.channel_name}`}>
                              <RotateCcw /> Replay
                            </Button>
                          )}
                        </Td>
                      )}
                    </Tr>
                  ))}
                </TBody>
              </Table>
            </div>
            <Pagination page={page} pageCount={pageCount} onPage={setPage} total={total} pageSize={pageSize} label="deliveries" />
            {!fleet && all.length >= 100 && <p className="text-xs text-muted">Showing the 100 most recent deliveries for this device.</p>}
          </>
        )}
      </CardContent>
      <ConfirmDialog
        open={!!replay}
        onOpenChange={(o) => { if (!o) setReplay(null) }}
        title="Replay this notification?"
        description={
          <>
            The {replay?.status === 'disabled' ? 'disabled' : 'failed'} delivery to <strong>{replay?.channel_name}</strong> is reset to pending
            and sent on the next delivery run. The payload keeps its original event id, so receivers that deduplicate by event id
            will not process it twice. This action is audited.
          </>
        }
        confirmText="Replay"
        loading={retry.isPending}
        onConfirm={() => replay && retry.mutate(replay)}
      />
    </Card>
  )
}
