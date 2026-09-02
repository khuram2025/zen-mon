import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Globe2, Radio, Settings2 } from 'lucide-react'
import { api } from '@/lib/api'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { fmtCount } from '@/components/apm/viz'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type {
  RumAction,
  RumBreakdown,
  RumError,
  RumFacets,
  RumIngestHealth,
  RumListResponse,
  RumOverview,
  RumResource,
  RumSession,
  RumSessionDetail,
  RumTimeseries,
  RumView,
} from '@/types/apm'
import { RumDetailDialog } from './rum/RumDetailDialog'
import { RumOverviewPanel, RumWebVitalsPanel } from './rum/RumOverview'
import {
  RumActionsTable,
  RumErrorsTable,
  RumResourcesTable,
  RumSessionsTable,
  RumViewsTable,
  actionRowKey,
  errorRowKey,
  resourceRowKey,
  viewRowKey,
} from './rum/RumTables'
import {
  QueryErrorPanel,
  RefreshIndicator,
  RumEmptyState,
  RumExplorerShell,
  RumFilterBar,
  RumPageSkeleton,
  RumRangePicker,
  RumTabBar,
} from './rum/RumUi'
import { useRumUrlState } from './rum/useRumUrlState'
import { buildRumHref, formatWindowLabel, parseUtc, volumeBuckets } from './rum/model'
import { relativeTime } from '@/lib/utils'

const REFRESH_MS = 30_000
/** An ingest pipeline is "live" when the newest event is at most this old. */
const LIVE_WINDOW_MS = 15 * 60_000
const SESSION_PAGE_SIZE = 500
const MAX_SESSION_TIMELINE_EVENTS = 2_000

async function get<T>(path: string): Promise<T> {
  return (await api.get<T>(path)).data
}

async function getRumSessionDetail(sessionId: string, baseQuery: string): Promise<RumSessionDetail> {
  const path = `/apm/rum/sessions/${encodeURIComponent(sessionId)}`
  const params = new URLSearchParams(baseQuery)
  params.set('page', '1')
  params.set('page_size', String(SESSION_PAGE_SIZE))
  const first = await get<RumSessionDetail>(`${path}?${params}`)
  const available = Math.min(first.total ?? first.timeline.length, MAX_SESSION_TIMELINE_EVENTS)
  const pageCount = Math.max(1, Math.ceil(available / SESSION_PAGE_SIZE))
  if (pageCount === 1) return first

  const remaining = await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => {
    const pageParams = new URLSearchParams(params)
    pageParams.set('page', String(index + 2))
    return get<RumSessionDetail>(`${path}?${pageParams}`)
  }))
  return {
    ...first,
    page: 1,
    page_size: SESSION_PAGE_SIZE,
    timeline: [first, ...remaining].flatMap((response) => response.timeline).slice(0, available),
  }
}

