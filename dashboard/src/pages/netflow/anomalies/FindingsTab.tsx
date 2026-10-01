import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { CircleHelp, Crosshair, Download, Info, Loader2, OctagonAlert, Play, RefreshCw, Search, ShieldCheck, ShieldQuestion, X } from 'lucide-react'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Card, CardContent } from '@/components/ui/Card'
import { PanelMessage, QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile, type StatusTone } from '@/components/netflow/StatusTile'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { FilterChips } from '@/components/netflow/FilterChips'
import { nf, nfKeys, nfParams, toSearchParams, useNfQuery } from '@/lib/netflow/api'
import { formatCount } from '@/lib/netflow/format'
import type { NfResult } from '@/lib/netflow/types'
import { apiErrorMessage, panelStatus } from '@/lib/netflow/status'
import { useFlowFilter } from '@/lib/netflow/useFlowFilter'
import type { NfWindow } from '@/lib/netflow/window'
import { useCan } from '@/stores/auth'
import { cn } from '@/lib/utils'
import {
  STATUS_FILTERS,
  STATUS_FILTER_LABEL,
  SEVERITY_LABEL,
  SEVERITY_TONE,
  assessEmpty,
  findingFocusId,
  groupFindings,
  isDetectorKind,
  isSeverity,
  isStatusFilter,
  type Finding,
  type FindingsResponse,
  type RunResponse,
  type Severity,
  type StatusFilter,
} from './model'
import { DetectorCoverage } from './DetectorCoverage'
import { FindingCard } from './FindingCard'

const PAGE = 50
const TONE_ICON: Record<StatusTone, typeof ShieldCheck> = {
  success: ShieldCheck,
  warning: ShieldQuestion,
  danger: OctagonAlert,
  info: Info,
  neutral: CircleHelp,
  unknown: CircleHelp,
}

