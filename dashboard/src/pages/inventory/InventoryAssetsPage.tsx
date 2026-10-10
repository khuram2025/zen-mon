import { useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { AssetFlyout } from '@/components/inventory/AssetFlyout'
import { AssetTable } from '@/components/inventory/AssetTable'
import { AssetToolbar } from '@/components/inventory/AssetToolbar'
import { exportStartedText, facetGroups, pageState, rangeText, toggleColumn, visibleColumns } from '@/components/inventory/assetModel'
import { sourceLabel } from '@/components/inventory/model'
import { FacetPanel } from '@/components/inventory/FacetPanel'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
import { Sheet, useMediaQuery } from '@/components/inventory/Sheet'
import { useAssetPrefs } from '@/components/inventory/useAssetPrefs'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { TablePager } from '@/components/servers/tables'
import { blobErrorMessage, downloadBlob, filenameFromDisposition } from '@/components/services/list/model'
import { serverNow } from '@/lib/serverClock'
import { useCan } from '@/stores/auth'
import { inventoryApi } from './api'
import { softwareApi, softwareKeys } from './softwareSettings'
import {
  activeFilterChips, assetFilterCodec, type AssetFilterKey, type AssetQuery, type AssetSortKey,
} from './filters'
import { useInventoryUrlState } from './useInventoryUrlState'
import { SUMMARY_KEY } from './useInventoryMeta'

/**
 * /inventory/assets (doc 03 section 3.2): the asset union as one list, filtered, sorted and paged on the
 * server. Every filter, the sort, the page and the open flyout live in the address bar (the URL
 * parameters are the API's own), so every view is shareable and Back-button safe.
 */
export function InventoryAssetsPage() {
  const access = useInventoryAccess()
  const allowed = access === 'allowed'
  const can = useCan()
  const url = useInventoryUrlState()
  const { pathname } = useLocation()
  const wide = useMediaQuery('(min-width: 1024px)')
  const [prefs, setPrefs] = useAssetPrefs()
  const [drawer, setDrawer] = useState(false)
  const [exporting, setExporting] = useState(false)

  const query: AssetQuery = useMemo(
    () => ({ ...url.filters, sort: url.sort as AssetSortKey, order: url.order, page: url.page, page_size: url.pageSize }),
    [url.filters, url.sort, url.order, url.page, url.pageSize],
  )
  const filterKey = assetFilterCodec.write(url.filters, new URLSearchParams()).toString()

  // Keys and staleness of doc 03 section 8: refetched when the filters change, 30 s stale time.
  const list = useQuery({
    queryKey: ['inventory', 'assets', url.apiParams.toString()],
    queryFn: () => inventoryApi.assets(query),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    enabled: allowed,
  })
  const facets = useQuery({
    queryKey: ['inventory', 'facets', filterKey],
    queryFn: () => inventoryApi.assetFacets(url.filters),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    enabled: allowed,
  })
  // Only needed to tell "nothing collected yet" from "your filters matched nothing"; shared with the meta-line.
  const summary = useQuery({ queryKey: SUMMARY_KEY, queryFn: inventoryApi.summary, refetchInterval: 60_000, staleTime: 30_000, enabled: allowed })

  // A product filter is a key (`google-chrome`); its chip says the product's name. The product page shares this query.
  const productKey = url.filters.product
  const productNames = useQuery({
    queryKey: softwareKeys.product(productKey ?? ''),
    queryFn: () => softwareApi.product(productKey as string),
    staleTime: 5 * 60_000,
    retry: false,
    enabled: allowed && !!productKey,
  })
  const chips = activeFilterChips(url.filters, {
    source: sourceLabel,
    product: (key) => (productNames.data?.header.product_key === key ? productNames.data.header.name : undefined),
  })
  const columns = useMemo(() => visibleColumns(prefs), [prefs])
  const data = list.data
  const total = data?.total
  const now = serverNow()
  const paging = data ? pageState(url.page, data.total, url.pageSize) : null
  const groups = useMemo(() => facetGroups(facets.data), [facets.data])
  const fleetEmpty = summary.data?.assets.total === 0
  const hasFilters = chips.length > 0

  const hrefForAsset = (address: string) => {
    const p = new URLSearchParams(url.params)
    p.set('asset', address)
    return `${pathname}?${p.toString()}`
  }
  const removeChip = (c: (typeof chips)[number]) =>
    url.hrefWith({ [c.key as AssetFilterKey]: c.key === 'tag' ? (url.filters.tag ?? []).filter((t) => t !== c.value) : null })

  async function exportCsv() {
    setExporting(true)
    try {
      const file = await inventoryApi.exportAssets(query)
      const stamp = new Date(serverNow()).toISOString().slice(0, 10)
      downloadBlob(file.blob, filenameFromDisposition(file.disposition, `inventory-assets-${stamp}.csv`))
      const t = exportStartedText(total ?? 0, file)
      if (t.truncated) toast.warning(t.title, t.description)
      else toast.success(t.title, t.description)
    } catch (e) {
      toast.error('Export failed', await blobErrorMessage(e, 'The export could not be prepared'))
    } finally {
      setExporting(false)
    }
  }

  if (access === 'denied') return null // the layout's gate has already said so

  const facetPanel = facets.data ? (
    <FacetPanel groups={groups} filters={url.filters} hrefFor={url.hrefWith} label="Filter assets" />
  ) : facets.isError ? (
    <QueryFailure compact what="the filters" error={facets.error} onRetry={() => void facets.refetch()} retrying={facets.isFetching} />
  ) : (
    <div className="space-y-2 rounded-lg border border-border bg-surface p-3" aria-busy="true" aria-label="Loading filters">
      {Array.from({ length: 7 }).map((_, i) => <Skeleton key={i} className="h-5 w-full" />)}
    </div>
  )

  const body = () => {
    if (list.isLoading || (access === 'unknown' && !data)) {
      return <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading assets">{Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-7 w-full" />)}</div>
    }
    if (list.isError && !data) {
      return <div className="p-4"><QueryFailure error={list.error} what="the asset list" onRetry={() => void list.refetch()} retrying={list.isFetching} /></div>
    }
    if (!data || !paging) return null
    if (paging.beyond) {
      return (
        <div className="p-4" data-state="beyond">
          <InventoryEmpty
            kind="no-match"
            title={`Page ${url.page.toLocaleString()} is past the end`}
            action={<Button size="sm" variant="outline" onClick={() => url.setPage(paging.lastPage)}>Go to the last page ({paging.lastPage.toLocaleString()})</Button>}
          >
            These filters match {data.total.toLocaleString()} {data.total === 1 ? 'asset' : 'assets'}, which is {paging.pageCount.toLocaleString()} {paging.pageCount === 1 ? 'page' : 'pages'} at {url.pageSize} per page.
          </InventoryEmpty>
        </div>
      )
    }
    if (data.total === 0) {
      if (hasFilters) {
        return (
          <InventoryEmpty
            kind="no-match"
            action={<Button size="sm" variant="outline" onClick={() => url.clearFilters()}>Clear all filters</Button>}
          >
            No asset matches every one of the active filters. Remove one to widen the list; the numbers in the filter pane show what each choice would give.
          </InventoryEmpty>
        )
      }
      if (summary.data?.scope.restricted) {
        return <InventoryEmpty kind="no-scope">Your role limits Inventory to assets tagged {summary.data.scope.tags.join(', ') || 'for your team'}, and none carry those tags yet.</InventoryEmpty>
      }
      return (
        <InventoryEmpty kind="no-data" title={fleetEmpty ? 'No assets yet' : 'Nothing here yet'}>
          Servers appear once their agent reports, and network devices once the poller has read them. This list fills in by itself.
        </InventoryEmpty>
      )
    }
    return (
      <>
        {list.isError && (
          <div className="border-b border-border p-2">
            <QueryFailure compact what="the asset list" error={list.error} onRetry={() => void list.refetch()} retrying={list.isFetching} />
            <p className="mt-1 text-[11px] text-muted">Showing the last list that loaded; it may be out of date.</p>
          </div>
        )}
        <div className={list.isFetching && list.isPlaceholderData ? 'opacity-60 transition-opacity' : 'transition-opacity'} aria-busy={list.isFetching}>
          <div className="overflow-auto lg:max-h-[calc(100dvh-15rem)]">
            <AssetTable
              rows={data.items}
              columns={columns}
              density={prefs.density}
              sort={url.sort}
              order={url.order}
              onSort={(key) => url.setSort(key)}
              onOpen={(address) => url.openAsset(address)}
              hrefFor={hrefForAsset}
              activeAddress={url.asset}
              now={now}
              caption={`Assets, ${rangeText(data.page, data.page_size, data.total)}, sorted by ${url.sort} ${url.order === 'asc' ? 'ascending' : 'descending'}`}
            />
          </div>
        </div>
        <TablePager
          page={url.page}
          pageCount={paging.pageCount}
          pageSize={url.pageSize}
          total={data.total}
          setPage={(p) => url.setPage(p)}
          setPageSize={(n) => url.setPageSize(n)}
          noun="assets"
        />
      </>
    )
  }

  return (
    <div className="flex items-start gap-4" data-page="assets">
      {wide && prefs.facets && (
        <aside aria-label="Asset filters" className="sticky top-4 hidden max-h-[calc(100dvh-2rem)] w-64 shrink-0 overflow-y-auto rounded-lg lg:block">
          {facetPanel}
        </aside>
      )}
      <section aria-label="Assets" className="min-w-0 flex-1 space-y-3">
        <AssetToolbar
          q={url.filters.q ?? ''}
          onQ={(q) => url.setFilter('q', q || null)}
          chips={chips}
          hrefWith={removeChip}
          onClear={() => url.clearFilters()}
          filtersOpen={prefs.facets}
          onToggleFilters={() => (wide ? setPrefs((p) => ({ ...p, facets: !p.facets })) : setDrawer(true))}
          wide={wide}
          filterCount={url.activeFilterCount}
          columns={prefs.columns}
          onToggleColumn={(id) => setPrefs((p) => toggleColumn(p, id))}
          density={prefs.density}
          onDensity={(density) => setPrefs((p) => ({ ...p, density }))}
          canExport={can('inventory.export')}
          onExport={() => void exportCsv()}
          exporting={exporting}
          total={total}
          loading={list.isLoading}
        />
        {url.rejected.length > 0 && (
          <p role="status" className="flex items-start gap-1.5 rounded-md border border-warning/35 bg-warning/10 px-3 py-1.5 text-xs text-warning-fg">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>Ignored in the address: {url.rejected.map((r) => `${r.key}=${r.value} (${r.reason})`).join('; ')}.</span>
          </p>
        )}
        <Card className="overflow-hidden">{body()}</Card>
      </section>

      {!wide && (
        <Sheet
          open={drawer}
          onOpenChange={setDrawer}
          side="left"
          width="max-w-[20rem]"
          title="Filters"
          description="Counts follow the other active filters."
          footer={<Button size="sm" className="w-full" onClick={() => setDrawer(false)}>{total === undefined ? 'Show assets' : `Show ${total.toLocaleString()} ${total === 1 ? 'asset' : 'assets'}`}</Button>}
        >
          <div className="p-3">{facetPanel}</div>
        </Sheet>
      )}

      {url.asset && <AssetFlyout address={url.asset} onClose={() => url.closeAsset()} />}
    </div>
  )
}
