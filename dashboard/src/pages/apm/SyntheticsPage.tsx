/**
 * APM Synthetics — scripted user scenarios (multi-step HTTP journeys).
 *
 * The list shows every scenario's availability, timings and failures for the selected range (default 24h); the
 * scenario name opens its detail page (SyntheticDetailPage) with the same range.
 * Backend: /api/v1/apm/synthetics (list, /{id}, /{id}/summary, /{id}/runs, /{id}/run-detail, /{id}/run).
 */
import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, CheckCircle2, Loader2, Pencil, Play, Plus, Trash2, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { regionalDate } from '@/lib/regionalTime'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker, rangePhrase, useTimeRange } from '@/components/TimeRangePicker'
import { fmtMs } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmKpi, fmtCount } from '@/components/apm/viz'
import { useNotificationChannels } from '@/hooks/useNotificationChannels'
import {
  type SynListResponse, type SynMonitor, type SynRunDetail, type SynStep,
  detailHref, fmtInterval, fmtUptime, formatVariables, monitorPayload, parseVariables, uptimeTone,
} from '@/features/apm/synthetics'
import { AvailabilitySpark, StatusPill, TONE_TEXT } from './synthetics/parts'

// ─── Types ──────────────────────────────────────────────────────────────────

type Step = SynStep
type Monitor = SynMonitor

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

// ─── Run now (shared with the detail page) ──────────────────────────────────

/** POST /{id}/run with the result toast; `onDone` receives the run (shaped like run-detail). */
export function useRunNow(m: Pick<Monitor, 'id' | 'name'>, onDone?: (r: SynRunDetail) => void) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => (await api.post(`/apm/synthetics/${m.id}/run`)).data as SynRunDetail,
    onSuccess: (r: SynRunDetail) => {
      r.success
        ? toast.success(`${m.name} passed`, `${r.steps_passed}/${r.steps_total} steps in ${fmtMs(r.total_ms)}`)
        : toast.error(`${m.name} failed`, `at "${r.failed_step}": ${r.error}`)
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
      onDone?.(r)
    },
    onError: (e: unknown) => toast.error('Run failed', apiErrorMessage(e)),
  })
}

// ─── Page ───────────────────────────────────────────────────────────────────

