import { useCallback, useMemo, useRef } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { addressIsPage } from '@/pages/inventory/urlState'
import {
  catalogApiParams, catalogFilterCodec, catalogPushesHistory, readCatalogState, withCatalogFilters, withCatalogPage,
  withCatalogPageSize, withCatalogSort, type CatalogFilters, type CatalogSort,
} from './softwareModel'

/** Whether a change adds a history entry. */
export interface Commit {
  /** `true` pushes (the Back button undoes the change), `false` replaces. */
  push?: boolean
}

/** A patch of the filters: a key set to `undefined` (or an empty list) is removed. */
export type FiltersPatch = { [K in keyof CatalogFilters]?: CatalogFilters[K] | undefined }

/** `current` with `patch` applied; a key whose patch value is undefined, blank or an empty list is dropped. */
export function patchCatalogFilters(current: CatalogFilters, patch: FiltersPatch): CatalogFilters {
  const next: Record<string, unknown> = { ...current }
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined || value === null || value === '' || (Array.isArray(value) && value.length === 0)) delete next[key]
    else next[key] = value
  }
  return next as CatalogFilters
}

/**
 * What a change of filters writes: the new address (the live one with the new filters, page back to 1, everything
 * else kept) and whether it adds a history entry. A pure function of the live address, so the hook, a debounced
 * search and the tests all see the same rule (FE-02, FE-07).
 */
export function planFilters(
  live: URLSearchParams,
  filters: CatalogFilters | ((live: CatalogFilters) => CatalogFilters),
  o: Commit = {},
): { next: URLSearchParams; push: boolean } {
  const current = readCatalogState(live).filters
  const wanted = typeof filters === 'function' ? filters(current) : filters
  return { next: withCatalogFilters(live, wanted), push: o.push ?? catalogPushesHistory(current, wanted) }
}

/**
 * Filters, sort, page and page size of the Software catalog, kept in the address bar so every view is
 * shareable and Back-button safe. The parameters are the API's own (`q`, `category`, `publisher`, `hidden`,
 * `unmatched`, `sort`, `order`, `page`, `page_size`). Typing in the search box and the page size replace the
 * current history entry; a filter, a sort or a page is a different view and pushes. A new filter set always
 * goes back to page 1.
 *
 * Every setter works on the address AS IT IS WHEN IT IS CALLED, and a write advances that address at once, so a
 * debounced search that lands after a facet click (FE-07), or two changes made before the router has re-rendered,
 * compose instead of the later one undoing the earlier. `setFilters` takes the new filters or a function from the
 * LIVE filters to them; `patchFilters` changes some keys and leaves the rest alone.
 */
export function useSoftwareUrlState() {
  const [params, setParams] = useSearchParams()
  const { pathname } = useLocation()
  const qs = params.toString()
  const state = useMemo(() => readCatalogState(new URLSearchParams(qs)), [qs])
  const apiParams = useMemo(() => catalogApiParams(state), [state])

  // The address as of the last render, moved on by every write of this hook; it follows the router again as soon
  // as the router's own address changes (a link, Back, or our own write arriving).
  const live = useRef(params)
  const renderedQs = useRef(qs)
  if (renderedQs.current !== qs) {
    renderedQs.current = qs
    live.current = params
  }
  const router = useRef({ setParams, pathname })
  router.current = { setParams, pathname }

  const commit = useCallback((next: URLSearchParams, push: boolean) => {
    // A debounced search of a page the user has already left must not navigate back to it (NF-02).
    if (!addressIsPage(router.current.pathname)) return
    live.current = next
    router.current.setParams(next, { replace: !push })
  }, [])

  const setFilters = useCallback(
    (filters: CatalogFilters | ((live: CatalogFilters) => CatalogFilters), o: Commit = {}) => {
      const plan = planFilters(live.current, filters, o)
      commit(plan.next, plan.push)
    },
    [commit],
  )
  const patchFilters = useCallback((patch: FiltersPatch, o: Commit = {}) => setFilters((current) => patchCatalogFilters(current, patch), o), [setFilters])

  return {
    ...state,
    params,
    apiParams,
    activeFilterCount: catalogFilterCodec.count(state.filters),
    setFilters,
    patchFilters,
    setSort: useCallback((column: CatalogSort, o: Commit = {}) => commit(withCatalogSort(live.current, column), o.push ?? true), [commit]),
    setPage: useCallback((page: number, o: Commit = {}) => commit(withCatalogPage(live.current, page), o.push ?? true), [commit]),
    setPageSize: useCallback((size: number, o: Commit = {}) => commit(withCatalogPageSize(live.current, size), o.push ?? false), [commit]),
    /** URL of this page with other filters (what a chip's remove link points at); sort and page size stay. */
    hrefWith: useCallback(
      (filters: CatalogFilters) => {
        const out = withCatalogFilters(params, filters).toString()
        return out ? `${pathname}?${out}` : pathname
      },
      [params, pathname],
    ),
  }
}
