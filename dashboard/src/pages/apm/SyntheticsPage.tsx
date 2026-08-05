/**
 * APM Synthetics — scripted user scenarios (multi-step HTTP journeys).
 *
 * Monitors live in apm_synthetic_monitors (PG); run history in ClickHouse.
 * Backend: /api/v1/apm/synthetics (+ /{id}/run, /{id}/results).
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Loader2,
  Pencil, Play, Plus, Trash2, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { KpiTile, fmtMs } from '@/components/apm/shared'

// ─── Types ──────────────────────────────────────────────────────────────────

interface Assertion { type: string; operator?: string; value?: any; path?: string }
interface Extract { var: string; from: string; path: string }
interface Step {
  name: string; method: string; url: string
  headers?: Record<string, string>; body?: string
  assertions?: Assertion[]; extract?: Extract[]
}
interface Monitor {
  id: string; name: string; steps: Step[]
  variables: Record<string, string>; verify_tls: boolean
  notify_channels: string[]; check_interval: number; timeout: number
  retry_count: number; enabled: boolean; status: string
  last_check_at: string | null
  runs: number; uptime_pct: number | null; avg_ms: number | null
  last_run_at: string | null
}
interface StepResult {
  name: string; method: string; url: string; ok: boolean
  status_code: number; ms: number; error: string
  asserts: { type: string; ok: boolean; detail: string }[]
  body_snippet?: string
}
interface RunResult {
  status: string; success: boolean; total_ms: number
  steps_total: number; steps_passed: number
  failed_step: string; error: string; steps: StepResult[]
}
interface HistoryRow {
  timestamp: string; status: string; success: boolean; total_ms: number
  steps_total: number; steps_passed: number; failed_step: string; error: string
  steps?: StepResult[]
}

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']
const ASSERT_TYPES = [
  { v: 'status_code', label: 'Status code' },
  { v: 'latency_ms', label: 'Latency (ms)' },
  { v: 'body_contains', label: 'Body contains' },
  { v: 'json_path', label: 'JSON path' },
]
const OPERATORS = ['eq', 'neq', 'lt', 'lte', 'gt', 'gte', 'exists']

function emptyStep(): Step {
  return { name: '', method: 'GET', url: '', assertions: [{ type: 'status_code', operator: 'eq', value: 200 }] }
}

// ─── Page ───────────────────────────────────────────────────────────────────

export function SyntheticsPage() {
  const qc = useQueryClient()
  const [editor, setEditor] = useState<Monitor | 'new' | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Monitor | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const q = useQuery<{ monitors: Monitor[]; summary: { total: number; up: number; down: number; disabled: number } }>({
    queryKey: ['apm', 'synthetics'],
    queryFn: async () => (await api.get('/apm/synthetics')).data,
    refetchInterval: 15000,
  })

  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/apm/synthetics/${id}`)).data,
    onSuccess: () => {
      setDeleteTarget(null)
      toast.success('Scenario deleted')
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
    },
    onError: (e: any) => { setDeleteTarget(null); toast.error('Delete failed', apiErrorMessage(e)) },
  })

  const s = q.data?.summary

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-text">Synthetic Scenarios</h1>
          <p className="text-sm text-muted mt-1">
            Scripted user journeys run from the appliance on a schedule — multi-step HTTP
            flows with assertions, so you know a workflow is broken before users do.
          </p>
        </div>
        <Button onClick={() => setEditor('new')}>
          <Plus className="h-4 w-4" /> New scenario
        </Button>
      </div>

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load — {apiErrorMessage(q.error)}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-4">
        <KpiTile label="Scenarios" value={s?.total ?? '—'} />
        <KpiTile label="Up" value={s?.up ?? '—'} accent="#22c55e" />
        <KpiTile label="Down" value={s?.down ?? '—'} accent={s?.down ? '#ef4444' : undefined} />
        <KpiTile label="Disabled" value={s?.disabled ?? '—'} />
      </div>

      <Card>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="flex items-center gap-2 p-6 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading scenarios…
            </div>
          ) : (q.data?.monitors ?? []).length === 0 ? (
            <div className="p-10 text-center">
              <Activity className="mx-auto h-8 w-8 text-muted" />
              <p className="mt-3 text-sm text-text2">No synthetic scenarios yet.</p>
              <p className="mt-1 text-xs text-muted">
                Create one to continuously exercise a critical user journey — e.g. login →
                add to cart → checkout — and alert the moment it breaks.
              </p>
              <Button className="mt-4" onClick={() => setEditor('new')}>
                <Plus className="h-4 w-4" /> Create your first scenario
              </Button>
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th className="w-8" />
                  <Th>Scenario</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Uptime (24h)</Th>
                  <Th className="text-right">Avg duration</Th>
                  <Th className="text-right">Interval</Th>
                  <Th>Last run</Th>
                  <Th className="text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {q.data!.monitors.map((m) => (
                  <MonitorRow
                    key={m.id} m={m}
                    expanded={expanded === m.id}
                    onToggle={() => setExpanded(expanded === m.id ? null : m.id)}
                    onEdit={() => setEditor(m)}
                    onDelete={() => setDeleteTarget(m)}
                  />
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {editor && (
        <ScenarioEditor
          monitor={editor === 'new' ? null : editor}
          onClose={() => setEditor(null)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title="Delete scenario?"
        description={`"${deleteTarget?.name}" and its run history reference will be removed. Recorded results age out of the history store automatically.`}
        confirmText="Delete"
        destructive
        loading={remove.isPending}
        onConfirm={() => deleteTarget && remove.mutate(deleteTarget.id)}
      />
    </div>
  )
}

// ─── Row + expanded history ─────────────────────────────────────────────────

function StatusPill({ status, enabled }: { status: string; enabled: boolean }) {
  if (!enabled) return <Badge variant="outline">Disabled</Badge>
  if (status === 'up') return <Badge variant="success"><CheckCircle2 className="h-3 w-3" /> Up</Badge>
  if (status === 'down') return <Badge variant="danger"><XCircle className="h-3 w-3" /> Down</Badge>
  return <Badge variant="outline">Pending</Badge>
}

function MonitorRow({ m, expanded, onToggle, onEdit, onDelete }: {
  m: Monitor; expanded: boolean
  onToggle: () => void; onEdit: () => void; onDelete: () => void
}) {
  const qc = useQueryClient()
  const [runResult, setRunResult] = useState<RunResult | null>(null)

  const run = useMutation({
    mutationFn: async () => (await api.post(`/apm/synthetics/${m.id}/run`)).data as Promise<RunResult>,
    onSuccess: (r: RunResult) => {
      setRunResult(r)
      if (!expanded) onToggle()
      r.success
        ? toast.success(`${m.name} passed`, `${r.steps_passed}/${r.steps_total} steps in ${fmtMs(r.total_ms)}`)
        : toast.error(`${m.name} failed`, `at "${r.failed_step}": ${r.error}`)
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
    },
    onError: (e: any) => toast.error('Run failed', apiErrorMessage(e)),
  })

  return (
    <>
      <Tr className="cursor-pointer" onClick={onToggle}>
        <Td>{expanded ? <ChevronDown className="h-4 w-4 text-muted" /> : <ChevronRight className="h-4 w-4 text-muted" />}</Td>
        <Td>
          <div className="font-medium text-text">{m.name}</div>
          <div className="text-xs text-muted">{m.steps.length} step{m.steps.length === 1 ? '' : 's'}</div>
        </Td>
        <Td><StatusPill status={m.status} enabled={m.enabled} /></Td>
        <Td className={cn('text-right tabular-nums',
          m.uptime_pct != null && m.uptime_pct < 99 ? 'text-warning' : 'text-text2')}>
          {m.uptime_pct != null ? `${m.uptime_pct}%` : '—'}
        </Td>
        <Td className="text-right tabular-nums text-text2">{m.avg_ms != null ? fmtMs(m.avg_ms) : '—'}</Td>
        <Td className="text-right tabular-nums text-text2">{m.check_interval}s</Td>
        <Td className="text-xs text-muted">{m.last_run_at ? relativeTime(m.last_run_at) : 'never'}</Td>
        <Td onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-end gap-1">
            <Button variant="ghost" size="icon" title="Run now" disabled={run.isPending} onClick={() => run.mutate()}>
              {run.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            </Button>
            <Button variant="ghost" size="icon" title="Edit" onClick={onEdit}><Pencil className="h-4 w-4" /></Button>
            <Button variant="ghost" size="icon" title="Delete" onClick={onDelete}><Trash2 className="h-4 w-4 text-danger" /></Button>
          </div>
        </Td>
      </Tr>
      {expanded && (
        <Tr>
          <Td colSpan={8} className="bg-surface2/20 p-4">
            <MonitorDetail m={m} freshRun={runResult} />
          </Td>
        </Tr>
      )}
    </>
  )
}

function MonitorDetail({ m, freshRun }: { m: Monitor; freshRun: RunResult | null }) {
  const hist = useQuery<{ results: HistoryRow[] }>({
    queryKey: ['apm', 'synthetics', m.id, 'results'],
    queryFn: async () => (await api.get(`/apm/synthetics/${m.id}/results?hours=24`)).data,
    refetchInterval: 30000,
  })

  const rows = hist.data?.results ?? []
  const latestWithSteps = freshRun ?? rows.find((r) => r.steps && r.steps.length)

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div>
        <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted">
          Latest run — step detail
        </div>
        {!latestWithSteps ? (
          <p className="text-sm text-muted">No runs recorded yet. Click “Run now”.</p>
        ) : (
          <div className="space-y-1.5">
            {(latestWithSteps.steps ?? []).map((s2, i) => (
              <div key={i} className={cn(
                'rounded-md border px-3 py-2',
                s2.ok ? 'border-border/60 bg-surface' : 'border-danger/40 bg-danger/10',
              )}>
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    {s2.ok
                      ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
                      : <XCircle className="h-3.5 w-3.5 shrink-0 text-danger" />}
                    <span className="truncate text-sm text-text">{s2.name}</span>
                    <span className="font-mono text-[10px] text-muted">{s2.method}</span>
                  </div>
                  <span className="shrink-0 text-xs tabular-nums text-text2">
                    {s2.status_code || '—'} · {fmtMs(s2.ms)}
                  </span>
                </div>
                {(s2.asserts ?? []).length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 pl-5">
                    {s2.asserts.map((a, j) => (
                      <span key={j} className={cn('text-[11px]', a.ok ? 'text-muted' : 'text-danger')}>
                        {a.ok ? '✓' : '✗'} {a.detail}
                      </span>
                    ))}
                  </div>
                )}
                {s2.error && <div className="mt-1 pl-5 text-[11px] text-danger">{s2.error}</div>}
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted">
          Run history (24h{rows.length ? ` · ${rows.length} runs` : ''})
        </div>
        {/* success/failure strip, most recent first */}
        <div className="flex flex-wrap gap-[3px]">
          {rows.slice(0, 120).map((r, i) => (
            <span
              key={i}
              title={`${new Date(r.timestamp).toLocaleString()} — ${r.status}${r.error ? `: ${r.error}` : ''} (${fmtMs(r.total_ms)})`}
              className={cn('h-5 w-2 rounded-sm', r.success ? 'bg-success/70' : 'bg-danger')}
            />
          ))}
          {rows.length === 0 && <span className="text-sm text-muted">No history yet.</span>}
        </div>
        {rows.some((r) => !r.success) && (
          <div className="mt-3 space-y-1">
            {rows.filter((r) => !r.success).slice(0, 5).map((r, i) => (
              <div key={i} className="flex items-baseline justify-between gap-2 text-[11px]">
                <span className="truncate text-danger">
                  <AlertTriangle className="mr-1 inline h-3 w-3" />
                  {r.failed_step || 'run'}: {r.error}
                </span>
                <span className="shrink-0 tabular-nums text-muted">{relativeTime(r.timestamp)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Scenario editor dialog ─────────────────────────────────────────────────

function ScenarioEditor({ monitor, onClose }: { monitor: Monitor | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState(monitor?.name ?? '')
  const [interval, setIntervalS] = useState(String(monitor?.check_interval ?? 60))
  const [timeout_, setTimeout_] = useState(String(monitor?.timeout ?? 30))
  const [retries, setRetries] = useState(String(monitor?.retry_count ?? 1))
  const [enabled, setEnabled] = useState(monitor?.enabled ?? true)
  const [steps, setSteps] = useState<Step[]>(
    monitor?.steps?.length ? JSON.parse(JSON.stringify(monitor.steps)) : [emptyStep()],
  )

  const { data: channels } = useQuery<any[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => (await api.get('/settings/channels')).data,
    staleTime: 60_000,
  })
  const [notify, setNotify] = useState<string[]>(monitor?.notify_channels ?? [])

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name,
        check_interval: Number(interval) || 60,
        timeout: Number(timeout_) || 30,
        retry_count: Number(retries) || 0,
        enabled,
        notify_channels: notify,
        steps: steps.map((s) => ({
          ...s,
          headers: s.headers && Object.keys(s.headers).length ? s.headers : undefined,
          body: s.body || undefined,
          assertions: (s.assertions ?? []).filter((a) => a.type),
          extract: (s.extract ?? []).filter((e) => e.var && e.path),
        })),
      }
      return monitor
        ? (await api.put(`/apm/synthetics/${monitor.id}`, body)).data
        : (await api.post('/apm/synthetics', body)).data
    },
    onSuccess: () => {
      toast.success(monitor ? 'Scenario updated' : 'Scenario created')
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
      onClose()
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const updateStep = (i: number, patch: Partial<Step>) =>
    setSteps(steps.map((s, j) => (j === i ? { ...s, ...patch } : s)))

  const valid = name.trim() && steps.length > 0 && steps.every((s) => s.name.trim() && s.url.trim())

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{monitor ? 'Edit scenario' : 'New synthetic scenario'}</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-4">
            <label className="col-span-2 text-xs text-text2">
              Scenario name
              <Input className="mt-1" value={name} onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Web store — checkout journey" />
            </label>
            <label className="text-xs text-text2">
              Run every
              <div className="mt-1 flex items-center gap-1.5">
                <Input type="number" min={15} max={86400} value={interval}
                  onChange={(e) => setIntervalS(e.target.value)} className="w-20 text-right tabular-nums" />
                <span className="text-muted">sec</span>
              </div>
            </label>
            <label className="text-xs text-text2">
              Timeout / retries
              <div className="mt-1 flex items-center gap-1.5">
                <Input type="number" min={1} max={120} value={timeout_}
                  onChange={(e) => setTimeout_(e.target.value)} className="w-16 text-right tabular-nums" />
                <span className="text-muted">s ·</span>
                <Input type="number" min={0} max={5} value={retries}
                  onChange={(e) => setRetries(e.target.value)} className="w-12 text-right tabular-nums" />
              </div>
            </label>
          </div>

          <div className="space-y-3">
            {steps.map((s, i) => (
              <StepEditor
                key={i} step={s} index={i} total={steps.length}
                onChange={(p) => updateStep(i, p)}
                onRemove={() => setSteps(steps.filter((_, j) => j !== i))}
                onMove={(dir) => {
                  const n = [...steps]
                  const t = n[i]; n[i] = n[i + dir]; n[i + dir] = t
                  setSteps(n)
                }}
              />
            ))}
            <Button variant="outline" size="sm" onClick={() => setSteps([...steps, emptyStep()])}>
              <Plus className="h-3.5 w-3.5" /> Add step
            </Button>
            <p className="text-[11px] text-muted">
              Steps run in order in one session (cookies persist). Use{' '}
              <code className="rounded bg-surface2 px-1">{'{{var}}'}</code> to reference values
              extracted from earlier responses.
            </p>
          </div>

          {(channels ?? []).length > 0 && (
            <div>
              <div className="mb-1.5 text-xs text-text2">Notify on failure</div>
              <div className="flex flex-wrap gap-2">
                {(channels ?? []).map((c: any) => {
                  const cid = String(c.id ?? c.channel_type ?? c.type)
                  const on = notify.includes(cid)
                  return (
                    <button
                      key={cid} type="button"
                      onClick={() => setNotify(on ? notify.filter((x) => x !== cid) : [...notify, cid])}
                      className={cn(
                        'rounded-full border px-2.5 py-1 text-xs transition-colors',
                        on ? 'border-primary/50 bg-primary/15 text-primary'
                           : 'border-border bg-surface2 text-text2 hover:border-primary/40',
                      )}
                    >
                      {c.name ?? cid}
                    </button>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {monitor ? 'Save changes' : 'Create scenario'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function StepEditor({ step, index, total, onChange, onRemove, onMove }: {
  step: Step; index: number; total: number
  onChange: (p: Partial<Step>) => void
  onRemove: () => void
  onMove: (dir: -1 | 1) => void
}) {
  const [showAdvanced, setShowAdvanced] = useState(
    Boolean(step.body || Object.keys(step.headers ?? {}).length || (step.extract ?? []).length),
  )
  const asserts = step.assertions ?? []
  const extracts = step.extract ?? []

  return (
    <div className="rounded-lg border border-border bg-surface2/30 p-3">
      <div className="flex items-center gap-2">
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
          {index + 1}
        </span>
        <Input className="h-8 flex-1" placeholder="Step name (e.g. Login)"
          value={step.name} onChange={(e) => onChange({ name: e.target.value })} />
        <div className="flex gap-0.5">
          <Button variant="ghost" size="icon" className="h-7 w-7" disabled={index === 0} onClick={() => onMove(-1)}>↑</Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" disabled={index === total - 1} onClick={() => onMove(1)}>↓</Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" disabled={total === 1} onClick={onRemove}>
            <Trash2 className="h-3.5 w-3.5 text-danger" />
          </Button>
        </div>
      </div>

      <div className="mt-2 flex gap-2">
        <div className="w-28">
          <Select value={step.method} onValueChange={(v) => onChange({ method: v })}>
            <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
            <SelectContent>{METHODS.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <Input className="h-8 flex-1 font-mono text-xs" placeholder="https://app.example.com/api/login"
          value={step.url} onChange={(e) => onChange({ url: e.target.value })} />
      </div>

      {/* assertions */}
      <div className="mt-2 space-y-1.5">
        {asserts.map((a, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <div className="w-36">
              <Select value={a.type} onValueChange={(v) =>
                onChange({ assertions: asserts.map((x, j) => (j === i ? { ...x, type: v } : x)) })}>
                <SelectTrigger className="h-7 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>{ASSERT_TYPES.map((t) => <SelectItem key={t.v} value={t.v}>{t.label}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            {a.type === 'json_path' && (
              <Input className="h-7 w-40 font-mono text-xs" placeholder="user.id"
                value={a.path ?? ''} onChange={(e) =>
                  onChange({ assertions: asserts.map((x, j) => (j === i ? { ...x, path: e.target.value } : x)) })} />
            )}
            {a.type !== 'body_contains' && (
              <div className="w-24">
                <Select value={a.operator ?? 'eq'} onValueChange={(v) =>
                  onChange({ assertions: asserts.map((x, j) => (j === i ? { ...x, operator: v } : x)) })}>
                  <SelectTrigger className="h-7 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>{OPERATORS.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}</SelectContent>
                </Select>
              </div>
            )}
            {a.operator !== 'exists' && (
              <Input className="h-7 w-32 text-xs" placeholder="value"
                value={a.value ?? ''} onChange={(e) => {
                  const raw = e.target.value
                  const num = Number(raw)
                  onChange({
                    assertions: asserts.map((x, j) =>
                      (j === i ? { ...x, value: raw !== '' && !Number.isNaN(num) ? num : raw } : x)),
                  })
                }} />
            )}
            <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() =>
              onChange({ assertions: asserts.filter((_, j) => j !== i) })}>
              <XCircle className="h-3 w-3 text-muted" />
            </Button>
          </div>
        ))}
        <button type="button" className="text-[11px] text-primary hover:underline" onClick={() =>
          onChange({ assertions: [...asserts, { type: 'status_code', operator: 'eq', value: 200 }] })}>
          + assertion
        </button>
        <button type="button" className="ml-3 text-[11px] text-muted hover:text-text2" onClick={() => setShowAdvanced(!showAdvanced)}>
          {showAdvanced ? 'hide' : 'show'} body / headers / extraction
        </button>
      </div>

      {showAdvanced && (
        <div className="mt-2 space-y-2 border-t border-border/50 pt-2">
          <label className="block text-[11px] text-text2">
            Request body (supports {'{{vars}}'})
            <Textarea className="mt-1 h-16 font-mono text-xs" value={step.body ?? ''}
              onChange={(e) => onChange({ body: e.target.value })}
              placeholder='{"username":"demo","password":"{{password}}"}' />
          </label>
          <label className="block text-[11px] text-text2">
            Headers (one per line, Name: value)
            <Textarea className="mt-1 h-12 font-mono text-xs"
              value={Object.entries(step.headers ?? {}).map(([k, v]) => `${k}: ${v}`).join('\n')}
              onChange={(e) => {
                const headers: Record<string, string> = {}
                for (const line of e.target.value.split('\n')) {
                  const idx = line.indexOf(':')
                  if (idx > 0) headers[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
                }
                onChange({ headers })
              }}
              placeholder="Content-Type: application/json" />
          </label>
          <div className="space-y-1.5">
            <div className="text-[11px] text-text2">Extract into variables</div>
            {extracts.map((ex, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <Input className="h-7 w-28 font-mono text-xs" placeholder="var name" value={ex.var}
                  onChange={(e) => onChange({ extract: extracts.map((x, j) => (j === i ? { ...x, var: e.target.value } : x)) })} />
                <div className="w-24">
                  <Select value={ex.from} onValueChange={(v) =>
                    onChange({ extract: extracts.map((x, j) => (j === i ? { ...x, from: v } : x)) })}>
                    <SelectTrigger className="h-7 text-xs"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="json">JSON path</SelectItem>
                      <SelectItem value="header">Header</SelectItem>
                      <SelectItem value="regex">Regex</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <Input className="h-7 flex-1 font-mono text-xs"
                  placeholder={ex.from === 'header' ? 'Set-Cookie' : ex.from === 'regex' ? 'token=(\\w+)' : 'data.access_token'}
                  value={ex.path}
                  onChange={(e) => onChange({ extract: extracts.map((x, j) => (j === i ? { ...x, path: e.target.value } : x)) })} />
                <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() =>
                  onChange({ extract: extracts.filter((_, j) => j !== i) })}>
                  <XCircle className="h-3 w-3 text-muted" />
                </Button>
              </div>
            ))}
            <button type="button" className="text-[11px] text-primary hover:underline" onClick={() =>
              onChange({ extract: [...extracts, { var: '', from: 'json', path: '' }] })}>
              + extraction
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
