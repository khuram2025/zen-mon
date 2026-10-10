import { useMemo } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { Boxes, Cpu, Gauge, History, MonitorCog, Package } from 'lucide-react'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
import { InventoryKpiCard } from '@/components/inventory/InventoryKpiCard'
import { fmtInt, fmtPct } from '@/components/inventory/model'
import {
  AgentCoverageLine, HardwarePanel, HealthPanel, NetworkOsPanel, OnboardingCards, RecentChangesPanel, ServerOsPanel, SprawlPanel, TopSoftwarePanel,
  TrendsNote,
} from '@/components/inventory/OverviewPanels'
import {
  agentCoverage, canDrawTrend, freshShareSeries, kpiModels, readTrends, sumSeries, trendUntil, withoutDay, type DayValue, type KpiModel,
} from '@/components/inventory/overviewModel'
import { Sparkline } from '@/components/inventory/Sparkline'
import { QueryBoundary, QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import type { KPI_ACCENT } from '@/pages/dashboards/shared'
import { inventoryApi } from './api'
import { SUMMARY_KEY } from './useInventoryMeta'

// Query keys and refetch intervals of Documentation/Inventory/03-UI-UX-SPEC.md section 8. The summary
// shares its key with the meta-line under the tabs, so it is one request, polled once a minute.
const MINUTE = 60_000
const TREND_DAYS = 30

export const OVERVIEW_KEYS = {
  summary: SUMMARY_KEY,
  os: (kind: 'server' | 'device') => ['inventory', 'os', kind] as const,
  hardware: ['inventory', 'hardware'] as const,
  changes: (pageSize: number) => ['inventory', 'changes', { page_size: pageSize }] as const,
  trends: (metric: string) => ['inventory', 'trends', metric] as const,
} as const

const KPI_LOOK: Record<KpiModel['id'], { icon: JSX.Element; accent: keyof typeof KPI_ACCENT }> = {
  assets: { icon: <Boxes className="h-4 w-4" />, accent: 'primary' },
  os: { icon: <MonitorCog className="h-4 w-4" />, accent: 'accent' },
  software: { icon: <Package className="h-4 w-4" />, accent: 'info' },
  hardware: { icon: <Cpu className="h-4 w-4" />, accent: 'primary' },
  fresh: { icon: <Gauge className="h-4 w-4" />, accent: 'success' },
  changes: { icon: <History className="h-4 w-4" />, accent: 'accent' },
}

/** Which snapshot family feeds which sparkline, and how its days become one line. */
const TREND_SOURCES = [
  { id: 'assets', metric: 'assets', label: 'Assets', line: (s: ReturnType<typeof readTrends>, _raw?: unknown) => sumSeries(s) },
  { id: 'software', metric: 'software_products', label: 'Software titles', line: (s: ReturnType<typeof readTrends>) => sumSeries(s, ['products']) },
  { id: 'fresh', metric: 'coverage', label: 'Fresh inventory', line: (s: ReturnType<typeof readTrends>) => freshShareSeries(s) },
  // Today's change count is still being written: the line stops at yesterday.
  { id: 'changes', metric: 'changes', label: 'Changes per day', line: (s: ReturnType<typeof readTrends>, raw: unknown) => withoutDay(sumSeries(s), trendUntil(raw)) },
] as const

export function InventoryOverviewPage() {
  const access = useInventoryAccess()
  const allowed = access === 'allowed'

  const summary = useQuery({
    queryKey: OVERVIEW_KEYS.summary,
    queryFn: inventoryApi.summary,
    refetchInterval: MINUTE,
    staleTime: 30_000,
    enabled: allowed,
  })
  const data = summary.data
  // With no assets at all there is nothing for the other panels to say, so they are not asked.
  const empty = data !== undefined && data.assets.total === 0
  const panelsOn = allowed && !empty
  const osServer = useQuery({ queryKey: OVERVIEW_KEYS.os('server'), queryFn: () => inventoryApi.os('server'), refetchInterval: 5 * MINUTE, staleTime: 60_000, enabled: panelsOn })
  const osDevice = useQuery({ queryKey: OVERVIEW_KEYS.os('device'), queryFn: () => inventoryApi.os('device'), refetchInterval: 5 * MINUTE, staleTime: 60_000, enabled: panelsOn })
  const hardware = useQuery({ queryKey: OVERVIEW_KEYS.hardware, queryFn: () => inventoryApi.hardware(), refetchInterval: 5 * MINUTE, staleTime: 60_000, enabled: panelsOn })
  const changes = useQuery({ queryKey: OVERVIEW_KEYS.changes(8), queryFn: () => inventoryApi.changes({ page_size: 8 }), refetchInterval: MINUTE, staleTime: 30_000, enabled: panelsOn })

  // Sparklines (scope decision S4): only for an unrestricted caller, and only if the endpoint answers
  // with at least three days. Any failure, a restricted answer or a short history means no line at all.
  const restricted = data?.scope.restricted === true
  const trendQueries = useQueries({
    queries: TREND_SOURCES.map((t) => ({
      queryKey: OVERVIEW_KEYS.trends(t.metric),
      queryFn: () => inventoryApi.trends({ metric: t.metric, days: TREND_DAYS }),
      refetchInterval: 15 * MINUTE,
      staleTime: 5 * MINUTE,
      retry: false,
      enabled: panelsOn && data !== undefined && !restricted,
    })),
  })
  const lines = useMemo(() => {
    const out = new Map<string, DayValue[]>()
    // Fleet-wide history is never drawn for a restricted caller, even if something is cached from an earlier session.
    if (restricted) return out
    TREND_SOURCES.forEach((t, i) => {
      const raw = trendQueries[i]?.data
      const points = t.line(readTrends(raw), raw)
      if (canDrawTrend(points)) out.set(t.id, points)
    })
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restricted, trendQueries.map((q) => q.dataUpdatedAt).join(',')])

  if (access === 'denied') return null // the layout's gate has already said so

  return (
    <div className="space-y-4" data-page="overview">
      {empty ? (
        data.scope.restricted ? (
          <InventoryEmpty kind="no-scope" title="No assets match your scope">
            Your role limits Inventory to assets tagged {data.scope.tags.join(', ') || 'for your team'}, and none carry those tags yet.
          </InventoryEmpty>
        ) : (
          <OnboardingCards />
        )
      ) : (
        <>
          <section aria-label="Key figures">
            {data === undefined ? (
              <KpiRowState query={summary} />
            ) : (
              <QueryBoundary query={summary} what="the inventory summary">
                {(s) => {
                  const agent = agentCoverage(s)
                  return (
                    <ul role="list" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
                      {kpiModels(s).map((m) => {
                        const line = m.trend ? lines.get(m.trend) : undefined
                        const source = TREND_SOURCES.find((t) => t.id === m.trend)
                        return (
                          <li key={m.id}>
                            <InventoryKpiCard
                              model={m}
                              icon={KPI_LOOK[m.id].icon}
                              accent={KPI_LOOK[m.id].accent}
                              foot={m.id === 'software' && agent ? <AgentCoverageLine coverage={agent} compact /> : undefined}
                              trend={line && source ? <Sparkline points={line} label={source.label} format={m.id === 'fresh' ? fmtPct : fmtInt} /> : undefined}
                            />
                          </li>
                        )
                      })}
                    </ul>
                  )
                }}
              </QueryBoundary>
            )}
            {restricted && <div className="mt-2"><TrendsNote>Trend lines are available to unrestricted users: they are fleet-wide.</TrendsNote></div>}
          </section>

          {/* Each panel has its own request and its own loading and failure state: one failing never blanks the page. */}
          {/* The two OS panels differ a lot in height (a donut and a legend against one bar per product): top-aligned, not stretched. */}
          <div className="grid items-start gap-4 lg:grid-cols-2">
            <ServerOsPanel query={osServer} />
            <NetworkOsPanel query={osDevice} />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <TopSoftwarePanel query={summary} />
            <SprawlPanel query={summary} />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <HardwarePanel summary={summary} hardware={hardware} />
            <RecentChangesPanel changes={changes} summary={summary} />
          </div>
          <HealthPanel query={summary} />
        </>
      )}
    </div>
  )
}

/** The summary is loading or failed outright: skeleton cards, or the failure with a retry. */
function KpiRowState({ query }: { query: { isError: boolean; error: unknown; refetch: () => unknown; isFetching: boolean } }) {
  // No data and no error yet: loading, or waiting for the account's permissions to arrive.
  if (!query.isError) {
    return (
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-busy="true" aria-label="Loading the inventory summary">
        {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[104px] w-full rounded-lg" />)}
      </div>
    )
  }
  return <QueryFailure error={query.error} what="the inventory summary" onRetry={() => void query.refetch()} retrying={query.isFetching} />
}
