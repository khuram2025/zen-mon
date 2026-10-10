import { useCallback, useMemo, useRef } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { assetFilterCodec, patchAssetFilters, type AssetFilters, type FilterCodec, type SortOrder } from './filters'
import {
  addressIsPage, apiParamsFor, createUrlWriter, readUrlState, urlStateConfig, withFilters,
  type Commit, type UrlStateConfig,
} from './urlState'

export type { Commit } from './urlState'

export interface InventoryUrlStateOptions<F> {
  /** How this page's filters read from and write to the query string. Defaults to the asset vocabulary. */
  codec?: FilterCodec<F>
  sorts?: readonly string[]
  defaultSort?: { sort: string; order: SortOrder }
  defaultPageSize?: number
  tabs?: readonly string[]
  defaultTab?: string
}

type FilterPatch = Parameters<typeof patchAssetFilters>[1]

/**
 * Filters, sort, page, tab and the open asset of an Inventory page, kept in the URL so every view is
 * shareable and Back-button safe. Which changes push a history entry and which replace it:
 *
 * - replace: typing in the search box (`q`), the page size and closing a flyout that was deep-linked: they
 *   would otherwise bury the history in near-identical entries;
 * - push: a facet or chip, a tab, a sort, a page, opening the asset flyout, a drill-down: a different
 *   view the Back button should return to. Closing a flyout that was opened by a push goes Back, so the
 *   list does not gain a second identical entry.
 *
 * Any setter takes `{ push }` to override. A new filter set always goes back to page 1 and closes the
 * flyout. The URL parameters are the API parameters (`apiParams`), see ./filters.ts.
 *
 * Every setter writes against the address AS IT IS WHEN IT IS CALLED (see urlState.createUrlWriter): the
 * functions below are stable, a handler or timer that outlives a render can safely call them, and
 * `setFilters(live => ...)` receives the live filters.
 */
export function useInventoryUrlState<F = AssetFilters>(options: InventoryUrlStateOptions<F> = {}) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const { pathname } = location
  const config = useMemo(
    () => urlStateConfig((options.codec ?? assetFilterCodec) as unknown as FilterCodec<F>, {
      sorts: options.sorts,
      defaultSort: options.defaultSort,
      defaultPageSize: options.defaultPageSize,
      tabs: options.tabs,
      defaultTab: options.defaultTab,
    }) as UrlStateConfig<F>,
    // The options are static per page; compare their content, not their identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [options.codec, options.sorts?.join(','), options.defaultSort?.sort, options.defaultSort?.order, options.defaultPageSize, options.tabs?.join(','), options.defaultTab],
  )
  const qs = params.toString()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const state = useMemo(() => readUrlState(new URLSearchParams(qs), config), [qs, config])
  const apiParams = useMemo(() => apiParamsFor(new URLSearchParams(qs), config), [qs, config])

  // The address as of the last render, advanced at once by every write of this hook, so two changes made
  // before the router has re-rendered compose instead of the second one undoing the first.
  // It follows the router again as soon as the router's own address changes (a link, Back, our own write
  // arriving): a render with the SAME address must not undo a write the router has not caught up with yet.
  const liveParams = useRef(params)
  const renderedQs = useRef(qs)
  if (renderedQs.current !== qs) {
    renderedQs.current = qs
    liveParams.current = params
  }
  const router = useRef({ setParams, navigate, state: location.state as unknown, pathname })
  router.current = { setParams, navigate, state: location.state as unknown, pathname }

  const writer = useMemo(
    () => createUrlWriter<F>(config, {
      read: () => new URLSearchParams(liveParams.current),
      write: (next, o) => {
        // A timer of a page the user has already left (a tab clicked while the next chunk downloads) must not
        // navigate back to it (NF-02).
        if (!addressIsPage(router.current.pathname)) return
        liveParams.current = next
        router.current.setParams(next, { replace: o.replace, state: o.state })
      },
      back: () => router.current.navigate(-1),
      locationState: () => router.current.state,
    }),
    [config],
  )

  // Links are built at render time from the rendered address: that is what a link is.
  const patched = useCallback(
    (patch: FilterPatch) => withFilters(params, config, patchAssetFilters(state.filters as unknown as AssetFilters, patch) as unknown as F),
    [params, config, state.filters],
  )

  return {
    ...state,
    params,
    /** The request: filters plus explicit sort, order, page and page_size. */
    apiParams,
    activeFilterCount: config.codec.count(state.filters),
    /** New filters, or a function from the LIVE filters to new ones. */
    setFilters: writer.setFilters,
    /** A patch of the asset vocabulary applied to the live filters (`null`, `undefined`, empty remove a key). */
    patchFilters: writer.patchFilters,
    setFilter: <K extends keyof AssetFilters>(key: K, value: AssetFilters[K] | null, o: Commit = {}) =>
      writer.patchFilters({ [key]: value } as FilterPatch, o),
    toggleTag: writer.toggleTag,
    clearFilters: writer.clearFilters,
    setTab: writer.setTab,
    setSort: writer.setSort,
    setPage: writer.setPage,
    setPageSize: writer.setPageSize,
    openAsset: writer.openAsset,
    closeAsset: writer.closeAsset,
    /** URL of this page with a filter patch applied: what a facet link points at. */
    hrefWith: useCallback(
      (patch: FilterPatch) => {
        const out = patched(patch).toString()
        return out ? `${pathname}?${out}` : pathname
      },
      [patched, pathname],
    ),
  }
}
