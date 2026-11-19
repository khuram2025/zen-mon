/**
 * One synthetic scenario over a time range (default 24h, shared `range`/`from`/`to` URL params with the list):
 * availability and duration trends, per-step statistics, grouped errors, outages, every run, and the configuration.
 * `?step=<index>` opens a step's detail, `?run=<timestamp>` a run's waterfall.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowLeft, CheckCircle2, ChevronLeft, ChevronRight, Clock, Loader2, Pencil, Play, ShieldCheck,
  ShieldOff, Timer, X, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { regionalDate } from '@/lib/regionalTime'
import { seriesVar, tokenColor } from '@/lib/chartTheme'
import { useCan } from '@/stores/auth'
import { useTheme } from '@/stores/theme'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton, SkeletonTable } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker, rangePhrase, useTimeRange } from '@/components/TimeRangePicker'
import { fmtMs } from '@/components/apm/shared'
import { ApmKpi, ApmTimeChart, fmtCount } from '@/components/apm/viz'
import { useNotificationChannels } from '@/hooks/useNotificationChannels'
import {
  type RunStatusFilter, type SynMonitor, type SynOutage, type SynRun, type SynRunDetail, type SynRunsResponse,
  type SynStep, type SynStepStats, type SynSummary,
  MASK, RUNS_PAGE_SIZE, describeAssertion, fmtBytes, fmtDuration, fmtInterval, fmtUptime, hasMaskedValues,
  hostPath, listHref, monitorPayload, outageWindow, pageInfo, parseRunParam, parseStepParam, runsQuery,
  stepChips, uptimeTone, waterfall, withParam,
} from '@/features/apm/synthetics'
import { ScenarioEditor, useRunNow } from './SyntheticsPage'
import { AvailabilityChart, StepStackChart } from './synthetics/charts'
import {
  Drawer, Field, MethodTag, Section, StatusCode, StatusPill, StepChips, TONE_TEXT, TimingBar, TimingLegend,
} from './synthetics/parts'

const base = (id: string) => `/apm/synthetics/${encodeURIComponent(id)}`

function rangeQs(from: string, to: string): string {
  return new URLSearchParams({ from, to }).toString()
}

function when(iso: string | null | undefined): string {
  return iso ? regionalDate(new Date(iso), 'datetime') : '—'
}

// ─── Page ───────────────────────────────────────────────────────────────────

export function SyntheticDetailPage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const can = useCan()
  const manage = can('apm.manage')
  const [sp, setSp] = useSearchParams()
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange('24h')
  const qs = rangeQs(range.fromISO, range.toISO)
  const [editing, setEditing] = useState(false)
  const [freshRun, setFreshRun] = useState<SynRunDetail | null>(null)
  const [runsStatus, setRunsStatus] = useState<RunStatusFilter>('all')
  const [runsStep, setRunsStep] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const runsRef = useRef<HTMLDivElement>(null)

  // A new range or filter starts the runs table on its first page (a rolling preset sliding each minute does not).
  const rangeIdentity = isCustom ? `${range.fromISO}|${range.toISO}` : range.key
  useEffect(() => { setOffset(0) }, [rangeIdentity, runsStatus, runsStep])

  const monitorQ = useQuery<SynMonitor>({
    queryKey: ['apm', 'synthetics', id, 'monitor', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(`${base(id)}?${qs}`)).data,
    enabled: !!id,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  })
  const summaryQ = useQuery<SynSummary>({
    queryKey: ['apm', 'synthetics', id, 'summary', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(`${base(id)}/summary?${qs}`)).data,
    enabled: !!id,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  })
  const runsQ = useQuery<SynRunsResponse>({
    queryKey: ['apm', 'synthetics', id, 'runs', range.fromISO, range.toISO, runsStatus, runsStep, offset],
    queryFn: async () => (await api.get(`${base(id)}/runs?${runsQuery({
      from: range.fromISO, to: range.toISO, status: runsStatus, step: runsStep, offset,
    })}`)).data,
    enabled: !!id,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  })

  const m = monitorQ.data
  const summary = summaryQ.data
  const run = useRunNow({ id, name: m?.name ?? 'Scenario' }, (r) => setFreshRun(r))

  const toggle = useMutation({
    mutationFn: async (enabled: boolean) => {
      if (!m) throw new Error('Scenario not loaded')
      return (await api.put(base(id), monitorPayload(m, { enabled }))).data
    },
    onSuccess: (_d, enabled) => {
      toast.success(enabled ? 'Scenario enabled' : 'Scenario disabled')
      qc.invalidateQueries({ queryKey: ['apm', 'synthetics'] })
    },
    onError: (e: unknown) => toast.error('Update failed', apiErrorMessage(e)),
  })

  const stepParam = parseStepParam(sp.get('step'), Math.max(m?.steps.length ?? 0, summary?.steps.length ?? 0))
  const runParam = parseRunParam(sp.get('run'))
  const openStep = (i: number) => setSp(withParam(withParam(sp, 'run', null), 'step', i))
  const openRun = (ts: string) => { setFreshRun(null); setSp(withParam(withParam(sp, 'step', null), 'run', ts)) }
  const closeDrawers = () => { setFreshRun(null); setSp(withParam(withParam(sp, 'step', null), 'run', null)) }

  const showRuns = (status: RunStatusFilter, step: string | null) => {
    setRunsStatus(status)
    setRunsStep(step)
    runsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const zoomTo = (from: string, to: string) => {
    setCustom(from, to)
    showRuns('all', null)
  }

  if (monitorQ.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-80" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
        </div>
        <Skeleton className="h-56 w-full" />
      </div>
    )
  }
  if (!m) {
    return (
      <div className="space-y-4">
        <BackLink search={sp} />
        <QueryFailure error={monitorQ.error} what="this scenario" onRetry={() => void monitorQ.refetch()} retrying={monitorQ.isFetching} />
      </div>
    )
  }

  const t = summary?.totals
  const uptime = t?.uptime_pct ?? m.uptime_pct ?? null
  const masked = !!m.secrets_masked
  const lastFailure = m.status === 'down' && m.last_error

  return (
    <div className="space-y-4">
      {/* ── header ─────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-xs text-muted">
            <Link to="/apm" className="hover:text-text">Application Performance</Link>
            <ChevronRight className="h-3 w-3" aria-hidden />
            <Link to={listHref(sp)} className="hover:text-text">Synthetics</Link>
            <ChevronRight className="h-3 w-3" aria-hidden />
            <span className="truncate text-text2">{m.name}</span>
          </nav>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="truncate text-2xl font-semibold tracking-tight text-text">{m.name}</h1>
            <StatusPill status={m.status} enabled={m.enabled} />
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text2">
            <span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" aria-hidden /> every {fmtInterval(m.check_interval)}</span>
            <span>{m.steps.length} step{m.steps.length === 1 ? '' : 's'}</span>
            <span className="inline-flex items-center gap-1"><Timer className="h-3 w-3" aria-hidden /> timeout {m.timeout}s · {m.retry_count} retr{m.retry_count === 1 ? 'y' : 'ies'}</span>
            <span>last run {m.last_run_at ? relativeTime(m.last_run_at) : 'never'}</span>
            {(m.tags ?? []).map((tag) => <Badge key={tag} variant="outline">{tag}</Badge>)}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <TimeRangePicker
            rangeIdx={rangeIdx}
            isCustom={isCustom}
            customFrom={range.fromISO}
            customTo={range.toISO}
            onPreset={setPreset}
            onCustom={setCustom}
          />
          {manage && (
            <>
              <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-md border border-border bg-surface2/40 px-2.5 text-xs font-semibold text-text2">
                <Switch
                  checked={m.enabled}
                  disabled={toggle.isPending || hasMaskedValues(m)}
                  onCheckedChange={(v) => toggle.mutate(v)}
                  aria-label="Enabled"
                />
                {m.enabled ? 'Enabled' : 'Disabled'}
              </label>
              <Button variant="outline" size="sm" onClick={() => setEditing(true)} disabled={hasMaskedValues(m)}>
                <Pencil className="h-3.5 w-3.5" /> Edit
              </Button>
              <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}>
                {run.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />} Run now
              </Button>
            </>
          )}
        </div>
      </div>

      {lastFailure && (
        <div role="status" className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden />
          <div className="min-w-0">
            <div className="font-medium text-danger-fg">
              Failing{m.last_failed_step ? <> at step “{m.last_failed_step}”</> : null}
            </div>
            <div className="break-words text-xs text-text2">{m.last_error}</div>
          </div>
        </div>
      )}

      {summaryQ.isError && (
        <QueryFailure compact error={summaryQ.error} what="scenario statistics" onRetry={() => void summaryQ.refetch()} retrying={summaryQ.isFetching} />
      )}

      {/* ── KPIs ───────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <ApmKpi
          label="Uptime"
          tone={uptime == null ? 'muted' : uptimeTone(uptime) === 'ok' ? 'success' : uptimeTone(uptime) === 'warn' ? 'warning' : 'danger'}
          value={fmtUptime(uptime)}
          sub={rangePhrase(range.label)}
        />
        <ApmKpi label="Runs" tone="info" value={t ? fmtCount(t.runs) : '—'} sub={t?.last_run_at ? `last ${relativeTime(t.last_run_at)}` : 'no runs'} />
        <ApmKpi
          label="Failures"
          tone={!t ? 'muted' : t.failed ? 'danger' : 'success'}
          value={t ? fmtCount(t.failed) : '—'}
          sub={t ? `${fmtCount(t.passed)} passed` : undefined}
        />
        <ApmKpi
          label="Outages"
          tone={!t ? 'muted' : t.outages ? 'warning' : 'success'}
          value={t ? t.outages : '—'}
          sub={t ? `${fmtDuration(t.downtime_s)} downtime` : undefined}
        />
        <ApmKpi label="Avg duration" tone="primary" value={fmtMs(t?.avg_ms ?? m.avg_ms)} sub={t ? `p50 ${fmtMs(t.p50_ms)}` : undefined} />
        <ApmKpi label="p95 duration" tone="primary" value={fmtMs(t?.p95_ms ?? m.p95_ms)} sub={t ? `max ${fmtMs(t.max_ms)}` : undefined} />
      </div>

      {/* ── trends ─────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <Section title="Availability" hint="Passed and failed runs per interval — click a bar to zoom in">
          <div className="px-2 pb-2 pt-1">
            {summaryQ.isLoading ? <Skeleton className="h-[200px] w-full" /> : (
              <AvailabilityChart
                series={summary?.series ?? []}
                bucketSeconds={summary?.range.bucket_seconds}
                onBucketClick={zoomTo}
              />
            )}
          </div>
        </Section>
        <Section title="Run duration" hint="Average and 95th percentile per interval">
          <div className="px-2 pb-2 pt-1">
            <DurationChart summary={summary} loading={summaryQ.isLoading} />
          </div>
        </Section>
      </div>
      {(summary?.steps.length ?? 0) > 1 && (
        <Section title="Duration by step" hint="Average time per step, stacked — the tallest band is where the journey spends its time">
          <div className="px-2 pb-2 pt-1"><StepStackChart steps={summary!.steps} /></div>
        </Section>
      )}

      {/* ── steps ──────────────────────────────────────────────── */}
      <Section title="Steps" hint="Click a step for its trend, failures and assertions">
        <StepsTable monitor={m} summary={summary} loading={summaryQ.isLoading} onOpen={openStep} />
      </Section>

      {/* ── errors + outages ───────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <Section title="Errors" hint="Grouped by step and message — click to list those runs">
          <ErrorsList summary={summary} loading={summaryQ.isLoading} onPick={(step) => showRuns('failed', step || null)} />
        </Section>
        <Section title="Outages" hint="Consecutive failed runs — click to zoom the range to that window">
          <OutagesList summary={summary} loading={summaryQ.isLoading} onPick={(o) => {
            const w = outageWindow(o, Date.now())
            if (w) zoomTo(w.from, w.to)
          }} />
        </Section>
      </div>

      {/* ── runs ───────────────────────────────────────────────── */}
      <div ref={runsRef} className="scroll-mt-4">
        <Section
          title="Runs"
          hint={runsQ.data ? `${fmtCount(runsQ.data.total)} run${runsQ.data.total === 1 ? '' : 's'} in ${rangePhrase(range.label)}` : undefined}
          right={
            <div className="flex flex-wrap items-center gap-2">
              {runsStep && (
                <button
                  type="button"
                  onClick={() => setRunsStep(null)}
                  className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-xs text-primary"
                  title="Clear the step filter"
                >
                  failed at “{runsStep}” <X className="h-3 w-3" aria-hidden />
                </button>
              )}
              <Segmented
                value={runsStatus}
                onChange={(v) => { setRunsStatus(v); if (v !== 'failed') setRunsStep(null) }}
                options={[['all', 'All'], ['failed', 'Failed'], ['passed', 'Passed']]}
                label="Run result"
              />
            </div>
          }
        >
          <RunsTable
            q={runsQ}
            monitor={m}
            offset={offset}
            onOffset={setOffset}
            onOpen={openRun}
          />
        </Section>
      </div>

      {/* ── configuration ──────────────────────────────────────── */}
      <ConfigPanel monitor={m} masked={masked} manage={manage} />

      {/* ── drawers ────────────────────────────────────────────── */}
      <StepDrawer
        id={id}
        index={runParam || freshRun ? null : stepParam}
        monitor={m}
        summary={summary}
        range={{ from: range.fromISO, to: range.toISO, label: range.label }}
        onClose={closeDrawers}
        onOpenRun={openRun}
        onShowRuns={(step) => { closeDrawers(); showRuns('failed', step) }}
      />
      <RunDrawer id={id} at={freshRun ? null : runParam} fresh={freshRun} monitor={m} onClose={closeDrawers} />

      {editing && manage && <ScenarioEditor monitor={m} onClose={() => setEditing(false)} />}
    </div>
  )
}

