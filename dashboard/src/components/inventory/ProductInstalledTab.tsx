import { useMemo, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Download, Loader2, Search, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { toast } from '@/components/ui/Toast'
import { TablePager } from '@/components/servers/tables'
import { blobErrorMessage, downloadBlob, filenameFromDisposition } from '@/components/services/list/model'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { addressIsPage } from '@/pages/inventory/urlState'
import { softwareApi, softwareKeys, type InstallationsQuery, type ProductResponse } from '@/pages/inventory/softwareSettings'
import { exportStartedText, pageState } from './assetModel'
import { InventoryEmpty } from './InventoryEmpty'
import { ageWords, fmtInt } from './model'
import {
  installAssetHref, installedSummary, installSourceLabel, readProductState, versionFilterLabel,
  versionFilterOptions, withInstalled, withInstallSort, type InstallSort,
} from './productModel'
import { FilterSelect } from './SoftwareFilterSelect'
import { SortableHeader } from './SoftwareSortHeader'
import { FirstSeen } from './SoftwareTable'
import { shortDate } from './softwareModel'
import { useDebouncedSearch } from './useDebouncedSearch'

const focus = 'outline-none focus-visible:ring-2 focus-visible:ring-primary'

const COLUMNS: { label: string; sort: InstallSort; show: string }[] = [
  { label: 'Server', sort: 'asset_name', show: 'sticky left-0 z-[1] bg-surface' },
  { label: 'Package', sort: 'package_name', show: 'hidden md:table-cell' },
  { label: 'Version', sort: 'version', show: '' },
  { label: 'Architecture', sort: 'architecture', show: 'hidden lg:table-cell' },
  { label: 'Source', sort: 'source', show: 'hidden sm:table-cell' },
  { label: 'Installed', sort: 'install_date', show: 'hidden lg:table-cell' },
  { label: 'First seen', sort: 'first_seen_at', show: 'hidden xl:table-cell' },
  { label: 'Last report', sort: 'updated_at', show: '' },
]

const SORT_WORDS: Record<InstallSort, string> = Object.fromEntries(COLUMNS.map((c) => [c.sort, c.label.toLowerCase()])) as Record<InstallSort, string>

/** The table's caption for a screen reader: the product by name and the column by its label, not the API's identifiers (UI-19). */
export function captionText(productName: string, total: number, sort: InstallSort | string, order: 'asc' | 'desc'): string {
  const column = SORT_WORDS[sort as InstallSort] ?? String(sort).replace(/_/g, ' ')
  return `Installations of ${productName}: ${fmtInt(total)} in total, sorted by ${column}, ${order === 'asc' ? 'ascending' : 'descending'}`
}

/**
 * The Installed on tab: where the product is installed, one row per software row (a server with two titles of the
 * product appears twice), server-paged and server-sorted. Filters and sort live in the address (`version`, `q`,
 * `sort`, `order`, `page`, `page_size`: the API's own names). A server opens in the asset flyout of the Assets page.
 * Needs `inventory.export` for the CSV, which is the same rows in the same order.
 */
export function ProductInstalledTab({
  productKey, product, canExport, enabled, historyStart,
}: {
  productKey: string
  product: Pick<ProductResponse, 'header' | 'versions' | 'unversioned'> | undefined
  canExport: boolean
  enabled: boolean
  /** When change capture began (`history_start`): a first-seen at or before it reads "before <date>". */
  historyStart?: string | null
}) {
  const [params, setParams] = useSearchParams()
  const { pathname } = useLocation()
  const state = useMemo(() => readProductState(params).installed, [params])
  const [exporting, setExporting] = useState(false)

  const query: InstallationsQuery = useMemo(
    () => ({ version: state.version, q: state.q, sort: state.sort, order: state.order, page: state.page, page_size: state.pageSize }),
    [state],
  )
  const list = useQuery({
    queryKey: softwareKeys.installations(productKey, JSON.stringify(query)),
    queryFn: () => softwareApi.installations(productKey, query),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
    enabled,
    retry: 1,
  })
  const data = list.data
  const paging = data ? pageState(state.page, data.total, state.pageSize) : null

  // The box and the address bar (see useDebouncedSearch): the commit patches `q` alone, against the address as it is when the
  // text lands, so a version chosen in the meantime survives (FE-07) and the box is not overwritten by its own echo (FE-02).
  const search = useDebouncedSearch({
    value: state.q ?? '',
    // A search that lands after the user clicked another tab (whose chunk is still downloading) must not navigate back here (NF-02).
    onCommit: (next) => { if (addressIsPage(pathname)) setParams((prev) => withInstalled(prev, { q: next || undefined }), { replace: true }) },
  })

  const options = versionFilterOptions(product, state.version).map((o) => ({
    value: o.value, label: o.label, hint: o.installs === null ? undefined : fmtInt(o.installs),
  }))
  const setVersion = (v: string) => setParams((prev) => withInstalled(prev, { version: v || undefined }))
  const now = serverNow()

  async function exportCsv() {
    setExporting(true)
    try {
      const file = await softwareApi.exportInstallations(productKey, query)
      const stamp = new Date(serverNow()).toISOString().slice(0, 10)
      downloadBlob(file.blob, filenameFromDisposition(file.disposition, `inventory-installations-${productKey}-${stamp}.csv`))
      const t = exportStartedText(data?.total ?? 0, file)
      if (t.truncated) toast.warning(t.title, t.description)
      else toast.success(t.title, t.description)
    } catch (e) {
      toast.error('Export failed', await blobErrorMessage(e, 'The export could not be prepared'))
    } finally {
      setExporting(false)
    }
  }

  const filtered = !!state.version || !!state.q
  const clear = () => { search.reset(); setParams((prev) => withInstalled(prev, { version: undefined, q: undefined })) }

  const body = () => {
    if (list.isLoading) {
      return <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading installations">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-7 w-full" />)}</div>
    }
    if (list.isError && !data) {
      return <div className="p-4"><QueryFailure error={list.error} what="the installations" onRetry={() => void list.refetch()} retrying={list.isFetching} /></div>
    }
    if (!data || !paging) return null
    if (paging.beyond) {
      return (
        <div className="p-4" data-state="beyond">
          <InventoryEmpty
            kind="no-match"
            title={`Page ${state.page.toLocaleString()} is past the end`}
            action={<Button size="sm" variant="outline" onClick={() => setParams((prev) => withInstalled(prev, { page: paging.lastPage }, { keepPage: true }))}>Go to the last page ({paging.lastPage.toLocaleString()})</Button>}
          >
            These filters match {data.total.toLocaleString()} {data.total === 1 ? 'installation' : 'installations'}.
          </InventoryEmpty>
        </div>
      )
    }
    if (data.total === 0) {
      return filtered ? (
        <InventoryEmpty kind="no-match" action={<Button size="sm" variant="outline" onClick={clear}>Clear the filters</Button>}>
          No installation of this product matches the version and search you chose.
        </InventoryEmpty>
      ) : (
        <InventoryEmpty kind="no-data">This product is not installed on any server you can see.</InventoryEmpty>
      )
    }
    return (
      <>
        {list.isError && (
          <div className="border-b border-border p-2">
            <QueryFailure compact what="the installations" error={list.error} onRetry={() => void list.refetch()} retrying={list.isFetching} />
            <p className="mt-1 text-xs text-muted">Showing the last list that loaded; it may be out of date.</p>
          </div>
        )}
        <div className={list.isFetching && list.isPlaceholderData ? 'opacity-60 transition-opacity' : 'transition-opacity'} aria-busy={list.isFetching}>
          <div className="overflow-auto lg:max-h-[calc(100dvh-22rem)]">
            <table className="w-full border-separate border-spacing-0 text-sm" data-installed-table="">
              <caption className="sr-only">{captionText(product?.header.name ?? productKey, data.total, state.sort, state.order)}</caption>
              <thead className="sticky top-0 z-10 bg-surface">
                <tr className="[&>th]:border-b [&>th]:border-border">
                  {COLUMNS.map((c) => (
                    <SortableHeader<InstallSort>
                      key={c.sort}
                      label={c.label}
                      sortKey={c.sort}
                      sort={state.sort}
                      order={state.order}
                      onSort={(key) => setParams((prev) => withInstallSort(prev, key))}
                      className={c.show}
                    />
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.items.map((r, i) => (
                  <tr key={`${r.asset_key}:${r.package_name}:${i}`} className="group hover:bg-surface2/60 [&>td]:border-b [&>td]:border-border/60 [&>th]:border-b [&>th]:border-border/60 [&>td]:py-1.5 [&>th]:py-1.5">
                    <th scope="row" className="sticky left-0 z-[1] max-w-[14rem] bg-surface px-3 text-left align-middle font-normal group-hover:bg-surface2">
                      <Link to={installAssetHref(r)} title="Open this server in the asset flyout" className={cn('block truncate rounded-sm font-medium text-text hover:text-primary', focus)}>{r.asset_name}</Link>
                    </th>
                    <td className="hidden max-w-[16rem] truncate px-3 align-middle text-text2 md:table-cell" title={r.package_name}>{r.package_name}</td>
                    <td className="px-3 align-middle">
                      {r.version ? <span className="font-mono text-xs" title={r.version}>{r.version}</span> : <span className="italic text-muted">No version</span>}
                    </td>
                    <td className="hidden px-3 align-middle text-text2 lg:table-cell">{r.architecture ?? <span className="text-muted">—</span>}</td>
                    <td className="hidden px-3 align-middle sm:table-cell">
                      <span className="rounded-full border border-border bg-surface2 px-1.5 py-px text-xs text-text2" title={r.source ?? 'Source not reported'}>{installSourceLabel(r.source)}</span>
                    </td>
                    <td className="hidden whitespace-nowrap px-3 align-middle text-text2 lg:table-cell"><span title={r.install_date ?? undefined}>{shortDate(r.install_date)}</span></td>
                    <td className="hidden whitespace-nowrap px-3 align-middle text-text2 xl:table-cell"><FirstSeen iso={r.first_seen_at} historyStart={historyStart} /></td>
                    <td className="whitespace-nowrap px-3 align-middle text-text2"><span title={r.updated_at ?? undefined}>{ageWords(r.updated_at, now)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <TablePager
          page={state.page}
          pageCount={paging.pageCount}
          pageSize={state.pageSize}
          total={data.total}
          setPage={(p) => setParams((prev) => withInstalled(prev, { page: p }, { keepPage: true }))}
          setPageSize={(n) => setParams((prev) => withInstalled(prev, { pageSize: n }), { replace: true })}
          noun="installations"
        />
      </>
    )
  }

  return (
    <div className="space-y-3" data-tab="installed">
      <div className="flex flex-wrap items-center gap-2">
        <FilterSelect
          label="Version"
          allLabel="All versions"
          value={state.version ?? ''}
          valueText={versionFilterLabel(state.version)}
          options={options}
          onChange={setVersion}
          className="w-[14rem]"
          contentClassName="max-h-80"
        />
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
          <input
            type="search"
            value={search.text}
            onChange={(e) => search.onChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') search.flush() }}
            placeholder="Search server, package or version"
            aria-label="Search installations"
            maxLength={200}
            className="h-9 w-full rounded-md border border-border bg-surface pl-8 pr-8 text-sm shadow-sm placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          />
          {search.text && (
            <button type="button" onClick={() => search.clear()} aria-label="Clear search" className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-primary">
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          )}
        </div>
        {canExport && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9"
            onClick={() => void exportCsv()}
            aria-label="Export CSV"
            disabled={exporting || data?.total === 0}
            title={data?.total ? `Export all ${fmtInt(data.total)} matching installations as CSV` : 'Nothing to export'}
          >
            {exporting ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Download className="h-4 w-4" aria-hidden />}
            <span className="hidden sm:inline">Export</span>
          </Button>
        )}
      </div>
      <p className="min-h-[16px] text-xs text-muted" role="status" aria-live="polite" data-installed-count="">
        {data ? <strong className="font-semibold tabular-nums text-text2">{installedSummary(data.total, product?.header.installs ?? data.total, state.version)}</strong> : ''}
        {filtered && (
          <button type="button" onClick={clear} className="ml-3 rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">Clear filters</button>
        )}
      </p>
      <Card className="overflow-hidden">{body()}</Card>
    </div>
  )
}