export function SyntheticsPage() {
  const qc = useQueryClient()
  const can = useCan()
  const manage = can('apm.manage')
  const [searchParams] = useSearchParams()
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange('24h')
  const [editor, setEditor] = useState<Monitor | 'new' | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Monitor | null>(null)

  const q = useQuery<SynListResponse>({
    queryKey: ['apm', 'synthetics', 'list', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(`/apm/synthetics?${new URLSearchParams({ from: range.fromISO, to: range.toISO })}`)).data,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  })

  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/apm/synthetics/${id}`)).data,
    onSuccess: () => {
      setDeleteTarget(null)
      toast.success('Scenario deleted')
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
    },
    onError: (e: unknown) => { setDeleteTarget(null); toast.error('Delete failed', apiErrorMessage(e)) },
  })

  const s = q.data?.summary
  const monitors = q.data?.monitors ?? []
  const spanFrom = q.data?.range?.from ?? range.fromISO
  const spanTo = q.data?.range?.to ?? range.toISO
  const failRate = s?.runs ? (s.failures ?? 0) / s.runs : 0

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Synthetic Scenarios"
        description="Scripted user journeys run from the appliance on a schedule — multi-step HTTP flows with assertions, so you know a workflow is broken before users do."
        article="synthetics"
        actions={
          <>
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
            />
            {manage && (
              <Button size="sm" onClick={() => setEditor('new')}>
                <Plus className="h-4 w-4" /> New scenario
              </Button>
            )}
          </>
        }
      />

      {q.isError && (
        <QueryFailure
          compact={q.data !== undefined}
          error={q.error}
          what="synthetic scenarios"
          onRetry={() => void q.refetch()}
          retrying={q.isFetching}
        />
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <ApmKpi label="Scenarios" tone="primary" value={s?.total ?? '—'} sub={s ? `${s.total - s.disabled} enabled` : undefined} />
        <ApmKpi label="Up" tone="success" value={s?.up ?? '—'} sub="passing now" icon={<CheckCircle2 className="h-4 w-4" />} />
        <ApmKpi label="Down" tone={s?.down ? 'danger' : 'success'} value={s?.down ?? '—'} sub="failing now" icon={<XCircle className="h-4 w-4" />} />
        <ApmKpi label="Disabled" tone="muted" value={s?.disabled ?? '—'} sub="not scheduled" />
        <ApmKpi label="Runs" tone="info" value={s?.runs != null ? fmtCount(s.runs) : '—'} sub={rangePhrase(range.label)} />
        <ApmKpi
          label="Failed runs"
          tone={!s?.failures ? 'success' : failRate >= 0.05 ? 'danger' : 'warning'}
          value={s?.failures != null ? fmtCount(s.failures) : '—'}
          sub={s?.runs ? `${(failRate * 100).toFixed(failRate < 0.01 ? 2 : 1)}% of runs` : 'no runs'}
          icon={<AlertTriangle className="h-4 w-4" />}
        />
      </div>

      <Card>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="p-4"><SkeletonTable rows={5} cols={7} /></div>
          ) : q.isError && !q.data ? null : monitors.length === 0 ? (
            <div className="p-10 text-center">
              <Activity className="mx-auto h-8 w-8 text-muted" />
              <p className="mt-3 text-sm text-text2">No synthetic scenarios yet.</p>
              <p className="mt-1 text-xs text-muted">
                {manage
                  ? 'Create one to continuously exercise a critical user journey — e.g. login → add to cart → checkout — and alert the moment it breaks.'
                  : 'A user with APM management access can create scenarios that exercise critical user journeys.'}
              </p>
              {manage && (
                <Button className="mt-4" onClick={() => setEditor('new')}>
                  <Plus className="h-4 w-4" /> Create your first scenario
                </Button>
              )}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <Tr>
                    <Th>Scenario</Th>
                    <Th>Status</Th>
                    <Th>Availability</Th>
                    <Th className="text-right">Avg / p95</Th>
                    <Th className="text-right">Failures</Th>
                    <Th>Last failure</Th>
                    <Th className="text-right">Interval</Th>
                    <Th>Last run</Th>
                    {manage && <Th className="text-right">Actions</Th>}
                  </Tr>
                </THead>
                <TBody>
                  {monitors.map((m) => (
                    <MonitorRow
                      key={m.id} m={m} manage={manage}
                      href={detailHref(m.id, searchParams)}
                      spanFrom={spanFrom} spanTo={spanTo}
                      onEdit={() => setEditor(m)}
                      onDelete={() => setDeleteTarget(m)}
                    />
                  ))}
                </TBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {editor && manage && (
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
        onConfirm={() => { if (deleteTarget) remove.mutate(deleteTarget.id) }}
      />
    </div>
  )
}

// ─── Row ────────────────────────────────────────────────────────────────────

function MonitorRow({ m, manage, href, spanFrom, spanTo, onEdit, onDelete }: {
  m: Monitor; manage: boolean; href: string; spanFrom: string; spanTo: string
  onEdit: () => void; onDelete: () => void
}) {
  const navigate = useNavigate()
  const run = useRunNow(m)
  const tone = uptimeTone(m.uptime_pct)
  const failureTitle = m.last_failure_at
    ? `${regionalDate(new Date(m.last_failure_at), 'datetime')}${m.last_failed_step ? ` — ${m.last_failed_step}` : ''}${m.last_error ? `: ${m.last_error}` : ''}`
    : undefined

  return (
    <Tr className="cursor-pointer" onClick={() => navigate(href)}>
      <Td>
        <Link to={href} className="font-medium text-text hover:text-primary hover:underline" onClick={(e) => e.stopPropagation()}>
          {m.name}
        </Link>
        <div className="text-xs text-muted">
          {m.steps.length} step{m.steps.length === 1 ? '' : 's'}
          {(m.tags ?? []).length > 0 && <> · {(m.tags ?? []).join(', ')}</>}
        </div>
      </Td>
      <Td><StatusPill status={m.status} enabled={m.enabled} /></Td>
      <Td>
        <div className="flex min-w-[10rem] items-center gap-2">
          <span className={cn('w-14 shrink-0 text-right text-xs font-semibold tabular-nums', TONE_TEXT[tone])}>
            {fmtUptime(m.uptime_pct)}
          </span>
          <AvailabilitySpark spark={m.spark} from={spanFrom} to={spanTo} className="w-32" />
        </div>
      </Td>
      <Td className="whitespace-nowrap text-right text-xs tabular-nums text-text2">
        {fmtMs(m.avg_ms)} <span className="text-muted">/</span> {fmtMs(m.p95_ms)}
      </Td>
      <Td className={cn('text-right text-xs tabular-nums', m.failures ? 'font-semibold text-danger-fg' : 'text-muted')}>
        {m.failures ?? 0}
        {m.runs ? <span className="font-normal text-muted"> / {fmtCount(m.runs)}</span> : null}
      </Td>
      <Td className="max-w-[16rem]" title={failureTitle}>
        {m.last_failure_at ? (
          <div className="min-w-0">
            <div className="text-xs text-text2">{relativeTime(m.last_failure_at)}</div>
            {(m.last_failed_step || m.last_error) && (
              <div className="truncate text-2xs text-danger-fg">
                {m.last_failed_step ? `${m.last_failed_step}: ` : ''}{m.last_error}
              </div>
            )}
          </div>
        ) : <span className="text-xs text-muted">none</span>}
      </Td>
      <Td className="text-right text-xs tabular-nums text-text2">{fmtInterval(m.check_interval)}</Td>
      <Td className="whitespace-nowrap text-xs text-muted">{m.last_run_at ? relativeTime(m.last_run_at) : 'never'}</Td>
      {manage && (
        <Td onClick={(e) => e.stopPropagation()}>
          <div className="flex justify-end gap-1">
            <Button variant="ghost" size="icon" title="Run now" aria-label={`Run ${m.name} now`} disabled={run.isPending} onClick={() => run.mutate()}>
              {run.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            </Button>
            <Button variant="ghost" size="icon" title="Edit" aria-label={`Edit ${m.name}`} onClick={onEdit}><Pencil className="h-4 w-4" /></Button>
            <Button variant="ghost" size="icon" title="Delete" aria-label={`Delete ${m.name}`} onClick={onDelete}><Trash2 className="h-4 w-4 text-danger" /></Button>
          </div>
        </Td>
      )}
    </Tr>
  )
}

// ─── Scenario editor dialog ─────────────────────────────────────────────────

export function ScenarioEditor({ monitor, onClose }: { monitor: Monitor | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState(monitor?.name ?? '')
  const [interval, setIntervalS] = useState(String(monitor?.check_interval ?? 60))
  const [timeout_, setTimeout_] = useState(String(monitor?.timeout ?? 30))
  const [retries, setRetries] = useState(String(monitor?.retry_count ?? 1))
  const [enabled, setEnabled] = useState(monitor?.enabled ?? true)
  const [verifyTls, setVerifyTls] = useState(monitor?.verify_tls ?? true)
  const [variables, setVariables] = useState(() => formatVariables(monitor?.variables))
  const [steps, setSteps] = useState<Step[]>(
    monitor?.steps?.length ? JSON.parse(JSON.stringify(monitor.steps)) : [emptyStep()],
  )

  const channelsQuery = useNotificationChannels()
  const channels = channelsQuery.data ?? []
  const [notify, setNotify] = useState<string[]>(monitor?.notify_channels ?? [])

  const save = useMutation({
    mutationFn: async () => {
      // Every stored field goes back (variables, TLS verification, tags), so saving never resets them.
      const body = monitorPayload(monitor ?? { name, steps }, {
        name,
        check_interval: Number(interval) || 60,
        timeout: Number(timeout_) || 30,
        retry_count: Number(retries) || 0,
        enabled,
        verify_tls: verifyTls,
        variables: parseVariables(variables),
        notify_channels: notify,
        steps,
      })
      return monitor
        ? (await api.put(`/apm/synthetics/${monitor.id}`, body)).data
        : (await api.post('/apm/synthetics', body)).data
    },
    onSuccess: () => {
      toast.success(monitor ? 'Scenario updated' : 'Scenario created')
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
      onClose()
    },
    onError: (e: unknown) => toast.error('Save failed', apiErrorMessage(e)),
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
            <p className="text-xs text-muted">
              Steps run in order in one session (cookies persist). Use{' '}
              <code className="rounded bg-surface2 px-1">{'{{var}}'}</code> to reference values
              extracted from earlier responses.
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-xs text-text2">
              Variables (one per line, NAME=value)
              <Textarea
                className="mt-1 h-16 font-mono text-xs"
                value={variables}
                onChange={(e) => setVariables(e.target.value)}
                placeholder={'base=https://app.example.com\nusername=monitor'}
                spellCheck={false}
              />
              <span className="mt-1 block text-muted">Referenced as {'{{name}}'} in URLs, headers and bodies. Values are hidden from read-only users.</span>
            </label>
            <div className="space-y-2.5 text-xs text-text2">
              <label className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface2/30 px-3 py-2">
                <span>
                  Enabled
                  <span className="block text-muted">Run on schedule and alert on failure</span>
                </span>
                <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Enabled" />
              </label>
              <label className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface2/30 px-3 py-2">
                <span>
                  Verify TLS certificates
                  <span className="block text-muted">Turn off only for internal hosts with self-signed certificates</span>
                </span>
                <Switch checked={verifyTls} onCheckedChange={setVerifyTls} aria-label="Verify TLS certificates" />
              </label>
            </div>
          </div>

            <div>
              <div className="mb-1.5 text-xs text-text2">Notify on failure</div>
              <p className="mb-2 text-xs text-muted">Used when no enabled Alert Rule matches this scenario. Matching Alert Rules control notification routing.</p>
              {channelsQuery.isPending && <p role="status" className="text-xs text-muted">Loading notification channels…</p>}
              {channelsQuery.isError && <div role="alert" className="text-xs text-danger">
                Could not load notification channels: {apiErrorMessage(channelsQuery.error)}
                <Button variant="ghost" size="sm" onClick={() => channelsQuery.refetch()}>Retry</Button>
              </div>}
              {channelsQuery.isSuccess && channels.length === 0 && <p className="text-xs text-muted">No notification channels configured. Add a channel in Notification Channels.</p>}
              <div className="flex flex-wrap gap-2">
                {channels.map((c) => {
                  const cid = String(c.id)
                  const on = notify.includes(cid)
                  return (
                    <button
                      key={cid} type="button"
                      aria-pressed={on}
                      disabled={c.enabled === false && !on}
                      onClick={() => setNotify(on ? notify.filter((x) => x !== cid) : [...notify, cid])}
                      className={cn(
                        'rounded-full border px-2.5 py-1 text-xs transition-colors',
                        on ? 'border-primary/50 bg-primary/15 text-primary'
                           : 'border-border bg-surface2 text-text2 hover:border-primary/40',
                      )}
                    >
                      {c.name ?? cid}{c.enabled === false ? ' (disabled)' : ''}
                    </button>
                  )
                })}
              </div>
            </div>
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
                value={a.value == null ? '' : String(a.value)} onChange={(e) => {
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
        <button type="button" className="text-xs text-primary hover:underline" onClick={() =>
          onChange({ assertions: [...asserts, { type: 'status_code', operator: 'eq', value: 200 }] })}>
          + assertion
        </button>
        <button type="button" className="ml-3 text-xs text-muted hover:text-text2" onClick={() => setShowAdvanced(!showAdvanced)}>
          {showAdvanced ? 'hide' : 'show'} body / headers / extraction
        </button>
      </div>

      {showAdvanced && (
        <div className="mt-2 space-y-2 border-t border-border/50 pt-2">
          <label className="block text-xs text-text2">
            Request body (supports {'{{vars}}'})
            <Textarea className="mt-1 h-16 font-mono text-xs" value={step.body ?? ''}
              onChange={(e) => onChange({ body: e.target.value })}
              placeholder='{"username":"demo","password":"{{password}}"}' />
          </label>
          <label className="block text-xs text-text2">
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
            <div className="text-xs text-text2">Extract into variables</div>
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
            <button type="button" className="text-xs text-primary hover:underline" onClick={() =>
              onChange({ extract: [...extracts, { var: '', from: 'json', path: '' }] })}>
              + extraction
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
