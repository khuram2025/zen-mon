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
import { Button } from '@/components/ui/Button'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
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

const ISSUE_CATEGORIES: { value: string; label: string }[] = [
  { value: 'all', label: 'All / not sure' },
  { value: 'update_migration', label: 'Update / migration' },
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

// ─── Component ──────────────────────────────────────────────────────────────

export function SupportTabContent() {
  const qc = useQueryClient()
  const [form, setForm] = useState<CreatePayload>(DEFAULT_FORM)
  const [activeId, setActiveId] = useState<string | null>(null)

  const list = useQuery<BundleStatus[]>({
    queryKey: ['support', 'bundles'],
    queryFn: async () => (await api.get('/support/bundles')).data,
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
    downloadBundle(active.data.id, active.data.filename).then(() => {
      qc.invalidateQueries({ queryKey: ['support', 'bundles'] })
    })
    setActiveId(null)
  }, [active.data?.status, active.data?.id])

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
            Generate a diagnostic archive (logs, configuration, health, database/ClickHouse status)
            for ZenPlus support. Secrets are redacted before packaging.
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <FormField label="Issue category">
              <Select
                value={form.issue_category}
                onValueChange={(v) => setForm({ ...form, issue_category: v })}
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
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
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TIME_RANGES.map((r) => (
                    <SelectItem key={r.value} value={r.value}>{r.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>

            <FormField label="Short summary (optional)" className="md:col-span-2">
              <Textarea
                value={form.issue_summary}
                onChange={(e) => setForm({ ...form, issue_summary: e.target.value.slice(0, 500) })}
                placeholder="What were you trying to do? Any error messages?"
                rows={2}
              />
            </FormField>

            <div className="md:col-span-2 flex items-center justify-between rounded-md border border-border bg-surface2/40 px-3 py-2">
              <div>
                <p className="text-sm font-medium">Include extended logs</p>
                <p className="text-[11px] text-muted">
                  Off keeps the bundle small (≤50 MB). Turn on to include the
                  longer journal/log tails when troubleshooting a specific timeframe.
                </p>
              </div>
              <Switch
                checked={form.include_extended_logs}
                onCheckedChange={(v) => setForm({ ...form, include_extended_logs: !!v })}
              />
            </div>

            <div className="md:col-span-2 flex items-center justify-between border-t border-border pt-3">
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
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </Button>
        </CardHeader>
        <CardContent>
          {list.data && list.data.length > 0 ? (
            <div className="divide-y divide-border">
              {list.data.map((b) => (
                <BundleRow
                  key={b.id}
                  bundle={b}
                  onDownload={() => downloadBundle(b.id, b.filename)}
                  onDelete={() => remove.mutate(b.id)}
                  deleting={remove.isPending && remove.variables === b.id}
                />
              ))}
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
  return (
    <div className="flex flex-col gap-1 py-2 text-sm md:flex-row md:items-center md:justify-between">
      <div className="min-w-0">
        <p className="flex items-center gap-2 font-mono text-xs">
          {ready ? (
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
          ) : bundle.status === 'failed' ? (
            <AlertTriangle className="h-3.5 w-3.5 text-rose-400" />
          ) : (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-400" />
          )}
          <span className="truncate" title={bundle.filename ?? bundle.id}>
            {bundle.filename ?? bundle.id}
          </span>
        </p>
        <p className="text-[11px] text-muted">
          {bundle.status} · {bundle.phase} · {formatBytes(bundle.size_bytes)} · {formatTime(bundle.created_at)}
        </p>
      </div>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!ready}
          onClick={() => { void onDownload() }}
        >
          <Download className="h-3.5 w-3.5" />
          Download
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={deleting}
          onClick={onDelete}
          aria-label="Delete bundle"
        >
          {deleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
        </Button>
      </div>
    </div>
  )
}

// ─── Helpers ────────────────────────────────────────────────────────────────

async function downloadBundle(id: string, filename: string | null): Promise<void> {
  try {
    const res = await api.get(`/support/bundles/${id}/download`, { responseType: 'blob', timeout: 120_000 })
    const blob = new Blob([res.data], { type: 'application/gzip' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename || `zenplus-support-${id}.tar.gz`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 5000)
  } catch (e) {
    toast.error('Download failed', apiErrorMessage(e))
  }
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
