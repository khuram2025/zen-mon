import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Globe2, Radio, Settings2 } from 'lucide-react'
import { api } from '@/lib/api'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import type {
  RumAction,
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
import { buildRumHref } from './rum/model'

const REFRESH_MS = 30_000
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
    queryFn: () => get(`/apm/rum/overview?${commonQuery}`),
    refetchInterval: REFRESH_MS,
  })
  const timeseriesQ = useQuery<RumTimeseries>({
    queryKey: ['apm', 'rum', 'timeseries', commonQuery],
    queryFn: () => get(`/apm/rum/timeseries?${commonQuery}`),
    enabled: overviewMode || state.tab === 'web-vitals',
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
    'web-vitals': buildRumHref('web-vitals', state.range, state.filters),
    views: buildRumHref('views', state.range, state.filters),
    sessions: buildRumHref('sessions', state.range, state.filters),
    errors: buildRumHref('errors', state.range, state.filters),
    resources: buildRumHref('resources', state.range, state.filters),
    actions: buildRumHref('actions', state.range, state.filters),
  }), [state.filters, state.range])

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
      <RumExplorerShell noun="views" total={viewsQ.data?.total} rangeLabel={state.range} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} items={viewsQ.data?.items} getTime={(row) => row.last_seen || ''} isErr={(row) => (row.error_session_rate ?? 0) >= 0.05 || row.errors > 0}>
        <RumViewsTable embedded {...sharedTableProps} data={viewsQ.data} loading={viewsQ.isLoading} error={viewsQ.error} onRetry={() => viewsQ.refetch()} onOpen={(row) => state.openDetail('view', viewRowKey(row))} />
      </RumExplorerShell>
    )
    if (state.tab === 'sessions') return (
      <RumExplorerShell noun="sessions" total={sessionsQ.data?.total} rangeLabel={state.range} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} items={sessionsQ.data?.items} getTime={(row) => row.last_seen} isErr={(row) => row.errors > 0}>
        <RumSessionsTable embedded {...sharedTableProps} data={sessionsQ.data} loading={sessionsQ.isLoading} error={sessionsQ.error} onRetry={() => sessionsQ.refetch()} onOpen={(row) => state.openDetail('session', row.session_id)} />
      </RumExplorerShell>
    )
    if (state.tab === 'errors') return (
      <RumExplorerShell noun="issues" total={errorsQ.data?.total} rangeLabel={state.range} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} items={errorsQ.data?.items} getTime={(row) => row.last_seen} isErr={() => true} okLabel="Other" errLabel="JS errors">
        <RumErrorsTable embedded {...sharedTableProps} data={errorsQ.data} loading={errorsQ.isLoading} error={errorsQ.error} onRetry={() => errorsQ.refetch()} onOpen={(row) => state.openDetail('error', errorRowKey(row))} />
      </RumExplorerShell>
    )
    if (state.tab === 'resources') return (
      <RumExplorerShell noun="resources" total={resourcesQ.data?.total} rangeLabel={state.range} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} items={resourcesQ.data?.items} getTime={(row) => row.last_seen} isErr={(row) => row.failed_count > 0}>
        <RumResourcesTable embedded {...sharedTableProps} data={resourcesQ.data} loading={resourcesQ.isLoading} error={resourcesQ.error} onRetry={() => resourcesQ.refetch()} onOpen={(row) => state.openDetail('resource', resourceRowKey(row))} />
      </RumExplorerShell>
    )
    if (state.tab === 'actions') return (
      <RumExplorerShell noun="actions" total={actionsQ.data?.total} rangeLabel={state.range} filters={state.filters} facets={facetsQ.data} onFilter={state.setFilter} items={actionsQ.data?.items} getTime={(row) => row.last_seen} isErr={(row) => row.error_count > 0 || ['rage_click', 'dead_click', 'error_click'].includes(row.action_type)}>
        <RumActionsTable embedded {...sharedTableProps} data={actionsQ.data} loading={actionsQ.isLoading} error={actionsQ.error} onRetry={() => actionsQ.refetch()} onOpen={(row) => state.openDetail('action', actionRowKey(row))} />
      </RumExplorerShell>
    )
    return null
  })()

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Real User Monitoring"
        description="Field performance, JavaScript reliability and user journeys — from Core Web Vitals down to a correlated backend trace."
        article="rum"
        actions={
          <>
            <RefreshIndicator active={anyFetching && !overviewQ.isLoading} />
            <Badge variant={overviewQ.data?.totals.events ? 'success' : 'outline'}>
              <Radio className="h-3 w-3" />
              {overviewQ.data?.totals.events ? 'Receiving data' : overviewQ.isLoading ? 'Checking ingest' : 'No recent data'}
            </Badge>
            <Button asChild variant="outline" size="sm">
              <Link to="/apm/settings?tab=keys&create=rum"><Settings2 className="h-4 w-4" /> Setup</Link>
            </Button>
            <RumRangePicker value={state.range} onChange={state.setRange} />
          </>
        }
      />

      <RumTabBar
        value={state.tab}
        onChange={state.setTab}
        counts={overviewQ.data ? {
          views: overviewQ.data.totals.views,
          sessions: overviewQ.data.totals.sessions,
          errors: overviewQ.data.totals.errors,
          resources: overviewQ.data.totals.resources,
          actions: overviewQ.data.totals.actions,
        } : undefined}
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
