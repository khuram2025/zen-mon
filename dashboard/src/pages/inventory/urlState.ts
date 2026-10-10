// The URL half of an Inventory list page, as pure functions over URLSearchParams: read the page
// state (filters, sort, order, page, page size, tab, open asset) and change it. useInventoryUrlState
// only wires these to the router, so what the address bar says is testable without a browser.

import {
  ASSET_SORT_KEYS,
  DEFAULT_ASSET_SORT,
  DEFAULT_PAGE_SIZE,
  normaliseListState,
  nextSort,
  parseAssetAddress,
  parseListState,
  patchAssetFilters,
  toggleTag,
  type AssetFilters,
  type FilterCodec,
  type RejectedFilter,
  type SortOrder,
} from './filters'

export interface UrlStateConfig<F> {
  codec: FilterCodec<F>
  sorts: readonly string[]
  defaultSort: { sort: string; order: SortOrder }
  defaultPageSize: number
  tabs: readonly string[]
  defaultTab: string
}

export function urlStateConfig<F>(codec: FilterCodec<F>, partial: Partial<Omit<UrlStateConfig<F>, 'codec'>> = {}): UrlStateConfig<F> {
  return {
    codec,
    sorts: partial.sorts ?? ASSET_SORT_KEYS,
    defaultSort: partial.defaultSort ?? DEFAULT_ASSET_SORT,
    defaultPageSize: partial.defaultPageSize ?? DEFAULT_PAGE_SIZE,
    tabs: partial.tabs ?? [],
    defaultTab: partial.defaultTab ?? '',
  }
}

export interface UrlState<F> {
  filters: F
  rejected: RejectedFilter[]
  tab: string
  sort: string
  order: SortOrder
  page: number
  pageSize: number
  /** `<kind>:<uuid>` of the asset whose flyout is open, or null. */
  asset: string | null
}

/** Reads the page state; every value that is missing or invalid falls back to its default. */
export function readUrlState<F>(params: URLSearchParams, config: UrlStateConfig<F>): UrlState<F> {
  const { filters, rejected } = config.codec.parse(params)
  const list = parseListState(params, config.sorts)
  const tabRaw = params.get('tab')
  const tab = tabRaw !== null && config.tabs.includes(tabRaw) ? tabRaw : config.defaultTab
  const address = parseAssetAddress(params.get('asset'))
  return {
    filters,
    rejected,
    tab,
    sort: list.sort ?? config.defaultSort.sort,
    order: list.order ?? config.defaultSort.order,
    page: list.page ?? 1,
    pageSize: list.page_size ?? config.defaultPageSize,
    asset: address ? `${address.kind}:${address.id}` : null,
  }
}

/** Page 1 and the default size are not written: a plain list has a plain URL. */
function writeList(next: URLSearchParams, config: UrlStateConfig<unknown>, list: { sort: string; order: SortOrder; page: number; pageSize: number }) {
  for (const k of ['sort', 'order', 'page', 'page_size']) next.delete(k)
  const clean = normaliseListState({ sort: list.sort as never, order: list.order, page: list.page, page_size: list.pageSize }, config.sorts)
  const sort = clean.sort ?? config.defaultSort.sort
  const order = clean.order ?? config.defaultSort.order
  if (sort !== config.defaultSort.sort || order !== config.defaultSort.order) {
    next.set('sort', sort)
    next.set('order', order)
  }
  if ((clean.page ?? 1) > 1) next.set('page', String(clean.page))
  if ((clean.page_size ?? config.defaultPageSize) !== config.defaultPageSize) next.set('page_size', String(clean.page_size))
}

/**
 * New filters replace the old ones (and are written first, in vocabulary order, so URLs read the same
 * whichever way they were reached); the page goes back to 1 and the open asset closes, since it may no
 * longer be listed. Every other parameter stays, in its place.
 */
