import { FormEvent, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileArchive,
  Loader2,
  LifeBuoy,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { FormField } from '@/components/ui/FormField'
import { Textarea } from '@/components/ui/Textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'

// ─── Types ──────────────────────────────────────────────────────────────────

interface BundleStatus {
  id: string
  status: 'queued' | 'running' | 'ready' | 'failed' | 'expired'
  phase: string
  created_at: string | null
  completed_at: string | null
  size_bytes: number
  sha256: string | null
  filename: string | null
  requested_by: string | null
  error: string
  skipped_files?: string[]
  truncated_files?: string[]
  bundle_schema_version?: number | null
  worker_version?: string | null
  collector_failures?: string[]
  collector_warnings?: string[]
  request?: {
    issue_category?: string
    issue_summary?: string
    time_range?: string
    include_extended_logs?: boolean
  }
}

interface CreatePayload {
  issue_category: string
  issue_summary: string
  time_range: string
  include_extended_logs: boolean
}

const WORKING_STATUSES = new Set<BundleStatus['status']>(['queued', 'running'])

const ISSUE_CATEGORIES: { value: string; label: string }[] = [
  { value: 'all', label: 'All / not sure' },
  { value: 'update_migration', label: 'Update / migration' },
  { value: 'server_monitoring', label: 'Server monitoring / agent data' },
  { value: 'apm', label: 'Application monitoring / APM' },
  { value: 'agent_upgrade', label: 'Agent install / upgrade' },
  { value: 'appliance_reachability', label: 'Appliance / reachability' },
  { value: 'device_management', label: 'Device management' },
  { value: 'snmp_discovery', label: 'SNMP / discovery' },
  { value: 'windows_credentials', label: 'Windows credentials' },
  { value: 'alerts_notifications', label: 'Alerts / notifications' },
  { value: 'performance_storage', label: 'Performance / storage' },
  { value: 'ui_api_error', label: 'UI / API error' },
  { value: 'other', label: 'Other' },
]

const TIME_RANGES: { value: string; label: string }[] = [
  { value: '1h', label: 'Last 1 hour' },
  { value: '6h', label: 'Last 6 hours' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
]

const DEFAULT_FORM: CreatePayload = {
  issue_category: 'all',
  issue_summary: '',
  time_range: '24h',
  include_extended_logs: false,
}

// Status labels for the in-progress button.
const PHASE_LABELS: Record<string, string> = {
  queued: 'Queued…',
  running: 'Collecting…',
  inventory: 'Collecting inventory…',
  health: 'Running health checks…',
  logs: 'Collecting logs…',
  database: 'Querying PostgreSQL…',
  clickhouse: 'Querying ClickHouse…',
  config_files: 'Redacting config…',
  network: 'Capturing network state…',
  storage: 'Reading storage stats…',
  updates: 'Reading update history…',
  features: 'Probing feature tables…',
  package: 'Packaging archive…',
  done: 'Done',
  failed: 'Failed',
}

const STATUS_LABELS: Record<BundleStatus['status'], string> = {
  queued: 'Queued',
  running: 'Collecting',
  ready: 'Ready',
  failed: 'Failed',
  expired: 'Expired',
}

// ─── Component ──────────────────────────────────────────────────────────────

export function SupportTabContent() {
  const qc = useQueryClient()
  const [form, setForm] = useState<CreatePayload>(DEFAULT_FORM)
  const [activeId, setActiveId] = useState<string | null>(null)

  const list = useQuery<BundleStatus[]>({
    queryKey: ['support', 'bundles'],
    queryFn: async () => (await api.get('/support/bundles')).data,
    refetchInterval: (query) => {
      const bundles = query.state.data
      return bundles?.some((bundle) => WORKING_STATUSES.has(bundle.status)) ? 3000 : false
    },
  })

  // Status poll for the in-flight job — drives the button label and triggers
  // the auto-download once the worker reports ``ready``.
  const active = useQuery<BundleStatus | null>({
    queryKey: ['support', 'bundle', activeId],
    queryFn: async () => activeId ? (await api.get(`/support/bundles/${activeId}`)).data : null,
    enabled: !!activeId,
    refetchInterval: (data) => {
      // ``data`` here is the Query object in newer versions; both shapes work.
      const state = (data as any)?.state?.data ?? (data as any)
      const status = state?.status as string | undefined
      if (!status) return 2000
      return status === 'queued' || status === 'running' ? 2000 : false
    },
  })

  const create = useMutation({
    mutationFn: async (payload: CreatePayload) => {
      const res = await api.post('/support/bundles', payload)
      return res.data as BundleStatus
    },
    onSuccess: (data) => {
      setActiveId(data.id)
      toast.success('Support bundle generation started')
      qc.invalidateQueries({ queryKey: ['support', 'bundles'] })
    },
    onError: (e) => toast.error('Could not start bundle', apiErrorMessage(e)),
  })

  const remove = useMutation({
    mutationFn: async (id: string) => api.delete(`/support/bundles/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['support', 'bundles'] })
      toast.success('Bundle deleted')
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  // Auto-download when the active job goes ``ready``. Triggers once per job.
  useEffect(() => {
    if (!active.data) return
    if (active.data.status !== 'ready') return
    if (hasPartialDiagnostics(active.data)) {
      toast.info('Support bundle ready with partial diagnostics', 'Review the collection issues in Recent bundles before sharing it.')
    }
    downloadBundle(active.data.id, active.data.filename, active.data.sha256, active.data.size_bytes).then(() => {
      qc.invalidateQueries({ queryKey: ['support', 'bundles'] })
    })
    setActiveId(null)
  }, [active.data?.status, active.data?.id])

  // Keep history accurate when a foreground job reaches a terminal state.
  useEffect(() => {
    const status = active.data?.status
    if (status === 'ready' || status === 'failed' || status === 'expired') {
      void qc.invalidateQueries({ queryKey: ['support', 'bundles'] })
    }
  }, [active.data?.status, qc])

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    create.mutate(form)
  }

  const isBusy =
    create.isPending ||
    (active.data?.status === 'queued' || active.data?.status === 'running')

  const phaseLabel = active.data?.phase ? (PHASE_LABELS[active.data.phase] ?? `Phase: ${active.data.phase}`) : null

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <LifeBuoy className="h-4 w-4 text-primary" />
            Tech Support Bundle
          </CardTitle>
          <p className="text-xs text-muted">
            Generate a diagnostic archive for troubleshooting this appliance. The file stays on the
            appliance and is transferred only when an administrator downloads it; ZenPlus does not
            upload it automatically.
          </p>
        </CardHeader>
        <CardContent>
          <div className="mb-4 grid gap-3 lg:grid-cols-2">
            <div className="rounded-md border border-border bg-surface2/40 p-3">
              <p className="text-xs font-medium text-text">What the bundle covers</p>
              <p className="mt-1 text-[11px] leading-relaxed text-muted">
                Appliance and service health, recent logs, redacted configuration, PostgreSQL and
                ClickHouse diagnostics, storage, network and reachability state, updates and
                migrations, plus module-level feature inventories.
              </p>
            </div>
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
              <p className="flex items-center gap-1.5 text-xs font-medium text-amber-300">
                <AlertTriangle className="h-3.5 w-3.5" /> Review before sharing
              </p>
              <p className="mt-1 text-[11px] leading-relaxed text-muted">
                Credentials and known secrets are redacted, but diagnostic logs can still contain
                operational details such as hostnames, IP addresses, usernames, topology, and
                application error context. Share the archive only through an approved secure channel.
              </p>
            </div>
          </div>

          <form onSubmit={onSubmit} className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Issue category">
              <Select
                value={form.issue_category}
                onValueChange={(v) => setForm({ ...form, issue_category: v })}
              >
                <SelectTrigger aria-label="Issue category"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {ISSUE_CATEGORIES.map((c) => (
                    <SelectItem key={c.value} value={c.value}>{c.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>

            <FormField label="Time range">
              <Select
                value={form.time_range}
                onValueChange={(v) => setForm({ ...form, time_range: v })}
              >
                <SelectTrigger aria-label="Diagnostic time range"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TIME_RANGES.map((r) => (
                    <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>

            <FormField
              label="Short summary (optional)"
              hint={`${form.issue_summary.length}/500 characters. Included in the bundle manifest.`}
              className="md:col-span-2"
            >
              <Textarea
                value={form.issue_summary}
                onChange={(e) => setForm({ ...form, issue_summary: e.target.value.slice(0, 500) })}
                placeholder="What failed, when did it happen, and which device, server, agent, or application was affected?"
                rows={2}
                maxLength={500}
                aria-label="Short issue summary"
              />
            </FormField>

            <div className="md:col-span-2 flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
              <div>
                <p className="text-sm font-medium">Include extended logs</p>
                <p className="text-[11px] text-muted">
                  Bundles are capped at 50 MB. Turn this on to allocate more of that space to
                  longer journal and application-log tails for the selected timeframe.
                </p>
              </div>
              <Switch
                checked={form.include_extended_logs}
                onCheckedChange={(v) => setForm({ ...form, include_extended_logs: !!v })}
                aria-label="Include extended logs"
              />
            </div>

            <div className="md:col-span-2 flex flex-col items-start justify-between gap-3 border-t border-border pt-3 sm:flex-row sm:items-center">
              <p className="text-[11px] text-muted">
                Bundle file is also retained under <code>/opt/zenplus/support/bundles</code> for 7 days (max 5).
              </p>
              <Button type="submit" disabled={isBusy}>
                {isBusy ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {phaseLabel ?? 'Working…'}
                  </>
                ) : (
                  <>
                    <FileArchive className="h-4 w-4" />
                    Generate support file
                  </>
                )}
              </Button>
            </div>

            {(isBusy || active.isError) && (
              <div
                className="md:col-span-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2"
                role="status"
                aria-live="polite"
              >
                {active.isError ? (
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex items-start gap-2">
                      <AlertTriangle className="mt-0.5 h-4 w-4 text-amber-400" />
                      <div className="text-xs">
                        <p className="font-medium text-text">Could not read bundle progress</p>
                        <p className="mt-0.5 text-muted">{apiErrorMessage(active.error)}</p>
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <Button type="button" size="sm" variant="outline" onClick={() => void active.refetch()}>
                        <RefreshCw className="h-3.5 w-3.5" /> Retry status
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setActiveId(null)}>
                        Dismiss
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-xs">
                    <Loader2 className="h-4 w-4 animate-spin text-primary" />
                    <div>
                      <p className="font-medium text-text">{phaseLabel ?? 'Preparing support bundle…'}</p>
                      <p className="mt-0.5 text-muted">
                        The job continues if you leave this page. The Recent bundles list resumes updating when you return.
                      </p>
                    </div>
                  </div>
                )}
              </div>
            )}

            {create.isError && (
              <div className="md:col-span-2 flex items-start gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 text-rose-400" />
                <div className="text-xs">
                  <p className="font-medium text-rose-300">Could not start bundle generation</p>
                  <p className="mt-1 break-words text-rose-300/80">{apiErrorMessage(create.error)}</p>
                </div>
              </div>
            )}

            {active.data?.status === 'failed' && (
              <div className="md:col-span-2 flex items-start gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 text-rose-400" />
                <div className="text-xs">
                  <p className="font-medium text-rose-300">Bundle generation failed</p>
                  <p className="mt-1 text-rose-300/80 whitespace-pre-wrap break-words">
                    {active.data.error || 'Unknown error'}
                  </p>
                </div>
              </div>
            )}
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="flex items-center gap-2 text-base">
            Recent bundles
            {list.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
          </CardTitle>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => list.refetch()}
            disabled={list.isFetching}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </Button>
        </CardHeader>
        <CardContent>
          {list.isError && !list.data ? (
            <div className="flex flex-col items-start gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 p-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 text-rose-400" />
                <div className="text-xs">
                  <p className="font-medium text-rose-300">Could not load bundle history</p>
                  <p className="mt-0.5 break-words text-rose-300/80">{apiErrorMessage(list.error)}</p>
                </div>
              </div>
              <Button type="button" size="sm" variant="outline" onClick={() => void list.refetch()}>
                <RefreshCw className="h-3.5 w-3.5" /> Retry
              </Button>
            </div>
          ) : list.isPending ? (
            <div className="flex items-center gap-2 py-3 text-sm text-muted" role="status">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading bundle history…
            </div>
          ) : list.data && list.data.length > 0 ? (
            <div>
              {list.isError && (
                <div className="mb-2 flex items-center justify-between gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11px] text-muted">
                  <span>Showing the last known history because the latest refresh failed.</span>
                  <Button type="button" size="sm" variant="ghost" onClick={() => void list.refetch()}>
                    Retry
                  </Button>
                </div>
              )}
              <div className="divide-y divide-border">
                {list.data.map((b) => (
                  <BundleRow
                    key={b.id}
                    bundle={b}
                    onDownload={() => downloadBundle(b.id, b.filename, b.sha256, b.size_bytes)}
                    onDelete={() => remove.mutate(b.id)}
                    deleting={remove.isPending && remove.variables === b.id}
                  />
                ))}
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted">No bundles yet. Click "Generate support file" above.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ─── Bundle row ─────────────────────────────────────────────────────────────

function BundleRow({
  bundle,
  onDownload,
  onDelete,
  deleting,
}: {
  bundle: BundleStatus
  onDownload: () => Promise<void> | void
  onDelete: () => void
  deleting: boolean
}) {
  const ready = bundle.status === 'ready'
  const working = WORKING_STATUSES.has(bundle.status)
  const partial = hasPartialDiagnostics(bundle)
  const [downloading, setDownloading] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  async function handleDownload() {
    setDownloading(true)
    try {
      await onDownload()
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 py-3 text-sm lg:flex-row lg:items-start lg:justify-between">
      <div className="min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          {ready ? (
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
          ) : bundle.status === 'failed' ? (
            <AlertTriangle className="h-3.5 w-3.5 text-rose-400" />
          ) : working ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-400" />
          ) : (
            <FileArchive className="h-3.5 w-3.5 text-muted" />
          )}
          <span className="max-w-full truncate font-mono text-xs" title={bundle.filename ?? bundle.id}>
            {bundle.filename ?? bundle.id}
          </span>
          <BundleStatusBadge status={bundle.status} partial={partial} />
        </div>
        <dl className="grid grid-cols-1 gap-x-5 gap-y-0.5 text-[11px] text-muted sm:grid-cols-2 xl:grid-cols-3">
          <Metadata label="Phase" value={formatPhase(bundle.phase)} />
          <Metadata label="Job ID" value={bundle.id} />
          <Metadata label="Created" value={formatTime(bundle.created_at)} />
          <Metadata label="Completed" value={formatTime(bundle.completed_at)} />
          <Metadata label="Size" value={formatBytes(bundle.size_bytes)} />
          <Metadata label="Category" value={formatCategory(bundle.request?.issue_category)} />
          <Metadata label="Log range" value={formatRange(bundle.request?.time_range)} />
          <Metadata label="Extended logs" value={bundle.request?.include_extended_logs ? 'Included' : 'Not included'} />
          <Metadata label="Requested by" value={bundle.requested_by || '—'} />
          <Metadata label="Bundle schema" value={bundle.bundle_schema_version?.toString() || '—'} />
          <Metadata label="Collector" value={bundle.worker_version || '—'} />
        </dl>
        {bundle.request?.issue_summary && (
          <p className="max-w-4xl break-words text-[11px] text-muted">
            <span className="font-medium text-text">Summary:</span> {bundle.request.issue_summary}
          </p>
        )}
        {bundle.sha256 && (
          <p className="break-all font-mono text-[10px] text-muted" title="SHA-256 checksum">
            SHA-256 {bundle.sha256}
          </p>
        )}
        {bundle.status === 'failed' && (
          <p className="max-w-4xl whitespace-pre-wrap break-words text-[11px] text-rose-300">
            {bundle.error || 'The collector did not provide an error message.'}
          </p>
        )}
        {bundle.status === 'expired' && (
          <p className="text-[11px] text-muted">The retained archive is no longer available. Generate a new bundle if needed.</p>
        )}
        <BundleDiagnosticDetails bundle={bundle} />
      </div>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!ready || downloading}
          onClick={() => { void handleDownload() }}
        >
          {downloading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
          {downloading ? 'Downloading…' : 'Download'}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={deleting || working}
          onClick={() => setConfirmDelete(true)}
          aria-label="Delete bundle"
          title={working ? 'A bundle cannot be deleted while collection is in progress' : 'Delete bundle'}
        >
          {deleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
        </Button>
      </div>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete support bundle?"
        description={`This permanently deletes ${bundle.filename || 'this support bundle'} from the appliance. Download it first if it is still needed.`}
        confirmText="Delete bundle"
        destructive
        loading={deleting}
        onConfirm={() => {
          onDelete()
          setConfirmDelete(false)
        }}
      />
    </div>
  )
}

function BundleStatusBadge({ status, partial }: { status: BundleStatus['status']; partial: boolean }) {
  const variant = status === 'ready' && !partial
    ? 'success'
    : status === 'failed'
      ? 'danger'
      : status === 'queued' || status === 'running' || partial
        ? 'warning'
        : 'outline'
  return <Badge variant={variant}>{status === 'ready' && partial ? 'Ready · partial' : STATUS_LABELS[status]}</Badge>
}

function BundleDiagnosticDetails({ bundle }: { bundle: BundleStatus }) {
  const groups = [
    { label: 'Collector failures', values: bundle.collector_failures ?? [], danger: true },
    { label: 'Skipped files', values: bundle.skipped_files ?? [], danger: true },
    { label: 'Truncated files', values: bundle.truncated_files ?? [], danger: true },
    { label: 'Collector warnings', values: bundle.collector_warnings ?? [], danger: false },
  ].filter((group) => group.values.length > 0)

  if (groups.length === 0) return null
  const partialCount = groups
    .filter((group) => group.danger)
    .reduce((count, group) => count + group.values.length, 0)
  const warningCount = groups
    .filter((group) => !group.danger)
    .reduce((count, group) => count + group.values.length, 0)

  return (
    <details className={`max-w-4xl rounded-md border px-3 py-2 text-[11px] ${partialCount > 0 ? 'border-amber-500/30 bg-amber-500/5' : 'border-border bg-surface2/40'}`}>
      <summary className="cursor-pointer font-medium text-text">
        {partialCount > 0
          ? `Partial diagnostics: ${partialCount} collection issue${partialCount === 1 ? '' : 's'}`
          : `${warningCount} collector warning${warningCount === 1 ? '' : 's'}`}
      </summary>
      <div className="mt-2 space-y-2 text-muted">
        {groups.map((group) => (
          <div key={group.label}>
            <p className={group.danger ? 'font-medium text-amber-300' : 'font-medium text-text'}>
              {group.label} ({group.values.length})
            </p>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
              {group.values.slice(0, 20).map((value, index) => (
                <li key={`${group.label}-${index}`} className="break-all font-mono">{value}</li>
              ))}
              {group.values.length > 20 && (
                <li>{group.values.length - 20} more entries are recorded in the bundle manifest.</li>
              )}
            </ul>
          </div>
        ))}
      </div>
    </details>
  )
}

function Metadata({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 gap-1">
      <dt className="shrink-0 font-medium text-text">{label}:</dt>
      <dd className="min-w-0 truncate" title={value}>{value}</dd>
    </div>
  )
}

function hasPartialDiagnostics(bundle: BundleStatus): boolean {
  return Boolean(
    bundle.collector_failures?.length ||
    bundle.skipped_files?.length ||
    bundle.truncated_files?.length,
  )
}

// ─── Helpers ────────────────────────────────────────────────────────────────

async function downloadBundle(
  id: string,
  filename: string | null,
  expectedSha256: string | null,
  expectedSize: number,
): Promise<void> {
  try {
    const res = await api.get(`/support/bundles/${id}/download`, { responseType: 'blob', timeout: 120_000 })
    const blob = new Blob([res.data], { type: 'application/gzip' })
    let checksumVerified = false
    if (expectedSize > 0 && blob.size !== expectedSize) {
      toast.error(
        'Download integrity check failed',
        `Expected ${formatBytes(expectedSize)}, but received ${formatBytes(blob.size)}. Generate a new bundle.`,
      )
      return
    }
    if (expectedSha256 && /^[0-9a-f]{64}$/i.test(expectedSha256) && globalThis.crypto?.subtle) {
      const actualSha256 = await sha256Hex(blob)
      if (actualSha256.toLowerCase() !== expectedSha256.toLowerCase()) {
        toast.error('Download integrity check failed', 'The SHA-256 checksum does not match. Generate a new bundle.')
        return
      }
      checksumVerified = true
    }
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename || `zenplus-support-${id}.tar.gz`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 5000)
    const downloadedName = filename || `zenplus-support-${id}.tar.gz`
    toast.success('Support bundle downloaded', checksumVerified ? `${downloadedName} · SHA-256 verified` : downloadedName)
  } catch (e) {
    toast.error('Download failed', apiErrorMessage(e))
  }
}

async function sha256Hex(blob: Blob): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function formatBytes(n: number): string {
  if (!n) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function formatTime(ts: string | null): string {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function formatPhase(phase: string): string {
  if (!phase) return '—'
  return PHASE_LABELS[phase]?.replace(/…$/, '') ?? titleCase(phase)
}

function formatCategory(category?: string): string {
  if (!category) return '—'
  return ISSUE_CATEGORIES.find((item) => item.value === category)?.label ?? titleCase(category)
}

function formatRange(range?: string): string {
  if (!range) return '—'
  return TIME_RANGES.find((item) => item.value === range)?.label ?? range
}

function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (character) => character.toUpperCase())
}
