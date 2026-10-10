// Types, query keys and fetchers of the Operating Systems, Hardware and Changes pages, in ONE module
// next to ./api.ts (which stays the shared fetch helper and is not edited here). The response types
// themselves (OsResponse, HardwareResponse, ChangesResponse, ...) are ./types.ts; this file only holds
// what those pages add: the Changes page's filter shape, the daily-window helpers' result types and the
// CSV exports of the three datasets.
//
// The URL of /inventory/changes carries its filters under the API's own parameter names
// (05-P1-API-CONTRACT.md section 3.7, scope decision S6), so ChangesFilters IS the API's filter set.

import { api } from '@/lib/api'
import { changesQuery } from '@/components/inventory/changesModel'
import { inventoryApi, type DownloadedFile } from './api'
import type { AssetKind } from './filters'
import type { ChangeAction, ChangeCategory, ChangesResponse, HardwareResponse, OsResponse } from './types'

export type { ChangeAction, ChangeCategory, ChangesResponse, HardwareResponse, OsResponse } from './types'
export type { DownloadedFile } from './api'

/* ── Types ───────────────────────────────────────────────────────────────── */

/** The filters of /inventory/changes, in the API's vocabulary. `since` and `until` are ISO instants (`2026-10-01T12:00:00Z`). */
export interface ChangesFilters {
  /** Contains, over asset name, subject, product key, old and new value. */
  q?: string
  kind?: AssetKind
  category?: ChangeCategory
  action?: ChangeAction
  /** Software version changes that are upgrades or downgrades by comparator. */
  label?: 'upgrade' | 'downgrade'
  /** `<kind>:<uuid>`: one asset (for a server, also its SNMP twin). */
  asset?: string
  /** A software product key. */
  subject_key?: string
  /** Inclusive. Without it the API uses the last seven days. */
  since?: string
  /** Exclusive. */
  until?: string
}

/** The four windows of the stats strip. */
export type ChangesWindowKey = '24h' | '7d' | '30d' | '90d'

export type InventoryExportDataset = 'changes' | 'os' | 'hardware'

/* ── Query keys (doc 03 section 8) ───────────────────────────────────────── */

export const OSHW_KEYS = {
  os: (kind: 'all' | AssetKind) => ['inventory', 'os', kind] as const,
  hardware: ['inventory', 'hardware'] as const,
  hardwareVendor: (vendor: string) => ['inventory', 'hardware', vendor] as const,
  changes: (query: string) => ['inventory', 'changes', query] as const,
} as const

/* ── Fetchers ────────────────────────────────────────────────────────────── */

export const fetchOs = (kind?: AssetKind): Promise<OsResponse> => inventoryApi.os(kind)
export const fetchHardware = (vendor?: string): Promise<HardwareResponse> => inventoryApi.hardware(vendor)

/** One page of events with the window's stats and daily series; `tz` names the zone of the series' days. */
export function fetchChanges(filters: ChangesFilters, page: number, pageSize: number, tz?: string): Promise<ChangesResponse> {
  return inventoryApi.changes(changesQuery(filters, page, pageSize, tz))
}

/**
 * CSV of a dataset as the server's own list returns it (the whole result, not the page; the endpoint needs
 * `inventory.export`, is audit-logged and answers with an attachment). The token travels in the
 * Authorization header, so the file is received as a Blob and saved by the caller.
 */
export async function exportDataset(dataset: InventoryExportDataset, params: URLSearchParams): Promise<DownloadedFile> {
  const query = new URLSearchParams({ dataset })
  for (const [k, v] of params) query.append(k, v)
  const res = await api.get('/inventory/export', { params: query, responseType: 'blob', timeout: 120_000 })
  const header = res.headers?.['content-disposition']
  const rows = Number(res.headers?.['x-export-rows'])
  return {
    blob: res.data as Blob,
    disposition: typeof header === 'string' ? header : null,
    rows: Number.isInteger(rows) && rows >= 0 ? rows : null,
    truncated: String(res.headers?.['x-export-truncated'] ?? '').toLowerCase() === 'true',
  }
}

/** The change feed's file: the same filters as the list, no `tz` (a file has no daily series), no paging. */
export function exportChanges(filters: ChangesFilters): Promise<DownloadedFile> {
  const q = changesQuery(filters, 1, 1)
  const params = new URLSearchParams()
  for (const key of ['category', 'action', 'kind', 'asset', 'subject_key', 'q', 'label', 'since', 'until'] as const) {
    const v = q[key]
    if (v !== undefined && v !== '') params.set(key, String(v))
  }
  return exportDataset('changes', params)
}

export const exportOs = (kind?: AssetKind): Promise<DownloadedFile> => exportDataset('os', new URLSearchParams(kind ? { kind } : {}))
export const exportHardware = (vendor?: string): Promise<DownloadedFile> => exportDataset('hardware', new URLSearchParams(vendor ? { vendor } : {}))
