// Response shapes and fetchers of the software catalog, the product page and the Inventory settings
// (Documentation/Inventory/05-P1-API-CONTRACT.md sections 3.8 to 3.11, 3.13 and 3.14). A new module of its
// own next to ./api.ts and ./types.ts: they are shared and stay untouched, this one only holds what the
// Software, Product and Settings pages add.
//
// The types follow the contract and server/app/schemas/inventory.py. Each one that has a golden fixture
// (./fixtures/software*.json, settings.json) is checked against it at compile time by
// ./softwareSettings.typecheck.ts (the strict build fails on drift) and at run time by
// scripts/test-inventory-software-settings.mjs. A null is always present, never omitted. Timestamps are UTC
// ISO-8601 strings.

import { api } from '@/lib/api'
import {
  catalogApiParams, defaultOrderFor, publisherQueryFilters, PUBLISHER_LIMIT, writeCatalogFilters,
  type CatalogFilters, type CatalogQuery, type CatalogSort,
} from '@/components/inventory/softwareModel'
import { INSTALL_DEFAULT_SORT, type InstallSort } from '@/components/inventory/productModel'
import type { DownloadedFile } from './api'
import type { SortOrder } from './filters'
import type { DataStatus, ScopeInfo } from './types'

/**
 * The value of the installations `version` filter (and of `unversioned.address`) that selects the rows with no
 * version: NULL or blank. `unknown` is NOT an alias for it any more; a version spelled "unknown" is an ordinary
 * version (backend wave 2).
 */
export const NO_VERSION = '__none__'

/* ── Catalog (GET /inventory/software) ───────────────────────────────────── */

/** `normalized`: a catalog rule placed it; `publisher_only`: only the publisher is a known alias; `unmatched`: neither. */
export type NormStatus = 'normalized' | 'publisher_only' | 'unmatched'
export type VersionScheme = 'semverish' | 'dpkg' | 'rpm'

export interface VersionBarEntry {
  version: string | null
  hosts: number
  scheme: VersionScheme
  /** 1 = newest of its scheme group; null: the scheme cannot order it. */
  rank: number | null
}

/** Up to eight versions, newest first, and the rest as one bucket (how many versions, the sum of their hosts). */
export interface VersionBar {
  top: VersionBarEntry[]
  other: { versions: number; hosts: number }
}

export interface CatalogRow {
  /** The address of the product page: /inventory/software/<product_key>. */
  product_key: string
  name: string
  publisher: string | null
  publisher_key: string | null
  /** Never null: `other` when the rows carry none. */
  category: string
  norm_status: NormStatus
  /** Distinct servers with at least one row of the product. */
  hosts: number
  /** Rows of the product (a server can hold several). */
  installs: number
  /** Distinct non-blank versions. */
  version_count: number
  latest_version: string | null
  distribution: VersionBar
  first_seen: string | null
  last_change: string | null
}

export interface CatalogResponse {
  generated_at: string
  scope: ScopeInfo
  items: CatalogRow[]
  total: number
  page: number
  page_size: number
  sort: string
  order: SortOrder
  /** Footer: the products, and their installs, in the hidden categories among those the other filters select. */
  hidden_products: number
  hidden_installs: number
  unmatched_titles: number
  /** The categories hidden by default (settings), sorted. */
  hidden_categories: string[]
  /** Whether existing software lists are still being normalised (then these figures are incomplete). Scope-aware. */
  data_status: DataStatus
}

/* ── Publishers (GET /inventory/software/publishers) ─────────────────────── */

export interface PublisherItem {
  /** Goes into the catalog's `publisher` filter (`unknown`: products without a publisher). */
  value: string
  label: string
  products: number
  installs: number
  hosts: number
}

export interface PublishersResponse {
  generated_at: string
  scope: ScopeInfo
  /** Products in this view = the catalog total for the same filters. */
  total: number
  /** Publishers, uncapped. */
  distinct: number
  items: PublisherItem[]
  data_status: DataStatus
}

/* ── Product (GET /inventory/software/{product_key}) ─────────────────────── */

export interface ProductHeader {
  product_key: string
  name: string
  publisher: string | null
  publisher_key: string | null
  category: string
  norm_status: NormStatus
  /** The pack rule that placed the product's sample title; null when none did. */
  rule_id: string | null
  hosts: number
  installs: number
  version_count: number
  latest_version: string | null
  first_seen: string | null
  last_change: string | null
  /** The versions belong to more than one scheme group (Windows and Linux packages). */
  mixed_schemes: boolean
}

