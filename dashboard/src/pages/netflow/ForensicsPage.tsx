import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useInfiniteQuery, useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { Bookmark, Columns3, DatabaseZap, Loader2, RefreshCw, RotateCcw, Save, Search } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { QueryPanel, PanelMessage } from '@/components/netflow/QueryPanel'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { FilterChips } from '@/components/netflow/FilterChips'
import { SaveViewDialog, type SaveViewDraft } from '@/components/netflow/SaveViewDialog'
import { useCan } from '@/stores/auth'
import { cn } from '@/lib/utils'
import { nf, nfRetry, paramsKey, useNfQuery } from '@/lib/netflow/api'
import {
  buildSavedQuery,
  FLOW_FILTER_ALIASES,
  isFlowFilterEmpty,
  FLOW_FILTER_KEYS,
  type FlowFilterKey,
} from '@/lib/netflow/filters'
import { formatCountExact, formatDateTime, formatDuration } from '@/lib/netflow/format'
import { buildNetflowLink } from '@/lib/netflow/links'
import type { NfResult, NfRetention } from '@/lib/netflow/types'
import { savedQueryWindow, useNfWindow, windowParams } from '@/lib/netflow/window'
import { serverNow } from '@/lib/serverClock'
import { errorText, saveBlob, useSavedView, useSavedViewMutations } from './forensics/api'
import { EvidenceBanner } from './forensics/EvidenceBanner'
import { ExportControls, ExportJobsPanel } from './forensics/ExportControls'
import { ADVANCED_KEYS, FilterBuilder } from './forensics/FilterBuilder'
import { ResultsTable, useNarrowScreen } from './forensics/ResultsTable'
import {
  appliedSearchFromUrl,
  broadenToHostLink,
  COLUMNS,
  columnsSearchParams,
  displayedRowsCsv,
  displayedRowsFilename,
  draftFromApplied,
  draftsEqual,
  EVIDENCE_PARAMS,
  exportApiParams,
  firewallEventCount,
  flowRecordsOnlyParams,
  forensicsHeaderLinks,
  invalidKeys,
  isDefaultColumns,
  isRunnable,
  nextSort,
  parseDraft,
  resetSearchParams,
  sameSearchParams,
  searchApiParams,
  searchHeading,
  showingLabel,
  sortLabel,
  sortSearchParams,
  submitSearchParams,
  type DraftState,
  type ForensicsPageData,
  type ForensicsSort,
} from './forensics/model'

/** Records per page: 500 in the table, 100 as cards on a phone (each page is still complete and ordered). */
const PAGE_SIZE = 500
const PAGE_SIZE_NARROW = 100
type SearchData = InfiniteData<NfResult<ForensicsPageData>>

/**
 * Flow Forensics: search individual flow records (NF-08, NF-09, NF-10, NF-11, NF-12, FE-04).
 *
 * The URL holds the applied search; the form is a draft applied on Search or Enter. Results are
 * paged with a keyset cursor ("Load more"), the total is counted separately (exact or a lower
 * bound), and exports say exactly what they contain.
 */
