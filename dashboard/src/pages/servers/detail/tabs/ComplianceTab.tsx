/** Compliance tab — this server evaluated against its software baselines.
 *
 *  A score and per-status counts up top, a per-baseline breakdown, then every
 *  rule result, failing first. */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Ban, CheckCircle2, ClipboardCheck, History, Layers, PackageX, RefreshCw, Search, ShieldCheck,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import {
  ExportCsvButton, QueryError, TableStateRow, cmp, sortIndicator, sortableTh,
} from '@/components/servers/tables'
import type {
  ComplianceResult, ComplianceStatus, ComplianceSummary, ServerItem,
} from '@/types/servers'
import {
  EmptyState, Meter, Panel, Pill, Segmented, StatTile, type Tone,
} from '../ui'

/* ── Labels ──────────────────────────────────────────────────────── */

const STATUS_META: Record<ComplianceStatus, { label: string; tone: Tone; rank: number }> = {
  missing: { label: 'Missing', tone: 'danger', rank: 0 },
  prohibited: { label: 'Prohibited', tone: 'danger', rank: 1 },
  outdated: { label: 'Outdated', tone: 'warning', rank: 2 },
  compliant: { label: 'Compliant', tone: 'success', rank: 3 },
}

const SEV_RANK: Record<string, number> = { critical: 0, warning: 1, info: 2 }

function SevPill({ sev }: { sev: string }) {
  const tone: Tone = sev === 'critical' ? 'danger' : sev === 'warning' ? 'warning' : 'info'
  return <Pill tone={tone}>{sev ? sev.charAt(0).toUpperCase() + sev.slice(1) : 'Info'}</Pill>
}

function scoreTone(score: number | null): Tone {
  if (score == null) return 'muted'
  return score >= 95 ? 'success' : score >= 80 ? 'warning' : 'danger'
}

type Filter = 'all' | 'failing' | ComplianceStatus
type SortKey = 'status' | 'baseline' | 'rule' | 'severity' | 'failing'

const CSV_COLUMNS = [
  { header: 'Status', value: (r: ComplianceResult) => r.status },
  { header: 'Baseline', value: (r: ComplianceResult) => r.baseline_name },
  { header: 'Rule type', value: (r: ComplianceResult) => r.rule_type },
  { header: 'Package match', value: (r: ComplianceResult) => r.package_match },
  { header: 'Match type', value: (r: ComplianceResult) => r.match_type },
  { header: 'Min version', value: (r: ComplianceResult) => r.min_version },
  { header: 'Found package', value: (r: ComplianceResult) => r.found_package },
  { header: 'Found version', value: (r: ComplianceResult) => r.found_version },
  { header: 'Expected', value: (r: ComplianceResult) => r.expected },
  { header: 'Severity', value: (r: ComplianceResult) => r.severity },
  { header: 'Failing since', value: (r: ComplianceResult) => r.first_failed_at },
  { header: 'Evaluated', value: (r: ComplianceResult) => r.evaluated_at },
]

/* ── Tab ─────────────────────────────────────────────────────────── */