export function withFilters<F>(params: URLSearchParams, config: UrlStateConfig<F>, filters: F): URLSearchParams {
  const next = new URLSearchParams()
  config.codec.write(filters, next)
  for (const [key, value] of params) {
    if (config.codec.keys.includes(key) || key === 'page' || key === 'asset') continue
    next.append(key, value)
  }
  return next
}

export function withTab<F>(params: URLSearchParams, config: UrlStateConfig<F>, tab: string): URLSearchParams {
  const next = new URLSearchParams(params)
  if (!tab || tab === config.defaultTab || !config.tabs.includes(tab)) next.delete('tab')
  else next.set('tab', tab)
  // A tab is a different table: its own sort and page.
  for (const k of ['sort', 'order', 'page', 'page_size', 'asset']) next.delete(k)
  return next
}

/** Clicking a column: a new column sorts ascending, the same column flips; back to page 1. */
export function withSort<F>(params: URLSearchParams, config: UrlStateConfig<F>, column: string): URLSearchParams {
  const state = readUrlState(params, config)
  const s = nextSort({ sort: state.sort, order: state.order }, column)
  const next = new URLSearchParams(params)
  writeList(next, config, { ...s, page: 1, pageSize: state.pageSize })
  return next
}

export function withPage<F>(params: URLSearchParams, config: UrlStateConfig<F>, page: number): URLSearchParams {
  const state = readUrlState(params, config)
  const next = new URLSearchParams(params)
  writeList(next, config, { sort: state.sort, order: state.order, page: Math.max(1, Math.floor(page) || 1), pageSize: state.pageSize })
  return next
}

export function withPageSize<F>(params: URLSearchParams, config: UrlStateConfig<F>, pageSize: number): URLSearchParams {
  const state = readUrlState(params, config)
  const next = new URLSearchParams(params)
  writeList(next, config, { sort: state.sort, order: state.order, page: 1, pageSize })
  return next
}

export function withAsset(params: URLSearchParams, address: string | null): URLSearchParams {
  const next = new URLSearchParams(params)
  const parsed = parseAssetAddress(address)
  if (parsed) next.set('asset', `${parsed.kind}:${parsed.id}`)
  else next.delete('asset')
  return next
}

/** The request for the data behind the page: filters plus explicit sort, order, page and page_size. */
export function apiParamsFor<F>(params: URLSearchParams, config: UrlStateConfig<F>): URLSearchParams {
  const state = readUrlState(params, config)
  const out = new URLSearchParams()
  config.codec.write(state.filters, out)
  out.set('sort', state.sort)
  out.set('order', state.order)
  out.set('page', String(state.page))
  out.set('page_size', String(state.pageSize))
  return out
}

/**
 * Whether a filter change adds a history entry. Typing in the search box alone replaces the current
 * entry (otherwise one search leaves a trail of near-identical entries); a facet, a chip or a
 * drill-down is a different view and pushes, so the Back button returns to the previous one.
 */
export function pushesHistory(patch: object): boolean {
  const keys = Object.keys(patch)
  return !(keys.length === 1 && keys[0] === 'q')
}

/* ── Writing to the LIVE address ─────────────────────────────────────────── */

/** Whether a change adds a history entry. */
export interface Commit {
  /** `true` pushes (the Back button undoes the change), `false` replaces. */
  push?: boolean
}

type FilterPatch = Parameters<typeof patchAssetFilters>[1]

/**
 * `history.state.usr` marker of an entry the flyout was opened with (a push). Closing such a flyout goes Back
 * (so the list does not gain a second identical entry); a flyout opened by a deep link has nothing to go back
 * to and is closed by replacing the entry (review finding FE-09).
 */
export const FLYOUT_STATE = { zpFlyout: true } as const

export function closeAssetPlan(locationState: unknown): 'back' | 'replace' {
  return typeof locationState === 'object' && locationState !== null && (locationState as { zpFlyout?: unknown }).zpFlyout === true ? 'back' : 'replace'
}

const comparablePath = (path: string): string => {
  let p = path
  try { p = decodeURIComponent(path) } catch { /* keep the raw text */ }
  return p.replace(/\/+$/, '').toLowerCase() || '/'
}

