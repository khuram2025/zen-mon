/**
 * NCCM configuration compliance: fleet workspace (overview, results explorer,
 * policy packs, exceptions) and the per-device compliance tab.
 *
 * Status is always shown as icon + text; colour is never the only cue.
 * Every query renders loading, error and empty states distinctly, so a failed
 * load can never be mistaken for "no findings".
 */
import { useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ChevronDown, ChevronRight, Copy, Download, Eye, FlaskConical, Pencil, Play, Plus, RefreshCw, Search, ShieldOff,
  Trash2, Undo2,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import { useAuth, useCan } from '@/stores/auth'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { useVizColors } from '@/components/availability/viz'
import { absoluteUtc, download, ncmError, pct, toCsv, TONE_BADGE, type Tone } from './model'
import { Ago, EmptyState, Kpi, Pagination, SectionError, ToneIcon } from './ui'

/* ── Types ─────────────────────────────────────────────────────────────── */

type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info'
type Status = 'fail' | 'unknown' | 'excepted' | 'pass'
type CheckType = 'must_match' | 'must_not_match' | 'section_must_match' | 'section_must_not_match' | 'section_any_must_match'

interface Evidence { line: number; text: string }

interface ResultRow {
  device_id: string
  hostname?: string | null
  ip?: string | null
  rule_id: string
  policy_id: string
  policy_name: string
  title: string
  severity: Severity
  status: Status
  detail?: string | null
  evidence?: unknown
  remediation?: string | null
  description?: string | null
  check_type?: CheckType
  rule_key?: string
  assessed_at?: string | null
  version_id?: string | null
  version_captured_at?: string | null
  exception_id?: string | null
}

interface Rule {
  id: string
  rule_key: string
  title: string
  description?: string | null
  severity: Severity
  check_type: CheckType
  pattern: string
  section_pattern?: string | null
  platforms?: string[] | null
  remediation?: string | null
  enabled: boolean
  position?: number
}

interface Policy {
  id: string
  name: string
  description?: string | null
  platforms?: string[] | null
  enabled: boolean
  builtin: boolean
  version: number
  rules: Rule[]
}

interface PolicySummary {
  id: string; name: string; enabled: boolean; builtin: boolean; version: number; platforms?: string[] | null
  rules: number; devices: number; passed: number; failed: number; unknown: number; excepted: number; assessed_at?: string | null
}

interface TrendPoint { day: string; devices: number; passed: number; failed: number; unknown: number; excepted: number }

interface Summary {
  devices_assessed: number
  devices_enrolled: number
  devices_compliant: number
  devices_failing: number
  devices_unknown: number
  failures_by_severity: Record<Severity, number>
  policies: PolicySummary[]
  trend: TrendPoint[]
  trend_note?: string
  exceptions_expiring_14d: number
}

interface ExceptionRow {
  id: string; rule_id: string; device_id?: string | null; hostname?: string | null; title: string; severity: Severity
  policy_name: string; owner: string; reason: string; expires_at: string; created_by?: string | null; created_at?: string | null
  revoked_at?: string | null; revoked_by?: string | null; state: 'active' | 'expiring' | 'expired' | 'revoked'
}

interface EvalTotals { devices: number; pass: number; fail: number; unknown: number; excepted: number }

interface ExceptionTarget { rule_id: string; device_id: string; hostname?: string | null; title: string; policy_name?: string }

/* ── Vocabulary ────────────────────────────────────────────────────────── */

const SEVERITIES: Severity[] = ['critical', 'high', 'medium', 'low', 'info']
const SEVERITY_LABEL: Record<Severity, string> = { critical: 'Critical', high: 'High', medium: 'Medium', low: 'Low', info: 'Info' }
const SEVERITY_TONE: Record<Severity, Tone> = { critical: 'danger', high: 'danger', medium: 'warning', low: 'info', info: 'neutral' }

const STATUSES: Status[] = ['fail', 'unknown', 'excepted', 'pass']
const STATUS_META: Record<Status, { label: string; tone: Tone; description: string }> = {
  fail: { label: 'Fail', tone: 'danger', description: 'The configuration does not satisfy this rule.' },
  unknown: { label: 'Not assessable', tone: 'warning', description: 'No validated running configuration, or the rule could not be evaluated.' },
  excepted: { label: 'Excepted', tone: 'neutral', description: 'Failing or not assessable, but covered by an active, owned exception.' },
  pass: { label: 'Pass', tone: 'success', description: 'The configuration satisfies this rule.' },
}

const CHECK_TYPES: { value: CheckType; label: string; help: string }[] = [
  { value: 'must_match', label: 'Must match', help: 'At least one configuration line must match the pattern.' },
  { value: 'must_not_match', label: 'Must not match', help: 'No configuration line may match the pattern.' },
  { value: 'section_must_match', label: 'Every section must match', help: 'Every section whose header matches the section pattern must contain a line matching the pattern.' },
  { value: 'section_must_not_match', label: 'No section may match', help: 'No section whose header matches the section pattern may contain a line matching the pattern.' },
  { value: 'section_any_must_match', label: 'Any section must match', help: 'At least one section whose header matches the section pattern must contain a line matching the pattern.' },
]
const CHECK_LABEL = Object.fromEntries(CHECK_TYPES.map((c) => [c.value, c.label])) as Record<CheckType, string>
const isSection = (t?: string) => !!t && t.startsWith('section')

const EXCEPTION_STATE: Record<ExceptionRow['state'], { label: string; tone: Tone }> = {
  active: { label: 'Active', tone: 'info' },
  expiring: { label: 'Expiring soon', tone: 'warning' },
  expired: { label: 'Expired', tone: 'neutral' },
  revoked: { label: 'Revoked', tone: 'neutral' },
}

const PAGE_SIZE = 50
const DEFAULT_STATUSES: Status[] = ['fail', 'unknown']

/* ── Small helpers ─────────────────────────────────────────────────────── */

function evidenceOf(raw: unknown): Evidence[] {
  let v = raw
  if (typeof v === 'string') {
    try { v = JSON.parse(v) } catch { return [] }
  }
  return Array.isArray(v) ? v.filter((e) => e && typeof e === 'object').map((e: any) => ({ line: Number(e.line), text: String(e.text ?? '') })) : []
}

function totalsText(t: EvalTotals) {
  return `${t.devices} device${t.devices === 1 ? '' : 's'} · ${t.fail} failed · ${t.unknown} not assessable · ${t.excepted} excepted · ${t.pass} passed`
}

function utcDate(offsetDays: number) {
  return new Date(serverNow() + offsetDays * 86400000).toISOString().slice(0, 10)
}

/** "in 12 d" / "3 d ago" for future-or-past timestamps (Ago only handles the past). */
function Relative({ iso }: { iso: string }) {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return <span className="text-muted">—</span>
  const diff = t - serverNow()
  if (diff <= 0) return <Ago iso={iso} />
  const h = diff / 3600000
  const text = h < 1 ? `in ${Math.max(1, Math.round(diff / 60000))} min` : h < 48 ? `in ${Math.round(h)} h` : `in ${Math.round(h / 24)} d`
  return <time dateTime={iso} title={absoluteUtc(iso)}>{text}</time>
}

function Loading({ what }: { what: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 py-8 text-xs text-muted">
      <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden /> Loading {what}…
    </div>
  )
}

function StatusBadge({ status }: { status: Status }) {
  const m = STATUS_META[status] || STATUS_META.unknown
  return (
    <Badge variant={TONE_BADGE[m.tone]} title={m.description} className="whitespace-nowrap">
      <ToneIcon tone={m.tone} className={m.tone === 'neutral' ? '' : 'text-current'} />{m.label}
    </Badge>
  )
}

function SeverityBadge({ severity }: { severity: Severity }) {
  const tone = SEVERITY_TONE[severity] || 'neutral'
  return <Badge variant={TONE_BADGE[tone]} className="whitespace-nowrap">{SEVERITY_LABEL[severity] || severity}</Badge>
}

function EvidenceLines({ evidence, emptyText = 'No evidence lines recorded for this outcome.' }: { evidence: Evidence[]; emptyText?: string }) {
  if (!evidence.length) return <p className="text-xs text-muted">{emptyText}</p>
  return (
    <div className="min-w-0">
      <ol aria-label="Evidence lines" className="overflow-x-auto rounded-md border border-border bg-surface2 py-1 font-mono text-xs">
        {evidence.map((e, i) => (
          <li key={`${e.line}-${i}`} className="flex gap-3 px-2 py-0.5">
            <span className="w-12 shrink-0 select-none text-right tabular-nums text-muted">
              <span className="sr-only">Line </span>{e.line}
            </span>
            <code className="whitespace-pre">{e.text}</code>
          </li>
        ))}
      </ol>
      <p className="mt-1 text-xs text-muted">Line numbers refer to the assessed configuration version. Secrets are redacted before evidence is stored.</p>
    </div>
  )
}

function Remediation({ text }: { text?: string | null }) {
  if (!text) return null
  return (
    <div className="rounded-md border border-info/30 bg-info/5 px-3 py-2 text-sm">
      <span className="text-xs font-medium text-info">Remediation: </span>{text}
    </div>
  )
}

function Toggle({ pressed, onClick, children, className }: { pressed: boolean; onClick: () => void; children: ReactNode; className?: string }) {
  return (
    <Button type="button" size="sm" variant={pressed ? 'default' : 'outline'} aria-pressed={pressed} onClick={onClick} className={className}>
      {children}
    </Button>
  )
}

/* ── Shared queries ────────────────────────────────────────────────────── */

function useSummary() {
  return useQuery<Summary>({
    queryKey: ['ncm', 'compliance', 'summary'],
    queryFn: async () => (await api.get('/ncm/compliance/summary', { params: { days: 30 } })).data,
  })
}

function usePolicies() {
  return useQuery<Policy[]>({
    queryKey: ['ncm', 'compliance', 'policies'],
    queryFn: async () => (await api.get('/ncm/compliance/policies')).data?.data ?? [],
  })
}

function usePlatformOptions() {
  return useQuery<{ value: string; label: string }[]>({
    queryKey: ['ncm', 'compliance', 'platforms'],
    queryFn: async () => ((await api.get('/ncm/platforms')).data?.data ?? [])
      .filter((p: any) => p.value !== 'autodetect')
      .map((p: any) => ({ value: String(p.value), label: String(p.label || p.value) })),
    staleTime: 10 * 60 * 1000,
  })
}

function useEnrolledDevices(enabled = true) {
  return useQuery<{ device_id: string; hostname: string }[]>({
    queryKey: ['ncm', 'compliance', 'enrolled-devices'],
    queryFn: async () => ((await api.get('/ncm/overview', { params: { page_size: 1000 } })).data?.data ?? [])
      .filter((d: any) => d.enrolled)
      .map((d: any) => ({ device_id: String(d.device_id), hostname: String(d.hostname || d.ip || d.device_id) }))
      .sort((a: any, b: any) => a.hostname.localeCompare(b.hostname)),
    enabled,
    staleTime: 60 * 1000,
  })
}

function useEvaluate(deviceId?: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => (await api.post('/ncm/compliance/evaluate', null, { params: deviceId ? { device_id: deviceId } : undefined })).data as EvalTotals,
    onSuccess: (t) => {
      toast.success('Compliance evaluated', totalsText(t))
      qc.invalidateQueries({ queryKey: ['ncm'] })
    },
    onError: (e) => toast.error('Evaluation failed', ncmError(e)),
  })
}

