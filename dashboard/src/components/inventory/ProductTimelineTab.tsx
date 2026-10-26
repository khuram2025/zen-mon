import { useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { TablePager } from '@/components/servers/tables'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { inventoryApi } from '@/pages/inventory/api'
import { softwareKeys, type ProductResponse } from '@/pages/inventory/softwareSettings'
import { pageState } from './assetModel'
import { ChangesList } from './ChangesList'
import { browserTimeZone, buildEntries, groupByDay, historyText } from './changesModel'
import { InventoryEmpty } from './InventoryEmpty'
import { fmtInt, plural } from './model'
import {
  productChangesHref, readProductState, TIMELINE_DAYS, timelineSince, timelineStats, withTimelineDays, withTimelinePage, withTimelinePageSize,
  type TimelineDays,
} from './productModel'

const focus = 'outline-none focus-visible:ring-2 focus-visible:ring-primary'

/**
 * The Timeline tab: installs, removals and version changes of this product on the servers you can see, newest
 * first, grouped by day, over 7, 30 or 90 days. The feed is `GET /inventory/changes?subject_key=<key>`, and the
 * rows are drawn by the Changes page's own list with its pairing rule: an upgrade of a product whose name embeds
 * its version arrives as a removal and an installation written together, and reads as ONE change. The statistics
 * are links to the Changes page, each with the total of the list it opens.
 */
export function ProductTimelineTab({ productKey, product, enabled }: { productKey: string; product: Pick<ProductResponse, 'header'> | undefined; enabled: boolean }) {
  const [params, setParams] = useSearchParams()
  const state = useMemo(() => readProductState(params), [params])
  const days: TimelineDays = state.days
  const since = useMemo(() => timelineSince(days, serverNow()), [days])
  const tz = useMemo(() => browserTimeZone(), [])
  const productName = product?.header.name ?? productKey

  const list = useQuery({
    queryKey: softwareKeys.timeline(productKey, `${days}:${state.page}:${state.pageSize}:${since}`),
    queryFn: () => inventoryApi.changes({ subject_key: productKey, since, tz, page: state.page, page_size: state.pageSize }),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    enabled,
    retry: 1,
  })
  const data = list.data
  const groups = useMemo(() => (data ? groupByDay(buildEntries(data.items).entries, tz, serverNow()) : []), [data, tz])
  const stats = timelineStats(productKey, data?.stats, since)
  const paging = data ? pageState(state.page, data.total, state.pageSize) : null
  // When capture began and how long changes are kept come from the response, as on the Changes page.
  const history = data ? historyText(data, serverNow(), data.window?.tz ?? tz) : null

  const body = () => {
    if (list.isLoading) {
      return <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading changes">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
    }
    if (list.isError && !data) {
      return <div className="p-4"><QueryFailure error={list.error} what="the change history" onRetry={() => void list.refetch()} retrying={list.isFetching} /></div>
    }
    if (!data || !paging) return null
    if (paging.beyond) {
      return (
        <div className="p-4" data-state="beyond">
          <InventoryEmpty kind="no-match" title={`Page ${state.page.toLocaleString()} is past the end`}>
            The last {days} days hold {plural(data.total, 'event')}, which fit {plural(paging.pageCount, 'page')}.{' '}
            <Link to={`?${withTimelinePage(params, paging.lastPage).toString()}`} className={cn('rounded font-medium text-primary hover:underline', focus)}>Go to the last page</Link>
          </InventoryEmpty>
        </div>
      )
    }
    if (data.total === 0) {
      return (
        <InventoryEmpty kind="no-data" title={`No changes in the last ${days} days`}>
          No installation, removal or version change of {productName} was recorded on the servers you can see in this window.
          {days < 90 && ' A longer window may show more.'}
        </InventoryEmpty>
      )
    }
    return (
      <>
        {list.isError && (
          <div className="border-b border-border p-2">
            <QueryFailure compact what="the change history" error={list.error} onRetry={() => void list.refetch()} retrying={list.isFetching} />
            <p className="mt-1 text-xs text-muted">Showing the last events that loaded; they may be out of date.</p>
          </div>
        )}
        <div className={cn(list.isFetching && list.isPlaceholderData && 'opacity-60 transition-opacity')} aria-busy={list.isFetching}>
          <ChangesList groups={groups} tz={tz} />
        </div>
        <TablePager
          page={state.page}
          pageCount={paging.pageCount}
          pageSize={state.pageSize}
          total={data.total}
          setPage={(p) => setParams(withTimelinePage(params, p))}
          setPageSize={(n) => setParams(withTimelinePageSize(params, n), { replace: true })}
          noun="events"
        />
      </>
    )
  }

  return (
    <div className="space-y-3" data-tab="timeline">
      {/* The day groups below are h3: this keeps the outline h1 > h2 > h3 (NF-3). */}
      <h2 className="sr-only">Changes in the last {days} days</h2>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div role="group" aria-label="Time window" className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
          {TIMELINE_DAYS.map((d) => (
            <button
              key={d}
              type="button"
              aria-pressed={days === d}
              onClick={() => setParams(withTimelineDays(params, d))}
              className={cn(
                'rounded px-2.5 py-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary',
                days === d ? 'bg-primary text-on-primary shadow-sm' : 'text-text2 hover:bg-surface3 hover:text-text',
              )}
            >
              {d} days
            </button>
          ))}
        </div>
        {stats.length > 0 && (
          <ul role="list" aria-label={`Changes in the last ${days} days`} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted" data-timeline-stats="">
            {stats.map((s) => (
              <li key={s.id}>
                <Link to={s.href} data-drill="changes" data-drill-count={s.value} title={`Open these ${s.value === 1 ? 'event' : 'events'} on the Changes page`} className={cn('rounded text-text2 hover:text-primary hover:underline', focus)}>
                  <strong className="font-semibold tabular-nums text-text">{fmtInt(s.value)}</strong> {s.label}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      <Card className="overflow-hidden">{body()}</Card>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span title={history?.title ?? undefined} data-timeline-history="">{history?.text ?? 'History starts when change capture was enabled; earlier changes were not recorded.'}</span>
        <Link to={productChangesHref(productKey, { since })} data-drill="changes" className={cn('inline-flex items-center gap-1 rounded font-medium text-primary hover:underline', focus)}>
          All changes of this product <ArrowRight className="h-3 w-3" aria-hidden />
        </Link>
      </p>
    </div>
  )
}