export interface ProductVersion {
  version: string
  hosts: number
  installs: number
  scheme: VersionScheme
  /** 1 = newest of its scheme group; equal spellings share a rank; null: the scheme cannot parse it. */
  rank: number | null
  /** Rank 1, rank 2, rank 3 and up. */
  generation: 'N' | 'N-1' | 'older' | null
}

/** A distinct raw (package name, publisher) pair that maps to this product. */
export interface EvidenceItem {
  package_name: string
  vendor: string | null
  /** hklm, dpkg, rpm, ... */
  source: string | null
  hosts: number
  installs: number
  /** What the normaliser makes of this raw title today. */
  status: string | null
  rule_id: string | null
}

export interface ProductResponse {
  generated_at: string
  scope: ScopeInfo
  header: ProductHeader
  /**
   * ALL distinct non-blank versions, in display order. One entry per version EQUIVALENCE KEY: 7.2 and 7.2.0 are one
   * entry, spelled as the longest spelling (7.2.0), and `hosts` counts the distinct servers across the spellings.
   */
  versions: ProductVersion[]
  /**
   * Rows with no or a blank version (zero when none). `address` is the value of the installations `version`
   * filter that lists exactly these rows (always `__none__`): use it, never a literal.
   */
  unversioned: { hosts: number; installs: number; address: string }
  evidence: { items: EvidenceItem[]; total: number; truncated: boolean }
  data_status: DataStatus
}

/* ── Installations (GET /inventory/software/{product_key}/installations) ─── */

export interface InstallationRow {
  /** `server:<uuid>`: the Assets address. */
  asset_key: string
  asset_id: string
  asset_name: string
  package_name: string
  version: string | null
  architecture: string | null
  source: string | null
  install_date: string | null
  first_seen_at: string | null
  /** The last report that listed it. */
  updated_at: string | null
}

export interface InstallationsResponse {
  generated_at: string
  product_key: string
  /** The filter, echoed. */
  version: string | null
  items: InstallationRow[]
  total: number
  page: number
  page_size: number
  sort: string
  order: SortOrder
}

export interface InstallationsQuery {
  /** A version (every spelling of it: 7.2 lists 7.2.0 too), or `NO_VERSION` for the rows with none. */
  version?: string
  q?: string
  sort?: InstallSort
  order?: SortOrder
  page?: number
  page_size?: number
}

/* ── Settings (GET and PUT /inventory/settings) ──────────────────────────── */

export interface InventorySettings {
  /** 1 to 720 hours; never above stale_days x 24. */
  fresh_hours: { server: number; device: number }
  /** 1 to 365. */
  stale_days: number
  /** 30 to 3650. */
  change_retention_days: number
  /** 0 to 23, appliance time; the loop runs at hh:15. */
  snapshot_hour: number
  /** Category names; [] hides nothing. */
  hide_categories_by_default: string[]
  trends_enabled: boolean
}

export type NumericSettingKey = 'fresh_hours' | 'stale_days' | 'change_retention_days' | 'snapshot_hour'
export type SettingsLimits = Record<NumericSettingKey, { min: number; max: number }>

export interface SettingsResponse {
  /** What is in force now. */
  settings: InventorySettings
  defaults: InventorySettings
  limits: SettingsLimits
  /** The vocabulary of hide_categories_by_default. */
  categories: string[]
  /** null: never saved, the defaults apply. */
  updated_at: string | null
  /** PUT: the top-level keys whose value changed. GET: []. */
  changed: string[]
  /** PUT: requested numbers the validator changed (clamped or capped), e.g. "stale_days", "fresh_hours.server". GET: []. */
  adjusted: string[]
}

/** What PUT /inventory/settings takes: any subset, and `fresh_hours` may name one kind only. Keys left out keep their value. */
export interface SettingsPatch {
  fresh_hours?: Partial<InventorySettings['fresh_hours']>
  stale_days?: number
  change_retention_days?: number
  snapshot_hour?: number
  hide_categories_by_default?: string[]
  trends_enabled?: boolean
}

/* ── Query keys (doc 03 section 8) ───────────────────────────────────────── */

