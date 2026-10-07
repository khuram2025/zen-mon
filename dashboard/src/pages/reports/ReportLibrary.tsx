/**
 * Report Library: the template gallery. A rail of views and categories, a
 * toolbar (search, audience, origin, sort, layout), the "All" overview with
 * Featured / Recently viewed shelves and category sections, a details drawer,
 * and the Delivered reports list. View state lives in the URL
 * (?view=&category=&q=&audience=&origin=&sort=); favourites, recently viewed
 * and the layout are per-user browser preferences (features/reports/storage).
 */
import { useCallback, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  CalendarClock, FileText, FolderOpen, History, Plus, SearchX, Star, UserRound, Users,
} from 'lucide-react'
import { copyText } from '@/lib/utils'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { ReportScheduleFormDialog } from '@/components/forms/ReportScheduleFormDialog'
import { useDuplicateTemplate, useReportBranding, useReportCatalog } from '@/features/reports/api'
import { describeFailure } from '@/lib/access'
import { LibraryActionsProvider, type LibraryActions } from '@/features/reports/library/actions'
import { DeleteTemplateDialog, type DeleteTarget } from '@/features/reports/library/DeleteTemplateDialog'
import { DeliveredReports } from '@/features/reports/library/DeliveredReports'
import { LibraryRail, RailSelect, type RailSelection } from '@/features/reports/library/LibraryRail'
import { LibraryList, LibraryOverview, CARD_GRID } from '@/features/reports/library/LibraryResults'
import { LibraryFiltersBar, SearchBox } from '@/features/reports/library/LibraryToolbar'
import {
  applyFilters, audienceOptions, buildLibraryItems, DEFAULT_FILTERS, filtersFromParams, filtersToParams,
  isFiltered, LIBRARY_VIEWS, openLink, railCategories, schedulePrefill, sectionMap, viewCounts,
  type LibraryFilters, type LibraryItem, type SchedulePrefill,
} from '@/features/reports/library/model'
import { EmptyState } from '@/features/reports/library/parts'
import { TemplateDrawer } from '@/features/reports/library/TemplateDrawer'
import { useReportPrefs } from '@/features/reports/library/usePrefs'

const SCOPED_NOTE = 'Your account is limited to a visibility scope, so it cannot create report schedules. Open the report to see the data your scope includes.'