/* ── Fleet workspace ───────────────────────────────────────────────────── */

interface ResultFilters { policy: string; statuses: Status[]; severity: string; q: string }
const DEFAULT_FILTERS: ResultFilters = { policy: 'all', statuses: DEFAULT_STATUSES, severity: 'all', q: '' }

export function ComplianceWorkspace() {
  const can = useCan()
  const canEvaluate = can('ncm.manage') || can('ncm.compliance')
  const evaluate = useEvaluate()
  const [tab, setTab] = useState('overview')
  const [filters, setFilters] = useState<ResultFilters>(DEFAULT_FILTERS)
  const [exceptionTarget, setExceptionTarget] = useState<ExceptionTarget | null>(null)

  const showResults = (patch: Partial<ResultFilters>) => {
    setFilters({ ...DEFAULT_FILTERS, ...patch })
    setTab('results')
  }

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">Configuration compliance</h2>
          <p className="text-xs text-muted">
            Policy rules are checked against each enrolled device&apos;s latest validated running configuration. Evaluation never contacts devices.
          </p>
        </div>
        {canEvaluate && (
          <Button size="sm" onClick={() => evaluate.mutate()} disabled={evaluate.isPending}>
            {evaluate.isPending ? <RefreshCw className="animate-spin" aria-hidden /> : <Play aria-hidden />}
            {evaluate.isPending ? 'Evaluating…' : 'Evaluate now'}
          </Button>
        )}
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="h-auto flex-wrap">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="results">Results</TabsTrigger>
          <TabsTrigger value="policies">Policies</TabsTrigger>
          <TabsTrigger value="exceptions">Exceptions</TabsTrigger>
        </TabsList>
        <TabsContent value="overview" className="min-w-0">
          <OverviewTab onResults={showResults} onExceptions={() => setTab('exceptions')} />
        </TabsContent>
        <TabsContent value="results" className="min-w-0">
          <ResultsExplorer filters={filters} onFilters={setFilters} onException={setExceptionTarget} />
        </TabsContent>
        <TabsContent value="policies" className="min-w-0">
          <PoliciesTab onResults={(policy) => showResults({ policy, statuses: [] })} />
        </TabsContent>
        <TabsContent value="exceptions" className="min-w-0">
          <ExceptionsTab />
        </TabsContent>
      </Tabs>

      {exceptionTarget && <ExceptionDialog target={exceptionTarget} onClose={() => setExceptionTarget(null)} />}
    </div>
  )
}

/* ── Overview ──────────────────────────────────────────────────────────── */

