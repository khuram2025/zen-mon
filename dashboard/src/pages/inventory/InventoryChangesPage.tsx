import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { ChangesChart } from '@/components/inventory/ChangesChart'
import { ChangesFootnote } from '@/components/inventory/ChangesFootnote'
import { ChangesList } from '@/components/inventory/ChangesList'
import { ChangesStatsStrip } from '@/components/inventory/ChangesStats'
import { ChangesToolbar } from '@/components/inventory/ChangesToolbar'
import {
  CHANGE_WINDOWS, CHANGES_DEFAULT_PAGE_SIZE, activeFilters, browserTimeZone, buildEntries, changesCodec, changesPageHref, changesQuery,
  countText, groupByDay, isChangesFiltersEmpty, pastTheEnd, windowOf, windowText, withDay, withWindow,
} from '@/components/inventory/changesModel'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
import { ExportDatasetButton } from '@/components/inventory/OsExportButton'
import { QueryGate } from '@/components/inventory/OsQueryGate'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { TablePager } from '@/components/servers/tables'
import { serverNow } from '@/lib/serverClock'
import { useCan } from '@/stores/auth'
import { inventoryApi } from './api'
import { exportChanges, fetchChanges, OSHW_KEYS, type ChangesFilters } from './osHardwareChanges'
import type { ChangesResponse } from './types'
import { SUMMARY_KEY } from './useInventoryMeta'
import { useInventoryUrlState } from './useInventoryUrlState'

const MINUTE = 60_000

/**
 * /inventory/changes (doc 03 section 3.7): installs, removals, upgrades, firmware, OS and hardware changes,
 * newest first and grouped by day. The address bar carries the API's own parameters (category, action, kind,
 * asset, subject_key, label, q, since, until), so every view is shareable and the Overview's links land on exactly
 * the list whose total they showed. An installation and a removal of one product on one asset at one instant are
 * ONE upgrade row. The footnote says when change capture began and how long changes are kept, both from the response.
 */
