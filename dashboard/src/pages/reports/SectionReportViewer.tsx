/**
 * Report viewer: /reports/view/:key — a built-in template, or `custom` with
 * `custom_id` for an organisation template.
 *
 * URL contract (shared with the library, designer and schedules):
 *   range=<PeriodId|1h|24h|7d|1M|month|last_month|quarter|custom> &from &to
 *   custom_id=<uuid>  nodes=<device id,…>  app=<application id>  nf=<NetFlow scope>
 * No range → the template's default period, else the last 24 hours. Calendar
 * periods and custom ranges are in the organisation timezone.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  AppWindow, ArrowLeft, CalendarClock, ChevronDown, CopyPlus, FileQuestion, Pencil, RefreshCw, Server,
  SlidersHorizontal, X,
} from 'lucide-react'
import '@/features/reports/render/print.css'
import '@/features/reports/viewer/viewer.css'
import { cn } from '@/lib/utils'
import { formatPeriod } from '@/lib/branding'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { QueryFailure } from '@/components/ui/QueryState'
import { FilterChips } from '@/components/netflow/FilterChips'
import { ReportScheduleFormDialog } from '@/components/forms/ReportScheduleFormDialog'
import { useRenderReport, useReportBranding, useReportCatalog, type RenderParams } from '@/features/reports/api'
import { bucketNow, PERIODS } from '@/features/reports/periods'
import { recordRecent, refFor } from '@/features/reports/storage'
import type { PeriodId, ScopeFilter, SectionDef } from '@/features/reports/types'
import { ReportDocument, ReportDocumentSkeleton } from '@/features/reports/render/ReportDocument'
import { ContentsRail, ContentsSelect, useScrollSpy } from '@/features/reports/render/ContentsRail'
import { sectionAnchorId } from '@/features/reports/render/format'
import { PeriodPicker } from '@/features/reports/viewer/PeriodPicker'
import { DeviceScopePicker } from '@/features/reports/viewer/DeviceScopePicker'
import { ApplicationScopePicker } from '@/features/reports/viewer/ApplicationScopePicker'
import { ExportMenu } from '@/features/reports/viewer/ExportMenu'
import { ShareMenu } from '@/features/reports/viewer/ShareMenu'
import { printReport, ReportPrintStyle } from '@/features/reports/viewer/print'
import {
  filterableFor, needsDefaultPeriod, parseNodes, parseSelection, resolveWindow, selectionKey, selectionLabel,
  selectionParams, type PeriodSelection,
} from '@/features/reports/viewer/range'
import { NetflowScopePicker, parseReportScope, reportScopeObject, reportScopeParam } from './NetflowReportScope'
import type { FlowFilter } from '@/lib/netflow/filters'

const ID_PREFIX = 'rpt'
const CALENDAR = new Set<string>(PERIODS.filter((p) => p.kind === 'calendar').map((p) => p.id))

function NotFoundState({ legacy }: { legacy?: boolean }) {
  return (
    <div className="mx-auto mt-6 max-w-lg rounded-xl border border-border bg-surface px-6 py-12 text-center shadow-card dark:shadow-card-dark">
      <FileQuestion className="mx-auto h-9 w-9 text-muted" aria-hidden />
      <h1 className="mt-3 text-lg font-semibold text-text">{legacy ? 'This report opens elsewhere' : 'Report not found'}</h1>
      <p className="mt-1.5 text-sm text-muted">
        {legacy
          ? 'This is a classic report type that the document viewer does not render. Open it from the report library.'
          : 'The link is not valid, or the template was deleted or is private to its owner.'}
      </p>
      <Button asChild className="mt-6">
        <Link to="/reports"><ArrowLeft aria-hidden /> Back to the report library</Link>
      </Button>
    </div>
  )
}

function ScopeChip({ icon: Icon, children }: { icon: typeof Server; children: string }) {
  return (
    <li className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-[11px] font-medium text-text2">
      <Icon className="h-3 w-3 text-muted" aria-hidden />
      {children}
    </li>
  )
}

export function SectionReportViewer() {
  const { key = '' } = useParams<{ key: string }>()
  const [sp, setSp] = useSearchParams()
  const customId = sp.get('custom_id')
  const can = useCan()
  const userId = useAuth((s) => s.user?.id)

  const catalogQ = useReportCatalog()
  const catalog = catalogQ.data
  const brandingQ = useReportBranding()
  const orgBranding = brandingQ.branding

  /* ── Template ─────────────────────────────────────────────────────── */
  const custom = key === 'custom' ? catalog?.custom.find((c) => c.id === customId) ?? null : null
  const builtin = key !== 'custom' ? catalog?.types.find((t) => t.key === key) ?? null : null
  const catalogSettled = catalog !== undefined || catalogQ.isError
  const unknownKey = !key || (key === 'custom' && !customId) || (catalog !== undefined && key !== 'custom' && !builtin)
  const legacy = builtin?.engine === 'legacy'
  const template = key === 'custom' ? custom : builtin
  const sectionMeta = useMemo<Record<string, SectionDef>>(
    () => Object.fromEntries((catalog?.sections ?? []).map((s) => [s.id, s])),
    [catalog?.sections],
  )
  const filterable = useMemo(() => filterableFor(template, catalog?.sections), [template, catalog?.sections])

  /* ── Period ───────────────────────────────────────────────────────── */
  const rangeParam = sp.get('range')
  const fromParam = sp.get('from')
  const toParam = sp.get('to')
  const defaultPeriod: PeriodId = custom?.default_period || builtin?.default_period || 'last_24h'
  const needsDefault = needsDefaultPeriod(rangeParam, fromParam, toParam)
  const selection = useMemo(
    () => parseSelection(rangeParam, fromParam, toParam, defaultPeriod),
    [rangeParam, fromParam, toParam, defaultPeriod],
  )
  const selKey = selectionKey(selection)
  const [now, setNow] = useState(() => bucketNow())
  const tz = orgBranding.timezone
  const weekStart = orgBranding.week_start
  const win = useMemo(
    () => resolveWindow(selection, tz, weekStart, now),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selKey, tz, weekStart, now],
  )

  const update = (mutate: (next: URLSearchParams) => void) => {
    const next = new URLSearchParams(sp)
    mutate(next)
    setSp(next, { replace: true })
  }

  const setSelection = (sel: PeriodSelection) => {
    const p = selectionParams(sel)
    setNow(bucketNow())
    update((next) => {
      next.set('range', p.range)
      if (p.from && p.to) {
        next.set('from', p.from)
        next.set('to', p.to)
      } else {
        next.delete('from')
        next.delete('to')
      }
    })
  }

  /* ── Scope ────────────────────────────────────────────────────────── */
  const nodeIds = useMemo(() => parseNodes(sp.get('nodes')), [sp])
  const appId = sp.get('app') || null
  const nfScope = useMemo(() => parseReportScope(sp.get('nf')), [sp])
  const nfParam = reportScopeParam(nfScope.filter)
  const hasScopeParams = nodeIds.length > 0 || !!appId || !!nfParam
  // Send a scope only to reports whose sections honour it; if the catalog is
  // unavailable, send what the link carries (the server ignores the rest).
  const allows = (f: ScopeFilter) => (template ? filterable.includes(f) : catalogQ.isError)
  const sentNodes = allows('devices') ? nodeIds : []
  const sentApp = allows('applications') ? appId : null
  const sentNf = allows('netflow') ? nfParam : ''

  const setNodeIds = (ids: string[]) => update((n) => (ids.length ? n.set('nodes', ids.join(',')) : n.delete('nodes')))
  const setAppId = (id: string | null) => update((n) => (id ? n.set('app', id) : n.delete('app')))
  const setNf = (f: FlowFilter) => {
    const q = reportScopeParam(f)
    update((n) => (q ? n.set('nf', q) : n.delete('nf')))
  }
  const clearScope = () => update((n) => {
    n.delete('nodes')
    n.delete('app')
    n.delete('nf')
  })

  /* ── Render ───────────────────────────────────────────────────────── */
  const calendar = selection.kind === 'period' && CALENDAR.has(selection.id)
  const ready = !unknownKey && !legacy
    && (!needsDefault || catalogSettled)
    && (!hasScopeParams || catalogSettled)
    && (!calendar || !brandingQ.isLoading)
  const params: RenderParams | null = ready
    ? {
        key,
        customId: key === 'custom' ? customId : null,
        from: win.from.toISOString(),
        to: win.to.toISOString(),
        deviceIds: sentNodes,
        applicationId: sentApp,
        netflow: sentNf || null,
      }
    : null
  const renderQ = useRenderReport(params)

  // Keep the previous render while a new period or scope loads — but never
  // show one report's data under another report's URL.
  const ref = refFor(key, customId)
  const [loadedRef, setLoadedRef] = useState<string | null>(null)
  useEffect(() => {
    if (renderQ.data && !renderQ.isPlaceholderData) setLoadedRef(ref)
  }, [renderQ.data, renderQ.isPlaceholderData, ref])
  const report = renderQ.data && !(renderQ.isPlaceholderData && loadedRef !== ref) ? renderQ.data : undefined
  const updating = !!report && (renderQ.isPlaceholderData || renderQ.isFetching)
  const status = (renderQ.error as { response?: { status?: number } } | null)?.response?.status
  const notFound = unknownKey || status === 404

  const recorded = useRef<string | null>(null)
  useEffect(() => {
    if (!report || renderQ.isPlaceholderData || recorded.current === ref) return
    recorded.current = ref
    recordRecent(userId, ref)
  }, [report, renderQ.isPlaceholderData, ref, userId])

  const rerun = () => {
    const n = bucketNow()
    if (selection.kind === 'custom' || n.getTime() === now.getTime()) void renderQ.refetch()
    else setNow(n)
  }

  /* ── Permissions ──────────────────────────────────────────────────── */
  const canExport = can('reports.export') && (catalog ? catalog.can.export : true)
  const canManage = can('reports.manage') && (catalog ? catalog.can.manage : true)
  // Schedule writes are refused for accounts limited to a visibility scope.
  const canSchedule = canManage && !report?.scoped

  /* ── Sticky chrome, contents and scroll-spy ───────────────────────── */
  const toolbarRef = useRef<HTMLDivElement>(null)
  const [toolbarH, setToolbarH] = useState(52)
  useLayoutEffect(() => {
    const el = toolbarRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setToolbarH(Math.round(el.getBoundingClientRect().height)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [unknownKey, legacy])
  const remPx = useMemo(() => (typeof document !== 'undefined' ? parseFloat(getComputedStyle(document.documentElement).fontSize) : 14) || 14, [])
  const offsetPx = Math.round(remPx * 3.5 + toolbarH)
  const sections = report?.sections ?? []
  const anchorIds = useMemo(() => sections.map((_, i) => sectionAnchorId(ID_PREFIX, i)), [sections])
  const active = useScrollSpy(anchorIds, offsetPx)
  const hasRail = sections.length > 1

  const [pinned, setPinned] = useState(false)
  useEffect(() => {
    const el = document.getElementById(`${ID_PREFIX}-title`)
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(
      ([e]) => setPinned(!e.isIntersecting && e.boundingClientRect.top < offsetPx),
      { rootMargin: `-${offsetPx}px 0px 0px 0px` },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [report, offsetPx])

  /* ── Derived labels ───────────────────────────────────────────────── */
  const title = report?.title || custom?.name || builtin?.title || 'Report'
  const branding = useMemo(() => ({ ...orgBranding, ...(report?.branding ?? {}) }), [orgBranding, report?.branding])
  const periodText = formatPeriod(win.from, win.to, branding)

  const linkFor = (sel: PeriodSelection) => {
    const q = new URLSearchParams(sp)
    const p = selectionParams(sel)
    q.set('range', p.range)
    if (p.from && p.to) {
      q.set('from', p.from)
      q.set('to', p.to)
    } else {
      q.delete('from')
      q.delete('to')
    }
    return `${window.location.origin}/reports/view/${encodeURIComponent(key)}?${q.toString()}`
  }
  const exactUrl = linkFor({ kind: 'custom', from: win.from, to: win.to })
  const latestUrl = selection.kind === 'custom' ? null : linkFor(selection)

  const scheduleFilters: Record<string, unknown> = {}
  if (sentNodes.length) scheduleFilters.device_ids = sentNodes
  if (sentApp) scheduleFilters.application_id = sentApp
  if (sentNf) scheduleFilters.netflow = reportScopeObject(nfScope.filter)
  // Built as a variable: the dialog's prefill type grows over time (name, format).
  const schedulePrefill = {
    name: title,
    report_type: key === 'custom' ? 'custom' : key,
    custom_report_id: key === 'custom' ? customId : null,
    period: selection.kind === 'period' ? selection.id : undefined,
    format: 'pdf',
    filters: Object.keys(scheduleFilters).length ? scheduleFilters : undefined,
  }
  const [scheduleOpen, setScheduleOpen] = useState(false)

  if (notFound || legacy) {
    return <NotFoundState legacy={legacy && !notFound} />
  }

  const scopeSlot = (
    <div className="flex flex-wrap items-center gap-1.5">
      <ul role="list" className="flex flex-wrap items-center gap-1.5">
        {report?.scoped && report.scope_label && <ScopeChip icon={SlidersHorizontal}>{report.scope_label}</ScopeChip>}
        {sentNodes.length > 0 && (
          <ScopeChip icon={Server}>{`${sentNodes.length} selected device${sentNodes.length === 1 ? '' : 's'}`}</ScopeChip>
        )}
        {sentApp && <ScopeChip icon={AppWindow}>{`Application ${sentApp}`}</ScopeChip>}
        {!report?.scoped && !sentNodes.length && !sentApp && !sentNf && (
          <ScopeChip icon={SlidersHorizontal}>All monitored infrastructure</ScopeChip>
        )}
      </ul>
      {sentNf && <FilterChips filter={nfScope.filter} tz={tz} />}
      {(sentNodes.length > 0 || sentApp || sentNf) && (
        <button
          type="button"
          onClick={clearScope}
          className="inline-flex items-center gap-0.5 rounded px-1 text-[11px] font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 print:hidden"
        >
          <X className="h-3 w-3" aria-hidden /> Clear scope
        </button>
      )}
    </div>
  )

  const customBadges = custom ? (
    <span className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">
      {custom.visibility === 'shared' ? 'Shared' : 'Private'}
      {custom.owner_name && <> · {custom.is_owner ? 'yours' : custom.owner_name}</>}
    </span>
  ) : null

  return (
    <div
      className="zp-report-viewer zp-report-print mx-auto w-full max-w-[1480px]"
      style={{ '--zp-report-offset': `${offsetPx}px` } as CSSProperties}
    >
      <ReportPrintStyle branding={branding} title={title} />

      {/* Toolbar: sticks under the app header. */}
      <div
        ref={toolbarRef}
        className="relative z-10 -mx-5 -mt-5 mb-4 border-b md:sticky md:top-11 border-border bg-bg/90 px-5 py-2.5 backdrop-blur supports-[backdrop-filter]:bg-bg/75 print:hidden"
      >
        <div className="flex flex-wrap items-center gap-2">
          <div className="mr-auto flex min-w-0 items-center gap-2">
            <Link
              to="/reports"
              className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md px-1.5 text-xs font-medium text-muted transition-colors hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            >
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
              <span>Reports</span>
            </Link>
            <span
              aria-hidden
              className={cn(
                'min-w-0 truncate border-l border-border pl-2 text-sm font-semibold text-text transition-opacity duration-150',
                pinned ? 'opacity-100' : 'pointer-events-none opacity-0',
              )}
            >
              {title}
            </span>
          </div>

          <div role="group" aria-label="Report period and scope" className="flex flex-wrap items-center gap-2">
            <PeriodPicker value={selection} onChange={setSelection} window={win} branding={branding} defaultPeriod={template ? defaultPeriod : null} />
            {filterable.includes('devices') && <DeviceScopePicker selected={nodeIds} onChange={setNodeIds} />}
            {filterable.includes('applications') && <ApplicationScopePicker selected={appId} onChange={setAppId} />}
            {filterable.includes('netflow') && <NetflowScopePicker value={nfScope.filter} onChange={setNf} />}
          </div>

          <span aria-hidden className="hidden h-5 w-px bg-border sm:block" />

          <div role="group" aria-label="Report actions" className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-8 w-8 p-0"
              onClick={rerun}
              disabled={!params || renderQ.isFetching}
              aria-label="Generate again with the latest data"
              title="Generate again"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', renderQ.isFetching && 'animate-spin')} aria-hidden />
            </Button>
            <ShareMenu exactUrl={exactUrl} exactLabel={periodText} latestUrl={latestUrl} latestLabel={selectionLabel(selection)} />
            {canManage && (key === 'custom' && customId ? (
              custom?.can_edit ? (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="outline" size="sm" className="h-8 gap-1.5">
                      <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
                      <span className="hidden text-xs font-semibold xl:inline">Customize</span>
                      <ChevronDown className="h-3 w-3 opacity-60" aria-hidden />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent className="w-60">
                    <DropdownMenuItem asChild>
                      <Link to={`/reports/builder?edit=${encodeURIComponent(customId)}`}>
                        <Pencil aria-hidden /> Edit this template
                      </Link>
                    </DropdownMenuItem>
                    <DropdownMenuItem asChild>
                      <Link to={`/reports/builder?clone=${encodeURIComponent(customId)}`}>
                        <CopyPlus aria-hidden /> Customize a copy
                      </Link>
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : (
                <Button asChild variant="outline" size="sm" className="h-8 gap-1.5">
                  <Link to={`/reports/builder?clone=${encodeURIComponent(customId)}`} aria-label="Customize a copy of this template">
                    <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
                    <span className="hidden text-xs font-semibold xl:inline">Customize</span>
                  </Link>
                </Button>
              )
            ) : (
              <Button asChild variant="outline" size="sm" className="h-8 gap-1.5">
                <Link to={`/reports/builder?from=${encodeURIComponent(key)}`} aria-label="Customize: start a template from this report">
                  <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
                  <span className="hidden text-xs font-semibold xl:inline">Customize</span>
                </Link>
              </Button>
            ))}
            {canSchedule && (
              <Button variant="outline" size="sm" className="h-8 gap-1.5" onClick={() => setScheduleOpen(true)} aria-label="Schedule this report with the current period and scope">
                <CalendarClock className="h-3.5 w-3.5" aria-hidden />
                <span className="hidden text-xs font-semibold lg:inline">Schedule</span>
              </Button>
            )}
            {canExport && <ExportMenu params={params} formats={builtin?.formats} onPrint={printReport} />}
          </div>
        </div>

        {nfScope.invalid.length > 0 && (
          <p role="status" className="mt-2 rounded-md border border-warning/35 bg-warning/10 px-2.5 py-1.5 text-[11px] text-text">
            <span className="font-semibold text-warning-fg">Not applied:</span> the link's NetFlow scope has invalid {nfScope.invalid.join(', ')}.
          </p>
        )}
        {hasScopeParams && template && (nodeIds.length > sentNodes.length || (appId && !sentApp) || (nfParam && !sentNf)) && (
          <p role="status" className="mt-2 text-[11px] text-muted">
            Part of the link's scope does not apply to this report's sections and was ignored.
          </p>
        )}
      </div>

      {renderQ.isError && !report ? (
        <div className="mx-auto max-w-2xl">
          <QueryFailure error={renderQ.error} what="this report" onRetry={() => void renderQ.refetch()} retrying={renderQ.isFetching} />
        </div>
      ) : !report ? (
        <ReportDocumentSkeleton />
      ) : (
        <div className={cn(hasRail && 'lg:grid lg:grid-cols-[13.5rem_minmax(0,1fr)] lg:gap-6 print:!block')}>
          {hasRail && (
            <div className="hidden lg:block print:hidden">
              <div className="sticky top-[var(--zp-report-offset)] max-h-[calc(100vh_-_var(--zp-report-offset)_-_1rem)] overflow-y-auto pb-4">
                <ContentsRail sections={sections} idPrefix={ID_PREFIX} active={active} />
              </div>
            </div>
          )}
          <div className="min-w-0">
            {hasRail && (
              <ContentsSelect sections={sections} idPrefix={ID_PREFIX} active={active} className="mb-3 lg:hidden print:hidden" />
            )}
            <p role="status" className="sr-only">
              {updating ? 'Updating the report…' : `${title}, ${periodText}`}
            </p>
            <div className="relative" aria-busy={updating}>
              {updating && (
                <div role="progressbar" aria-label="Updating report" className="absolute inset-x-0 -top-2 h-0.5 overflow-hidden rounded-full bg-primary/15 print:hidden">
                  <div className="zp-report-progress h-full w-1/3 rounded-full bg-primary" />
                </div>
              )}
              {renderQ.isError && (
                <div className="mb-3 print:hidden">
                  <QueryFailure compact error={renderQ.error} what="the latest version" onRetry={() => void renderQ.refetch()} retrying={renderQ.isFetching} />
                </div>
              )}
              <div className={cn('transition-opacity duration-200', renderQ.isPlaceholderData && 'opacity-60')}>
                <ReportDocument
                  report={report}
                  branding={orgBranding}
                  sectionMeta={sectionMeta}
                  canExport={canExport}
                  idPrefix={ID_PREFIX}
                  origin={key === 'custom' ? 'custom' : 'builtin'}
                  scope={scopeSlot}
                  badges={customBadges}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {canSchedule && (
        <ReportScheduleFormDialog
          open={scheduleOpen}
          onOpenChange={setScheduleOpen}
          schedule={null}
          prefill={schedulePrefill}
        />
      )}
    </div>
  )
}

export default SectionReportViewer