function OverviewTab({ onResults, onExceptions }: { onResults: (p: Partial<ResultFilters>) => void; onExceptions: () => void }) {
  const summary = useSummary()
  if (summary.isLoading) return <Loading what="compliance summary" />
  if (summary.isError || !summary.data) return <SectionError what="The compliance summary" onRetry={() => summary.refetch()} />
  const s = summary.data
  return (
    <div className="min-w-0 space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <Kpi label="Devices assessed" value={s.devices_assessed} sub={`of ${s.devices_enrolled} enrolled`} />
        <Kpi label="Compliant" value={s.devices_compliant} tone={s.devices_compliant ? 'success' : 'neutral'}
          sub={`${pct(s.devices_compliant, s.devices_assessed)} of assessed`} />
        <Kpi label="Failing" value={s.devices_failing} tone={s.devices_failing ? 'danger' : 'neutral'} sub="devices with a failed rule"
          onClick={() => onResults({ statuses: ['fail'] })} />
        <Kpi label="Not assessable" value={s.devices_unknown} tone={s.devices_unknown ? 'warning' : 'neutral'} sub="no validated config / not evaluable"
          onClick={() => onResults({ statuses: ['unknown'] })} />
        <Kpi label="Exceptions expiring" value={s.exceptions_expiring_14d} tone={s.exceptions_expiring_14d ? 'warning' : 'neutral'}
          sub="within 14 days" onClick={onExceptions} />
      </div>

      {s.devices_enrolled > 0 && s.devices_assessed === 0 && (
        <div role="note" className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          No enrolled device has been assessed yet. Devices are assessed after a validated running configuration is captured and an enabled policy applies to their platform.
        </div>
      )}

      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card className="min-w-0">
          <CardContent className="space-y-3 p-4">
            <div>
              <h3 className="text-sm font-semibold">Failed checks by severity</h3>
              <p className="text-xs text-muted">Current failing rule results across your visible devices.</p>
            </div>
            <SeverityBars counts={s.failures_by_severity} onPick={(sev) => onResults({ statuses: ['fail'], severity: sev })} />
          </CardContent>
        </Card>
        <Card className="min-w-0">
          <CardContent className="space-y-3 p-4">
            <div>
              <h3 className="text-sm font-semibold">Daily trend (30 days)</h3>
              <p className="text-xs text-muted">Failed and passed rule checks per day, fleet-wide.</p>
            </div>
            <TrendChart points={s.trend || []} note={s.trend_note} />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function SeverityBars({ counts, onPick }: { counts: Record<Severity, number>; onPick: (s: Severity) => void }) {
  const { c } = useVizColors()
  const colour: Record<Severity, string> = { critical: c('danger'), high: c('warning'), medium: c('amber'), low: c('muted'), info: c('muted2') }
  const total = SEVERITIES.reduce((n, s) => n + (counts?.[s] || 0), 0)
  const max = Math.max(1, ...SEVERITIES.map((s) => counts?.[s] || 0))
  return (
    <div className="space-y-1">
      {!total && <p className="text-xs text-muted">No failed checks at the moment.</p>}
      <ul className="space-y-1">
        {SEVERITIES.map((sev) => {
          const n = counts?.[sev] || 0
          return (
            <li key={sev}>
              <button type="button" onClick={() => onPick(sev)} disabled={!n}
                aria-label={`${SEVERITY_LABEL[sev]}: ${n} failed check${n === 1 ? '' : 's'}${n ? ', show in results' : ''}`}
                className="group grid w-full grid-cols-[4.5rem_minmax(0,1fr)_2.5rem] items-center gap-2 rounded px-1 py-1 text-left text-sm hover:bg-surface2 disabled:cursor-default disabled:hover:bg-transparent">
                <span className="text-xs font-medium">{SEVERITY_LABEL[sev]}</span>
                <span className="h-2 overflow-hidden rounded-full bg-surface2">
                  <span className="block h-full rounded-full" style={{ width: n ? `${Math.max(3, (n / max) * 100)}%` : 0, background: colour[sev] }} />
                </span>
                <span className={cn('text-right tabular-nums', n ? 'font-semibold' : 'text-muted')}>{n}</span>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function TrendChart({ points, note }: { points: TrendPoint[]; note?: string }) {
  const { c } = useVizColors()
  if (!points.length) {
    return (
      <div className="space-y-2">
        <EmptyState>No trend points yet. The first point is recorded when compliance is evaluated.</EmptyState>
        {note && <p className="text-xs text-muted">{note}</p>}
      </div>
    )
  }
  const max = Math.max(1, ...points.map((p) => (p.failed || 0) + (p.passed || 0)))
  const last = points[points.length - 1]
  const failed = c('danger')
  const passed = c('success')
  return (
    <figure className="min-w-0 space-y-2">
      <div className="flex h-32 items-end gap-px" role="img"
        aria-label={`Daily rule checks over ${points.length} day${points.length === 1 ? '' : 's'}. Latest ${last.day}: ${last.failed} failed, ${last.passed} passed.`}>
        {points.map((p) => {
          const f = p.failed || 0
          const ok = p.passed || 0
          return (
            <div key={p.day} className="flex h-full min-w-0 max-w-[28px] flex-1 flex-col justify-end"
              title={`${p.day}: ${f} failed, ${ok} passed, ${p.unknown || 0} not assessable, ${p.excepted || 0} excepted (${p.devices} device-policy assessments)`}>
              {f > 0 && <div className="rounded-t-sm" style={{ height: `${(f / max) * 100}%`, minHeight: 2, background: failed }} />}
              {ok > 0 && <div className={cn(!f && 'rounded-t-sm')} style={{ height: `${(ok / max) * 100}%`, minHeight: 2, background: passed, opacity: 0.55 }} />}
              {!f && !ok && <div className="h-px bg-border" />}
            </div>
          )
        })}
      </div>
      <div className="flex justify-between border-t border-border pt-1 text-xs tabular-nums text-muted">
        <span>{points[0].day}</span>{points.length > 1 && <span>{last.day}</span>}
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="inline-flex items-center gap-1.5"><span aria-hidden className="h-2.5 w-2.5 rounded-sm" style={{ background: failed }} />Failed checks (latest {last.failed})</span>
        <span className="inline-flex items-center gap-1.5"><span aria-hidden className="h-2.5 w-2.5 rounded-sm" style={{ background: passed, opacity: 0.55 }} />Passed checks (latest {last.passed})</span>
      </div>
      {note && <figcaption className="text-xs text-muted">{note}</figcaption>}
      <details className="text-xs">
        <summary className="cursor-pointer text-muted hover:text-text">Show trend as a table</summary>
        <div className="mt-2 max-h-56 overflow-auto">
          <table className="w-full text-xs tabular-nums">
            <thead><tr className="text-left text-muted"><th className="py-1 pr-3 font-medium">Day</th><th className="pr-3 font-medium">Failed</th><th className="pr-3 font-medium">Passed</th><th className="pr-3 font-medium">Not assessable</th><th className="font-medium">Excepted</th></tr></thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.day} className="border-t border-border"><td className="py-1 pr-3">{p.day}</td><td className="pr-3">{p.failed}</td><td className="pr-3">{p.passed}</td><td className="pr-3">{p.unknown}</td><td>{p.excepted}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  )
}

/* ── Results explorer ──────────────────────────────────────────────────── */

function ResultsExplorer({ filters, onFilters, onException }: {
  filters: ResultFilters; onFilters: Dispatch<SetStateAction<ResultFilters>>; onException: (t: ExceptionTarget) => void
}) {
  const can = useCan()
  const canExcept = can('ncm.compliance')
  const policies = usePolicies()
  const [search, setSearch] = useState(filters.q)
  const filterKey = JSON.stringify(filters)
  // Page is keyed to the filter set so a filter change never queries a stale offset.
  const [pageState, setPageState] = useState({ key: filterKey, page: 1 })
  const page = pageState.key === filterKey ? pageState.page : 1
  const setPage = (p: number) => setPageState({ key: filterKey, page: p })
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  useEffect(() => { setSearch(filters.q) }, [filters.q])
  useEffect(() => {
    const t = setTimeout(() => onFilters((f) => (f.q === search ? f : { ...f, q: search })), 300)
    return () => clearTimeout(t)
  }, [search]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setExpanded(new Set()) }, [filterKey])

  const params = {
    policy_id: filters.policy !== 'all' ? filters.policy : undefined,
    status: filters.statuses.length ? filters.statuses.join(',') : undefined,
    severity: filters.severity !== 'all' ? filters.severity : undefined,
    q: filters.q.trim() || undefined,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  }
  const results = useQuery<{ data: ResultRow[]; total: number }>({
    queryKey: ['ncm', 'compliance', 'results', params],
    queryFn: async () => (await api.get('/ncm/compliance/results', { params })).data,
    placeholderData: keepPreviousData,
  })
  const rows = results.data?.data ?? []
  const total = results.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const toggleStatus = (s: Status) => {
    const next = filters.statuses.includes(s) ? filters.statuses.filter((x) => x !== s) : [...filters.statuses, s]
    onFilters({ ...filters, statuses: STATUSES.filter((x) => next.includes(x)) })
  }
  const toggleRow = (key: string) => setExpanded((prev) => {
    const next = new Set(prev)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })
  const exportCsv = () => {
    const header = ['Device', 'IP', 'Rule', 'Rule key', 'Policy', 'Severity', 'Status', 'Detail', 'Evidence', 'Remediation', 'Assessed (UTC)']
    const body = rows.map((r) => [
      r.hostname || r.device_id, r.ip, r.title, r.rule_key, r.policy_name, SEVERITY_LABEL[r.severity] || r.severity,
      STATUS_META[r.status]?.label || r.status, r.detail,
      evidenceOf(r.evidence).map((e) => `L${e.line}: ${e.text}`).join(' | '), r.remediation, absoluteUtc(r.assessed_at),
    ])
    download(`compliance-results-${utcDate(0)}-p${page}.csv`, toCsv([header, ...body]), 'text/csv')
  }
  const filtersActive = filterKey !== JSON.stringify(DEFAULT_FILTERS)

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="w-full sm:w-56">
          <Select value={filters.policy} onValueChange={(v) => onFilters({ ...filters, policy: v })}>
            <SelectTrigger aria-label="Filter by policy"><SelectValue placeholder="All policies" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All policies</SelectItem>
              {(policies.data ?? []).map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div className="w-full sm:w-40">
          <Select value={filters.severity} onValueChange={(v) => onFilters({ ...filters, severity: v })}>
            <SelectTrigger aria-label="Filter by severity"><SelectValue placeholder="Any severity" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Any severity</SelectItem>
              {SEVERITIES.map((s) => <SelectItem key={s} value={s}>{SEVERITY_LABEL[s]}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search device or rule" aria-label="Search device or rule" className="pl-8" />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted" id="status-filter-label">Status:</span>
        <div role="group" aria-labelledby="status-filter-label" className="flex flex-wrap gap-1.5">
          {STATUSES.map((s) => (
            <Toggle key={s} pressed={filters.statuses.includes(s)} onClick={() => toggleStatus(s)}>
              <ToneIcon tone={STATUS_META[s].tone} className={filters.statuses.includes(s) ? 'text-current' : ''} />{STATUS_META[s].label}
            </Toggle>
          ))}
        </div>
        {!filters.statuses.length && <span className="text-xs text-muted">(none selected: all statuses shown)</span>}
        <div className="ml-auto flex flex-wrap gap-2">
          {filtersActive && <Button size="sm" variant="ghost" onClick={() => onFilters(DEFAULT_FILTERS)}>Reset filters</Button>}
          <Button size="sm" variant="outline" onClick={exportCsv} disabled={!rows.length}><Download aria-hidden /> Export CSV</Button>
        </div>
      </div>

      {results.isLoading ? <Loading what="compliance results" />
        : results.isError ? <SectionError what="Compliance results" onRetry={() => results.refetch()} />
        : !rows.length ? (
          <EmptyState>
            {filtersActive ? 'No compliance results match these filters.' : 'No failing or unassessable rule results. Use the status toggles to include passed and excepted results.'}
          </EmptyState>
        ) : (
          <div className={cn('min-w-0 space-y-2', results.isFetching && 'opacity-70')} aria-busy={results.isFetching}>
            {results.isPlaceholderData && <p role="status" className="text-xs text-muted">Loading page {page}…</p>}
            <div className="min-w-0 overflow-x-auto rounded-md border border-border">
              <Table className="min-w-[900px]">
                <THead>
                  <Tr>
                    <Th>Device</Th><Th>Rule</Th><Th>Policy</Th><Th>Severity</Th><Th>Status</Th><Th>Evidence</Th><Th>Assessed</Th>
                    {canExcept && <Th><span className="sr-only">Actions</span></Th>}
                  </Tr>
                </THead>
                <TBody>
                  {rows.map((r) => {
                    const key = `${r.device_id}:${r.rule_id}`
                    const open = expanded.has(key)
                    const ev = evidenceOf(r.evidence)
                    return (
                      <ResultRowView key={key} row={r} open={open} evidence={ev} onToggle={() => toggleRow(key)}
                        canExcept={canExcept} onException={onException} />
                    )
                  })}
                </TBody>
              </Table>
            </div>
            <Pagination page={page} pageCount={pageCount} onPage={setPage} total={total} pageSize={PAGE_SIZE} label="results" />
          </div>
        )}
    </div>
  )
}

function ResultRowView({ row: r, open, evidence, onToggle, canExcept, onException }: {
  row: ResultRow; open: boolean; evidence: Evidence[]; onToggle: () => void; canExcept: boolean; onException: (t: ExceptionTarget) => void
}) {
  const panelId = `ev-${r.device_id}-${r.rule_id}`
  return (
    <>
      <Tr>
        <Td className="max-w-[12rem]">
          <Link to={`/ncm/${r.device_id}?tab=compliance`} className="block truncate font-medium text-primary hover:underline">{r.hostname || r.device_id}</Link>
          {r.ip && <div className="text-xs text-muted">{r.ip}</div>}
        </Td>
        <Td className="max-w-[16rem]"><div className="break-words">{r.title}</div>{r.check_type && <div className="text-xs text-muted">{CHECK_LABEL[r.check_type]}</div>}</Td>
        <Td className="max-w-[12rem] text-xs"><span className="break-words">{r.policy_name}</span></Td>
        <Td><SeverityBadge severity={r.severity} /></Td>
        <Td><StatusBadge status={r.status} /></Td>
        <Td>
          <Button size="sm" variant="ghost" onClick={onToggle} aria-expanded={open} aria-controls={panelId}
            aria-label={`${open ? 'Hide' : 'Show'} evidence and remediation for ${r.title} on ${r.hostname || 'device'}`}>
            {open ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />}
            {evidence.length ? `${evidence.length} line${evidence.length === 1 ? '' : 's'}` : 'Details'}
          </Button>
        </Td>
        <Td className="whitespace-nowrap text-xs"><Ago iso={r.assessed_at} /></Td>
        {canExcept && (
          <Td className="text-right">
            {(r.status === 'fail' || r.status === 'unknown') && (
              <Button size="sm" variant="outline" onClick={() => onException({ rule_id: r.rule_id, device_id: r.device_id, hostname: r.hostname, title: r.title, policy_name: r.policy_name })}>
                <ShieldOff aria-hidden /> Add exception
              </Button>
            )}
          </Td>
        )}
      </Tr>
      {open && (
        <Tr className="hover:bg-transparent">
          <Td colSpan={canExcept ? 8 : 7} id={panelId} className="bg-surface2/40">
            <div className="max-w-4xl space-y-2">
              {r.detail && <p className="text-sm">{r.detail}</p>}
              <EvidenceLines evidence={evidence} emptyText={r.status === 'fail' && r.check_type === 'must_match'
                ? 'Required configuration was not found, so there is no line to show.' : undefined} />
              <Remediation text={r.remediation} />
              <p className="text-xs text-muted">Assessed {absoluteUtc(r.assessed_at)}{r.rule_key ? ` · rule ${r.rule_key}` : ''}</p>
            </div>
          </Td>
        </Tr>
      )}
    </>
  )
}

/* ── Policies ──────────────────────────────────────────────────────────── */

function PoliciesTab({ onResults }: { onResults: (policyId: string) => void }) {
  const can = useCan()
  const canEdit = can('ncm.compliance')
  const qc = useQueryClient()
  const policies = usePolicies()
  const summary = useSummary()
  const platforms = usePlatformOptions()
  const [viewing, setViewing] = useState<Policy | null>(null)
  const [editing, setEditing] = useState<Policy | 'new' | null>(null)
  const [disabling, setDisabling] = useState<Policy | null>(null)
  const [deleting, setDeleting] = useState<Policy | null>(null)

  const platformLabel = useMemo(() => new Map((platforms.data ?? []).map((p) => [p.value, p.label])), [platforms.data])
  const counts = useMemo(() => new Map((summary.data?.policies ?? []).map((p) => [p.id, p])), [summary.data])
  const invalidate = () => qc.invalidateQueries({ queryKey: ['ncm', 'compliance'] })

  const setEnabled = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => (await api.put(`/ncm/compliance/policies/${id}/enabled`, { enabled })).data,
    onSuccess: (_d, v) => {
      toast.success(v.enabled ? 'Policy enabled' : 'Policy disabled', v.enabled ? 'It is applied at the next evaluation. Use "Evaluate now" to assess immediately.' : 'Its results were removed.')
      setDisabling(null)
      invalidate()
    },
    onError: (e) => toast.error('Could not change the policy', ncmError(e)),
  })
  const clone = useMutation({
    mutationFn: async (p: Policy) => (await api.post(`/ncm/compliance/policies/${p.id}/clone`)).data,
    onSuccess: (_d, p) => { toast.success('Policy cloned', `"${p.name} (custom)" was created disabled. Review and enable it.`); setViewing(null); invalidate() },
    onError: (e) => toast.error('Could not clone the policy', ncmError(e)),
  })
  const remove = useMutation({
    mutationFn: async (p: Policy) => (await api.delete(`/ncm/compliance/policies/${p.id}`)).data,
    onSuccess: () => { toast.success('Policy deleted'); setDeleting(null); invalidate() },
    onError: (e) => toast.error('Could not delete the policy', ncmError(e)),
  })

  const platformText = (list?: string[] | null) => {
    if (!list?.length) return 'All platforms'
    const names = list.map((v) => platformLabel.get(v) || v)
    return names.length > 3 ? `${names.slice(0, 3).join(', ')} +${names.length - 3}` : names.join(', ')
  }

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">Built-in packs are starter hardening checks versioned with the product; clone one to customise it.</p>
        {canEdit && <Button size="sm" onClick={() => setEditing('new')}><Plus aria-hidden /> New policy</Button>}
      </div>
      {summary.isError && <SectionError what="Policy result counts" onRetry={() => summary.refetch()} />}
      {policies.isLoading ? <Loading what="policies" />
        : policies.isError ? <SectionError what="Compliance policies" onRetry={() => policies.refetch()} />
        : !policies.data?.length ? <EmptyState>No policies exist yet.</EmptyState>
        : (
          <div className="min-w-0 overflow-x-auto rounded-md border border-border">
            <Table className="min-w-[860px]">
              <THead>
                <Tr><Th>Policy</Th><Th>Platforms</Th><Th>Rules</Th><Th>Results</Th><Th>Assessed</Th><Th>Enabled</Th><Th><span className="sr-only">Actions</span></Th></Tr>
              </THead>
              <TBody>
                {policies.data.map((p) => {
                  const c = counts.get(p.id)
                  const enabledRules = p.rules.filter((r) => r.enabled).length
                  return (
                    <Tr key={p.id}>
                      <Td className="max-w-[18rem]">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium">{p.name}</span>
                          {p.builtin ? <Badge variant="info">Built-in</Badge> : <Badge variant="outline">Custom</Badge>}
                          <span className="text-xs text-muted">v{p.version}</span>
                        </div>
                        {p.description && <div className="mt-0.5 line-clamp-2 text-xs text-muted">{p.description}</div>}
                      </Td>
                      <Td className="max-w-[12rem] text-xs" title={(p.platforms ?? []).map((v) => platformLabel.get(v) || v).join(', ')}>{platformText(p.platforms)}</Td>
                      <Td className="whitespace-nowrap tabular-nums text-xs">{enabledRules}{enabledRules !== p.rules.length ? ` of ${p.rules.length}` : ''} enabled</Td>
                      <Td className="text-xs">
                        {summary.isLoading ? <span className="text-muted">Loading…</span>
                          : !c ? <span className="text-muted">—</span>
                          : !p.enabled ? <span className="text-muted">Not evaluated (disabled)</span>
                          : (c.passed + c.failed + c.unknown + c.excepted) === 0 ? <span className="text-muted">No results yet</span>
                          : (
                            <button type="button" onClick={() => onResults(p.id)} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded text-left hover:underline"
                              aria-label={`${p.name}: ${c.failed} failed, ${c.unknown} not assessable, ${c.excepted} excepted, ${c.passed} passed on ${c.devices} devices. Show results.`}>
                              <span className={cn('inline-flex items-center gap-1', c.failed && 'text-danger')}><ToneIcon tone={c.failed ? 'danger' : 'neutral'} />{c.failed} fail</span>
                              <span className={cn('inline-flex items-center gap-1', c.unknown && 'text-warning')}><ToneIcon tone={c.unknown ? 'warning' : 'neutral'} />{c.unknown} unknown</span>
                              <span className="inline-flex items-center gap-1"><ToneIcon tone="neutral" />{c.excepted} excepted</span>
                              <span className="inline-flex items-center gap-1"><ToneIcon tone={c.passed ? 'success' : 'neutral'} />{c.passed} pass</span>
                              <span className="text-muted">· {c.devices} device{c.devices === 1 ? '' : 's'}</span>
                            </button>
                          )}
                      </Td>
                      <Td className="whitespace-nowrap text-xs"><Ago iso={c?.assessed_at} /></Td>
                      <Td>
                        <Switch checked={p.enabled} aria-label={`${p.enabled ? 'Disable' : 'Enable'} policy ${p.name}`}
                          disabled={!canEdit || setEnabled.isPending} title={canEdit ? undefined : 'Requires the ncm.compliance permission'}
                          onCheckedChange={(v) => (v ? setEnabled.mutate({ id: p.id, enabled: true }) : setDisabling(p))} />
                      </Td>
                      <Td>
                        <div className="flex justify-end gap-1">
                          <Button size="sm" variant="ghost" onClick={() => setViewing(p)} aria-label={`View rules of ${p.name}`}><Eye aria-hidden /> Rules</Button>
                          {canEdit && p.builtin && (
                            <Button size="icon" variant="ghost" className="h-8 w-8" aria-label={`Clone ${p.name}`} title="Clone as custom policy"
                              disabled={clone.isPending} onClick={() => clone.mutate(p)}><Copy aria-hidden /></Button>
                          )}
                          {canEdit && !p.builtin && (
                            <>
                              <Button size="icon" variant="ghost" className="h-8 w-8" aria-label={`Edit ${p.name}`} title="Edit" onClick={() => setEditing(p)}><Pencil aria-hidden /></Button>
                              <Button size="icon" variant="ghost" className="h-8 w-8 text-danger" aria-label={`Delete ${p.name}`} title="Delete" onClick={() => setDeleting(p)}><Trash2 aria-hidden /></Button>
                            </>
                          )}
                        </div>
                      </Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          </div>
        )}

      {viewing && (
        <PolicyViewer policy={viewing} platformText={platformText} canEdit={canEdit} cloning={clone.isPending}
          onClose={() => setViewing(null)} onClone={() => clone.mutate(viewing)}
          onEdit={() => { setEditing(viewing); setViewing(null) }} />
      )}
      {editing && <PolicyEditor policy={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
      <ConfirmDialog open={!!disabling} onOpenChange={(o) => !o && setDisabling(null)} title="Disable policy?"
        description={<>Disabling <strong>{disabling?.name}</strong> stops it from being evaluated and removes its current results from the compliance views. Exceptions are kept.</>}
        confirmText="Disable" destructive loading={setEnabled.isPending}
        onConfirm={() => { if (disabling) setEnabled.mutate({ id: disabling.id, enabled: false }) }} />
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} title="Delete policy?"
        description={<>Delete <strong>{deleting?.name}</strong> and its {deleting?.rules.length ?? 0} rule(s)? Its results and exceptions are removed. This cannot be undone.</>}
        confirmText="Delete" destructive loading={remove.isPending} onConfirm={() => { if (deleting) remove.mutate(deleting) }} />
    </div>
  )
}

function PolicyViewer({ policy, platformText, canEdit, cloning, onClose, onClone, onEdit }: {
  policy: Policy; platformText: (l?: string[] | null) => string; canEdit: boolean; cloning: boolean
  onClose: () => void; onClone: () => void; onEdit: () => void
}) {
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] w-[calc(100vw-1rem)] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2 pr-6">
            {policy.name}{policy.builtin ? <Badge variant="info">Built-in · read-only</Badge> : <Badge variant="outline">Custom</Badge>}
          </DialogTitle>
          <DialogDescription>
            Version {policy.version} · {platformText(policy.platforms)} · {policy.enabled ? 'Enabled' : 'Disabled'}
            {policy.description ? <><br />{policy.description}</> : null}
          </DialogDescription>
        </DialogHeader>
        {!policy.rules.length ? <EmptyState>This policy has no rules.</EmptyState> : (
          <ol className="space-y-2">
            {policy.rules.map((r, i) => (
              <li key={r.id} className="space-y-1.5 rounded-md border border-border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs tabular-nums text-muted">{i + 1}.</span>
                  <SeverityBadge severity={r.severity} />
                  <span className="min-w-0 flex-1 break-words text-sm font-medium">{r.title}</span>
                  {!r.enabled && <Badge variant="outline">Rule disabled</Badge>}
                </div>
                {r.description && <p className="text-xs text-muted">{r.description}</p>}
                <dl className="grid gap-1 text-xs sm:grid-cols-[8rem_minmax(0,1fr)]">
                  <dt className="text-muted">Check</dt>
                  <dd>{CHECK_LABEL[r.check_type] || r.check_type} — <span className="text-muted">{CHECK_TYPES.find((c) => c.value === r.check_type)?.help}</span></dd>
                  {isSection(r.check_type) && <><dt className="text-muted">Section pattern</dt><dd><code className="break-all rounded bg-surface2 px-1 py-0.5 font-mono">{r.section_pattern}</code></dd></>}
                  <dt className="text-muted">Pattern</dt>
                  <dd><code className="break-all rounded bg-surface2 px-1 py-0.5 font-mono">{r.pattern}</code></dd>
                </dl>
                <Remediation text={r.remediation} />
              </li>
            ))}
          </ol>
        )}
        <DialogFooter className="flex-wrap">
          <Button variant="outline" onClick={onClose}>Close</Button>
          {canEdit && policy.builtin && <Button onClick={onClone} disabled={cloning}><Copy aria-hidden /> Clone as custom</Button>}
          {canEdit && !policy.builtin && <Button onClick={onEdit}><Pencil aria-hidden /> Edit</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Policy editor ─────────────────────────────────────────────────────── */

interface RuleDraft {
  _k: string
  rule_key?: string | null
  title: string
  description: string
  severity: Severity
  check_type: CheckType
  pattern: string
  section_pattern: string
  platforms: string[] | null
  remediation: string
  enabled: boolean
}

let draftSeq = 0
const nextKey = () => `r${++draftSeq}`

function draftOf(r?: Rule): RuleDraft {
  return {
    _k: nextKey(),
    rule_key: r?.rule_key ?? null,
    title: r?.title ?? '',
    description: r?.description ?? '',
    severity: r?.severity ?? 'medium',
    check_type: r?.check_type ?? 'must_match',
    pattern: r?.pattern ?? '',
    section_pattern: r?.section_pattern ?? '',
    platforms: r?.platforms?.length ? r.platforms : null,
    remediation: r?.remediation ?? '',
    enabled: r?.enabled ?? true,
  }
}

function ruleIn(d: RuleDraft, forTest = false) {
  const title = d.title.trim()
  return {
    rule_key: d.rule_key || undefined,
    title: forTest && title.length < 3 ? 'Rule under test' : title,
    description: d.description.trim() || null,
    severity: d.severity,
    check_type: d.check_type,
    pattern: d.pattern,
    section_pattern: isSection(d.check_type) ? d.section_pattern || null : null,
    platforms: d.platforms?.length ? d.platforms : null,
    remediation: d.remediation.trim() || null,
    enabled: d.enabled,
  }
}

function ruleIssues(d: RuleDraft): string[] {
  const out: string[] = []
  if (d.title.trim().length < 3) out.push('title must be at least 3 characters')
  if (!d.pattern.trim()) out.push('pattern is required')
  if (isSection(d.check_type) && !d.section_pattern.trim()) out.push('section pattern is required for section checks')
  return out
}

function PolicyEditor({ policy, onClose }: { policy: Policy | null; onClose: () => void }) {
  const qc = useQueryClient()
  const platforms = usePlatformOptions()
  const [name, setName] = useState(policy?.name ?? '')
  const [description, setDescription] = useState(policy?.description ?? '')
  const [selected, setSelected] = useState<string[]>(policy?.platforms ?? [])
  const [enabled, setEnabled] = useState(policy?.enabled ?? false)
  const [rules, setRules] = useState<RuleDraft[]>(() => (policy?.rules ?? []).map((r) => draftOf(r)))
  const [active, setActive] = useState<string | null>(null)
  const [removing, setRemoving] = useState<RuleDraft | null>(null)
  const [submitted, setSubmitted] = useState(false)

  const issues = useMemo(() => {
    const out: string[] = []
    if (name.trim().length < 3) out.push('Policy name must be at least 3 characters.')
    rules.forEach((r, i) => ruleIssues(r).forEach((m) => out.push(`Rule ${i + 1}${r.title.trim() ? ` ("${r.title.trim()}")` : ''}: ${m}.`)))
    return out
  }, [name, rules])

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: name.trim(), description: description.trim() || null, platforms: selected.length ? selected : null,
        enabled, rules: rules.map((r) => ruleIn(r)),
      }
      return policy ? (await api.put(`/ncm/compliance/policies/${policy.id}`, body)).data : (await api.post('/ncm/compliance/policies', body)).data
    },
    onSuccess: () => {
      toast.success(policy ? 'Policy saved' : 'Policy created', enabled ? 'Changes apply at the next evaluation. Use "Evaluate now" to assess immediately.' : 'The policy is disabled; enable it to evaluate devices.')
      qc.invalidateQueries({ queryKey: ['ncm', 'compliance'] })
      onClose()
    },
    onError: (e) => toast.error('Could not save the policy', ncmError(e)),
  })

  const update = (k: string, patch: Partial<RuleDraft>) => setRules((rs) => rs.map((r) => (r._k === k ? { ...r, ...patch } : r)))
  const addRule = () => {
    const d = draftOf()
    setRules((rs) => [...rs, d])
    setActive(d._k)
  }
  const togglePlatform = (v: string) => setSelected((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]))
  const current = rules.find((r) => r._k === active) || null

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-1rem)] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{policy ? `Edit policy: ${policy.name}` : 'New compliance policy'}</DialogTitle>
          <DialogDescription>
            Rules are regular expressions matched line by line against each device&apos;s latest validated running configuration.
            {policy ? ' Saving increments the policy version.' : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <FormField label="Name" required error={submitted && name.trim().length < 3 ? 'At least 3 characters.' : null}>
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
          </FormField>
          <FormField label="Enabled" hint="Enabled policies are evaluated for every device on a matching platform.">
            <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Policy enabled" />
          </FormField>
          <FormField label="Description" className="sm:col-span-2">
            <Textarea value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} rows={2} />
          </FormField>
        </div>

        <fieldset className="min-w-0 space-y-1.5">
          <legend className="text-xs font-medium uppercase tracking-wider text-muted">Platforms</legend>
          <p className="text-xs text-muted">Leave all unchecked to apply to every platform. {selected.length ? `${selected.length} selected.` : ''}</p>
          {platforms.isLoading ? <Loading what="platform drivers" />
            : platforms.isError ? <SectionError what="Platform drivers" onRetry={() => platforms.refetch()} />
            : (
              <div className="grid max-h-40 gap-1 overflow-y-auto rounded-md border border-border p-2 sm:grid-cols-2 lg:grid-cols-3">
                {(platforms.data ?? []).map((p) => (
                  <label key={p.value} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-surface2">
                    <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--primary))]" checked={selected.includes(p.value)} onChange={() => togglePlatform(p.value)} />
                    <span className="truncate">{p.label}</span>
                  </label>
                ))}
              </div>
            )}
        </fieldset>

        <section className="min-w-0 space-y-2" aria-label="Rules">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">Rules ({rules.length})</h3>
            <Button size="sm" variant="outline" onClick={addRule}><Plus aria-hidden /> Add rule</Button>
          </div>
          {!rules.length ? <EmptyState>No rules yet. Add a rule to start checking configurations.</EmptyState> : (
            <div className="min-w-0 overflow-x-auto rounded-md border border-border">
              <Table className="min-w-[640px]">
                <THead><Tr><Th className="w-8">#</Th><Th>Title</Th><Th>Severity</Th><Th>Check</Th><Th>Enabled</Th><Th><span className="sr-only">Actions</span></Th></Tr></THead>
                <TBody>
                  {rules.map((r, i) => {
                    const bad = ruleIssues(r).length > 0
                    return (
                      <Tr key={r._k} className={cn(active === r._k && 'bg-primary/5')}>
                        <Td className="text-xs tabular-nums text-muted">{i + 1}</Td>
                        <Td className="max-w-[16rem]">
                          <span className="break-words">{r.title.trim() || <span className="italic text-muted">Untitled rule</span>}</span>
                          {bad && <span className="ml-1 inline-flex items-center gap-1 text-xs text-warning"><ToneIcon tone="warning" />incomplete</span>}
                        </Td>
                        <Td><SeverityBadge severity={r.severity} /></Td>
                        <Td className="text-xs">{CHECK_LABEL[r.check_type]}</Td>
                        <Td><Switch checked={r.enabled} onCheckedChange={(v) => update(r._k, { enabled: v })} aria-label={`Rule ${i + 1} enabled`} /></Td>
                        <Td>
                          <div className="flex justify-end gap-1">
                            <Button size="sm" variant={active === r._k ? 'secondary' : 'ghost'} aria-expanded={active === r._k}
                              aria-label={`${active === r._k ? 'Close editor for' : 'Edit'} rule ${i + 1}`} onClick={() => setActive(active === r._k ? null : r._k)}>
                              <Pencil aria-hidden /> {active === r._k ? 'Close' : 'Edit'}
                            </Button>
                            <Button size="icon" variant="ghost" className="h-8 w-8 text-danger" aria-label={`Remove rule ${i + 1}`} onClick={() => setRemoving(r)}><Trash2 aria-hidden /></Button>
                          </div>
                        </Td>
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            </div>
          )}
          {current && (
            <RuleForm key={current._k} draft={current} index={rules.indexOf(current)} showErrors={submitted}
              onChange={(patch) => update(current._k, patch)} />
          )}
        </section>

        {submitted && issues.length > 0 && (
          <div role="alert" className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            <p className="font-medium">Fix these before saving:</p>
            <ul className="mt-1 list-disc pl-4">{issues.map((m) => <li key={m}>{m}</li>)}</ul>
          </div>
        )}

        <DialogFooter className="flex-wrap">
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button disabled={save.isPending} onClick={() => { setSubmitted(true); if (!issues.length) save.mutate() }}>
            {save.isPending ? 'Saving…' : policy ? 'Save policy' : 'Create policy'}
          </Button>
        </DialogFooter>
        <ConfirmDialog open={!!removing} onOpenChange={(o) => !o && setRemoving(null)} title="Remove rule?"
          description={<>Remove <strong>{removing?.title.trim() || 'this rule'}</strong> from the policy? Its results and exceptions are deleted when you save.</>}
          confirmText="Remove" destructive
          onConfirm={() => { if (removing) { setRules((rs) => rs.filter((r) => r._k !== removing._k)); if (active === removing._k) setActive(null) } setRemoving(null) }} />
      </DialogContent>
    </Dialog>
  )
}

function RuleForm({ draft, index, showErrors, onChange }: { draft: RuleDraft; index: number; showErrors: boolean; onChange: (p: Partial<RuleDraft>) => void }) {
  const section = isSection(draft.check_type)
  const help = CHECK_TYPES.find((c) => c.value === draft.check_type)?.help
  return (
    <div className="min-w-0 space-y-3 rounded-md border border-primary/40 p-3">
      <h4 className="text-sm font-semibold">Rule {index + 1}</h4>
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        <FormField label="Title" required className="sm:col-span-2" error={showErrors && draft.title.trim().length < 3 ? 'At least 3 characters.' : null}>
          <Input value={draft.title} onChange={(e) => onChange({ title: e.target.value })} maxLength={200} />
        </FormField>
        <FormField label="Severity">
          <Select value={draft.severity} onValueChange={(v) => onChange({ severity: v as Severity })}>
            <SelectTrigger aria-label="Severity"><SelectValue /></SelectTrigger>
            <SelectContent>{SEVERITIES.map((s) => <SelectItem key={s} value={s}>{SEVERITY_LABEL[s]}</SelectItem>)}</SelectContent>
          </Select>
        </FormField>
        <FormField label="Check type" hint={help}>
          <Select value={draft.check_type} onValueChange={(v) => onChange({ check_type: v as CheckType })}>
            <SelectTrigger aria-label="Check type"><SelectValue /></SelectTrigger>
            <SelectContent>{CHECK_TYPES.map((c) => <SelectItem key={c.value} value={c.value}>{c.label}</SelectItem>)}</SelectContent>
          </Select>
        </FormField>
        <details className="text-xs sm:col-span-2">
          <summary className="cursor-pointer text-muted hover:text-text">How each check type works</summary>
          <ul className="mt-1 space-y-0.5 pl-1">
            {CHECK_TYPES.map((c) => <li key={c.value}><span className="font-medium">{c.label}:</span> <span className="text-muted">{c.help}</span></li>)}
            <li className="text-muted">A section is a header line plus the lines indented beneath it (IOS/EOS blocks, FortiOS config/edit blocks).</li>
          </ul>
        </details>
        {section && (
          <FormField label="Section pattern" required className="sm:col-span-2" hint="Regular expression matched against section header lines, e.g. ^line vty\b"
            error={showErrors && !draft.section_pattern.trim() ? 'Required for section checks.' : null}>
            <Input value={draft.section_pattern} onChange={(e) => onChange({ section_pattern: e.target.value })} maxLength={300} className="font-mono" spellCheck={false} />
          </FormField>
        )}
        <FormField label="Pattern" required className="sm:col-span-2"
          hint={section ? 'Regular expression matched against lines inside each matching section, e.g. ^\\s+transport input ssh\\b' : 'Regular expression matched against each configuration line, e.g. ^ip ssh version 2\\b'}
          error={showErrors && !draft.pattern.trim() ? 'Required.' : null}>
          <Input value={draft.pattern} onChange={(e) => onChange({ pattern: e.target.value })} maxLength={300} className="font-mono" spellCheck={false} />
        </FormField>
        <FormField label="Remediation" className="sm:col-span-2" hint="What an engineer should change when this rule fails.">
          <Textarea value={draft.remediation} onChange={(e) => onChange({ remediation: e.target.value })} maxLength={2000} rows={2} />
        </FormField>
        <FormField label="Description" className="sm:col-span-2">
          <Textarea value={draft.description} onChange={(e) => onChange({ description: e.target.value })} maxLength={2000} rows={2} />
        </FormField>
        <FormField label="Rule enabled">
          <Switch checked={draft.enabled} onCheckedChange={(v) => onChange({ enabled: v })} aria-label="Rule enabled" />
        </FormField>
      </div>
      <RuleTester draft={draft} />
    </div>
  )
}

function RuleTester({ draft }: { draft: RuleDraft }) {
  const devices = useEnrolledDevices()
  const [deviceId, setDeviceId] = useState('')
  const test = useMutation({
    mutationFn: async () => (await api.post('/ncm/compliance/rules/test', { device_id: deviceId, rule: ruleIn(draft, true) })).data as {
      status: Status; detail?: string; evidence?: unknown; captured_at?: string
    },
    onError: (e) => toast.error('Rule test failed', ncmError(e)),
  })
  const missing = !draft.pattern.trim() ? 'Enter a pattern first.' : isSection(draft.check_type) && !draft.section_pattern.trim() ? 'Enter a section pattern first.' : null
  const result = test.data
  const hostname = devices.data?.find((d) => d.device_id === deviceId)?.hostname
  return (
    <div className="min-w-0 space-y-2 rounded-md bg-surface2/50 p-3">
      <div className="flex items-center gap-2 text-sm font-medium"><FlaskConical className="h-4 w-4" aria-hidden /> Test rule</div>
      <p className="text-xs text-muted">Dry-runs this rule (unsaved values) against a device&apos;s latest validated running configuration. Nothing is stored.</p>
      {devices.isLoading ? <Loading what="enrolled devices" />
        : devices.isError ? <SectionError what="Enrolled devices" onRetry={() => devices.refetch()} />
        : !devices.data?.length ? <p className="text-xs text-muted">No enrolled devices are available to test against.</p>
        : (
          <div className="flex flex-wrap items-center gap-2">
            <div className="w-full sm:w-72">
              <Select value={deviceId} onValueChange={(v) => { setDeviceId(v); test.reset() }}>
                <SelectTrigger aria-label="Device to test against"><SelectValue placeholder="Choose an enrolled device" /></SelectTrigger>
                <SelectContent>{devices.data.map((d) => <SelectItem key={d.device_id} value={d.device_id}>{d.hostname}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <Button size="sm" variant="outline" disabled={!deviceId || !!missing || test.isPending} onClick={() => test.mutate()}>
              {test.isPending ? <RefreshCw className="animate-spin" aria-hidden /> : <Play aria-hidden />} {test.isPending ? 'Testing…' : 'Run test'}
            </Button>
            {missing && <span className="text-xs text-muted">{missing}</span>}
          </div>
        )}
      {test.isError && <p role="alert" className="text-xs text-danger">{ncmError(test.error)}</p>}
      {result && (
        <div className="min-w-0 space-y-1.5" aria-live="polite">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge status={result.status} />
            <span className="min-w-0 break-words">{result.detail}</span>
          </div>
          {result.captured_at && <p className="text-xs text-muted">Tested on {hostname || 'device'} against the configuration captured <Ago iso={result.captured_at} /> ({absoluteUtc(result.captured_at)}).</p>}
          <EvidenceLines evidence={evidenceOf(result.evidence)} emptyText="No matching lines to show for this outcome." />
        </div>
      )}
    </div>
  )
}

/* ── Exceptions ────────────────────────────────────────────────────────── */

function ExceptionsTab() {
  const can = useCan()
  const canRevoke = can('ncm.compliance')
  const qc = useQueryClient()
  const [showInactive, setShowInactive] = useState(false)
  const [revoking, setRevoking] = useState<ExceptionRow | null>(null)
  const list = useQuery<ExceptionRow[]>({
    queryKey: ['ncm', 'compliance', 'exceptions', 'all'],
    queryFn: async () => (await api.get('/ncm/compliance/exceptions', { params: { include_inactive: true } })).data?.data ?? [],
  })
  const revoke = useMutation({
    mutationFn: async (x: ExceptionRow) => (await api.post(`/ncm/compliance/exceptions/${x.id}/revoke`)).data,
    onSuccess: () => {
      toast.success('Exception revoked', 'Affected devices were re-evaluated.')
      setRevoking(null)
      qc.invalidateQueries({ queryKey: ['ncm'] })
    },
    onError: (e) => toast.error('Could not revoke the exception', ncmError(e)),
  })
  const all = list.data ?? []
  const rows = showInactive ? all : all.filter((x) => x.state === 'active' || x.state === 'expiring')
  const inactive = all.length - all.filter((x) => x.state === 'active' || x.state === 'expiring').length

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted">Exceptions mark a failing or unassessable rule as accepted risk until they expire (at most one year). Add them from a result row.</p>
        <div role="group" aria-label="Exceptions shown" className="flex gap-1.5">
          <Toggle pressed={!showInactive} onClick={() => setShowInactive(false)}>Active</Toggle>
          <Toggle pressed={showInactive} onClick={() => setShowInactive(true)}>All{list.data ? ` (incl. ${inactive} inactive)` : ''}</Toggle>
        </div>
      </div>
      {list.isLoading ? <Loading what="exceptions" />
        : list.isError ? <SectionError what="Compliance exceptions" onRetry={() => list.refetch()} />
        : !rows.length ? <EmptyState>{showInactive ? 'No exceptions have been recorded.' : 'No active exceptions.'}</EmptyState>
        : (
          <div className="min-w-0 overflow-x-auto rounded-md border border-border">
            <Table className="min-w-[900px]">
              <THead><Tr><Th>State</Th><Th>Rule</Th><Th>Scope</Th><Th>Owner</Th><Th>Reason</Th><Th>Expires</Th><Th>Created</Th>{canRevoke && <Th><span className="sr-only">Actions</span></Th>}</Tr></THead>
              <TBody>
                {rows.map((x) => {
                  const st = EXCEPTION_STATE[x.state] || EXCEPTION_STATE.active
                  const live = x.state === 'active' || x.state === 'expiring'
                  return (
                    <Tr key={x.id}>
                      <Td><Badge variant={TONE_BADGE[st.tone]} className="whitespace-nowrap"><ToneIcon tone={st.tone} className={st.tone === 'neutral' ? '' : 'text-current'} />{st.label}</Badge></Td>
                      <Td className="max-w-[16rem]">
                        <div className="break-words">{x.title}</div>
                        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted"><SeverityBadge severity={x.severity} />{x.policy_name}</div>
                      </Td>
                      <Td className="text-xs">
                        {x.device_id
                          ? <Link to={`/ncm/${x.device_id}?tab=compliance`} className="text-primary hover:underline">{x.hostname || x.device_id}</Link>
                          : <span className="font-medium">All devices</span>}
                      </Td>
                      <Td className="text-xs">{x.owner}</Td>
                      <Td className="max-w-[18rem] text-xs"><span className="line-clamp-3 break-words" title={x.reason}>{x.reason}</span></Td>
                      <Td className="whitespace-nowrap text-xs">
                        <div><Relative iso={x.expires_at} /></div>
                        <div className="text-muted">{absoluteUtc(x.expires_at)}</div>
                      </Td>
                      <Td className="whitespace-nowrap text-xs">
                        <div>{x.created_by || '—'}</div>
                        <div className="text-muted"><Ago iso={x.created_at} /></div>
                        {x.revoked_at && <div className="text-muted">Revoked <Ago iso={x.revoked_at} />{x.revoked_by ? ` by ${x.revoked_by}` : ''}</div>}
                      </Td>
                      {canRevoke && (
                        <Td className="text-right">
                          {live && <Button size="sm" variant="outline" onClick={() => setRevoking(x)}><Undo2 aria-hidden /> Revoke</Button>}
                        </Td>
                      )}
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          </div>
        )}
      <ConfirmDialog open={!!revoking} onOpenChange={(o) => !o && setRevoking(null)} title="Revoke exception?"
        description={<>Revoke the exception for <strong>{revoking?.title}</strong> on {revoking?.device_id ? <strong>{revoking?.hostname || 'this device'}</strong> : <strong>all devices</strong>}? The rule is re-evaluated immediately and failures will count again.</>}
        confirmText="Revoke" destructive loading={revoke.isPending} onConfirm={() => { if (revoking) revoke.mutate(revoking) }} />
    </div>
  )
}

function ExceptionDialog({ target, onClose, allowFleet = true }: { target: ExceptionTarget; onClose: () => void; allowFleet?: boolean }) {
  const qc = useQueryClient()
  const username = useAuth((s) => s.user?.username)
  const minDate = utcDate(1)
  const maxDate = utcDate(365)
  const [owner, setOwner] = useState(username || '')
  const [reason, setReason] = useState('')
  const [expiry, setExpiry] = useState(utcDate(90))
  const [scope, setScope] = useState<'device' | 'all'>('device')
  const [submitted, setSubmitted] = useState(false)

  const errors = {
    owner: owner.trim().length < 2 ? 'Enter an owner (at least 2 characters).' : null,
    reason: reason.trim().length < 5 ? 'Explain why (at least 5 characters).' : null,
    expiry: !expiry ? 'Choose an expiry date.' : expiry < minDate ? 'Expiry must be in the future.' : expiry > maxDate ? 'Exceptions may last at most one year.' : null,
  }
  const valid = !errors.owner && !errors.reason && !errors.expiry

  const create = useMutation({
    mutationFn: async () => (await api.post('/ncm/compliance/exceptions', {
      rule_id: target.rule_id,
      device_id: scope === 'device' ? target.device_id : null,
      owner: owner.trim(),
      reason: reason.trim(),
      expires_at: `${expiry}T23:59:59Z`,
    })).data,
    onSuccess: () => {
      toast.success('Exception added', scope === 'device' ? `${target.hostname || 'The device'} was re-evaluated.` : 'All devices were re-evaluated.')
      qc.invalidateQueries({ queryKey: ['ncm'] })
      onClose()
    },
    onError: (e) => toast.error('Could not add the exception', ncmError(e)),
  })

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] w-[calc(100vw-1rem)] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Add compliance exception</DialogTitle>
          <DialogDescription>
            <strong>{target.title}</strong>{target.policy_name ? ` (${target.policy_name})` : ''} will count as excepted instead of failing until the expiry date. The exception is audited.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <fieldset className="space-y-1.5">
            <legend className="text-xs font-medium uppercase tracking-wider text-muted">Scope</legend>
            <label className="flex cursor-pointer items-start gap-2 text-sm">
              <input type="radio" name="exception-scope" className="mt-0.5 h-4 w-4" checked={scope === 'device'} onChange={() => setScope('device')} />
              <span>This device only <span className="text-muted">({target.hostname || target.device_id})</span></span>
            </label>
            {allowFleet && (
              <label className="flex cursor-pointer items-start gap-2 text-sm">
                <input type="radio" name="exception-scope" className="mt-0.5 h-4 w-4" checked={scope === 'all'} onChange={() => setScope('all')} />
                <span>All devices this rule applies to <span className="block text-xs text-muted">Requires unrestricted device scope.</span></span>
              </label>
            )}
          </fieldset>
          <FormField label="Owner" required hint="Person or team accountable for this accepted risk." error={submitted ? errors.owner : null}>
            <Input value={owner} onChange={(e) => setOwner(e.target.value)} maxLength={120} />
          </FormField>
          <FormField label="Reason" required hint="Why the deviation is accepted and what compensates for it." error={submitted ? errors.reason : null}>
            <Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} rows={3} />
          </FormField>
          <FormField label="Expires on (UTC)" required hint={`Ends at 23:59:59 UTC on this date. Up to one year (${maxDate}).`} error={submitted ? errors.expiry : null}>
            <Input type="date" value={expiry} min={minDate} max={maxDate} onChange={(e) => setExpiry(e.target.value)} />
          </FormField>
        </div>
        <DialogFooter className="flex-wrap">
          <Button variant="outline" onClick={onClose} disabled={create.isPending}>Cancel</Button>
          <Button disabled={create.isPending} onClick={() => { setSubmitted(true); if (valid) create.mutate() }}>
            {create.isPending ? 'Adding…' : 'Add exception'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Per-device compliance ─────────────────────────────────────────────── */

export function DeviceCompliance({ deviceId }: { deviceId: string }) {
  const can = useCan()
  const canExcept = can('ncm.compliance')
  const canEvaluate = can('ncm.manage') || can('ncm.compliance')
  const evaluate = useEvaluate(deviceId)
  const [problemsOnly, setProblemsOnly] = useState(false)
  const [exceptionTarget, setExceptionTarget] = useState<ExceptionTarget | null>(null)
  const q = useQuery<{ data: ResultRow[]; platform_resolved: boolean; note?: string | null }>({
    queryKey: ['ncm', 'compliance', 'device', deviceId],
    queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-compliance`)).data,
    enabled: !!deviceId,
  })
  const rows = q.data?.data ?? []
  const groups = useMemo(() => {
    const m = new Map<string, { id: string; name: string; rows: ResultRow[]; counts: Record<Status, number> }>()
    for (const r of rows) {
      let g = m.get(r.policy_id)
      if (!g) {
        g = { id: r.policy_id, name: r.policy_name, rows: [], counts: { fail: 0, unknown: 0, excepted: 0, pass: 0 } }
        m.set(r.policy_id, g)
      }
      g.rows.push(r)
      g.counts[r.status] = (g.counts[r.status] || 0) + 1
    }
    return [...m.values()]
  }, [rows])
  const totals = useMemo(() => rows.reduce((t, r) => ({ ...t, [r.status]: (t[r.status] || 0) + 1 }), { fail: 0, unknown: 0, excepted: 0, pass: 0 } as Record<Status, number>), [rows])

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">Configuration compliance</h3>
          <p className="text-xs text-muted">Results against the latest validated running configuration.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {rows.length > 0 && (
            <div role="group" aria-label="Rules shown" className="flex gap-1.5">
              <Toggle pressed={!problemsOnly} onClick={() => setProblemsOnly(false)}>All rules</Toggle>
              <Toggle pressed={problemsOnly} onClick={() => setProblemsOnly(true)}>Problems only</Toggle>
            </div>
          )}
          {canEvaluate && (
            <Button size="sm" variant="outline" onClick={() => evaluate.mutate()} disabled={evaluate.isPending}
              title="Re-assesses this device from its latest stored validated configuration; the device is not contacted.">
              <RefreshCw className={cn(evaluate.isPending && 'animate-spin')} aria-hidden /> {evaluate.isPending ? 'Evaluating…' : 'Re-evaluate'}
            </Button>
          )}
        </div>
      </div>

      {q.isLoading ? <Loading what="compliance results" />
        : q.isError ? <SectionError what="Compliance results for this device" onRetry={() => q.refetch()} />
        : !rows.length ? (
          <EmptyState>
            {q.data?.note || 'No compliance results for this device yet.'}
            {q.data && !q.data.platform_resolved && <span className="mt-1 block text-xs">Compliance rules are platform-specific, so an unresolved platform cannot be assessed.</span>}
          </EmptyState>
        ) : (
          <>
            <div className="flex flex-wrap gap-2 text-xs" aria-label="Status totals">
              {STATUSES.map((s) => (
                <Badge key={s} variant={totals[s] ? TONE_BADGE[STATUS_META[s].tone] : 'outline'}>
                  <ToneIcon tone={STATUS_META[s].tone} className={totals[s] && STATUS_META[s].tone !== 'neutral' ? 'text-current' : ''} />
                  {totals[s]} {STATUS_META[s].label.toLowerCase()}
                </Badge>
              ))}
            </div>
            {groups.map((g) => {
              const shown = problemsOnly ? g.rows.filter((r) => r.status === 'fail' || r.status === 'unknown') : g.rows
              return (
                <Card key={g.id} className="min-w-0">
                  <CardContent className="space-y-2 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h4 className="text-sm font-semibold">{g.name}</h4>
                      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
                        {STATUSES.map((s) => (
                          <span key={s} className={cn('inline-flex items-center gap-1', !g.counts[s] && 'text-muted')}>
                            <ToneIcon tone={g.counts[s] ? STATUS_META[s].tone : 'neutral'} />{g.counts[s]} {STATUS_META[s].label.toLowerCase()}
                          </span>
                        ))}
                      </div>
                    </div>
                    {!shown.length ? <p className="text-xs text-muted">No failing or unassessable rules in this policy.</p> : (
                      <ul className="space-y-2">
                        {shown.map((r) => (
                          <DeviceRuleRow key={r.rule_id} row={r} canExcept={canExcept}
                            onException={() => setExceptionTarget({ rule_id: r.rule_id, device_id: deviceId, hostname: r.hostname, title: r.title, policy_name: r.policy_name })} />
                        ))}
                      </ul>
                    )}
                  </CardContent>
                </Card>
              )
            })}
          </>
        )}
      {exceptionTarget && <ExceptionDialog target={exceptionTarget} allowFleet={false} onClose={() => setExceptionTarget(null)} />}
    </div>
  )
}

function DeviceRuleRow({ row: r, canExcept, onException }: { row: ResultRow; canExcept: boolean; onException: () => void }) {
  const evidence = evidenceOf(r.evidence)
  const problem = r.status === 'fail' || r.status === 'unknown'
  return (
    <li className={cn('min-w-0 space-y-2 rounded-md border p-3', r.status === 'fail' ? 'border-danger/30' : 'border-border')}>
      <div className="flex flex-wrap items-start gap-2">
        <StatusBadge status={r.status} />
        <SeverityBadge severity={r.severity} />
        <span className="min-w-0 flex-1 break-words text-sm font-medium">{r.title}</span>
        {canExcept && problem && (
          <Button size="sm" variant="outline" onClick={onException}><ShieldOff aria-hidden /> Add exception</Button>
        )}
      </div>
      {r.detail && <p className="text-sm">{r.detail}</p>}
      {r.description && <p className="text-xs text-muted">{r.description}</p>}
      {(evidence.length > 0 || r.status === 'fail') && (
        <EvidenceLines evidence={evidence} emptyText={r.check_type === 'must_match' ? 'Required configuration was not found, so there is no line to show.' : undefined} />
      )}
      {r.status !== 'pass' && <Remediation text={r.remediation} />}
      {r.status === 'excepted' && <p className="text-xs text-muted">Covered by an active exception; see the Exceptions tab of the Configuration workspace.</p>}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        <span>Assessed <Ago iso={r.assessed_at} /></span>
        <span>Configuration captured <Ago iso={r.version_captured_at} empty="unknown" />{r.version_captured_at ? ` (${absoluteUtc(r.version_captured_at)})` : ''}</span>
      </div>
    </li>
  )
}
