import { api } from '@/lib/api'
import { DEFAULT_ASSET_SORT, assetQueryToParams, normaliseListState, parseAssetAddress, writeAssetFilters, type AssetKind, type AssetQuery } from './filters'
import type {
  AssetDetail, AssetList, ChangeAction, ChangeCategory, ChangesResponse, FacetsResponse, HardwareResponse,
  InventorySummary, OsResponse, TrendsResponse,
} from './types'

// Fetch functions for the Inventory API, in ONE file next to ./types.ts. Query strings are built
// by ./filters.ts, so the address bar and the request carry the same parameter names.

const base = '/inventory'

/** What GET /inventory/changes accepts (Documentation/Inventory/05-P1-API-CONTRACT.md section 3.7). */
export interface ChangesQuery {
  category?: ChangeCategory
  action?: ChangeAction
  kind?: AssetKind
  /** `<kind>:<uuid>`. */
  asset?: string
  /** A software product key. */
  subject_key?: string
  q?: string
  label?: 'upgrade' | 'downgrade'
  since?: string
  until?: string
  tz?: string
  page?: number
  page_size?: number
}

/** A file the server generated; `disposition` is its raw Content-Disposition header (the suggested file name). */
export interface DownloadedFile {
  blob: Blob
  disposition: string | null
  /** Rows in the file (`X-Export-Rows`), when the server says. */
  rows: number | null
  /** The export hit the server's row cap (`X-Export-Truncated`): the file is not the whole result. */
  truncated: boolean
}

function definedParams(input: Record<string, string | number | undefined | null>): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  for (const [k, v] of Object.entries(input)) if (v !== undefined && v !== null && v !== '') out[k] = v
  return out
}

/** Whether an HTTP error is the API refusing the `tz` parameter (422 and the detail names it). */
export function isTimeZoneRejection(error: unknown): boolean {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown } } } | null)?.response
  if (response?.status !== 422) return false
  const detail = response.data?.detail
  const text = typeof detail === 'string' ? detail : JSON.stringify(detail ?? '')
  return /\btz\b|time ?zone/i.test(text)
}

/**
 * Sends a request that carries `tz` (the browser's zone, for the daily series) and, if the API refuses that
 * zone with a 422, sends it once more without `tz` so the page still loads in the server's zone. A browser can
 * report a legacy alias (`Asia/Calcutta`, `Europe/Kiev`) that an appliance without the legacy tz database cannot
 * resolve; the server falls back on its own, this is the belt to those braces (review finding FE-03).
 */
export async function withTimeZoneFallback<T>(
  send: (params: Record<string, string | number>) => Promise<T>,
  params: Record<string, string | number>,
): Promise<T> {
  try {
    return await send(params)
  } catch (e) {
    if (params.tz === undefined || !isTimeZoneRejection(e)) throw e
    const { tz: _dropped, ...rest } = params
    return send(rest)
  }
}

export const inventoryApi = {
  /** KPI block for the Overview and the meta-line under the tabs. */
  async summary(): Promise<InventorySummary> {
    return (await api.get(`${base}/summary`)).data
  },
  async assets(query: AssetQuery): Promise<AssetList> {
    return (await api.get(`${base}/assets`, { params: assetQueryToParams(query, { explicit: true }) })).data
  },
  /** Counts per facet; they reflect the other active filters. */
  async assetFacets(query: AssetQuery): Promise<FacetsResponse> {
    return (await api.get(`${base}/assets/facets`, { params: assetQueryToParams(query) })).data
  },
  /** The flyout payload of one asset (`<kind>:<uuid>`). A 404 means unknown, malformed or out of scope: one body for all three. */
  async assetDetail(address: string): Promise<AssetDetail> {
    const parsed = parseAssetAddress(address)
    if (!parsed) throw new Error(`"${address}" is not an asset address (<kind>:<uuid>)`)
    return (await api.get(`${base}/assets/${parsed.kind}/${parsed.id}`)).data
  },
  /** The OS and firmware tree; `kind` limits it to servers or to network devices. */
  async os(kind?: AssetKind): Promise<OsResponse> {
    return (await api.get(`${base}/os`, { params: definedParams({ kind }) })).data
  },
  /** Network vendors and models, ENTITY-MIB module groups and the server hardware distributions. */
  async hardware(vendor?: string): Promise<HardwareResponse> {
    return (await api.get(`${base}/hardware`, { params: definedParams({ vendor }) })).data
  },
  async changes(query: ChangesQuery = {}): Promise<ChangesResponse> {
    return withTimeZoneFallback(async (params) => (await api.get(`${base}/changes`, { params })).data, definedParams({ ...query }))
  },
  /** Snapshot series; a scoped caller gets `restricted: true` and no series. */
  async trends(params: { metric: string; dimension?: string; days?: number }): Promise<TrendsResponse> {
    return (await api.get(`${base}/trends`, { params: definedParams({ ...params }) })).data
  },
  /**
   * CSV of the assets the filters select (the whole result, not the page). The endpoint needs
   * `inventory.export`, is audit-logged on the server and answers with an attachment. The request is
   * an ordinary authenticated one (the token travels in the Authorization header), so the file is
   * received as a Blob and saved by the caller.
   */
  async exportAssets(query: AssetQuery): Promise<DownloadedFile> {
    const params = new URLSearchParams({ dataset: 'assets' })
    // Filters and ordering only: an export is not a page.
    writeAssetFilters(query, params)
    const list = normaliseListState(query)
    params.set('sort', list.sort ?? DEFAULT_ASSET_SORT.sort)
    params.set('order', list.order ?? DEFAULT_ASSET_SORT.order)
    const res = await api.get(`${base}/export`, { params, responseType: 'blob', timeout: 120_000 })
    const header = res.headers?.['content-disposition']
    const rows = Number(res.headers?.['x-export-rows'])
    return {
      blob: res.data as Blob,
      disposition: typeof header === 'string' ? header : null,
      rows: Number.isInteger(rows) && rows >= 0 ? rows : null,
      truncated: String(res.headers?.['x-export-truncated'] ?? '').toLowerCase() === 'true',
    }
  },
}