export function InventoryChangesPage() {
  const access = useInventoryAccess()
  const allowed = access === 'allowed'
  const can = useCan()
  const url = useInventoryUrlState<ChangesFilters>({ codec: changesCodec, sorts: [], defaultSort: { sort: '', order: 'desc' }, defaultPageSize: CHANGES_DEFAULT_PAGE_SIZE })
  const tz = useMemo(() => browserTimeZone(), [])
  const now = serverNow()
  const filters = url.filters
  const request = changesQuery(filters, url.page, url.pageSize, tz)

  const list = useQuery({
    queryKey: OSHW_KEYS.changes(JSON.stringify(request)),
    queryFn: () => fetchChanges(filters, url.page, url.pageSize, tz),
    refetchInterval: MINUTE,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    enabled: allowed,
  })
  const summary = useQuery({ queryKey: SUMMARY_KEY, queryFn: inventoryApi.summary, refetchInterval: MINUTE, staleTime: 30_000, enabled: allowed })
  const data = list.data
  const zone = data?.window.tz || tz
  const parsed = useMemo(() => (data ? buildEntries(data.items) : { entries: [], merged: 0 }), [data])
  const groups = useMemo(() => groupByDay(parsed.entries, zone, now), [parsed.entries, zone, now])
  // Names the page has learnt from the rows, for the chips of an asset or a product filter.
  const names = useMemo(() => {
    const items = data?.items ?? []
    return {
      asset: filters.asset ? items.find((r) => r.asset_key === filters.asset)?.asset_name ?? null : null,
      subject: filters.subject_key ? items.find((r) => r.subject_key === filters.subject_key)?.subject ?? null : null,
    }
  }, [data, filters.asset, filters.subject_key])
  const active = activeFilters(filters, now, zone, names)
  const hasFilters = !isChangesFiltersEmpty({ ...filters, since: undefined, until: undefined })
  const restricted = summary.data?.scope.restricted === true

  if (access === 'denied') return null // the layout's gate has already said so

  const exportButton = can('inventory.export') ? (
    <ExportDatasetButton run={() => exportChanges(filters)} filename="inventory-changes" total={data?.total} noun="events" disabled={data?.total === 0} />
  ) : undefined

  return (
    <div className="space-y-4" data-page="changes">
      <ChangesToolbar filters={filters} active={active} pageSize={url.pageSize} onQ={(q) => url.setFilters((live) => ({ ...live, q: q || undefined }), { push: false })} trailing={exportButton} />
      {url.rejected.length > 0 && (
        <p role="status" className="flex items-start gap-1.5 rounded-md border border-warning/35 bg-warning/10 px-3 py-1.5 text-xs text-warning-fg">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Ignored in the address: {url.rejected.map((r) => `${r.key}=${r.value} (${r.reason})`).join('; ')}.</span>
        </p>
      )}
      <QueryGate query={list} what="the change history" skeleton={<ChangesSkeleton />}>
        {(res: ChangesResponse) => {
          const paging = pastTheEnd(url.page, res.total, url.pageSize)
          const win = windowOf(filters.since, filters.until, now)
          const custom = win === 'custom'
          return (
            <div className={list.isPlaceholderData ? 'space-y-4 opacity-60 transition-opacity' : 'space-y-4 transition-opacity'} aria-busy={list.isFetching}>
              <ChangesStatsStrip stats={res.stats} filters={filters} now={now} tz={zone} pageSize={url.pageSize} />
              <ChangesChart
                series={res.series}
                since={res.window.since}
                filters={filters}
                now={now}
                tz={zone}
                pageSize={url.pageSize}
                onDay={(day) => url.setFilters((live) => withDay(live, day, zone))}
              />
              <Card className="overflow-hidden">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
                  <h2 className="text-sm font-semibold tracking-tight">Change history</h2>
                  <p className="text-xs text-muted" role="status" data-changes-count="">
                    {countText(res.total, parsed.entries.length, parsed.merged)}
                  </p>
                </div>
                {paging.beyond ? (
                  <div className="p-4" data-state="beyond">
                    <InventoryEmpty
                      kind="no-match"
                      title={`Page ${url.page.toLocaleString()} is past the end`}
                      action={<Button size="sm" variant="outline" onClick={() => url.setPage(paging.pageCount)}>Go to the last page ({paging.pageCount.toLocaleString()})</Button>}
                    >
                      These filters match {res.total.toLocaleString()} {res.total === 1 ? 'event' : 'events'}, which is {paging.pageCount.toLocaleString()} {paging.pageCount === 1 ? 'page' : 'pages'} at {url.pageSize} per page.
                    </InventoryEmpty>
                  </div>
                ) : res.total === 0 ? (
                  hasFilters || custom ? (
                    <InventoryEmpty
                      kind="no-match"
                      action={<Link to={changesPageHref({ since: filters.since, until: filters.until }, { page_size: url.pageSize })} className="text-xs font-medium text-primary hover:underline">Clear all filters</Link>}
                    >
                      No change matches every one of the active filters in this window ({windowText(filters.since, filters.until, now, zone)}). Remove one, or widen the window.
                    </InventoryEmpty>
                  ) : (
                    <InventoryEmpty
                      kind={restricted ? 'no-scope' : 'no-data'}
                      title={custom ? 'No changes in this window' : `No changes in the ${windowText(filters.since, filters.until, now, zone)}`}
                      action={
                        win !== '90d' ? (
                          <span className="flex flex-wrap justify-center gap-2 text-xs">
                            {CHANGE_WINDOWS.filter((w) => w.ms > (CHANGE_WINDOWS.find((x) => x.key === win)?.ms ?? 0)).map((w) => (
                              <Link key={w.key} to={changesPageHref(withWindow(filters, w.key, now), { page_size: url.pageSize })} className="font-medium text-primary hover:underline">Show the last {w.words}</Link>
                            ))}
                          </span>
                        ) : undefined
                      }
                    >
                      {restricted
                        ? 'None of the assets in your scope has changed in this window.'
                        : 'Installs, removals, upgrades and firmware changes appear here as they are reported. Widen the window to look further back.'}
                    </InventoryEmpty>
                  )
                ) : (
                  <>
                    <ChangesList groups={groups} tz={zone} />
                    <TablePager
                      page={url.page}
                      pageCount={paging.pageCount}
                      pageSize={url.pageSize}
                      total={res.total}
                      setPage={(p) => url.setPage(p)}
                      setPageSize={(n) => url.setPageSize(n)}
                      noun="events"
                    />
                  </>
                )}
              </Card>
              <ChangesFootnote info={res} now={now} tz={res.window.tz || zone} browserTz={tz} tzFallback={res.window.tz_fallback} restricted={restricted} />
            </div>
          )
        }}
      </QueryGate>
    </div>
  )
}

function ChangesSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading the change history">
      <Skeleton className="h-28 w-full rounded-lg" />
      <Skeleton className="h-60 w-full rounded-lg" />
      <Card className="space-y-2 p-4">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</Card>
    </div>
  )
}
