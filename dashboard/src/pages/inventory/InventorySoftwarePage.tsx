import { useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { exportStartedText, pageState } from '@/components/inventory/assetModel'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { InventoryEmpty } from '@/components/inventory/InventoryEmpty'
import { SoftwareFooter } from '@/components/inventory/SoftwareFooter'
import { SoftwareTable } from '@/components/inventory/SoftwareTable'
import { SoftwareToolbar } from '@/components/inventory/SoftwareToolbar'
import {
  catalogFilterChips, catalogFooter, clearedFilters, NORMALISING_EMPTY_TEXT, NO_SOFTWARE_TEXT, productRangeText, publisherNote,
  publisherOptions, withoutChip, type CatalogChip, type CatalogQuery,
} from '@/components/inventory/softwareModel'
import { useSoftwareHistoryStart } from '@/components/inventory/useSoftwareHistoryStart'
import { useSoftwareUrlState } from '@/components/inventory/useSoftwareUrlState'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { TablePager } from '@/components/servers/tables'
import { blobErrorMessage, downloadBlob, filenameFromDisposition } from '@/components/services/list/model'
import { serverNow } from '@/lib/serverClock'
import { useCan } from '@/stores/auth'
import { softwareApi, softwareKeys } from './softwareSettings'

const SORT_WORDS: Record<string, string> = {
  name: 'product', publisher: 'publisher', category: 'category', hosts: 'hosts', version_count: 'version count', latest_version: 'latest version', first_seen: 'first seen',
}

/**
 * /inventory/software (doc 03 section 3.4): the installed software as a catalog, one row per product, filtered,
 * sorted and paged on the server. The filters, the sort and the page live in the address bar (the URL parameters
 * are the API's own). Every host count opens the assets that have the product, every version a list of its own.
 */
export function InventorySoftwarePage() {
  const access = useInventoryAccess()
  const allowed = access === 'allowed'
  const can = useCan()
  const url = useSoftwareUrlState()
  const historyStart = useSoftwareHistoryStart()
  const [exporting, setExporting] = useState(false)

  const query: CatalogQuery = useMemo(
    () => ({ ...url.filters, sort: url.sort, order: url.order, page: url.page, page_size: url.pageSize }),
    [url.filters, url.sort, url.order, url.page, url.pageSize],
  )

  // Keys and staleness of doc 03 section 8: refetched when the filters change, a fresh table at most every minute.
  const list = useQuery({
    queryKey: softwareKeys.catalog(url.apiParams.toString()),
    queryFn: () => softwareApi.catalog(query),
    staleTime: 60_000,
    refetchInterval: 300_000,
    placeholderData: keepPreviousData,
    enabled: allowed,
  })
  // The publisher facet counts what choosing a publisher would give, so it follows every filter but the publisher.
  const facetFilters = useMemo(() => ({ ...url.filters, publisher: undefined }), [url.filters])
  const publishers = useQuery({
    queryKey: softwareKeys.publishers(JSON.stringify(facetFilters)),
    queryFn: () => softwareApi.publishers(url.filters),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    enabled: allowed,
  })

  const data = list.data
  const total = data?.total
  const options = useMemo(() => publisherOptions(publishers.data, url.filters.publisher), [publishers.data, url.filters.publisher])
  const selectOptions = useMemo(
    () => options.map((o) => ({ value: o.value, label: o.label, hint: o.products > 0 ? String(o.products) : undefined })),
    [options],
  )
  const chips = catalogFilterChips(url.filters, (value) => options.find((o) => o.value.toLowerCase() === value.toLowerCase())?.label)
  const paging = data ? pageState(url.page, data.total, url.pageSize) : null
  const footer = data ? catalogFooter(data, url.filters) : null
  const hasFilters = chips.length > 0 || !!url.filters.q

  const removeChip = (c: CatalogChip) => url.hrefWith(withoutChip(url.filters, c))

  async function exportCsv() {
    setExporting(true)
    try {
      const file = await softwareApi.exportCatalog(query)
      const stamp = new Date(serverNow()).toISOString().slice(0, 10)
      downloadBlob(file.blob, filenameFromDisposition(file.disposition, `inventory-software-${stamp}.csv`))
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

  const body = () => {
    if (list.isLoading || (access === 'unknown' && !data)) {
      return <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading software">{Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
    }
    if (list.isError && !data) {
      return <div className="p-4"><QueryFailure error={list.error} what="the software catalog" onRetry={() => void list.refetch()} retrying={list.isFetching} /></div>
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
            These filters match {data.total.toLocaleString()} {data.total === 1 ? 'product' : 'products'}, which is {paging.pageCount.toLocaleString()} {paging.pageCount === 1 ? 'page' : 'pages'} at {url.pageSize} per page.
          </InventoryEmpty>
        </div>
      )
    }
    if (data.total === 0) {
      if (hasFilters) {
        return (
          <InventoryEmpty
            kind="no-match"
            action={<Button size="sm" variant="outline" onClick={() => url.setFilters(clearedFilters)}>Clear all filters</Button>}
          >
            No product matches every one of the active filters. Remove one to widen the list; the publisher menu shows how many products each choice would give.
            {data.data_status.normalising && <> Software lists are still being normalised, so the catalog is incomplete.</>}
          </InventoryEmpty>
        )
      }
      if (data.hidden_products > 0) {
        return (
          <InventoryEmpty
            kind="no-match"
            title="Every product is in a hidden category"
            action={<Button size="sm" variant="outline" onClick={() => url.patchFilters({ hidden: 'include' })}>Show them</Button>}
          >
            {data.hidden_products.toLocaleString()} {data.hidden_products === 1 ? 'product is' : 'products are'} left out because {data.hidden_products === 1 ? 'its' : 'their'} category is hidden by default.
          </InventoryEmpty>
        )
      }
      // Rows that are not keyed yet mean the catalog is still being built, not that the estate has no software.
      if (data.data_status.normalising) {
        return <InventoryEmpty kind="no-data" title="The catalog is being built">{NORMALISING_EMPTY_TEXT}.</InventoryEmpty>
      }
      if (data.scope.restricted) {
        return <InventoryEmpty kind="no-scope">Your role limits Inventory to assets tagged {data.scope.tags.join(', ') || 'for your team'}, and none of those servers has reported software yet.</InventoryEmpty>
      }
      return (
        <InventoryEmpty kind="no-data" title="No software inventory yet">
          {NO_SOFTWARE_TEXT}
        </InventoryEmpty>
      )
    }
    return (
      <>
        {list.isError && (
          <div className="border-b border-border p-2">
            <QueryFailure compact what="the software catalog" error={list.error} onRetry={() => void list.refetch()} retrying={list.isFetching} />
            <p className="mt-1 text-[11px] text-muted">Showing the last list that loaded; it may be out of date.</p>
          </div>
        )}
        <div className={list.isFetching && list.isPlaceholderData ? 'opacity-60 transition-opacity' : 'transition-opacity'} aria-busy={list.isFetching}>
          <div className="overflow-auto lg:max-h-[calc(100dvh-17rem)]">
            <SoftwareTable
              rows={data.items}
              sort={url.sort}
              order={url.order}
              onSort={(key) => url.setSort(key)}
              caption={`Software, ${productRangeText(data.page, data.page_size, data.total)}, sorted by ${SORT_WORDS[url.sort] ?? url.sort} ${url.order === 'asc' ? 'ascending' : 'descending'}`}
              historyStart={historyStart}
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
          noun="products"
        />
      </>
    )
  }

  return (
    <section aria-label="Software" className="space-y-3" data-page="software">
      <h2 className="sr-only">Software catalog</h2>
      <SoftwareToolbar
        filters={url.filters}
        onPatch={(patch) => url.patchFilters(patch)}
        chips={chips}
        hrefWith={removeChip}
        onClear={() => url.setFilters(clearedFilters)}
        publishers={selectOptions}
        publisherNote={publisherNote(publishers.data)}
        hiddenCategories={data?.hidden_categories}
        canExport={can('inventory.export')}
        onExport={() => void exportCsv()}
        exporting={exporting}
        total={total}
        loading={list.isLoading}
      />
      {publishers.isError && !publishers.data && (
        <p role="status" className="flex flex-wrap items-center gap-x-2 text-xs text-warning-fg" data-publishers-failed="">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
          The publisher list could not be loaded, so the Publisher menu is empty. The catalog itself is not affected.
          <button type="button" onClick={() => void publishers.refetch()} className="rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">Retry</button>
        </p>
      )}
      {url.rejected.length > 0 && (
        <p role="status" className="flex items-start gap-1.5 rounded-md border border-warning/35 bg-warning/10 px-3 py-1.5 text-xs text-warning-fg">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Ignored in the address: {url.rejected.map((r) => `${r.key}=${r.value} (${r.reason})`).join('; ')}.</span>
        </p>
      )}
      <Card className="overflow-hidden">
        {body()}
        {footer && <SoftwareFooter footer={footer} />}
      </Card>
    </section>
  )
}