export const softwareKeys = {
  all: ['inventory', 'software'] as const,
  catalog: (params: string) => ['inventory', 'software', params] as const,
  publishers: (params: string) => ['inventory', 'software', 'publishers', params] as const,
  product: (key: string) => ['inventory', 'product', key] as const,
  installations: (key: string, params: string) => ['inventory', 'product', key, 'installed', params] as const,
  timeline: (key: string, params: string) => ['inventory', 'product', key, 'timeline', params] as const,
  settings: ['inventory', 'settings'] as const,
}

/* ── Fetchers ────────────────────────────────────────────────────────────── */

const base = '/inventory'

function installParams(query: InstallationsQuery, explicit: boolean): URLSearchParams {
  const p = new URLSearchParams()
  if (query.version) p.set('version', query.version)
  if (query.q) p.set('q', query.q)
  const sort = query.sort ?? INSTALL_DEFAULT_SORT.sort
  const order = query.order ?? INSTALL_DEFAULT_SORT.order
  if (explicit || sort !== INSTALL_DEFAULT_SORT.sort || order !== INSTALL_DEFAULT_SORT.order) {
    p.set('sort', sort)
    p.set('order', order)
  }
  if (explicit || (query.page ?? 1) > 1) p.set('page', String(query.page ?? 1))
  if (explicit || (query.page_size ?? 50) !== 50) p.set('page_size', String(query.page_size ?? 50))
  return p
}

async function download(params: URLSearchParams): Promise<DownloadedFile> {
  // An ordinary authenticated request (the token travels in the Authorization header), so the file arrives as a Blob.
  const res = await api.get(`${base}/export`, { params, responseType: 'blob', timeout: 120_000 })
  const header = res.headers?.['content-disposition']
  const rows = Number(res.headers?.['x-export-rows'])
  return {
    blob: res.data as Blob,
    disposition: typeof header === 'string' ? header : null,
    rows: Number.isInteger(rows) && rows >= 0 ? rows : null,
    truncated: String(res.headers?.['x-export-truncated'] ?? '').toLowerCase() === 'true',
  }
}

export const softwareApi = {
  /** The catalog, one row per product. The query string is the page's own address (same names). */
  async catalog(query: CatalogQuery): Promise<CatalogResponse> {
    const sort: CatalogSort = query.sort ?? 'hosts'
    const params = catalogApiParams({
      filters: query,
      sort,
      order: query.order ?? defaultOrderFor(sort),
      page: query.page ?? 1,
      pageSize: query.page_size ?? 50,
    })
    return (await api.get(`${base}/software`, { params })).data
  },
  /** The publisher facet: every catalog filter except `publisher`, so a count is what choosing the value would give. */
  async publishers(filters: CatalogFilters, limit = PUBLISHER_LIMIT): Promise<PublishersResponse> {
    const params = writeCatalogFilters(publisherQueryFilters(filters), new URLSearchParams())
    params.set('limit', String(limit))
    return (await api.get(`${base}/software/publishers`, { params })).data
  },
  /** A 404 means an unknown product or one with no host in the caller's scope: one body for both. */
  async product(key: string): Promise<ProductResponse> {
    return (await api.get(`${base}/software/${encodeURIComponent(key)}`)).data
  },
  async installations(key: string, query: InstallationsQuery = {}): Promise<InstallationsResponse> {
    return (await api.get(`${base}/software/${encodeURIComponent(key)}/installations`, { params: installParams(query, true) })).data
  },
  async settings(): Promise<SettingsResponse> {
    return (await api.get(`${base}/settings`)).data
  },
  /** Merges: keys left out keep their value. A 422 carries the reason as text in `detail`. */
  async saveSettings(patch: SettingsPatch): Promise<SettingsResponse> {
    return (await api.put(`${base}/settings`, patch)).data
  },
  /** The catalog as CSV: the whole result of the current filters in the current order, not the page. Needs inventory.export. */
  async exportCatalog(query: CatalogQuery): Promise<DownloadedFile> {
    const params = new URLSearchParams({ dataset: 'software' })
    writeCatalogFilters(query, params)
    const sort = query.sort ?? 'hosts'
    params.set('sort', sort)
    params.set('order', query.order ?? defaultOrderFor(sort))
    return download(params)
  },
  /** The installation list of a product as CSV (its version and search filters, its order). */
  async exportInstallations(key: string, query: InstallationsQuery = {}): Promise<DownloadedFile> {
    const params = new URLSearchParams({ dataset: 'installations', product: key })
    for (const [k, v] of installParams(query, true)) if (k === 'version' || k === 'q' || k === 'sort' || k === 'order') params.set(k, v)
    return download(params)
  },
}