function BackLink({ search }: { search: URLSearchParams }) {
  return (
    <Link to={listHref(search)} className="inline-flex items-center gap-1 text-sm text-text2 hover:text-text">
      <ArrowLeft className="h-4 w-4" aria-hidden /> Synthetics
    </Link>
  )
}

function Segmented<T extends string>({ value, onChange, options, label }: {
  value: T; onChange: (v: T) => void; options: Array<[T, string]>; label: string
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/40 p-0.5">
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          aria-pressed={value === v}
          onClick={() => onChange(v)}
          className={cn('rounded px-2 py-1 text-xs font-semibold transition-colors',
            value === v ? 'bg-primary text-on-primary' : 'text-text2 hover:text-text')}
        >
          {text}
        </button>
      ))}
    </div>
  )
}

// ─── Charts ─────────────────────────────────────────────────────────────────

function DurationChart({ summary, loading }: { summary?: SynSummary; loading: boolean }) {
  const theme = useTheme((s) => s.theme)
  const series = useMemo(() => [
    { key: 'avg_ms', name: 'Average', color: tokenColor('series-1'), fmt: (v: number) => fmtMs(v) },
    { key: 'p95_ms', name: 'p95', color: tokenColor('series-2'), fmt: (v: number) => fmtMs(v), dashed: true },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [theme])
  if (loading) return <Skeleton className="h-[200px] w-full" />
  return <ApmTimeChart data={summary?.series ?? []} timeKey="t" series={series} height={200} empty="No runs in this range" />
}

// ─── Steps table ────────────────────────────────────────────────────────────

interface StepRow { index: number; cfg?: SynStep; stats?: SynStepStats }

function stepRows(m: SynMonitor, summary?: SynSummary): StepRow[] {
  const n = Math.max(m.steps.length, ...(summary?.steps ?? []).map((s) => s.index + 1), 0)
  return Array.from({ length: n }, (_, i) => ({
    index: i,
    cfg: m.steps[i],
    stats: summary?.steps.find((s) => s.index === i),
  }))
}

function StepsTable({ monitor, summary, loading, onOpen }: {
  monitor: SynMonitor; summary?: SynSummary; loading: boolean; onOpen: (i: number) => void
}) {
  const rows = stepRows(monitor, summary)
  if (loading && !summary) return <div className="p-4"><SkeletonTable rows={Math.max(2, monitor.steps.length)} cols={8} /></div>
  return (
    <div className="overflow-x-auto">
      <Table>
        <THead>
          <Tr>
            <Th className="w-8">#</Th>
            <Th>Step</Th>
            <Th className="text-right">Pass</Th>
            <Th className="text-right">Executed / failed / skipped</Th>
            <Th className="text-right">Avg / p95</Th>
            <Th>Last status</Th>
            <Th>Status codes</Th>
            <Th>Timing (connect · TLS · wait · download)</Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map(({ index, cfg, stats }) => {
            const tone = uptimeTone(stats?.pass_pct)
            const url = stats?.url || cfg?.url || ''
            return (
              <Tr
                key={index}
                className="cursor-pointer"
                tabIndex={0}
                onClick={() => onOpen(index)}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(index) } }}
              >
                <Td className="text-xs tabular-nums text-muted">{index + 1}</Td>
                <Td>
                  <div className="flex min-w-0 items-center gap-2">
                    <MethodTag method={stats?.method || cfg?.method || 'GET'} />
                    <span className="truncate font-medium text-text">{stats?.name || cfg?.name || `Step ${index + 1}`}</span>
                    {!cfg && <Badge variant="outline">removed</Badge>}
                  </div>
                  <div className="mt-0.5 max-w-[24rem] truncate font-mono text-2xs text-muted" title={url}>{hostPath(url)}</div>
                </Td>
                <Td className={cn('text-right text-xs font-semibold tabular-nums', TONE_TEXT[tone])}>{fmtUptime(stats?.pass_pct)}</Td>
                <Td className="whitespace-nowrap text-right text-xs tabular-nums text-text2">
                  {stats ? (
                    <>
                      {fmtCount(stats.executed)}
                      <span className="text-muted"> / </span>
                      <span className={stats.failed ? 'font-semibold text-danger-fg' : ''}>{fmtCount(stats.failed)}</span>
                      <span className="text-muted"> / {fmtCount(stats.skipped)}</span>
                    </>
                  ) : '—'}
                </Td>
                <Td className="whitespace-nowrap text-right text-xs tabular-nums text-text2">
                  {fmtMs(stats?.avg_ms)} <span className="text-muted">/</span> {fmtMs(stats?.p95_ms)}
                </Td>
                <Td><StatusCode code={stats?.last_status} /></Td>
                <Td>
                  <div className="flex max-w-[10rem] flex-wrap gap-1">
                    {(stats?.status_codes ?? []).length
                      ? stats!.status_codes.map((c) => (
                        <span key={c} className="rounded border border-border px-1"><StatusCode code={c} /></span>
                      ))
                      : <span className="text-xs text-muted">—</span>}
                  </div>
                </Td>
                <Td><TimingBar timing={stats?.timing} /></Td>
              </Tr>
            )
          })}
        </TBody>
      </Table>
    </div>
  )
}