export function RumPage() {
  const state = useRumUrlState()
  const commonQuery = state.query()
  const filtered = state.activeFilterCount > 0
  const overviewMode = state.tab === 'overview'

  const overviewQ = useQuery<RumOverview>({
    queryKey: ['apm', 'rum', 'overview', commonQuery],
    // compare=1 adds the same figures for the previous window of equal length.
    queryFn: () => get(`/apm/rum/overview?${commonQuery}&compare=1`),
    refetchInterval: REFRESH_MS,
  })
  const timeseriesQ = useQuery<RumTimeseries>({
    queryKey: ['apm', 'rum', 'timeseries', commonQuery],
    queryFn: () => get(`/apm/rum/timeseries?${commonQuery}`),
    refetchInterval: REFRESH_MS,
  })
  const facetsQ = useQuery<RumFacets>({
    queryKey: ['apm', 'rum', 'facets', commonQuery],
    queryFn: () => get(`/apm/rum/facets?${commonQuery}`),
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
  const healthQ = useQuery<RumIngestHealth>({
    queryKey: ['apm', 'rum', 'health', commonQuery],
    queryFn: () => get(`/apm/rum/health?${commonQuery}`),
    refetchInterval: REFRESH_MS,
  })
  const breakdownQ = useQuery<RumBreakdown>({
    queryKey: ['apm', 'rum', 'breakdown', commonQuery],
    queryFn: () => get(`/apm/rum/breakdown?${commonQuery}`),
    enabled: overviewMode,
    refetchInterval: REFRESH_MS,
  })

  const viewsQuery = state.query({
    page: overviewMode ? 1 : state.page,
    page_size: overviewMode ? 5 : state.pageSize,
    sort: overviewMode ? 'lcp_p75' : state.sort,
    order: overviewMode ? 'desc' : state.order,
  })
  const viewsQ = useQuery<RumListResponse<RumView>>({
    queryKey: ['apm', 'rum', 'views', viewsQuery],
    queryFn: () => get(`/apm/rum/views?${viewsQuery}`),
    enabled: overviewMode || state.tab === 'views',
    refetchInterval: REFRESH_MS,
  })

  const errorsQuery = state.query({
    page: overviewMode ? 1 : state.page,
    page_size: overviewMode ? 5 : state.pageSize,
    sort: overviewMode ? 'count' : state.sort,
    order: overviewMode ? 'desc' : state.order,
  })
  const errorsQ = useQuery<RumListResponse<RumError>>({
    queryKey: ['apm', 'rum', 'errors', errorsQuery],
    queryFn: () => get(`/apm/rum/errors?${errorsQuery}`),
    enabled: overviewMode || state.tab === 'errors',
    refetchInterval: REFRESH_MS,
  })

  const listQuery = state.query({ page: state.page, page_size: state.pageSize, sort: state.sort, order: state.order })
  const sessionsQ = useQuery<RumListResponse<RumSession>>({
    queryKey: ['apm', 'rum', 'sessions', listQuery],
    queryFn: () => get(`/apm/rum/sessions?${listQuery}`),
    enabled: state.tab === 'sessions' || (state.detailKind === 'session' && !!state.detailId),
    refetchInterval: REFRESH_MS,
  })
  const resourcesQ = useQuery<RumListResponse<RumResource>>({
    queryKey: ['apm', 'rum', 'resources', listQuery],
    queryFn: () => get(`/apm/rum/resources?${listQuery}`),
    enabled: state.tab === 'resources',
    refetchInterval: REFRESH_MS,
  })
  const actionsQ = useQuery<RumListResponse<RumAction>>({
    queryKey: ['apm', 'rum', 'actions', listQuery],
    queryFn: () => get(`/apm/rum/actions?${listQuery}`),
    enabled: state.tab === 'actions',
    refetchInterval: REFRESH_MS,
  })

  const sessionDetailQ = useQuery<RumSessionDetail>({
    queryKey: ['apm', 'rum', 'session', state.detailId, commonQuery],
    queryFn: () => getRumSessionDetail(state.detailId, commonQuery),
    enabled: state.detailKind === 'session' && !!state.detailId,
  })

  const exploreTo = useMemo(() => ({
    'web-vitals': buildRumHref('web-vitals', state.range, state.filters, state.bounds),
    views: buildRumHref('views', state.range, state.filters, state.bounds),
    sessions: buildRumHref('sessions', state.range, state.filters, state.bounds),
    errors: buildRumHref('errors', state.range, state.filters, state.bounds),
    resources: buildRumHref('resources', state.range, state.filters, state.bounds),
    actions: buildRumHref('actions', state.range, state.filters, state.bounds),
  }), [state.bounds, state.filters, state.range])
  const rangeLabel = state.range === 'custom' ? formatWindowLabel(state.bounds) : state.range

  // CSV export of the active explorer: fetch through the authenticated API
  // client and hand the file to the browser (a plain link cannot carry the
  // bearer token).
  const [exporting, setExporting] = useState(false)
  const exportCsv = async (tab: 'views' | 'sessions' | 'errors' | 'resources' | 'actions') => {
    if (exporting) return
    setExporting(true)
    try {
      const params = new URLSearchParams(state.query({ sort: state.sort, order: state.order }))
      params.set('tab', tab)
      const response = await api.get<Blob>(`/apm/rum/export?${params}`, { responseType: 'blob' })
      const disposition = String(response.headers['content-disposition'] || '')
      const name = /filename="([^"]+)"/.exec(disposition)?.[1] || `rum-${tab}.csv`
      const url = URL.createObjectURL(response.data)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = name
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
    } finally {
      setExporting(false)
    }
  }

  const selected = useMemo(() => {
    if (!state.detailId) return undefined
    if (state.detailKind === 'session') return sessionsQ.data?.items.find((row) => row.session_id === state.detailId)
    if (state.detailKind === 'view') return viewsQ.data?.items.find((row) => viewRowKey(row) === state.detailId)
    if (state.detailKind === 'error') return errorsQ.data?.items.find((row) => errorRowKey(row) === state.detailId)
    if (state.detailKind === 'resource') return resourcesQ.data?.items.find((row) => resourceRowKey(row) === state.detailId)
    if (state.detailKind === 'action') return actionsQ.data?.items.find((row) => actionRowKey(row) === state.detailId)
    return undefined
  }, [actionsQ.data?.items, errorsQ.data?.items, resourcesQ.data?.items, sessionsQ.data?.items, state.detailId, state.detailKind, viewsQ.data?.items])

  const anyFetching = overviewQ.isFetching || timeseriesQ.isFetching || facetsQ.isFetching || viewsQ.isFetching || errorsQ.isFetching || sessionsQ.isFetching || resourcesQ.isFetching || actionsQ.isFetching
  const noTelemetry = !filtered && overviewQ.data?.totals.events === 0
  const lastEventAt = healthQ.data?.last_event_at
  const lastEventAge = lastEventAt ? Date.now() - parseUtc(lastEventAt) : Number.NaN
  const receiving = Number.isFinite(lastEventAge) && lastEventAge >= 0 && lastEventAge <= LIVE_WINDOW_MS
  const ingestBadge = receiving
    ? { variant: 'success' as const, label: 'Receiving data' }
    : overviewQ.data?.totals.events
      ? { variant: 'outline' as const, label: `Last event ${relativeTime(lastEventAt || overviewQ.data.releases?.[0]?.last_seen)}` }
      : overviewQ.isLoading
        ? { variant: 'outline' as const, label: 'Checking ingest' }
        : { variant: 'outline' as const, label: 'No recent data' }
  const explorerCounts = overviewQ.data?.explorer ?? (overviewQ.data ? {
    views: overviewQ.data.totals.views,
    sessions: overviewQ.data.totals.sessions,
    errors: overviewQ.data.totals.errors,
    resources: overviewQ.data.totals.resources,
    actions: overviewQ.data.totals.actions,
  } : undefined)
  const totals = overviewQ.data?.totals
  const volume = (count: number | undefined, noun: string) => (count == null ? undefined : `${fmtCount(count)} ${noun}`)
  const sharedTableProps = {
    page: state.page,
    pageSize: state.pageSize,
    sort: state.sort,
    order: state.order,
    filtered,
    onSort: state.setSort,
    onPage: state.setPage,
    onPageSize: state.setPageSize,
  }

  const content = (() => {
    if ((state.tab === 'overview' || state.tab === 'web-vitals') && overviewQ.isLoading) return <RumPageSkeleton />
    if ((state.tab === 'overview' || state.tab === 'web-vitals') && overviewQ.isError) return <QueryErrorPanel label="RUM overview" error={overviewQ.error} onRetry={() => overviewQ.refetch()} />
    if (noTelemetry) return (
      <Card>
        <RumEmptyState
          icon={Globe2}
          title="Connect your first web application"
          description="Create an origin-scoped Browser RUM key and install the controller-hosted SDK. Data is self-hosted, form values are masked, and no third-party network dependency is required."
          action={<Button asChild><Link to="/apm/settings?tab=keys&create=rum"><Settings2 className="h-4 w-4" /> Configure Browser RUM</Link></Button>}
        />
      </Card>
    )
    if (state.tab === 'overview' && overviewQ.data) return (
      <RumOverviewPanel
        overview={overviewQ.data}
        timeseries={timeseriesQ.data}
        topViews={viewsQ.data?.items}
        topErrors={errorsQ.data?.items}
        topViewsLoading={viewsQ.isLoading}
        topViewsError={viewsQ.error}
        topErrorsLoading={errorsQ.isLoading}
        topErrorsError={errorsQ.error}
        health={healthQ.data}
        breakdown={breakdownQ.data}
        breakdownLoading={breakdownQ.isLoading}
        breakdownError={breakdownQ.error}
        onRetryBreakdown={() => breakdownQ.refetch()}
        explorerCoverage={viewsQ.data?.coverage ?? errorsQ.data?.coverage}
        facets={facetsQ.data}
        trendsLoading={timeseriesQ.isLoading}
        trendsError={timeseriesQ.error}
        onRetryTrends={() => timeseriesQ.refetch()}
        onRetryViews={() => viewsQ.refetch()}
        onRetryErrors={() => errorsQ.refetch()}
        onOpenView={(row) => state.openDetail('view', viewRowKey(row))}
        onOpenError={(row) => state.openDetail('error', errorRowKey(row))}
        onShowViews={() => state.setTab('views')}
        onShowErrors={() => state.setTab('errors')}
        onFilter={state.setFilter}
        exploreTo={exploreTo}
      />
    )
    if (state.tab === 'web-vitals' && overviewQ.data) return <RumWebVitalsPanel overview={overviewQ.data} timeseries={timeseriesQ.data} loading={timeseriesQ.isLoading} error={timeseriesQ.error} onRetry={() => timeseriesQ.refetch()} />
    if (state.tab === 'views') return (
      <RumExplorerShell noun="routes" onExport={() => exportCsv('views')} exporting={exporting} total={viewsQ.data?.total} volume={volume(totals?.views, 'page views')} rangeLabel={rangeLabel} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} buckets={volumeBuckets(timeseriesQ.data, state.range, { ok: 'views', err: 'errors' }, { ok: 'Page views', err: 'JS errors' }, state.bounds)} okLabel="Page views" errLabel="JS errors">
        <RumViewsTable embedded {...sharedTableProps} data={viewsQ.data} loading={viewsQ.isLoading} error={viewsQ.error} onRetry={() => viewsQ.refetch()} onOpen={(row) => state.openDetail('view', viewRowKey(row))} />
      </RumExplorerShell>
    )
    if (state.tab === 'sessions') return (
      <RumExplorerShell noun="sessions" onExport={() => exportCsv('sessions')} exporting={exporting} total={sessionsQ.data?.total} rangeLabel={rangeLabel} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} buckets={volumeBuckets(timeseriesQ.data, state.range, { ok: 'sessions', err: 'error_sessions', errWithinOk: true }, { ok: 'Sessions', err: 'Sessions with errors' }, state.bounds)} okLabel="Healthy" errLabel="With errors">
        <RumSessionsTable embedded {...sharedTableProps} data={sessionsQ.data} loading={sessionsQ.isLoading} error={sessionsQ.error} onRetry={() => sessionsQ.refetch()} onOpen={(row) => state.openDetail('session', row.session_id)} />
      </RumExplorerShell>
    )
    if (state.tab === 'errors') return (
      <RumExplorerShell noun="issues" onExport={() => exportCsv('errors')} exporting={exporting} total={errorsQ.data?.total} volume={volume(totals?.errors, 'error events')} rangeLabel={rangeLabel} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} buckets={volumeBuckets(timeseriesQ.data, state.range, { err: 'errors' }, { ok: 'Events', err: 'JS errors' }, state.bounds)} okLabel="Events" errLabel="JS errors">
        <RumErrorsTable embedded {...sharedTableProps} data={errorsQ.data} loading={errorsQ.isLoading} error={errorsQ.error} onRetry={() => errorsQ.refetch()} onOpen={(row) => state.openDetail('error', errorRowKey(row))} />
      </RumExplorerShell>
    )
    if (state.tab === 'resources') return (
      <RumExplorerShell noun="resources" onExport={() => exportCsv('resources')} exporting={exporting} total={resourcesQ.data?.total} volume={volume(totals?.resources, 'requests')} rangeLabel={rangeLabel} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} buckets={volumeBuckets(timeseriesQ.data, state.range, { ok: 'resources', err: 'resource_failures', errWithinOk: true }, { ok: 'Requests', err: 'Failed requests' }, state.bounds)} okLabel="Succeeded" errLabel="Failed">
        <RumResourcesTable embedded {...sharedTableProps} data={resourcesQ.data} loading={resourcesQ.isLoading} error={resourcesQ.error} onRetry={() => resourcesQ.refetch()} onOpen={(row) => state.openDetail('resource', resourceRowKey(row))} />
      </RumExplorerShell>
    )
    if (state.tab === 'actions') return (
      <RumExplorerShell noun="actions" onExport={() => exportCsv('actions')} exporting={exporting} total={actionsQ.data?.total} volume={volume(totals?.actions, 'interactions')} rangeLabel={rangeLabel} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} buckets={volumeBuckets(timeseriesQ.data, state.range, { ok: 'actions', err: 'long_tasks' }, { ok: 'Interactions', err: 'Long tasks' }, state.bounds)} okLabel="Interactions" errLabel="Long tasks">
        <RumActionsTable embedded {...sharedTableProps} data={actionsQ.data} loading={actionsQ.isLoading} error={actionsQ.error} onRetry={() => actionsQ.refetch()} onOpen={(row) => state.openDetail('action', actionRowKey(row))} />
      </RumExplorerShell>
    )
    return null
  })()

  return (
    <div className="space-y-2.5">
      <ApmPageHeader
        title="Real User Monitoring"
        description="Field performance, JavaScript reliability and user journeys, correlated to backend traces."
        article="rum"
        actions={
          <>
            <RefreshIndicator active={anyFetching && !overviewQ.isLoading} />
            <Badge variant={ingestBadge.variant} title={lastEventAt ? `Last event received ${relativeTime(lastEventAt)}` : undefined}>
              <Radio className="h-3 w-3" />
              {ingestBadge.label}
            </Badge>
            <Button asChild variant="outline" size="sm">
              <Link to="/apm/settings?tab=keys&create=rum"><Settings2 className="h-4 w-4" /> Setup</Link>
            </Button>
            <RumRangePicker value={state.range} bounds={state.bounds} onChange={state.setRange} onCustom={state.setCustomRange} />
          </>
        }
      />

      <RumTabBar
        value={state.tab}
        onChange={state.setTab}
        counts={explorerCounts}
      />

      <RumFilterBar compact={state.tab !== 'overview' && state.tab !== 'web-vitals'} filters={state.filters} facets={facetsQ.data} loading={facetsQ.isLoading} error={facetsQ.isError} activeCount={state.activeFilterCount} onChange={state.setFilter} onClear={state.clearFilters} onRetry={() => facetsQ.refetch()} />

      {content}

      <RumDetailDialog
        open={!!state.detailId}
        kind={state.detailKind}
        selected={selected}
        sessionDetail={sessionDetailQ.data}
        sessionLoading={sessionDetailQ.isLoading}
        sessionError={sessionDetailQ.error}
        onRetrySession={() => sessionDetailQ.refetch()}
        onClose={state.closeDetail}
        onDrill={(tab, viewName) => state.drillTo(tab, { view_name: viewName })}
      />
    </div>
  )
}