export default function ReportLibrary() {
  const catalogQ = useReportCatalog()
  const catalog = catalogQ.data
  const { branding } = useReportBranding()
  const prefs = useReportPrefs()
  const can = useCan()
  const user = useAuth((s) => s.user)
  const [params, setParams] = useSearchParams()
  const filters = useMemo(() => filtersFromParams(params), [params])

  const update = useCallback((patch: Partial<LibraryFilters>, push = false) => {
    setParams(filtersToParams({ ...filters, ...patch }), { replace: !push })
  }, [filters, setParams])

  /* --- capabilities ------------------------------------------------ */
  const canManage = can('reports.manage') && (catalog?.can.manage ?? true)
  const scoped = (user?.scope_tags?.length ?? 0) + (user?.role_scope_tags?.length ?? 0) > 0
  const canSchedule = canManage && !scoped

  /* --- data -------------------------------------------------------- */
  const items = useMemo(() => (catalog ? buildLibraryItems(catalog) : []), [catalog])
  const sections = useMemo(() => sectionMap(catalog ?? { sections: [] }), [catalog])
  const ctx = useMemo(() => ({ favorites: prefs.favorites, recent: prefs.recent }), [prefs.favorites, prefs.recent])
  const result = useMemo(() => applyFilters(items, filters, ctx), [items, filters, ctx])
  const counts = useMemo(() => viewCounts(items, ctx), [items, ctx])
  const categories = useMemo(() => railCategories(items, catalog?.categories ?? []), [items, catalog])
  const audiences = useMemo(() => audienceOptions(items), [items])

  /* --- dialogs ----------------------------------------------------- */
  const [detailRef, setDetailRef] = useState<string | null>(null)
  const detail = detailRef ? items.find((i) => i.ref === detailRef) ?? null : null
  const [scheduling, setScheduling] = useState<SchedulePrefill | null>(null)
  const [deleting, setDeleting] = useState<DeleteTarget | null>(null)
  const duplicate = useDuplicateTemplate()

  const actions: LibraryActions = {
    canManage,
    canSchedule,
    scheduleNote: canManage && scoped ? SCOPED_NOTE : null,
    isFavorite: (item) => prefs.favorites.includes(item.ref),
    toggleFavorite: (item) => { prefs.toggleFavorite(item.ref) },
    showDetails: (item) => setDetailRef(item.ref),
    schedule: (item) => {
      setDetailRef(null)
      setScheduling({ ...schedulePrefill(item), name: item.title })
    },
    duplicate: (item) => {
      if (!item.customId) return
      duplicate.mutate(item.customId, {
        onSuccess: () => toast.success('Template duplicated', `A private copy of “${item.title}” is in My templates.`),
        onError: (e) => toast.error('The template was not duplicated', describeFailure(e).message),
      })
    },
    remove: (item) => {
      setDetailRef(null)
      setDeleting({ item, schedules: item.schedules })
    },
    copyLink: (item) => {
      void copyText(`${window.location.origin}${openLink(item)}`).then((ok) =>
        ok ? toast.success('Link copied', item.title) : toast.error('Could not copy the link', 'Your browser blocked clipboard access.'))
    },
    busyId: duplicate.isPending ? duplicate.variables ?? null : null,
  }

  const selection: RailSelection = { view: filters.view, category: filters.category }
  const select = (s: RailSelection) => update({ view: s.view, category: s.category }, true)
  const clearFilters = () => update({ q: '', audience: null, origin: 'all' })
  const delivered = filters.view === 'delivered'
  const filtered = isFiltered(filters)
  const overview = filters.view === 'all' && !filters.category && !filtered && prefs.layout === 'grid'
  const selectionLabel = filters.category
    ? categories.find((c) => c.id === filters.category)?.title ?? 'Category'
    : LIBRARY_VIEWS.find((v) => v.id === filters.view)?.label ?? 'All reports'

  return (
    <LibraryActionsProvider value={actions}>
      <div className="space-y-5">
        {/* Header */}
        <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight text-text">
              <FileText className="h-5 w-5 shrink-0 text-primary" aria-hidden />
              Reports
            </h1>
            <p className="mt-0.5 max-w-3xl text-xs text-text2">
              Built-in and organisation report templates. Open one for any period, schedule it for delivery, or customize a copy.
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to="/reports/schedules"><CalendarClock aria-hidden /> Schedules</Link>
            </Button>
            {canManage && (
              <Button asChild size="sm">
                <Link to="/reports/builder"><Plus aria-hidden /> New report</Link>
              </Button>
            )}
          </div>
        </header>

        <div className="flex gap-6">
          <LibraryRail
            selection={selection}
            viewCounts={counts}
            categories={categories}
            onSelect={select}
            loading={catalogQ.isLoading}
          />

          <div className="min-w-0 flex-1 space-y-4">
            {/* Toolbar */}
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <div className="w-full sm:w-56 lg:hidden">
                  <RailSelect selection={selection} viewCounts={counts} categories={categories} onSelect={select} />
                </div>
                <SearchBox
                  value={filters.q}
                  onChange={(q) => update({ q })}
                  label={delivered ? 'Search delivered reports' : 'Search report templates'}
                  placeholder={delivered ? 'Search delivered reports' : 'Search titles, sections and tags'}
                />
                {!delivered && (
                  <LibraryFiltersBar
                    filters={filters}
                    audiences={audiences}
                    layout={prefs.layout}
                    onChange={(patch) => update(patch)}
                    onLayout={prefs.setLayout}
                  />
                )}
              </div>
              {!delivered && catalog && (
                <div className="flex min-h-[1.25rem] flex-wrap items-center justify-between gap-2 text-xs text-muted">
                  <p aria-live="polite" className="tabular-nums">
                    <span className="font-medium text-text2">{selectionLabel}</span>
                    {' · '}Showing <span className="font-medium text-text">{result.items.length}</span> of {result.scopeTotal} templates
                  </p>
                  {filtered && (
                    <button type="button" onClick={clearFilters} className="rounded font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50">
                      Clear filters
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Body */}
            {delivered ? (
              <DeliveredReports q={filters.q} branding={branding} canSchedule={canSchedule} onClearSearch={() => update({ q: '' })} />
            ) : catalogQ.isLoading ? (
              <LibrarySkeleton />
            ) : catalogQ.isError && !catalog ? (
              <QueryFailure error={catalogQ.error} what="the report catalog" onRetry={() => void catalogQ.refetch()} retrying={catalogQ.isFetching} />
            ) : (
              <>
                {catalogQ.isError && (
                  <QueryFailure compact error={catalogQ.error} what="the report catalog" onRetry={() => void catalogQ.refetch()} retrying={catalogQ.isFetching} />
                )}
                {items.length === 0 ? (
                  <EmptyState icon={<FileText className="h-5 w-5" />} title="No reports are available to your role">
                    Every template reads data from modules your role cannot see. Ask an administrator for access to the areas you report on.
                  </EmptyState>
                ) : result.items.length === 0 ? (
                  <NoResults
                    filters={filters}
                    categoryTitle={selectionLabel}
                    canManage={canManage}
                    onClear={clearFilters}
                    onSearchAll={() => update({ view: 'all', category: null }, true)}
                    onBrowse={() => select({ view: 'builtin', category: null })}
                  />
                ) : overview ? (
                  <LibraryOverview
                    items={result.items}
                    categories={catalog?.categories ?? []}
                    recent={prefs.recent}
                    onSelectCategory={(id) => select({ view: 'all', category: id })}
                  />
                ) : (
                  <LibraryList
                    items={result.items}
                    layout={prefs.layout}
                    sort={filters.sort}
                    onSortByName={() => update({ sort: filters.sort === 'name' ? DEFAULT_FILTERS.sort : 'name' })}
                    label={`${selectionLabel} report templates`}
                  />
                )}
              </>
            )}
          </div>
        </div>
      </div>

      <TemplateDrawer
        item={detail}
        open={!!detail}
        onOpenChange={(o) => { if (!o) setDetailRef(null) }}
        sections={sections}
        branding={branding}
      />

      {scheduling && (
        <ReportScheduleFormDialog
          open
          onOpenChange={(o) => { if (!o) setScheduling(null) }}
          schedule={null}
          prefill={scheduling}
        />
      )}

      <DeleteTemplateDialog target={deleting} onClose={() => setDeleting(null)} />
    </LibraryActionsProvider>
  )
}

function NoResults({ filters, categoryTitle, canManage, onClear, onSearchAll, onBrowse }: {
  filters: LibraryFilters
  categoryTitle: string
  canManage: boolean
  onClear: () => void
  onSearchAll: () => void
  onBrowse: () => void
}) {
  if (isFiltered(filters)) {
    const narrowed = filters.view !== 'all' || !!filters.category
    return (
      <EmptyState
        icon={<SearchX className="h-5 w-5" />}
        title={filters.q.trim() ? `No templates match “${filters.q.trim()}”` : 'No templates match these filters'}
        action={(
          <>
            <Button size="sm" variant="outline" onClick={onClear}>Clear filters</Button>
            {narrowed && filters.q.trim() && <Button size="sm" variant="ghost" onClick={onSearchAll}>Search all reports</Button>}
          </>
        )}
      >
        Search looks at titles, descriptions, tags and section names{narrowed ? ` in ${categoryTitle}` : ''}.
      </EmptyState>
    )
  }
  switch (filters.view) {
    case 'favorites':
      return (
        <EmptyState icon={<Star className="h-5 w-5" />} title="No favourites yet">
          Star a template to pin it here.
        </EmptyState>
      )
    case 'recent':
      return (
        <EmptyState icon={<History className="h-5 w-5" />} title="Nothing viewed yet">
          Reports you open appear here so you can get back to them quickly.
        </EmptyState>
      )
    case 'mine':
      return (
        <EmptyState
          icon={<UserRound className="h-5 w-5" />}
          title="You have no templates yet"
          action={(
            <>
              <Button size="sm" variant="outline" onClick={onBrowse}>Browse built-in templates</Button>
              {canManage && <Button asChild size="sm"><Link to="/reports/builder"><Plus aria-hidden /> New report</Link></Button>}
            </>
          )}
        >
          {canManage ? 'Customize any built-in template to make it your own, or design one from scratch.' : 'Templates you create appear here. Creating templates needs the Manage reports permission.'}
        </EmptyState>
      )
    case 'shared':
      return (
        <EmptyState icon={<Users className="h-5 w-5" />} title="No shared templates">
          Templates shared with the organisation appear here.
        </EmptyState>
      )
    default:
      return (
        <EmptyState
          icon={<FolderOpen className="h-5 w-5" />}
          title={filters.category ? `No templates in ${categoryTitle} yet` : 'No templates here yet'}
          action={canManage ? <Button asChild size="sm" variant="outline"><Link to="/reports/builder"><Plus aria-hidden /> Design one</Link></Button> : undefined}
        >
          {filters.category ? 'Templates in this category are either not built in yet or read data your role cannot see.' : undefined}
        </EmptyState>
      )
  }
}

function LibrarySkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading report templates">
      <div className="flex gap-3 overflow-hidden">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[60px] w-64 shrink-0" />)}
      </div>
      {Array.from({ length: 2 }).map((_, g) => (
        <div key={g} className="space-y-3">
          <Skeleton className="h-5 w-48" />
          <div className={CARD_GRID}>
            {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-[184px]" />)}
          </div>
        </div>
      ))}
    </div>
  )
}