export function ComplianceTab({ server }: { server: ServerItem }) {
  const serverId = server.id
  const qc = useQueryClient()
  const [filter, setFilter] = useState<Filter>('all')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'status', dir: 'asc' })

  // Same key as useCompliance() in data.ts, so the header badge and this tab
  // share one request; this tab also reads the per-rule items.
  const { data, isLoading, isError, error, refetch } = useQuery<{ items: ComplianceResult[]; summary: ComplianceSummary }>({
    queryKey: ['servers', serverId, 'compliance'],
    queryFn: async () => (await api.get(`/servers/${serverId}/compliance`)).data,
    refetchInterval: 60_000,
  })

  const evaluate = useMutation({
    mutationFn: async () => (await api.post(`/servers/${serverId}/evaluate-baselines`)).data,
    onSuccess: (r) => {
      toast.success('Evaluation complete', `${r?.evaluated ?? 0} rules checked`)
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'compliance'] })
    },
    onError: (e) => toast.error('Evaluation failed', apiErrorMessage(e)),
  })

  const items = useMemo(() => data?.items ?? [], [data])
  const summary = data?.summary
  const total = summary?.total ?? items.length
  const failing = total - (summary?.compliant ?? 0)
  const score = total > 0 && summary ? (summary.compliant / total) * 100 : null
  const hasBaselines = total > 0

  const lastEvaluated = useMemo(() => {
    const ts = items.map((r) => Date.parse(r.evaluated_at)).filter((t) => !Number.isNaN(t))
    return ts.length ? new Date(Math.max(...ts)).toISOString() : null
  }, [items])

  /* Per-baseline breakdown. */
  const baselines = useMemo(() => {
    const m = new Map<string, { id: string; name: string; total: number; compliant: number; critical: number }>()
    for (const r of items) {
      const e = m.get(r.baseline_id) ?? { id: r.baseline_id, name: r.baseline_name || 'Baseline', total: 0, compliant: 0, critical: 0 }
      e.total += 1
      if (r.status === 'compliant') e.compliant += 1
      else if (r.severity === 'critical') e.critical += 1
      m.set(r.baseline_id, e)
    }
    return [...m.values()].sort((a, b) => a.compliant / a.total - b.compliant / b.total || a.name.localeCompare(b.name))
  }, [items])
  const [baseline, setBaseline] = useState<string | null>(null)

  /* Rows. */
  const rows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let r = items
    if (baseline) r = r.filter((x) => x.baseline_id === baseline)
    if (filter === 'failing') r = r.filter((x) => x.status !== 'compliant')
    else if (filter !== 'all') r = r.filter((x) => x.status === filter)
    if (q) {
      r = r.filter((x) =>
        x.package_match.toLowerCase().includes(q)
        || (x.found_package ?? '').toLowerCase().includes(q)
        || (x.baseline_name ?? '').toLowerCase().includes(q))
    }
    const dir = sort.dir === 'asc' ? 1 : -1
    const key = (x: ComplianceResult): unknown => {
      switch (sort.key) {
        case 'status': return STATUS_META[x.status]?.rank ?? 9
        case 'baseline': return x.baseline_name ?? ''
        case 'rule': return x.package_match
        case 'severity': return SEV_RANK[x.severity] ?? 9
        // Longest-failing first when descending; compliant rows sink.
        case 'failing': return x.first_failed_at ? Date.parse(x.first_failed_at) : Number.MAX_SAFE_INTEGER
      }
    }
    return [...r].sort((a, b) =>
      cmp(key(a), key(b)) * dir
      // Within a status, most severe first, then by name.
      || (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9)
      || a.package_match.localeCompare(b.package_match))
  }, [items, baseline, filter, search, sort])

  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: key === 'failing' ? 'desc' : 'asc' }))

  const toggleFilter = (f: Filter) => setFilter((cur) => (cur === f ? 'all' : f))

  const filterItems: { key: Filter; label: string; count?: number; tone?: Tone }[] = [
    { key: 'all', label: 'All', count: total, tone: 'muted' },
    { key: 'failing', label: 'Failing', count: failing, tone: 'danger' },
    { key: 'missing', label: 'Missing', count: summary?.missing, tone: 'danger' },
    { key: 'outdated', label: 'Outdated', count: summary?.outdated, tone: 'warning' },
    { key: 'prohibited', label: 'Prohibited', count: summary?.prohibited, tone: 'danger' },
    { key: 'compliant', label: 'Compliant', count: summary?.compliant, tone: 'success' },
  ]

  const actions = (
    <>
      <Button variant="ghost" size="sm" asChild>
        <Link to="/server-baselines"><Layers className="h-3.5 w-3.5" /> Manage baselines</Link>
      </Button>
      <Button variant="outline" size="sm" onClick={() => evaluate.mutate()} disabled={evaluate.isPending}>
        <RefreshCw className={cn('h-3.5 w-3.5', evaluate.isPending && 'animate-spin')} /> Evaluate now
      </Button>
    </>
  )

  if (isError) {
    return (
      <Panel icon={ClipboardCheck} title="Software compliance" actions={actions}>
        <QueryError error={error} onRetry={() => refetch()} />
      </Panel>
    )
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-[92px] rounded-xl" />)}
        </div>
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    )
  }

  if (!hasBaselines) {
    return (
      <Panel icon={ClipboardCheck} title="Software compliance" subtitle="No baselines apply to this server" actions={actions}>
        <EmptyState
          icon={ClipboardCheck}
          title="No software baseline matches this server"
          hint={(
            <>
              Define a software baseline matching this server&apos;s OS or tags — required apps,
              minimum versions, prohibited software — and violations will alert automatically.
            </>
          )}
          action={(
            <Button size="sm" asChild className="mt-1">
              <Link to="/server-baselines"><Layers className="h-3.5 w-3.5" /> Manage baselines</Link>
            </Button>
          )}
        />
      </Panel>
    )
  }

  const tone = scoreTone(score)

  return (
    <div className="space-y-4">
      {/* Summary */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <div className="col-span-2 flex min-w-0 flex-col gap-1 rounded-xl border border-border bg-surface p-3.5 md:col-span-1">
          <span className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
            <ShieldCheck className="h-3.5 w-3.5" /> Compliance score
          </span>
          <div
            className={cn(
              'text-2xl font-semibold leading-7 tabular-nums',
              tone === 'success' ? 'text-success' : tone === 'warning' ? 'text-warning' : tone === 'danger' ? 'text-danger' : 'text-muted',
            )}
          >
            {score == null ? '—' : `${score.toFixed(score >= 99.95 || score === 0 ? 0 : 1)}%`}
          </div>
          {/* Thresholds above 100 keep the bar neutral: here, full is good. */}
          <Meter pct={score} warn={101} crit={101} className="mt-0.5" />
          <div className="truncate text-xs text-muted">
            {summary?.compliant ?? 0} of {total} rules compliant
          </div>
        </div>
        <StatTile
          icon={CheckCircle2}
          label="Compliant"
          value={summary?.compliant ?? 0}
          tone={(summary?.compliant ?? 0) > 0 ? 'success' : 'default'}
          sub="rules passing"
          onClick={() => toggleFilter('compliant')}
          active={filter === 'compliant'}
        />
        <StatTile
          icon={PackageX}
          label="Missing"
          value={summary?.missing ?? 0}
          tone={(summary?.missing ?? 0) > 0 ? 'danger' : 'default'}
          sub="required software absent"
          onClick={() => toggleFilter('missing')}
          active={filter === 'missing'}
        />
        <StatTile
          icon={History}
          label="Outdated"
          value={summary?.outdated ?? 0}
          tone={(summary?.outdated ?? 0) > 0 ? 'warning' : 'default'}
          sub="below minimum version"
          onClick={() => toggleFilter('outdated')}
          active={filter === 'outdated'}
        />
        <StatTile
          icon={Ban}
          label="Prohibited"
          value={summary?.prohibited ?? 0}
          tone={(summary?.prohibited ?? 0) > 0 ? 'danger' : 'default'}
          sub="banned software installed"
          onClick={() => toggleFilter('prohibited')}
          active={filter === 'prohibited'}
        />
      </div>

      {/* Baselines */}
      {baselines.length > 1 && (
        <Panel icon={Layers} title="Baselines" subtitle={`${baselines.length} apply to this server`} flush>
          <div className="grid divide-y divide-border/60 md:grid-cols-2 md:divide-y-0">
            {baselines.map((b) => {
              const pct = (b.compliant / b.total) * 100
              const active = baseline === b.id
              return (
                <button
                  key={b.id}
                  type="button"
                  onClick={() => setBaseline(active ? null : b.id)}
                  title={active ? 'Show all baselines' : `Show only ${b.name}`}
                  className={cn(
                    'flex min-w-0 flex-col gap-1.5 border-border/60 px-4 py-2.5 text-left transition-colors hover:bg-surface2/40 md:border-b',
                    active && 'bg-primary/5',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className={cn('truncate text-[13px] font-medium', active ? 'text-primary' : 'text-text')} title={b.name}>
                      {b.name}
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-muted">
                      {b.critical > 0 && <span className="mr-2 font-medium text-danger">{b.critical} critical</span>}
                      <span className={cn('font-semibold', pct >= 100 ? 'text-success' : 'text-text')}>{b.compliant}/{b.total}</span>
                    </span>
                  </div>
                  <Meter pct={pct} warn={101} crit={101} thin />
                </button>
              )
            })}
          </div>
        </Panel>
      )}

      {/* Results */}
      <Panel
        icon={ClipboardCheck}
        title="Rule results"
        subtitle={(
          <>
            {summary?.compliant}/{total} compliant
            {lastEvaluated && <> · evaluated <span title={new Date(lastEvaluated).toLocaleString()}>{relativeTime(lastEvaluated)}</span></>}
            {baseline && <> · {baselines.find((b) => b.id === baseline)?.name}</>}
          </>
        )}
        flush
        actions={(
          <>
            {actions}
            <ExportCsvButton rows={rows} columns={CSV_COLUMNS} filename={`${server.display_name}-compliance.csv`} />
          </>
        )}
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-4 py-2.5">
          <Segmented size="sm" items={filterItems} value={filter} onChange={setFilter} />
          <div className="flex items-center gap-2">
            {baseline && (
              <Button size="sm" variant="ghost" className="h-8 text-xs" onClick={() => setBaseline(null)}>
                Clear baseline filter
              </Button>
            )}
            <div className="relative w-full sm:w-56">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter package or baseline…"
                className="h-8 pl-8 text-[13px]"
              />
            </div>
          </div>
        </div>
        <Table className="text-[13px]">
          <THead className="bg-surface2/40">
            <Tr className="hover:bg-transparent">
              <Th className={cn('h-9 w-[120px] pl-4', sortableTh)} onClick={() => toggleSort('status')}>
                Status{sortIndicator(sort.key === 'status', sort.dir)}
              </Th>
              <Th className={cn('h-9', sortableTh)} onClick={() => toggleSort('rule')}>
                Rule{sortIndicator(sort.key === 'rule', sort.dir)}
              </Th>
              <Th className={cn('h-9', sortableTh)} onClick={() => toggleSort('baseline')}>
                Baseline{sortIndicator(sort.key === 'baseline', sort.dir)}
              </Th>
              <Th className="h-9">Found</Th>
              <Th className="h-9">Expected</Th>
              <Th className={cn('h-9 w-[110px]', sortableTh)} onClick={() => toggleSort('severity')}>
                Severity{sortIndicator(sort.key === 'severity', sort.dir)}
              </Th>
              <Th className={cn('h-9 w-[130px] pr-4 text-right', sortableTh)} onClick={() => toggleSort('failing')}>
                Failing since{sortIndicator(sort.key === 'failing', sort.dir)}
              </Th>
            </Tr>
          </THead>
          <TBody>
            {rows.length === 0 ? (
              <TableStateRow colSpan={7}>
                <EmptyState
                  compact
                  icon={filter === 'failing' || filter === 'missing' || filter === 'outdated' || filter === 'prohibited' ? CheckCircle2 : Search}
                  title={
                    search ? 'No rules match the filter'
                      : filter === 'all' ? 'No rule results'
                        : filter === 'compliant' ? 'No compliant rules'
                          : `No ${filter === 'failing' ? 'failing' : STATUS_META[filter].label.toLowerCase()} rules`
                  }
                />
              </TableStateRow>
            ) : rows.map((r) => {
              const meta = STATUS_META[r.status] ?? STATUS_META.prohibited
              const ruleSub = `${r.rule_type} · ${r.match_type}${r.min_version ? ` · ≥ ${r.min_version}` : ''}`
              return (
                <Tr key={r.rule_id} className="border-border/60 hover:bg-surface2/40">
                  <Td className="py-2 pl-4"><Pill tone={meta.tone}>{meta.label}</Pill></Td>
                  <Td className="max-w-[260px] py-2">
                    <div className="truncate font-medium text-text" title={r.package_match}>{r.package_match}</div>
                    <div className="truncate text-xs text-muted" title={r.notes || ruleSub}>{ruleSub}</div>
                  </Td>
                  <Td className="max-w-[180px] truncate py-2 text-xs text-text2" title={r.baseline_name}>{r.baseline_name || '—'}</Td>
                  <Td className="max-w-[220px] py-2 text-xs">
                    {r.found_package ? (
                      <div className="truncate" title={`${r.found_package} ${r.found_version ?? ''}`}>
                        {r.found_package} <span className="tabular-nums text-muted">{r.found_version}</span>
                      </div>
                    ) : <span className="text-muted">—</span>}
                  </Td>
                  <Td className="max-w-[200px] truncate py-2 text-xs text-muted" title={r.expected ?? undefined}>{r.expected || '—'}</Td>
                  <Td className="py-2"><SevPill sev={r.severity} /></Td>
                  <Td
                    className="whitespace-nowrap py-2 pr-4 text-right text-xs tabular-nums text-muted"
                    title={r.first_failed_at ? new Date(r.first_failed_at).toLocaleString() : undefined}
                  >
                    {r.first_failed_at ? relativeTime(r.first_failed_at) : '—'}
                  </Td>
                </Tr>
              )
            })}
          </TBody>
        </Table>
      </Panel>
    </div>
  )
}