export function FindingsTab({ w }: { w: NfWindow }) {
  const [sp, setSp] = useSearchParams()
  const mode: 'stored' | 'run' = sp.get('mode') === 'run' ? 'run' : 'stored'
  const status: StatusFilter = isStatusFilter(sp.get('status')) ? (sp.get('status') as StatusFilter) : 'active'
  const severity: Severity | null = isSeverity(sp.get('severity')) ? (sp.get('severity') as Severity) : null
  const q = sp.get('q') ?? ''
  const kindParam = sp.get('kind') || null
  const focusId = findingFocusId(sp)
  const [qDraft, setQDraft] = useState(q)
  const [limit, setLimit] = useState(PAGE)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const { filter, invalid, errors, removeKeys, clearAll } = useFlowFilter()
  const can = useCan()
  const perms = { acknowledge: can('alerts.acknowledge') || can('netflow.manage'), suppress: can('netflow.manage') }

  const setParam = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(sp)
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === '') next.delete(k)
      else next.set(k, v)
    }
    setSp(next, { replace: true })
    setLimit(PAGE)
    setSelected(new Set())
  }

  // An unknown detector from an old link is shown as an ignored, removable filter (never a 400).
  const kind = isDetectorKind(kindParam) ? kindParam : null
  const kindUnknown = !!kindParam && !kind
  const storedParams = nfParams(w, filter, { status, severity: severity ?? undefined, kind: kind ?? undefined, q: q || undefined, limit })
  const stored = useNfQuery<FindingsResponse>('/netflow/findings', storedParams, { window: w, enabled: mode === 'stored' })
  const runParams = nfParams(w, filter.exporter ? { exporter: filter.exporter } : null)
  const run = useNfQuery<RunResponse>('/netflow/findings/run', runParams, {
    window: w,
    enabled: mode === 'run',
    refetchInterval: false,
    timeoutMs: 45_000,
  })

  const active = (mode === 'stored' ? stored : run) as UseQueryResult<NfResult<FindingsResponse | RunResponse>>
  const meta = active.data?.meta ?? null
  const st = panelStatus(active)
  const loadingTiles = st === 'loading'

  const items: Finding[] = useMemo(() => {
    if (mode === 'stored') return stored.data?.data.items ?? []
    let list = run.data?.data.items ?? []
    if (severity) list = list.filter((i) => i.severity === severity)
    if (kind) list = list.filter((i) => i.kind === kind)
    if (status === 'suppressed') list = list.filter((i) => i.suppressed)
    else if (status !== 'all') list = list.filter((i) => !i.suppressed)
    if (q.trim()) {
      const t = q.trim().toLowerCase()
      list = list.filter((i) => [i.title, i.description, i.src, i.dst, i.exporter].some((x) => (x ?? '').toLowerCase().includes(t)))
    }
    return list
  }, [mode, stored.data, run.data, severity, kind, status, q])
  const groups = useMemo(() => groupFindings(items), [items])

  const tiles = mode === 'stored' ? storedTiles(stored.data?.data) : runTiles(run.data?.data)

  const qc = useQueryClient()
  const bulk = useMutation({
    mutationFn: async (action: 'acknowledge' | 'resolve') =>
      (await api.post('/netflow/findings/bulk', { ids: Array.from(selected), action })).data as { done: string[]; skipped: { id: string; reason: string }[] },
    onSuccess: (r, action) => {
      toast.success(`${action === 'acknowledge' ? 'Acknowledged' : 'Resolved'} ${r.done.length}`, r.skipped.length ? `${r.skipped.length} skipped: ${r.skipped[0].reason}` : undefined)
      setSelected(new Set())
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('Bulk change failed', apiErrorMessage(e)),
  })

  const exportCsv = async () => {
    try {
      const params = toSearchParams({ ...storedParams, limit: 500, offset: 0, format: 'csv' })
      const res = await api.get('/netflow/findings', { params, responseType: 'blob' })
      const url = URL.createObjectURL(res.data as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `netflow-findings.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error('Export failed', apiErrorMessage(e))
    }
  }

  const focusLink = (id: string) => {
    const next = new URLSearchParams(sp)
    next.set('finding', id)
    return `?${next.toString()}`
  }
  const toggle = (ids: string[], on: boolean) => {
    const next = new Set(selected)
    for (const id of ids) {
      if (on) next.add(id)
      else next.delete(id)
    }
    setSelected(next)
  }

  const detectors = active.data?.data.detectors ?? []
  const byKind = mode === 'stored' ? stored.data?.data.counts.by_kind_all ?? stored.data?.data.counts.by_kind ?? {} : null
  const engine = mode === 'stored' ? stored.data?.data.engine : null
  const total = mode === 'stored' ? stored.data?.data.total ?? null : items.length

  return (
    <div className="space-y-3">
      {focusId && <FocusedFinding id={focusId} tz={w.tz} perms={perms} onClose={() => setParam({ finding: null })} />}
      <div className="flex flex-col gap-2 min-[720px]:flex-row min-[720px]:items-center min-[720px]:justify-between">
        <div role="radiogroup" aria-label="Source of findings" className="inline-flex w-fit rounded-md border border-border bg-surface2 p-0.5">
          {(['stored', 'run'] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={mode === m}
              onClick={() => setParam({ mode: m === 'stored' ? null : 'run' })}
              className={cn(
                'rounded px-3 py-1.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                mode === m ? 'bg-surface text-text shadow-sm' : 'text-text2 hover:text-text',
              )}
            >
              {m === 'stored' ? 'Recorded findings' : 'Evaluate this window now'}
            </button>
          ))}
        </div>
        <form
          className="flex w-full gap-1.5 min-[720px]:w-auto"
          role="search"
          onSubmit={(e) => {
            e.preventDefault()
            setParam({ q: qDraft.trim() || null })
          }}
        >
          <label className="sr-only" htmlFor="nf-findings-q">Search findings</label>
          <Input id="nf-findings-q" value={qDraft} onChange={(e) => setQDraft(e.target.value)} placeholder="Address, CIDR or text" className="h-9 min-w-0 flex-1 min-[720px]:w-64" />
          <Button type="submit" size="sm" variant="outline" className="h-9" aria-label="Search findings">
            <Search aria-hidden /> <span className="hidden min-[420px]:inline">Search</span>
          </Button>
        </form>
      </div>

      <FilterChips filter={filter} invalid={invalid} errors={errors} tz={w.tz} onRemove={removeKeys} onClearAll={clearAll} />

      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 lg:grid-cols-4" aria-live="polite">
        {tiles.map((t) => (
          <StatusTile
            key={t.key}
            tone={t.tone}
            label={t.label}
            value={st === 'error' || st === 'forbidden' ? null : t.value === null ? null : formatCount(t.value)}
            word={t.word}
            caption={t.caption}
            loading={loadingTiles}
            onClick={t.onClick ? () => t.onClick?.(setParam) : undefined}
            active={t.active?.(status, severity)}
            title={t.title}
          />
        ))}
      </div>

      {active.data && <DetectorCoverage detectors={detectors} engine={engine} tz={w.tz} mode={mode} />}

      <DataQualityBar meta={meta} window={w} filter={filter} />

      <div className="flex flex-col gap-2 min-[720px]:flex-row min-[720px]:items-center min-[720px]:justify-between">
        <div role="radiogroup" aria-label="Status" className="flex flex-wrap gap-1">
          {STATUS_FILTERS.filter((s) => mode === 'stored' || ['active', 'suppressed', 'all'].includes(s)).map((s) => (
            <button
              key={s}
              type="button"
              role="radio"
              aria-checked={status === s}
              onClick={() => setParam({ status: s === 'active' ? null : s })}
              className={cn(
                'rounded-md border px-2.5 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                status === s ? 'border-primary bg-primary/10 text-text' : 'border-border text-text2 hover:text-text',
              )}
            >
              {mode === 'run' && s === 'active' ? 'Candidates' : STATUS_FILTER_LABEL[s]}
            </button>
          ))}
          {severity && (
            <button type="button" onClick={() => setParam({ severity: null })} className="rounded-md border border-border px-2.5 py-1 text-xs text-text2 hover:text-text" aria-label={`Remove severity filter ${SEVERITY_LABEL[severity]}`}>
              {SEVERITY_LABEL[severity]} only ×
            </button>
          )}
          {kindUnknown && (
            <button type="button" onClick={() => setParam({ kind: null })} className="rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1 text-xs text-warning-fg" aria-label="Remove the unknown detector filter">
              Unknown detector “{kindParam}” ignored ×
            </button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {detectors.length > 0 && (
            <>
              <label htmlFor="nf-findings-kind" className="sr-only">Detector</label>
              <select
                id="nf-findings-kind"
                className="h-8 max-w-[14rem] rounded-md border border-border bg-surface px-2 text-xs text-text"
                value={kind ?? ''}
                onChange={(e) => setParam({ kind: e.target.value || null })}
              >
                <option value="">All detectors</option>
                {detectors.map((d) => (
                  <option key={d.kind} value={d.kind}>
                    {d.label}{byKind && byKind[d.kind] ? ` (${byKind[d.kind]} active)` : ''}
                  </option>
                ))}
              </select>
            </>
          )}
          {mode === 'run' && (
            <Button size="sm" variant="outline" onClick={() => void run.refetch()} disabled={run.isFetching}>
              {run.isFetching ? <Loader2 className="animate-spin" aria-hidden /> : <Play aria-hidden />} Run again
            </Button>
          )}
          {mode === 'stored' && (
            <>
              <Button size="sm" variant="outline" onClick={() => void stored.refetch()} disabled={stored.isFetching} aria-label="Refresh findings">
                {stored.isFetching ? <Loader2 className="animate-spin" aria-hidden /> : <RefreshCw aria-hidden />}
                <span className="hidden min-[420px]:inline">Refresh</span>
              </Button>
              <Button size="sm" variant="outline" onClick={() => void exportCsv()} disabled={!stored.data} aria-label="Export findings as CSV">
                <Download aria-hidden /> <span className="hidden min-[420px]:inline">Export CSV</span>
              </Button>
            </>
          )}
        </div>
      </div>

      {mode === 'stored' && selected.size > 0 && perms.acknowledge && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-primary/40 bg-primary/5 px-3 py-2 text-xs text-text" role="region" aria-label="Bulk actions">
          <span>{selected.size} selected</span>
          <Button size="sm" onClick={() => bulk.mutate('acknowledge')} disabled={bulk.isPending}>Acknowledge</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate('resolve')} disabled={bulk.isPending}>Resolve</Button>
          <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>Clear selection</Button>
        </div>
      )}

      <Card>
        <CardContent className="p-3">
          <QueryPanel
            query={active}
            label={mode === 'stored' ? 'recorded findings' : 'the detector run'}
            skeleton="rows"
            isEmpty={() => false}
          >
            {(res) => {
              const data = res.data
              if (!items.length) {
                const filtered = status !== 'active' || !!severity || !!kind || !!q
                if (filtered && (mode === 'run' ? (run.data?.data.items.length ?? 0) > 0 : (stored.data?.data.counts.open ?? 0) + (stored.data?.data.counts.acknowledged ?? 0) + (stored.data?.data.counts.suppressed ?? 0) + (stored.data?.data.counts.resolved ?? 0) > 0)) {
                  return <PanelMessage className="py-8" icon={Search} title="No findings match this status, severity, detector or search" detail="Other findings exist in this window; clear these filters to see them." />
                }
                const a = assessEmpty({
                  detectors: data.detectors,
                  complete: data.complete,
                  engine: mode === 'stored' ? (data as FindingsResponse).engine : null,
                  mode,
                  evaluatedPct: mode === 'stored' ? (data as FindingsResponse).engine?.evaluated_pct : null,
                })
                const Icon = TONE_ICON[a.tone]
                return (
                  <div className="flex flex-col items-center gap-2 px-4 py-8 text-center" role="status">
                    <Icon className={cn('h-7 w-7', a.tone === 'success' ? 'text-success-fg' : a.tone === 'danger' ? 'text-danger-fg' : a.tone === 'warning' ? 'text-warning-fg' : 'text-text2')} aria-hidden />
                    <p className="text-sm font-semibold text-text">{a.title}</p>
                    <p className="max-w-xl text-xs text-text2">{a.detail}</p>
                    {mode === 'stored' && (
                      <Button size="sm" variant="outline" className="mt-1" onClick={() => setParam({ mode: 'run' })}>
                        <Play aria-hidden /> Evaluate this window now
                      </Button>
                    )}
                  </div>
                )
              }
              return (
                <div>
                  {mode === 'run' && (run.data?.data.counts.truncated ?? false) && (
                    <p className="mb-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning-fg">
                      Some detectors matched more subjects than are listed ({formatCount(run.data?.data.counts.matches ?? null)} matches in total); the strongest are shown.
                    </p>
                  )}
                  <ul className="space-y-2" aria-label="Findings">
                    {groups.map((g) => {
                      const ids = [g.primary, ...g.others].filter((f) => f.persisted).map((f) => f.id)
                      return (
                        <FindingCard
                          key={g.key}
                          finding={g.primary}
                          others={g.others}
                          tz={w.tz}
                          perms={perms}
                          focusLink={focusLink}
                          selected={ids.length > 0 && ids.every((id) => selected.has(id))}
                          onSelect={mode === 'stored' && perms.acknowledge && ids.length ? (on) => toggle(ids, on) : undefined}
                        />
                      )
                    })}
                  </ul>
                  <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-text2">
                    <span>
                      Showing {formatCount(items.length)}
                      {total !== null ? ` of ${formatCount(total)}` : ''} finding{(total ?? items.length) === 1 ? '' : 's'}
                      {groups.length < items.length ? ` · ${formatCount(groups.length)} subject${groups.length === 1 ? '' : 's'} (the same activity seen by several exporters is grouped)` : ''}
                    </span>
                    {mode === 'stored' && total !== null && items.length < total && limit < 500 && (
                      <Button size="sm" variant="outline" onClick={() => setLimit((l) => Math.min(500, l + PAGE))}>Show more</Button>
                    )}
                  </div>
                </div>
              )
            }}
          </QueryPanel>
        </CardContent>
      </Card>
    </div>
  )
}

type SetParam = (patch: Record<string, string | null>) => void

interface TileDef {
  key: string
  label: string
  value: number | null
  tone: StatusTone
  word?: string | null
  caption?: string
  title?: string
  onClick?: (set: SetParam) => void
  active?: (status: StatusFilter, severity: Severity | null) => boolean
}

function storedTiles(d: FindingsResponse | undefined): TileDef[] {
  const c = d?.counts
  const sev = (s: Severity): TileDef => ({
    key: s,
    label: `${SEVERITY_LABEL[s]} (active)`,
    value: c ? c.by_severity[s] ?? 0 : null,
    tone: c && (c.by_severity[s] ?? 0) > 0 ? SEVERITY_TONE[s] : 'neutral',
    word: c && (c.by_severity[s] ?? 0) > 0 ? SEVERITY_LABEL[s] : null,
    onClick: (set) => set({ severity: s, status: null }),
    active: (_st, sv) => sv === s,
  })
  return [
    {
      key: 'active',
      label: 'Open + acknowledged',
      value: c ? c.active : null,
      tone: c ? (c.active > 0 ? 'warning' : 'neutral') : 'unknown',
      word: c && c.active > 0 ? 'Active' : null,
      caption: c ? `${formatCount(c.open)} open · ${formatCount(c.acknowledged)} acknowledged` : undefined,
      onClick: (set) => set({ status: null, severity: null }),
      active: (st, sv) => st === 'active' && !sv,
    },
    sev('critical'),
    sev('warning'),
    {
      key: 'other',
      label: 'Suppressed · resolved',
      value: c ? c.suppressed + c.resolved : null,
      tone: 'neutral',
      word: null,
      caption: c ? `${formatCount(c.suppressed)} suppressed · ${formatCount(c.resolved)} resolved` : undefined,
      onClick: (set) => set({ status: 'all', severity: null }),
      active: (st) => st === 'all',
      title: 'Suppressed findings stay visible here with their reason',
    },
  ]
}

function runTiles(d: RunResponse | undefined): TileDef[] {
  const c = d?.counts
  const sev = (s: Severity): TileDef => ({
    key: s,
    label: SEVERITY_LABEL[s],
    value: c ? c.by_severity[s] ?? 0 : null,
    tone: c && (c.by_severity[s] ?? 0) > 0 ? SEVERITY_TONE[s] : 'neutral',
    word: c && (c.by_severity[s] ?? 0) > 0 ? SEVERITY_LABEL[s] : null,
    onClick: (set) => set({ severity: s }),
    active: (_st, sv) => sv === s,
  })
  return [
    {
      key: 'candidates',
      label: 'Candidates',
      value: c ? c.candidate : null,
      tone: c ? (c.candidate > 0 ? 'warning' : 'neutral') : 'unknown',
      word: null,
      caption: c ? `${formatCount(c.matches)} matching subjects in total` : undefined,
      onClick: (set) => set({ severity: null, status: null }),
      active: (st, sv) => st === 'active' && !sv,
    },
    sev('critical'),
    sev('warning'),
    {
      key: 'suppressed',
      label: 'Covered by exceptions',
      value: c ? c.suppressed : null,
      tone: 'neutral',
      word: null,
      onClick: (set) => set({ status: 'suppressed', severity: null }),
      active: (st) => st === 'suppressed',
    },
  ]
}


/**
 * A finding opened by id (`?finding=<id>`): links from platform alerts, the host profile and
 * Forensics land on that finding with its evidence and lifecycle, whatever the page's window
 * or status filter would list.
 */
function FocusedFinding({ id, tz, perms, onClose }: { id: string; tz: string; perms: { acknowledge: boolean; suppress: boolean }; onClose: () => void }) {
  const q = useQuery({
    queryKey: nfKeys.query(`/netflow/findings/${id}`),
    queryFn: async ({ signal }) => (await nf.get<Finding>(`/netflow/findings/${id}`, null, { signal })).data,
    retry: false,
  })
  return (
    <section aria-labelledby="nf-focused-finding" className="rounded-lg border border-primary/40 bg-primary/5 p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2 px-0.5">
        <h2 id="nf-focused-finding" className="flex items-center gap-1.5 text-xs font-semibold text-text">
          <Crosshair className="h-3.5 w-3.5 text-primary" aria-hidden /> Linked finding
        </h2>
        <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close the linked finding">
          <X aria-hidden /> <span className="hidden min-[420px]:inline">Close</span>
        </Button>
      </div>
      <QueryPanel query={q} label="the linked finding" skeleton="rows" compact isEmpty={() => false}>
        {(f) => (
          <ul aria-label="Linked finding">
            <FindingCard finding={f} tz={tz} perms={f.permissions ?? perms} defaultOpen />
          </ul>
        )}
      </QueryPanel>
    </section>
  )
}