// ─── Errors and outages ─────────────────────────────────────────────────────

function ErrorsList({ summary, loading, onPick }: { summary?: SynSummary; loading: boolean; onPick: (step: string) => void }) {
  if (loading && !summary) return <div className="p-4"><SkeletonTable rows={3} cols={3} /></div>
  const errors = summary?.errors ?? []
  if (!errors.length) return <p className="px-4 py-6 text-center text-sm text-muted">No failures in this range.</p>
  return (
    <ul className="divide-y divide-border">
      {errors.map((e, i) => (
        <li key={i}>
          <button type="button" onClick={() => onPick(e.failed_step)} className="flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-surface2/50">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium text-text">{e.failed_step || 'Run'}</span>
              <span className="block break-words text-xs text-text2">{e.error || 'No error message'}</span>
              <span className="mt-0.5 block text-2xs text-muted">first {when(e.first_seen)} · last {relativeTime(e.last_seen)}</span>
            </span>
            <span className="shrink-0 rounded-full bg-danger/10 px-2 py-0.5 text-xs font-semibold tabular-nums text-danger-fg">{fmtCount(e.count)}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}

function OutagesList({ summary, loading, onPick }: { summary?: SynSummary; loading: boolean; onPick: (o: SynOutage) => void }) {
  if (loading && !summary) return <div className="p-4"><SkeletonTable rows={3} cols={4} /></div>
  const outages = summary?.outages ?? []
  if (!outages.length) return <p className="px-4 py-6 text-center text-sm text-muted">No outages in this range.</p>
  return (
    <div className="overflow-x-auto">
      <Table>
        <THead>
          <Tr>
            <Th>Start</Th>
            <Th>End</Th>
            <Th className="text-right">Duration</Th>
            <Th className="text-right">Failed runs</Th>
            <Th>First error</Th>
          </Tr>
        </THead>
        <TBody>
          {outages.map((o, i) => (
            <Tr key={i} className="cursor-pointer" tabIndex={0} onClick={() => onPick(o)}
              onKeyDown={(e) => { if (e.key === 'Enter') onPick(o) }}>
              <Td className="whitespace-nowrap text-xs text-text2">{when(o.start)}</Td>
              <Td className="whitespace-nowrap text-xs">
                {o.ongoing ? <Badge variant="danger">ongoing</Badge> : <span className="text-text2">{when(o.end)}</span>}
              </Td>
              <Td className="text-right text-xs tabular-nums text-text">{fmtDuration(o.duration_s)}</Td>
              <Td className="text-right text-xs tabular-nums text-danger-fg">{o.failed_runs}</Td>
              <Td className="max-w-[18rem]">
                <div className="truncate text-xs text-text2" title={`${o.failed_step}: ${o.error}`}>
                  {o.failed_step ? <span className="font-medium text-text">{o.failed_step}: </span> : null}{o.error}
                </div>
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  )
}

// ─── Runs table ─────────────────────────────────────────────────────────────

function RunsTable({ q, monitor, offset, onOffset, onOpen }: {
  q: { data?: SynRunsResponse; isLoading: boolean; isError: boolean; error: unknown; isFetching: boolean; refetch: () => unknown }
  monitor: SynMonitor
  offset: number
  onOffset: (o: number) => void
  onOpen: (ts: string) => void
}) {
  if (q.isLoading) return <div className="p-4"><SkeletonTable rows={6} cols={5} /></div>
  if (q.isError && !q.data) {
    return <div className="p-4"><QueryFailure error={q.error} what="runs" onRetry={() => void q.refetch()} retrying={q.isFetching} /></div>
  }
  const runs = q.data?.runs ?? []
  const total = q.data?.total ?? 0
  if (!runs.length) return <p className="px-4 py-6 text-center text-sm text-muted">No runs match in this range.</p>
  const p = pageInfo(total, offset)
  return (
    <>
      <div className="overflow-x-auto">
        <Table>
          <THead>
            <Tr>
              <Th>Time</Th>
              <Th>Result</Th>
              <Th className="text-right">Duration</Th>
              <Th>Steps</Th>
              <Th>Failure</Th>
            </Tr>
          </THead>
          <TBody>
            {runs.map((r: SynRun) => (
              <Tr key={r.timestamp} className="cursor-pointer" tabIndex={0} onClick={() => onOpen(r.timestamp)}
                onKeyDown={(e) => { if (e.key === 'Enter') onOpen(r.timestamp) }}>
                <Td className="whitespace-nowrap">
                  <div className="text-xs text-text">{when(r.timestamp)}</div>
                  <div className="text-2xs text-muted">{relativeTime(r.timestamp)}</div>
                </Td>
                <Td>{r.success ? <Badge variant="success">Passed</Badge> : <Badge variant="danger">Failed</Badge>}</Td>
                <Td className="text-right text-xs tabular-nums text-text2">{fmtMs(r.total_ms)}</Td>
                <Td><StepChips chips={stepChips(r, monitor.steps)} /></Td>
                <Td className="max-w-[22rem]">
                  {!r.success && (
                    <div className="truncate text-xs text-danger-fg" title={`${r.failed_step}: ${r.error}`}>
                      {r.failed_step ? <span className="font-medium">{r.failed_step}: </span> : null}{r.error}
                    </div>
                  )}
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-2 text-xs text-text2">
        <span className="tabular-nums">{p.first}–{p.last} of {fmtCount(total)}</span>
        <div className="flex items-center gap-1">
          {q.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" aria-label="Loading" />}
          <Button variant="ghost" size="sm" disabled={!p.hasPrev} onClick={() => onOffset(Math.max(0, offset - RUNS_PAGE_SIZE))}>
            <ChevronLeft className="h-3.5 w-3.5" /> Newer
          </Button>
          <span className="tabular-nums">page {p.page} / {p.pages}</span>
          <Button variant="ghost" size="sm" disabled={!p.hasNext} onClick={() => onOffset(offset + RUNS_PAGE_SIZE)}>
            Older <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    </>
  )
}

// ─── Step drawer ────────────────────────────────────────────────────────────

function StepDrawer({ id, index, monitor, summary, range, onClose, onOpenRun, onShowRuns }: {
  id: string
  index: number | null
  monitor: SynMonitor
  summary?: SynSummary
  range: { from: string; to: string; label: string }
  onClose: () => void
  onOpenRun: (ts: string) => void
  onShowRuns: (step: string) => void
}) {
  const theme = useTheme((s) => s.theme)
  const open = index != null
  const cfg = open ? monitor.steps[index] : undefined
  const stats = open ? summary?.steps.find((s) => s.index === index) : undefined
  const name = stats?.name || cfg?.name || ''
  const failuresQ = useQuery<SynRunsResponse>({
    queryKey: ['apm', 'synthetics', id, 'step-failures', name, range.from, range.to],
    queryFn: async () => (await api.get(`${base(id)}/runs?${runsQuery({ from: range.from, to: range.to, status: 'failed', step: name, limit: 50 })}`)).data,
    enabled: open && !!name,
  })
  const trendSeries = useMemo(() => [
    { key: 'avg_ms', name: 'Average', color: tokenColor('series-1'), fmt: (v: number) => fmtMs(v) },
    { key: 'failed', name: 'Failed runs', color: tokenColor('danger'), type: 'bar' as const, yAxisIndex: 1 as const, fmt: (v: number) => String(Math.round(v)) },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [theme])

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={<><MethodTag method={stats?.method || cfg?.method || 'GET'} /><span className="truncate">{index != null ? `${index + 1}. ` : ''}{name}</span></>}
      subtitle={<span className="break-all font-mono">{stats?.url || cfg?.url}</span>}
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="Pass rate"><span className={cn('font-semibold tabular-nums', TONE_TEXT[uptimeTone(stats?.pass_pct)])}>{fmtUptime(stats?.pass_pct)}</span></Field>
        <Field label="Executed / failed">
          <span className="tabular-nums">{stats ? fmtCount(stats.executed) : '—'} / <span className={stats?.failed ? 'text-danger-fg' : ''}>{stats ? fmtCount(stats.failed) : '—'}</span></span>
          {stats?.skipped ? <span className="text-xs text-muted"> · {fmtCount(stats.skipped)} skipped</span> : null}
        </Field>
        <Field label="Avg / p95"><span className="tabular-nums">{fmtMs(stats?.avg_ms)} / {fmtMs(stats?.p95_ms)}</span></Field>
        <Field label="Last status"><StatusCode code={stats?.last_status} /></Field>
      </div>

      {stats?.timing && (
        <div>
          <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Average timing</div>
          <TimingBar timing={stats.timing} showLegend />
        </div>
      )}

      <div>
        <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Duration and failures — {rangePhrase(range.label)}</div>
        <div className="rounded-md border border-border px-1">
          <ApmTimeChart data={stats?.series ?? []} timeKey="t" series={trendSeries} height={200} empty="No runs of this step in this range" />
        </div>
      </div>

      {(stats?.status_codes ?? []).length > 0 && (
        <Field label="Status codes seen">
          <span className="flex flex-wrap gap-1">
            {stats!.status_codes.map((c) => <span key={c} className="rounded border border-border px-1"><StatusCode code={c} /></span>)}
          </span>
        </Field>
      )}

      {(stats?.top_errors ?? []).length > 0 && (
        <div>
          <div className="mb-1 text-2xs font-semibold uppercase tracking-wider text-muted">Top errors</div>
          <ul className="space-y-1">
            {stats!.top_errors.map((e, i) => (
              <li key={i} className="flex items-start justify-between gap-3 rounded-md border border-danger/30 bg-danger/5 px-3 py-2">
                <span className="min-w-0 break-words text-xs text-text">{e.error}</span>
                <span className="shrink-0 text-right text-2xs text-muted">
                  <span className="block text-xs font-semibold tabular-nums text-danger-fg">{fmtCount(e.count)}×</span>
                  {relativeTime(e.last_seen)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-2xs font-semibold uppercase tracking-wider text-muted">Failures in this range</span>
          {(failuresQ.data?.total ?? 0) > 0 && (
            <button type="button" className="text-xs text-primary hover:underline" onClick={() => onShowRuns(name)}>
              Show in runs table
            </button>
          )}
        </div>
        {failuresQ.isLoading ? <SkeletonTable rows={3} cols={3} />
          : failuresQ.isError ? <QueryFailure compact error={failuresQ.error} what="failures" onRetry={() => void failuresQ.refetch()} />
          : !(failuresQ.data?.runs ?? []).length ? <p className="text-sm text-muted">This step did not fail in this range.</p>
          : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {failuresQ.data!.runs.map((r) => (
                <li key={r.timestamp}>
                  <button type="button" onClick={() => onOpenRun(r.timestamp)} className="flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-surface2/50">
                    <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-danger" aria-hidden />
                    <span className="min-w-0 flex-1">
                      <span className="block text-xs text-text">{when(r.timestamp)}</span>
                      <span className="block truncate text-2xs text-danger-fg">{r.error}</span>
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-muted">{fmtMs(r.total_ms)}</span>
                  </button>
                </li>
              ))}
              {(failuresQ.data!.total ?? 0) > failuresQ.data!.runs.length && (
                <li className="px-3 py-2 text-2xs text-muted">Showing the latest {failuresQ.data!.runs.length} of {fmtCount(failuresQ.data!.total)}.</li>
              )}
            </ul>
          )}
      </div>

      {cfg && <StepConfig step={cfg} masked={!!monitor.secrets_masked} />}
    </Drawer>
  )
}

function StepConfig({ step, masked }: { step: SynStep; masked: boolean }) {
  const headers = Object.entries(step.headers ?? {})
  return (
    <div className="space-y-2">
      <div className="text-2xs font-semibold uppercase tracking-wider text-muted">Configured checks</div>
      {(step.assertions ?? []).length ? (
        <ul className="space-y-1">
          {(step.assertions ?? []).map((a, i) => (
            <li key={i} className="flex items-center gap-2 text-xs text-text">
              <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
              <span className="font-mono">{describeAssertion(a, masked)}</span>
            </li>
          ))}
        </ul>
      ) : <p className="text-xs text-muted">No assertions — the step passes on any 2xx/3xx response.</p>}
      {(step.extract ?? []).length > 0 && (
        <Field label="Extracts">
          <ul className="space-y-0.5 font-mono text-xs">
            {(step.extract ?? []).map((e, i) => <li key={i}>{`{{${e.var}}}`} ← {e.from} <span className="text-text2">{e.path}</span></li>)}
          </ul>
        </Field>
      )}
      {headers.length > 0 && (
        <Field label="Headers">
          <ul className="space-y-0.5 font-mono text-xs">
            {headers.map(([k, v]) => <li key={k} className="break-all">{k}: <span className="text-text2">{v}</span></li>)}
          </ul>
        </Field>
      )}
      {step.body && (
        <Field label="Request body">
          {step.body === MASK
            ? <span className="font-mono text-xs text-muted">{MASK} (hidden)</span>
            : <pre className="max-h-40 overflow-auto rounded bg-surface2 p-2 font-mono text-xs text-text2">{step.body}</pre>}
        </Field>
      )}
    </div>
  )
}

// ─── Run drawer ─────────────────────────────────────────────────────────────

function RunDrawer({ id, at, fresh, monitor, onClose }: {
  id: string; at: string | null; fresh: SynRunDetail | null; monitor: SynMonitor; onClose: () => void
}) {
  const open = !!at || !!fresh
  const q = useQuery<SynRunDetail>({
    queryKey: ['apm', 'synthetics', id, 'run-detail', at],
    queryFn: async () => (await api.get(`${base(id)}/run-detail?${new URLSearchParams({ at: at! })}`)).data,
    enabled: !!at && !fresh,
    staleTime: 5 * 60_000,
  })
  const r = fresh ?? q.data
  const ts = r?.timestamp ?? at

  return (
    <Drawer
      open={open}
      onClose={onClose}
      wide
      title={<>
        {r ? (r.success ? <Badge variant="success">Passed</Badge> : <Badge variant="danger">Failed</Badge>) : null}
        <span className="truncate">Run {ts ? when(ts) : fresh ? 'just now' : ''}</span>
      </>}
      subtitle={r ? `${r.steps_passed}/${r.steps_total} steps passed · ${fmtMs(r.total_ms)}${r.location ? ` · from ${r.location}` : ''}` : undefined}
    >
      {!r && q.isLoading && <SkeletonTable rows={4} cols={4} />}
      {!r && q.isError && <QueryFailure error={q.error} what="this run" onRetry={() => void q.refetch()} retrying={q.isFetching} />}
      {r && (
        <>
          {!r.success && r.error && (
            <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm">
              <div className="font-medium text-danger-fg">Failed{r.failed_step ? ` at “${r.failed_step}”` : ''}</div>
              <div className="break-words text-xs text-text2">{r.error}</div>
            </div>
          )}
          <Waterfall run={r} configured={monitor.steps} />
          <div className="space-y-3">
            {r.steps.map((s, i) => <RunStepCard key={i} step={s} index={i} />)}
            {r.steps.length < r.steps_total && (
              <p className="text-xs text-muted">
                {r.steps_total - r.steps.length} later step{r.steps_total - r.steps.length === 1 ? ' was' : 's were'} not run because an earlier step failed.
              </p>
            )}
          </div>
        </>
      )}
    </Drawer>
  )
}

function Waterfall({ run, configured }: { run: SynRunDetail; configured: SynStep[] }) {
  const rows = waterfall(run.steps)
  const hasTiming = rows.some((r) => r.segments.length)
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-2xs font-semibold uppercase tracking-wider text-muted">Waterfall</span>
        <span className="text-2xs tabular-nums text-muted">0 → {fmtMs(run.total_ms)}</span>
      </div>
      <div className="space-y-1 rounded-md border border-border p-2">
        {run.steps.map((s, i) => {
          const row = rows[i]
          return (
            <div key={i} className="grid grid-cols-[minmax(0,12rem)_1fr_4.5rem] items-center gap-2">
              <div className="flex min-w-0 items-center gap-1.5">
                {s.ok ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" aria-hidden /> : <XCircle className="h-3.5 w-3.5 shrink-0 text-danger" aria-hidden />}
                <span className="truncate text-xs text-text" title={s.name}>{i + 1}. {s.name || configured[i]?.name}</span>
              </div>
              <div className="relative h-4 rounded bg-surface2/60">
                <div
                  className={cn('absolute inset-y-0 flex overflow-hidden rounded', !row.segments.length && (s.ok ? 'bg-success/70' : 'bg-danger'))}
                  style={{ left: `${row.offsetPct}%`, width: `${Math.min(100 - row.offsetPct, row.widthPct)}%` }}
                  title={`${s.name}: ${fmtMs(s.ms)} (starts at ${fmtMs(row.startMs)})`}
                >
                  {row.segments.map((seg) => (
                    <span key={seg.key} style={{ width: `${seg.pct}%`, backgroundColor: seriesVar(seg.series) }} />
                  ))}
                  {!s.ok && row.segments.length > 0 && <span className="absolute inset-0 rounded ring-2 ring-inset ring-danger" />}
                </div>
              </div>
              <div className="flex items-center justify-end gap-1.5 text-xs tabular-nums">
                <StatusCode code={s.status_code} />
                <span className="text-text2">{fmtMs(s.ms)}</span>
              </div>
            </div>
          )
        })}
      </div>
      {hasTiming
        ? <TimingLegend timing={sumTimings(run)} />
        : <p className="mt-1 text-2xs text-muted">Connection timing is recorded for runs from this release onwards.</p>}
    </div>
  )
}

function sumTimings(run: SynRunDetail) {
  const out = { connect_ms: 0, tls_ms: 0, send_ms: 0, ttfb_ms: 0, download_ms: 0 }
  for (const s of run.steps) {
    for (const k of Object.keys(out) as Array<keyof typeof out>) out[k] += Number(s.timing?.[k]) || 0
  }
  return out
}

function RunStepCard({ step, index }: { step: SynRunDetail['steps'][number]; index: number }) {
  const facts: Array<[string, string]> = []
  if (step.server_ip) facts.push(['Server', step.server_ip])
  if (step.http_version) facts.push(['Protocol', step.http_version])
  if (step.response_bytes != null) facts.push(['Size', fmtBytes(step.response_bytes)])
  if (step.content_type) facts.push(['Content type', step.content_type])
  if (step.redirects != null) facts.push(['Redirects', String(step.redirects)])
  return (
    <div className={cn('rounded-md border px-3 py-2.5', step.ok ? 'border-border bg-surface' : 'border-danger/40 bg-danger/5')}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-xs tabular-nums text-muted">{index + 1}.</span>
          <MethodTag method={step.method} />
          <span className="truncate text-sm font-medium text-text">{step.name}</span>
        </div>
        <div className="flex items-center gap-2 text-xs tabular-nums">
          <StatusCode code={step.status_code} />
          <span className="text-text2">{fmtMs(step.ms)}</span>
        </div>
      </div>
      {step.url && <div className="mt-0.5 break-all font-mono text-2xs text-muted">{step.url}</div>}
      {step.timing && <TimingBar timing={step.timing} showLegend className="mt-2" />}
      {facts.length > 0 && (
        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
          {facts.map(([k, v]) => <Field key={k} label={k}><span className="font-mono text-xs">{v}</span></Field>)}
        </div>
      )}
      {(step.asserts ?? []).length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {step.asserts.map((a, j) => (
            <li key={j} className={cn('flex items-start gap-1.5 text-xs', a.ok ? 'text-text2' : 'text-danger-fg')}>
              <span aria-hidden className="font-semibold">{a.ok ? '✓' : '✗'}</span>
              <span className="sr-only">{a.ok ? 'passed' : 'failed'}:</span>
              <span className="break-words">{a.detail || a.type}</span>
            </li>
          ))}
        </ul>
      )}
      {step.error && <div className="mt-2 break-words text-xs text-danger-fg">{step.error}</div>}
      {!step.ok && step.body_snippet && (
        <div className="mt-2">
          <div className="text-2xs font-semibold uppercase tracking-wider text-muted">Response body (start)</div>
          <pre className="mt-0.5 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-surface2 p-2 font-mono text-xs text-text2">{step.body_snippet}</pre>
        </div>
      )}
    </div>
  )
}

// ─── Configuration ──────────────────────────────────────────────────────────

function ConfigPanel({ monitor, masked, manage }: { monitor: SynMonitor; masked: boolean; manage: boolean }) {
  const notify = monitor.notify_channels ?? []
  const channelsQ = useNotificationChannels(notify.length > 0)
  const channelName = (cid: string) => channelsQ.data?.find((c) => String(c.id) === cid)?.name ?? cid
  const vars = Object.entries(monitor.variables ?? {})
  return (
    <Section
      title="Configuration"
      hint={masked ? 'Variable values, credential headers and request bodies are hidden for your role.' : undefined}
    >
      <div className="grid gap-4 p-4 lg:grid-cols-[2fr_1fr]">
        <ol className="space-y-2">
          {monitor.steps.map((s, i) => (
            <li key={i} className="rounded-md border border-border bg-surface2/30 px-3 py-2">
              <div className="flex min-w-0 items-center gap-2">
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/15 text-2xs font-semibold text-primary">{i + 1}</span>
                <MethodTag method={s.method} />
                <span className="truncate text-sm font-medium text-text">{s.name}</span>
              </div>
              <div className="mt-1 break-all font-mono text-xs text-text2">{s.url}</div>
              {((s.assertions ?? []).length > 0 || (s.extract ?? []).length > 0) && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {(s.assertions ?? []).map((a, j) => (
                    <span key={`a${j}`} className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-2xs text-text2">{describeAssertion(a, masked)}</span>
                  ))}
                  {(s.extract ?? []).map((e, j) => (
                    <span key={`e${j}`} className="rounded border border-primary/30 bg-primary/5 px-1.5 py-0.5 font-mono text-2xs text-primary">{`{{${e.var}}}`} ← {e.from}:{e.path}</span>
                  ))}
                </div>
              )}
              {(Object.keys(s.headers ?? {}).length > 0 || s.body) && (
                <div className="mt-1 text-2xs text-muted">
                  {Object.keys(s.headers ?? {}).length > 0 && <>headers: {Object.keys(s.headers ?? {}).join(', ')}</>}
                  {s.body && <>{Object.keys(s.headers ?? {}).length ? ' · ' : ''}request body {s.body === MASK ? '(hidden)' : `${s.body.length} chars`}</>}
                </div>
              )}
            </li>
          ))}
        </ol>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Runs every">{fmtInterval(monitor.check_interval)}</Field>
            <Field label="Timeout">{monitor.timeout}s per step</Field>
            <Field label="Retries">{monitor.retry_count}</Field>
            <Field label="TLS verification">
              {monitor.verify_tls === false
                ? <span className="inline-flex items-center gap-1 text-warning-fg"><ShieldOff className="h-3.5 w-3.5" aria-hidden /> off</span>
                : <span className="inline-flex items-center gap-1"><ShieldCheck className="h-3.5 w-3.5 text-success" aria-hidden /> on</span>}
            </Field>
            {monitor.created_at && <Field label="Created">{when(monitor.created_at)}</Field>}
          </div>
          <Field label="Notify on failure">
            {notify.length
              ? <span className="flex flex-wrap gap-1">{notify.map((c) => <Badge key={c} variant="outline">{channelName(c)}</Badge>)}</span>
              : <span className="text-muted">Alert rules only</span>}
          </Field>
          <Field label="Variables">
            {vars.length ? (
              <ul className="space-y-0.5 font-mono text-xs">
                {vars.map(([k, v]) => <li key={k} className="break-all">{k} = <span className="text-text2">{v}</span></li>)}
              </ul>
            ) : <span className="text-muted">none</span>}
          </Field>
          {!manage && <p className="text-2xs text-muted">Changing a scenario needs APM management access.</p>}
        </div>
      </div>
    </Section>
  )
}