export function NetflowForensicsPage() {
  const [sp, setSp] = useSearchParams()
  const location = useLocation()
  const qc = useQueryClient()
  const can = useCan()
  const nfw = useNfWindow()
  const pageSize = useNarrowScreen() ? PAGE_SIZE_NARROW : PAGE_SIZE
  const w = nfw.window
  const qs = sp.toString()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const applied = useMemo(() => appliedSearchFromUrl(sp), [qs])
  const appliedDraft = useMemo(() => draftFromApplied(applied), [applied])
  const appliedKey = JSON.stringify(appliedDraft)

  /* ─── Draft (NF-11) ─── */
  const [draft, setDraft] = useState<DraftState>(appliedDraft)
  const [showErrors, setShowErrors] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(() => ADVANCED_KEYS.some((k) => appliedDraft[k] !== ''))
  // Back/Forward and links change the applied search: the form follows it.
  useEffect(() => {
    setDraft(appliedDraft)
    setShowErrors(false)
    if (ADVANCED_KEYS.some((k) => appliedDraft[k] !== '')) setAdvancedOpen(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedKey])
  const draftResult = useMemo(() => parseDraft(draft), [draft])
  const dirty = !draftsEqual(draft, appliedDraft)
  const badKeys = invalidKeys(applied)
  const runnable = isRunnable(applied)
  const formRef = useRef<HTMLFormElement>(null)

  /* ─── Search ─── */
  const apiParams = useMemo(
    () => ({ ...windowParams(w), ...searchApiParams(applied.filter, applied.sort, applied.order, pageSize) }),
    [w, applied, pageSize],
  )
  const queryKey = useMemo(() => ['netflow', 'forensics-search', paramsKey(apiParams)] as const, [apiParams])
  const search = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }) =>
      nf.get<ForensicsPageData>('/netflow/forensics/search', { ...apiParams, cursor: pageParam }, { signal, timeoutMs: 45_000 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.data?.next_cursor ?? undefined,
    enabled: runnable,
    retry: nfRetry,
    placeholderData: keepPreviousData,
    // Forensics never refreshes by itself: results are a snapshot until Refresh or a new search.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: false,
  })
  const pages = search.data?.pages ?? []
  // While a new search loads, the previous search's rows stay on screen (dimmed), but its count,
  // quality bar, snapshot and export totals are not this search's: they are withheld until it answers.
  const placeholder = search.isPlaceholderData
  const first = placeholder ? undefined : pages[0]?.data
  const meta = placeholder ? null : pages[0]?.meta ?? null
  const rows = useMemo(() => pages.flatMap((p) => p.data?.items ?? []), [pages])

  const refresh = () => {
    // Only the first page is fetched again (with a new snapshot); later pages load on demand.
    qc.setQueryData<SearchData>(queryKey, (d) => (d ? { pages: d.pages.slice(0, 1), pageParams: d.pageParams.slice(0, 1) } : d))
    void search.refetch()
  }

  const submit = (e?: FormEvent) => {
    e?.preventDefault()
    setShowErrors(true)
    if (!draftResult.ok) {
      // Focus the first field with a problem; nothing is sent.
      const firstBad = FLOW_FILTER_KEYS.find((k) => draftResult.errors[k])
      const el = firstBad ? formRef.current?.querySelector<HTMLElement>(`[id$="-${firstBad}"]`) : null
      if (firstBad && ADVANCED_KEYS.includes(firstBad)) setAdvancedOpen(true)
      window.setTimeout(() => el?.focus(), 0)
      return
    }
    const next = submitSearchParams(sp, draftResult.filter, { keepEvidence: !dirty })
    if (runnable && sameSearchParams(next, sp)) {
      refresh()
      return
    }
    setSp(next)
  }

  const reset = () => {
    setDraft(draftFromApplied({ filter: {}, invalid: {} }))
    setShowErrors(false)
    setSp(resetSearchParams(sp))
  }

  const runAll = () => {
    const next = new URLSearchParams(sp)
    next.set('run', '1')
    setSp(next)
  }

  const removeKeys = (keys: FlowFilterKey[]) => {
    const next = new URLSearchParams(sp)
    for (const k of keys) {
      next.delete(k)
      for (const a of FLOW_FILTER_ALIASES[k] ?? []) next.delete(a)
      if (k === 'iface') next.delete('iface_dir')
    }
    for (const k of [...EVIDENCE_PARAMS, 'cursor']) next.delete(k)
    next.set('run', '1')
    setSp(next)
  }

  const onSort = (col: ForensicsSort) => {
    const n = nextSort({ sort: applied.sort, order: applied.order }, col)
    setSp(sortSearchParams(sp, n.sort, n.order))
  }

  /* ─── Columns ─── */
  const [columnsOpen, setColumnsOpen] = useState(false)
  const toggleColumn = (key: string) => {
    const set = new Set(applied.columns)
    if (set.has(key)) {
      if (set.size === 1) return
      set.delete(key)
    } else set.add(key)
    setSp(columnsSearchParams(sp, [...set]), { replace: true })
  }

  /* ─── Expanded rows (kept across refresh and Load more) ─── */
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const toggle = (id: string) =>
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })

  /* ─── Retention (FE-11) ─── */
  const retention = useNfQuery<NfRetention>('/netflow/retention', null, { refetchInterval: false, staleTime: 300_000 })
  const rawDays = meta?.retention?.raw_days ?? retention.data?.data?.raw_days ?? null

  /* ─── Save view / update view (NF-12, NF-34) ─── */
  const [saveOpen, setSaveOpen] = useState(false)
  const { create, update } = useSavedViewMutations()
  const openedView = useSavedView(applied.viewId)
  const view = openedView.data?.data ?? null
  const columnsToSave = isDefaultColumns(applied.columns) ? null : applied.columns
  const sortToSave = applied.sort === 'timestamp' && applied.order === 'desc' ? null : applied.sort

  const saveNew = (d: SaveViewDraft) => {
    const query = buildSavedQuery({
      kind: 'forensics',
      filter: applied.filter,
      window: savedQueryWindow(w, d.timeMode, meta?.window),
      sort: sortToSave,
      order: sortToSave ? applied.order : null,
      columns: columnsToSave,
    })
    create.mutate(
      { name: d.name, description: d.description || null, visibility: d.visibility, pinned: d.pinned, kind: 'forensics', query },
      {
        onSuccess: (v) => {
          setSaveOpen(false)
          toast.success('View saved', `"${v.name}" is in Saved Views.`)
          const next = new URLSearchParams(sp)
          next.set('view', v.id)
          setSp(next, { replace: true })
        },
      },
    )
  }

  const updateView = () => {
    if (!view) return
    const previous = view.query as { window?: { mode?: string } } | null
    const mode = previous?.window?.mode === 'fixed' || w.key === 'custom' ? 'fixed' : 'relative'
    const query = buildSavedQuery({
      kind: 'forensics',
      filter: applied.filter,
      window: savedQueryWindow(w, mode, meta?.window),
      sort: sortToSave,
      order: sortToSave ? applied.order : null,
      columns: columnsToSave,
    })
    update.mutate(
      { id: view.id, patch: { query } },
      {
        onSuccess: (v) => toast.success('Saved view updated', `"${v.name}" now reopens this search.`),
        onError: (err) => toast.error('The view was not updated', errorText(err)),
      },
    )
  }

  /* ─── Export ─── */
  const [jobsOpen, setJobsOpen] = useState(false)
  // Pinned to the period and the received-at snapshot of the search, so the file holds exactly the
  // rows counted (records arriving later with timestamps inside the window are not added).
  const exportParams = useMemo(
    () => exportApiParams({ filter: applied.filter, sort: applied.sort, order: applied.order, page: first, windowParams: windowParams(w), tz: w.tz }),
    [applied, first, w],
  )
  const exportDisplayed = () => {
    const csv = displayedRowsCsv(rows, applied.columns)
    saveBlob(new Blob([csv], { type: 'text/csv;charset=utf-8' }), displayedRowsFilename(rows.length, serverNow()))
    toast.success('Displayed rows exported', `${formatCountExact(rows.length)} rows (only the rows loaded on this page).`)
  }

  // When the period is beyond raw retention the results panel explains it; the bar must not also
  // claim a zero-length "covered" period.
  const barMeta =
    first?.unavailable && meta?.window ? { ...meta, window: { ...meta.window, effective_from: null, effective_to: null } } : meta
  const evidence = applied.finding !== null || applied.findingId !== null
  const loaded = rows.length
  const total = first?.total ?? null
  const exact = first?.total_exact ?? false
  const copyLink = `${location.pathname}?${qs}`
  const retentionNote = rawDays != null ? `Raw records are kept for ${rawDays} days.` : null
  const headerLinks = forensicsHeaderLinks(applied.filter, w)
  // NSEL create/deny events match unless the search already chose firewall events (FE: volume cards exclude them).
  const fwEvents = applied.filter.fw_event === undefined ? firewallEventCount(first) : 0

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="Flow Forensics"
        icon={Search}
        crumb="Forensics"
        rootTo={headerLinks.root}
        parents={headerLinks.parents}
        description={
          <>
            Search individual flow records by any retained field, page through complete results and export them.{' '}
            {retentionNote}
          </>
        }
        actions={
          <>
            <TimeRangePicker
              rangeIdx={nfw.rangeIdx}
              isCustom={nfw.isCustom}
              customFrom={nfw.range.fromISO}
              customTo={nfw.range.toISO}
              onPreset={nfw.setPreset}
              onCustom={nfw.setCustom}
            />
            <Link
              to="/netflow/saved-views"
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-xs font-medium text-text hover:bg-surface2"
            >
              <Bookmark className="h-4 w-4" aria-hidden />
              Saved views
            </Link>
          </>
        }
      />

      {evidence && (
        <EvidenceBanner
          finding={applied.finding}
          findingId={applied.findingId}
          filter={applied.filter}
          window={w}
          runnable={runnable}
          broadenLink={(host) => broadenToHostLink(sp, host, applied.filter.exporter)}
        />
      )}

      <Card>
        <CardContent className="p-4">
          <form ref={formRef} onSubmit={submit} noValidate aria-label="Search flow records" className="space-y-3">
            <FilterBuilder
              draft={draft}
              onChange={(k, v) => setDraft((d) => ({ ...d, [k]: v }))}
              errors={draftResult.errors}
              showErrors={showErrors}
              advancedOpen={advancedOpen}
              onToggleAdvanced={() => setAdvancedOpen((o) => !o)}
              tz={w.tz}
            />
            <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
              <Button type="submit" size="sm" className="gap-1.5">
                <Search aria-hidden />
                {runnable && !dirty ? 'Search again' : 'Search'}
              </Button>
              <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={reset}>
                <RotateCcw aria-hidden />
                Reset
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="gap-1.5"
                onClick={() => {
                  create.reset()
                  setSaveOpen(true)
                }}
                disabled={!runnable || dirty || !can('netflow.manage')}
                title={!can('netflow.manage') ? 'Saving views needs the NetFlow manage permission' : dirty ? 'Apply the search first: a view saves the applied search' : undefined}
              >
                <Save aria-hidden />
                Save view
              </Button>
              {view?.can_edit && (
                <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={updateView} disabled={!runnable || dirty || update.isPending}>
                  {update.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Bookmark aria-hidden />}
                  Update “{view.name}”
                </Button>
              )}
              <span className="text-xs text-text2" role="status" aria-live="polite">
                {dirty
                  ? showErrors && !draftResult.ok
                    ? 'Fix the highlighted fields to search.'
                    : 'Changes not applied yet: press Search or Enter.'
                  : ''}
              </span>
            </div>
          </form>
        </CardContent>
      </Card>

      {(runnable || badKeys.length > 0) && (
        <section aria-label="Applied search" className="space-y-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text2">
            <span className="font-semibold uppercase tracking-wide">Applied search</span>
            <span>{applied.sort !== 'timestamp' || applied.order !== 'desc' ? sortLabel({ sort: applied.sort, order: applied.order }) : 'Newest first'}</span>
            {isFlowFilterEmpty(applied.filter) && badKeys.length === 0 && <span>No filters: every record in {w.label}</span>}
          </div>
          <FilterChips
            filter={applied.filter}
            invalid={applied.invalid}
            errors={applied.errors}
            tz={w.tz}
            onRemove={removeKeys}
            onClearAll={reset}
            copyLink={copyLink}
          />
          {badKeys.length > 0 && (
            <p role="alert" className="text-xs font-medium text-danger-fg">
              The link contains filters that are not valid. Fix them in the form or remove them; the search does not run until then.
            </p>
          )}
          {runnable && barMeta !== null && <DataQualityBar meta={barMeta} window={w} filter={applied.filter} />}
        </section>
      )}

      <Card>
        <CardContent className="space-y-3 p-4">
          {!runnable ? (
            <PanelMessage
              className="py-10"
              icon={Search}
              title={badKeys.length ? 'Fix the invalid filters to search' : 'Search raw flow records'}
              detail={
                <>
                  Enter one or more conditions and press Search, or search every record of the period. Addresses accept IPv4,
                  IPv6 and CIDR prefixes; a host matches as source or destination.
                  {badKeys.length === 0 && (
                    <span className="mt-3 block">
                      <Button type="button" size="sm" variant="outline" onClick={runAll}>
                        Search all records in {w.label}
                      </Button>
                    </span>
                  )}
                </>
              }
            />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <div className="min-w-0 flex-1">
                  <div className={cn('text-sm font-semibold', search.isError && !first ? 'text-danger-fg' : 'text-text')} aria-live="polite">
                    {searchHeading({ page: first, placeholder, failed: search.isError, loaded })}
                  </div>
                  {placeholder && rows.length > 0 && (
                    <div className="text-xs text-text2">The dimmed rows below are from the previous search until this one finishes.</div>
                  )}
                  {first && !first.unavailable && (
                    <div className="text-xs text-text2">
                      {first.snapshot_at ? `Records received up to ${formatDateTime(first.snapshot_at, w.tz)}` : ''}
                      {meta?.query_ms != null ? ` · query ${formatDuration(meta.query_ms)}` : ''}
                      {!exact && total !== null ? ' · counting stopped at its time limit, the total is a lower bound' : ''}
                    </div>
                  )}
                  {fwEvents > 0 && (
                    <div className="text-xs text-text2">
                      Includes {first?.fw_events?.exact === false ? 'at least ' : ''}{formatCountExact(fwEvents)} firewall event records
                      (NSEL connection created/denied) without traffic volume, which traffic totals and cards leave out.{' '}
                      <button type="button" className="font-medium text-primary underline-offset-2 hover:underline" onClick={() => setSp(flowRecordsOnlyParams(sp))}>
                        Show flow records only
                      </button>
                    </div>
                  )}
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  onClick={refresh}
                  disabled={search.isFetching}
                  aria-label="Refresh results"
                >
                  <RefreshCw className={cn(search.isFetching && !search.isFetchingNextPage && 'animate-spin')} aria-hidden />
                  <span className="hidden sm:inline">Refresh</span>
                </Button>
                {!first?.unavailable && (
                  <>
                    <div className="relative">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="gap-1.5"
                        aria-expanded={columnsOpen}
                        aria-controls="nf-forensics-columns"
                        onClick={() => setColumnsOpen((o) => !o)}
                      >
                        <Columns3 aria-hidden />
                        <span className="hidden sm:inline">Columns</span>
                        <span className="sr-only sm:hidden">Choose columns</span>
                      </Button>
                      {columnsOpen && (
                        <fieldset
                          onKeyDown={(e) => {
                            if (e.key === 'Escape') setColumnsOpen(false)
                          }}
                          id="nf-forensics-columns"
                          className="absolute right-0 z-30 mt-1 w-64 rounded-md border border-border bg-surface p-2 shadow-elevated"
                        >
                          <legend className="sr-only">Visible columns</legend>
                          <div className="max-h-72 space-y-0.5 overflow-auto">
                            {COLUMNS.map((c) => (
                              <label key={c.key} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs hover:bg-surface2">
                                <input
                                  type="checkbox"
                                  checked={applied.columns.includes(c.key)}
                                  onChange={() => toggleColumn(c.key)}
                                  className="accent-[rgb(var(--primary))]"
                                />
                                {c.label}
                              </label>
                            ))}
                          </div>
                          <div className="mt-1 flex justify-between border-t border-border pt-1">
                            <button
                              type="button"
                              className="rounded px-1.5 py-0.5 text-xs text-text2 hover:text-text"
                              onClick={() => setSp(columnsSearchParams(sp, []), { replace: true })}
                            >
                              Default columns
                            </button>
                            <button type="button" className="rounded px-1.5 py-0.5 text-xs font-medium text-text" onClick={() => setColumnsOpen(false)}>
                              Done
                            </button>
                          </div>
                        </fieldset>
                      )}
                    </div>
                    {first && (
                    <ExportControls
                      displayedCount={loaded}
                      onExportDisplayed={exportDisplayed}
                      total={total}
                      exact={exact}
                      allowed={first?.export?.allowed ?? can('netflow.export')}
                      streamMax={first?.export?.stream_max_rows ?? 100_000}
                      jobMax={first?.export?.job_max_rows ?? 2_000_000}
                      params={exportParams}
                      jobsOpen={jobsOpen}
                      onJobsOpenChange={setJobsOpen}
                    />
                    )}
                  </>
                )}
              </div>
              {jobsOpen && <ExportJobsPanel tz={w.tz} onClose={() => setJobsOpen(false)} />}

              {first?.unavailable ? (
                <PanelMessage
                  className="py-10"
                  icon={DatabaseZap}
                  iconClass="text-warning-fg"
                  title="Individual records for this period no longer exist"
                  detail={
                    <>
                      {first.unavailable.message}
                      <span className="mt-3 block">
                        <Link
                          className="font-medium text-primary underline-offset-2 hover:underline"
                          to={buildNetflowLink('/netflow', { filter: applied.filter, window: w })}
                        >
                          Open the overview for this period
                        </Link>
                      </span>
                    </>
                  }
                />
              ) : (
                <QueryPanel
                  query={search}
                  label="flow records"
                  skeleton="rows"
                  isEmpty={(d: SearchData) => (d.pages[0]?.data?.items.length ?? 0) === 0}
                  empty={`No flow records matched this search in ${w.label}`}
                >
                  {() => (
                    <div className="space-y-3">
                      <ResultsTable
                        rows={rows}
                        columns={applied.columns}
                        sort={applied.sort}
                        order={applied.order}
                        onSort={onSort}
                        onSortChange={(s2, o2) => setSp(sortSearchParams(sp, s2, o2))}
                        expanded={expanded}
                        onToggle={toggle}
                        tz={w.tz}
                        window={w}
                        filter={applied.filter}
                        dimmed={search.isFetching && !search.isFetchingNextPage}
                      />
                      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text2">
                        <span>{placeholder ? 'Previous search: updating…' : showingLabel(loaded, total, exact)}</span>
                        {placeholder ? null : search.hasNextPage ? (
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="gap-1.5"
                            onClick={() => void search.fetchNextPage()}
                            disabled={search.isFetchingNextPage}
                          >
                            {search.isFetchingNextPage && <Loader2 className="animate-spin" aria-hidden />}
                            {search.isFetchingNextPage ? 'Loading…' : `Load the next ${formatCountExact(pageSize)}`}
                          </Button>
                        ) : (
                          loaded > 0 && <span>End of results</span>
                        )}
                      </div>
                      {search.isFetchNextPageError && (
                        <p role="alert" className="text-xs text-danger-fg">
                          The next page could not be loaded: {errorText(search.error)}
                        </p>
                      )}
                    </div>
                  )}
                </QueryPanel>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <SaveViewDialog
        open={saveOpen}
        onOpenChange={setSaveOpen}
        filter={applied.filter}
        window={w}
        metaWindow={meta?.window ?? null}
        canShare={can('netflow.manage_shared')}
        pending={create.isPending}
        error={create.isError ? errorText(create.error) : null}
        extraSummary={
          <>
            {sortLabel({ sort: applied.sort, order: applied.order }) ?? 'Newest first'}
            {columnsToSave ? ` · ${columnsToSave.length} chosen columns` : ''}
          </>
        }
        onSubmit={saveNew}
      />
    </div>
  )
}