/**
 * Whether the browser's address still points at `pagePath`, the page asking to change its own query string.
 *
 * The router keeps the OLD page mounted while the lazy chunk of the page being navigated to downloads (a
 * transition), and a relative `?q=...` write from that old page resolves against its own route: it would start a
 * new navigation BACK to it and cancel the user's click on a tab (review finding NF-02). The browser's address is
 * already the new one, so a page that finds it has moved on writes nothing. Without a `window` (the tests) the
 * answer is always yes.
 */
export function addressIsPage(pagePath: string, win: { location: { pathname: string } } | undefined = typeof window === 'undefined' ? undefined : window): boolean {
  if (!win) return true
  return comparablePath(win.location.pathname) === comparablePath(pagePath)
}

/** The router seen from the writer. */
export interface UrlIO {
  /** The address as it is NOW: after any change this writer made, even before the router has re-rendered. */
  read(): URLSearchParams
  write(next: URLSearchParams, o: { replace: boolean; state?: unknown }): void
  /** Goes back one history entry. */
  back(): void
  /** The location state of the current entry (`history.state.usr`). */
  locationState(): unknown
}

/**
 * Every change a list page makes to its address, applied to the address AS IT IS WHEN THE CHANGE HAPPENS.
 *
 * A setter that closes over the filters of an older render (a debounce timer, a handler that outlived a
 * re-render) writes those older filters back and undoes whatever changed in between: a facet clicked
 * while a search was waiting vanished (review finding FE-07). Here nothing is captured: each call re-reads
 * the address through `io.read()`, and a function passed to `setFilters` receives the live filters.
 */
export function createUrlWriter<F>(config: UrlStateConfig<F>, io: UrlIO) {
  const live = () => io.read()
  const commit = (next: URLSearchParams, push: boolean, state?: unknown) => io.write(next, { replace: !push, state })
  const filtersNow = (): F => readUrlState(live(), config).filters
  return {
    /** Filters of the address right now. */
    filters: filtersNow,
    setFilters(filters: F | ((live: F) => F), o: Commit = {}) {
      const base = live()
      const next = typeof filters === 'function' ? (filters as (live: F) => F)(readUrlState(base, config).filters) : filters
      commit(withFilters(base, config, next), o.push ?? true)
    },
    /** A patch in the asset vocabulary (`undefined`, `null`, empty removes a key) applied to the live filters. */
    patchFilters(patch: FilterPatch, o: Commit = {}) {
      const base = live()
      const next = patchAssetFilters(readUrlState(base, config).filters as unknown as AssetFilters, patch) as unknown as F
      // Typing in the search box alone replaces; everything else is a view worth going Back to.
      commit(withFilters(base, config, next), o.push ?? pushesHistory(patch))
    },
    toggleTag(tag: string, o: Commit = {}) {
      const base = live()
      const next = toggleTag(readUrlState(base, config).filters as unknown as AssetFilters, tag) as unknown as F
      commit(withFilters(base, config, next), o.push ?? true)
    },
    clearFilters(o: Commit = {}) {
      commit(withFilters(live(), config, {} as F), o.push ?? true)
    },
    setTab(tab: string, o: Commit = {}) { commit(withTab(live(), config, tab), o.push ?? true) },
    setSort(column: string, o: Commit = {}) { commit(withSort(live(), config, column), o.push ?? true) },
    setPage(page: number, o: Commit = {}) { commit(withPage(live(), config, page), o.push ?? true) },
    setPageSize(size: number, o: Commit = {}) { commit(withPageSize(live(), config, size), o.push ?? false) },
    openAsset(address: string, o: Commit = {}) {
      const push = o.push ?? true
      commit(withAsset(live(), address), push, push ? FLYOUT_STATE : undefined)
    },
    closeAsset(o: Commit = {}) {
      if (o.push === undefined && closeAssetPlan(io.locationState()) === 'back') {
        io.back()
        return
      }
      commit(withAsset(live(), null), o.push ?? false)
    },
  }
}
