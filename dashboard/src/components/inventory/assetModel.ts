// Pure logic behind the Assets page and the asset flyout (Documentation/Inventory/03-UI-UX-SPEC.md
// section 3.2): the columns and the per-user column choice, how one row's OS reads, the facet groups,
// paging past the end, the roving row focus and which flyout links a user may follow. No React, no
// router, no API: scripts/test-inventory-ui.mjs exercises it directly.

import { cleanTags } from '@/components/access/roleModel'
import {
  FRESHNESS_LABELS, HEALTH_LABELS, KIND_LABELS, UNKNOWN_FILTER, assetAddress,
  type AssetFilterKey, type AssetKind, type AssetSortKey, type FreshnessFilter, type HealthPredicate,
} from '@/pages/inventory/filters'
import type { AssetDetail, AssetRow, FacetsResponse, IdentityValue } from '@/pages/inventory/types'
import type { FacetGroupDef } from './FacetPanel'
import { changesPageHref } from './changesModel'
import { facetItemsFromApi, fmtInt, sourceLabel } from './model'

/* ── Columns ─────────────────────────────────────────────────────────────── */

export type ColumnId =
  | 'name' | 'kind' | 'ip' | 'vendor' | 'model' | 'os' | 'serial' | 'site' | 'sources' | 'inventory'
  | 'type' | 'status' | 'tags' | 'last_seen'

export interface ColumnDef {
  id: ColumnId
  label: string
  /** The API sort key, or null for a column the API cannot order by. */
  sort: AssetSortKey | null
  /** Shown until the user chooses otherwise. */
  defaultOn: boolean
  /** The name column is the way into the flyout: it cannot be hidden. */
  locked?: boolean
}

/**
 * Doc 03 section 3.2 order. Name (whose icon says server or network device), IP, vendor, model, OS and last
 * inventory are the default view: they fit beside the facet pane at 1440 px. Kind, serial, site, sources, type,
 * status, tags and last seen are one click away in the column chooser.
 */
export const ASSET_COLUMNS: readonly ColumnDef[] = [
  { id: 'name', label: 'Name', sort: 'name', defaultOn: true, locked: true },
  { id: 'kind', label: 'Kind', sort: 'kind', defaultOn: false },
  { id: 'ip', label: 'IP address', sort: 'ip', defaultOn: true },
  { id: 'vendor', label: 'Vendor', sort: 'vendor', defaultOn: true },
  { id: 'model', label: 'Model', sort: 'model', defaultOn: true },
  { id: 'os', label: 'OS / Firmware', sort: 'os', defaultOn: true },
  { id: 'serial', label: 'Serial', sort: 'serial', defaultOn: false },
  { id: 'site', label: 'Site / Location', sort: 'site', defaultOn: false },
  { id: 'sources', label: 'Sources', sort: null, defaultOn: false },
  { id: 'inventory', label: 'Last inventory', sort: 'inventory_at', defaultOn: true },
  { id: 'type', label: 'Type', sort: 'asset_type', defaultOn: false },
  { id: 'status', label: 'Status', sort: 'status', defaultOn: false },
  { id: 'tags', label: 'Tags', sort: null, defaultOn: false },
  { id: 'last_seen', label: 'Last seen', sort: 'last_seen', defaultOn: false },
]

export const DEFAULT_COLUMNS: readonly ColumnId[] = ASSET_COLUMNS.filter((c) => c.defaultOn).map((c) => c.id)

export type Density = 'comfortable' | 'compact'

export interface AssetViewPrefs {
  columns: ColumnId[]
  density: Density
  /** The facet pane on wide screens; on a narrow screen it is a drawer regardless. */
  facets: boolean
}

export const DEFAULT_PREFS: AssetViewPrefs = { columns: [...DEFAULT_COLUMNS], density: 'comfortable', facets: true }

const KNOWN_COLUMNS = new Set<string>(ASSET_COLUMNS.map((c) => c.id))

/**
 * Reads what was stored for the user, trusting none of it: unknown column ids are dropped, the locked
 * column is always there, and the columns come back in the table's own order (not the order they were
 * ticked in). Anything unreadable gives the defaults.
 */
export function parsePrefs(raw: unknown): AssetViewPrefs {
  let value: unknown = raw
  if (typeof raw === 'string') {
    try { value = JSON.parse(raw) } catch { return { ...DEFAULT_PREFS, columns: [...DEFAULT_COLUMNS] } }
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return { ...DEFAULT_PREFS, columns: [...DEFAULT_COLUMNS] }
  const v = value as Record<string, unknown>
  const picked = Array.isArray(v.columns) ? new Set(v.columns.filter((c): c is string => typeof c === 'string' && KNOWN_COLUMNS.has(c))) : null
  const columns = picked
    ? ASSET_COLUMNS.filter((c) => c.locked || picked.has(c.id)).map((c) => c.id)
    : [...DEFAULT_COLUMNS]
  return {
    columns,
    density: v.density === 'compact' ? 'compact' : 'comfortable',
    facets: v.facets === false ? false : true,
  }
}

export function serializePrefs(p: AssetViewPrefs): string {
  return JSON.stringify({ columns: parsePrefs(p).columns, density: p.density, facets: p.facets })
}

/** Storage key per user, so two people on one browser keep their own layout (house rule for layout preferences). */
export function prefsKey(userId: string | null | undefined): string {
  return `zp-inventory-assets-view:${userId ?? 'anonymous'}`
}

export function toggleColumn(prefs: AssetViewPrefs, id: ColumnId): AssetViewPrefs {
  const def = ASSET_COLUMNS.find((c) => c.id === id)
  if (!def || def.locked) return prefs
  const on = prefs.columns.includes(id)
  return parsePrefs({ ...prefs, columns: on ? prefs.columns.filter((c) => c !== id) : [...prefs.columns, id] })
}

export function visibleColumns(prefs: AssetViewPrefs): ColumnDef[] {
  const on = new Set(prefs.columns)
  return ASSET_COLUMNS.filter((c) => c.locked || on.has(c.id))
}

/* ── One row ─────────────────────────────────────────────────────────────── */

export interface OsCell {
  /** "Windows Server 2019", "FortiOS", or "Unknown". */
  label: string
  /** The version, set in mono beside the label ("10.0.17763.5696", "7.4.4"). */
  version: string | null
  unknown: boolean
  /** The whole thing for a tooltip. */
  title: string
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/**
 * How an asset's operating system or firmware reads in one cell. A server shows its release with the
 * product ("Windows Server 2019", "Ubuntu 22.04"); a network device its product ("FortiOS") and the
 * firmware version beside it. An unidentified OS is the word Unknown, never a blank.
 */
export function osCell(row: Pick<AssetRow, 'kind' | 'os_family' | 'os_product' | 'os_release' | 'os_version' | 'os_edition'>): OsCell {
  const known = row.os_family !== 'unknown' || row.os_product !== null
  if (!known) return { label: 'Unknown', version: null, unknown: true, title: 'Operating system or firmware not identified' }
  const product = row.os_product ?? cap(row.os_family)
  const label = row.kind === 'server' && row.os_release ? `${product} ${row.os_release}` : product
  const version = row.os_version ?? (row.kind === 'device' ? row.os_release : null)
  const edition = row.os_edition ? ` ${row.os_edition}` : ''
  return { label, version, unknown: false, title: `${label}${edition}${version ? ` (${version})` : ''}` }
}

export function kindLabel(kind: AssetKind): string {
  return KIND_LABELS[kind]
}

/** Row name for a screen reader: "srv-w19-03, server, 10.10.0.30". */
export function rowSummary(row: Pick<AssetRow, 'name' | 'kind' | 'ip'>): string {
  return [row.name, row.kind === 'server' ? 'server' : 'network device', row.ip].filter(Boolean).join(', ')
}

/** The status word the existing pages use, shown with a dot, never as a colour alone. */
export function statusTone(status: string | null): 'ok' | 'bad' | 'warn' | 'none' {
  const s = (status ?? '').toLowerCase()
  if (['up', 'healthy', 'online', 'ok'].includes(s)) return 'ok'
  if (['down', 'offline', 'critical', 'unreachable'].includes(s)) return 'bad'
  if (['warning', 'degraded', 'stale', 'unknown'].includes(s)) return 'warn'
  return 'none'
}

/* ── Facet groups ────────────────────────────────────────────────────────── */

const FACET_ORDER: { key: AssetFilterKey & keyof FacetsResponse['facets']; label: string; multi?: boolean; defaultOpen?: boolean; limit?: number; unknown?: FacetGroupDef['unknownPatch'] }[] = [
  { key: 'kind', label: 'Kind' },
  { key: 'os_family', label: 'OS family', unknown: UNKNOWN_FILTER.os_family },
  { key: 'os_product', label: 'OS / firmware', unknown: UNKNOWN_FILTER.os_product, defaultOpen: false },
  { key: 'vendor', label: 'Vendor', unknown: UNKNOWN_FILTER.vendor },
  { key: 'model', label: 'Model', unknown: UNKNOWN_FILTER.model, defaultOpen: false },
  { key: 'site', label: 'Site / Location', defaultOpen: false },
  { key: 'tag', label: 'Tags', multi: true, defaultOpen: false },
  { key: 'source', label: 'Source' },
  { key: 'freshness', label: 'Freshness' },
  { key: 'health', label: 'Inventory health' },
]

/** The words for the values of the facets whose API label is the raw key. */
function facetLabel(group: string, value: string, apiLabel: string | undefined): string | undefined {
  if (group === 'kind') return KIND_LABELS[value as AssetKind] ?? apiLabel
  if (group === 'freshness') return FRESHNESS_LABELS[value as FreshnessFilter] ?? apiLabel
  if (group === 'health') return HEALTH_LABELS[value as HealthPredicate] ?? apiLabel
  if (group === 'source') return sourceLabel(value)
  return apiLabel
}

/**
 * The facet panel's groups from `GET /inventory/assets/facets`. Counts come from the server and
 * already reflect every other active filter, so the number beside a value is exactly the total the
 * list shows after choosing it. `kind`, `freshness` and `health` keep the API's fixed order (zero
 * counts included) rather than being re-sorted by count.
 */
export function facetGroups(resp: FacetsResponse | undefined): FacetGroupDef[] {
  const fixed = new Set(['kind', 'freshness', 'health'])
  return FACET_ORDER.map((def) => {
    const group = resp?.facets[def.key]
    const items = facetItemsFromApi(group?.items).map((i) => ({ ...i, label: facetLabel(def.key, i.value, i.label) }))
    // Unknown is a value and sorts last: in a short list show everything so it is never behind "Show more".
    const showAll = items.some((i) => i.unknown) && items.length <= 25
    return {
      key: def.key,
      label: def.label,
      items,
      multi: def.multi,
      unknownPatch: def.unknown,
      defaultOpen: def.defaultOpen,
      limit: showAll ? items.length : def.limit,
      keepOrder: fixed.has(def.key) || undefined,
      more: group ? Math.max(0, group.distinct - group.items.length) : 0,
    }
  })
}

/* ── Paging past the end ─────────────────────────────────────────────────── */

export interface PageState {
  pageCount: number
  /** The URL names a page after the last one: the list is empty but the total is not. */
  beyond: boolean
  lastPage: number
}

/** A page number beyond the last page is not clamped by the URL or the API (200 with no items): the page must say so. */
export function pageState(page: number, total: number, pageSize: number): PageState {
  const size = Math.max(1, pageSize)
  const pageCount = Math.max(1, Math.ceil(total / size))
  return { pageCount, beyond: total > 0 && page > pageCount, lastPage: pageCount }
}

/** "1–50 of 145" wording for a screen reader live region. */
export function rangeText(page: number, pageSize: number, total: number): string {
  if (total === 0) return '0 assets'
  const first = (page - 1) * pageSize + 1
  const last = Math.min(total, page * pageSize)
  return `${fmtInt(first)} to ${fmtInt(last)} of ${fmtInt(total)} assets`
}

/* ── Roving focus over rows ──────────────────────────────────────────────── */

/** Which row takes focus after a key, or null when the key is not one of the row keys. */
export function nextRowIndex(current: number, key: string, count: number): number | null {
  if (count <= 0) return null
  const last = count - 1
  switch (key) {
    case 'ArrowDown': return Math.min(last, current + 1)
    case 'ArrowUp': return Math.max(0, current - 1)
    case 'Home': return 0
    case 'End': return last
    case 'PageDown': return Math.min(last, current + 10)
    case 'PageUp': return Math.max(0, current - 10)
    default: return null
  }
}

/* ── Flyout ──────────────────────────────────────────────────────────────── */

export interface FlyoutLink {
  id: 'server' | 'device'
  label: string
  /** Set when the user holds the permission; otherwise the entry is plain text with `reason`. */
  to: string | null
  /** Why there is no link ("Needs the servers.view permission"). */
  reason: string | null
}

type Can = (permission: string) => boolean

/**
 * Links from the flyout to the existing detail pages. Each is a link only when the user may open the
 * page it points to (`servers.view` for /servers/:id, `devices.view` for /devices/:id); without the
 * permission it stays as text with the reason, so nobody follows a link into a "no access" screen.
 * A server that also has an SNMP device row gets both.
 */
export function flyoutLinks(detail: Pick<AssetDetail, 'asset' | 'links'>, can: Can): FlyoutLink[] {
  const out: FlyoutLink[] = []
  const { server_id: serverId, device_id: deviceId } = detail.links
  if (serverId) {
    const ok = can('servers.view')
    out.push({ id: 'server', label: 'Open server page', to: ok ? `/servers/${serverId}` : null, reason: ok ? null : 'Needs the servers.view permission' })
  }
  if (deviceId) {
    const ok = can('devices.view')
    const twin = detail.asset.kind === 'server'
    out.push({
      id: 'device',
      label: twin ? 'Open SNMP device page' : 'Open device page',
      to: ok ? `/devices/${deviceId}` : null,
      reason: ok ? null : 'Needs the devices.view permission',
    })
  }
  return out
}

/**
 * "View all changes" for one asset. The flyout counts ALL of the asset's events but the Changes page defaults to the
 * last seven days, so the link carries `since` = the asset's oldest event (`changes.first_change_at`) and the page lands
 * on exactly the total the flyout showed (review finding FE-05). Without an event there is nothing to carry.
 */
export function assetChangesHref(address: string, firstChangeAt?: string | null): string {
  return changesPageHref({ asset: address, since: firstChangeAt ?? undefined })
}

/** The server detail page's software list, shown as a link only with servers.view. */
export function serverSoftwareHref(serverId: string): string {
  return `/servers/${serverId}?tab=system&sub=software`
}

export function addressOf(row: Pick<AssetRow, 'kind' | 'id'>): string {
  return assetAddress(row.kind, row.id)
}

/* ── Flyout values ───────────────────────────────────────────────────────── */

/** Fields whose value is an identifier or a version: set in mono. */
const MONO_FIELDS = new Set(['serial', 'os_version', 'os_build'])
export const isMonoField = (field: string) => MONO_FIELDS.has(field)

/** The API names the ENTITY-MIB badge `entity-mib`; the badge component knows it as `entity` (the stored source). */
export function badgeSource(source: string | null): string | null {
  return source === 'entity-mib' ? 'entity' : source
}

/** The value of an identity field as text; the OS family's raw `unknown` never leaks as a string. */
export function identityText(v: Pick<IdentityValue, 'value'>): string | null {
  const t = (v.value ?? '').trim()
  return t === '' ? null : t
}

const GIB = 1024 ** 3

/** "8 GB", "128 GB", "1.5 TB": binary units, the way the memory buckets are cut. */
export function memoryText(bytes: number | null | undefined): string | null {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes <= 0) return null
  const gb = bytes / GIB
  if (gb >= 1024) return `${(gb / 1024).toFixed(gb % 1024 === 0 ? 0 : 1)} TB`
  return `${gb >= 10 || Number.isInteger(gb) ? Math.round(gb) : gb.toFixed(1)} GB`
}

/** The agent's disk list as "2 disks: 447 GB, 894 GB"; the shape of each entry is the agent's, so every field is optional. */
export function disksText(disks: readonly Record<string, unknown>[] | null | undefined): string | null {
  if (!disks || disks.length === 0) return null
  const sizes = disks
    .map((d) => (typeof d.size_bytes === 'number' ? memoryText(d.size_bytes) : null))
    .filter((s): s is string => s !== null)
  const count = `${disks.length} ${disks.length === 1 ? 'disk' : 'disks'}`
  return sizes.length > 0 ? `${count}: ${sizes.join(', ')}` : count
}

export function cpuText(model: string | null, cores: number | null, physical: number | null): string | null {
  const parts: string[] = []
  if (model) parts.push(model)
  if (physical && cores && physical !== cores) parts.push(`${physical} cores, ${cores} threads`)
  else if (cores) parts.push(`${cores} ${cores === 1 ? 'core' : 'cores'}`)
  return parts.length > 0 ? parts.join(' · ') : null
}

/** The note under the software list when the agent's report hit its cap. */
export const TRUNCATED_NOTE =
  "The agent's software list was cut at its cap on the last report, so titles beyond the cap are not counted anywhere in Inventory."

/** Endpoint protection (the filter and the API keep the pinned name `security_agent`; people read "endpoint protection"). */
export const SECURITY_AGENT_TEXT = {
  present: 'Endpoint protection present',
  missing: 'No endpoint protection found',
  unknown: 'Endpoint protection unknown',
} as const

/** The reasons the API names for an unknown answer (a reason it adds later gets {@link SECURITY_AGENT_REASON_FALLBACK}). */
export type SecurityAgentReason = 'no_software_list' | 'list_truncated' | 'normalising' | 'no_service_list'

/**
 * Why the answer is unknown, in words. None of them promises that the server will report: a server whose agent
 * does not send software lists (Linux today) stays unknown, so the sentences name what is missing.
 */
export const SECURITY_AGENT_REASON: Record<SecurityAgentReason, string> = {
  no_software_list: 'No software list has been reported for this server',
  list_truncated: 'The software list was cut off, so protection cannot be judged',
  normalising: 'The software list is still being processed',
  no_service_list: 'Built-in Windows Defender can only be seen through the service list, and this server has not reported one',
}

/** For a reason this build does not know (the API is newer than the page). */
export const SECURITY_AGENT_REASON_FALLBACK = 'The software and service lists do not allow a verdict for this server'

/** The sentence for a reason string, defensively: an unknown reason gets the generic one, no reason gets none. */
export function securityAgentReasonText(reason: string | null | undefined): string | null {
  if (!reason) return null
  return Object.prototype.hasOwnProperty.call(SECURITY_AGENT_REASON, reason) ? SECURITY_AGENT_REASON[reason as SecurityAgentReason] : SECURITY_AGENT_REASON_FALLBACK
}

export function securityAgentState(sa: { present: boolean | null; state?: 'present' | 'missing' | 'unknown' }): keyof typeof SECURITY_AGENT_TEXT {
  if (sa.state) return sa.state
  return sa.present === null ? 'unknown' : sa.present ? 'present' : 'missing'
}

/** What the pill says and why: "Endpoint protection present" with the products and services that count, or the reason it is unknown. */
export function securityAgentSummary(sa: {
  present: boolean | null
  state?: 'present' | 'missing' | 'unknown'
  reason?: SecurityAgentReason | (string & {}) | null
  products: readonly string[]
  services?: readonly string[]
}): { state: keyof typeof SECURITY_AGENT_TEXT; text: string; detail: string | null } {
  const state = securityAgentState(sa)
  if (state === 'present') {
    const parts = [...sa.products, ...(sa.services ?? []).map((s) => `${s} service`)]
    return { state, text: SECURITY_AGENT_TEXT.present, detail: parts.length > 0 ? parts.join(', ') : null }
  }
  if (state === 'missing') return { state, text: SECURITY_AGENT_TEXT.missing, detail: null }
  return { state, text: SECURITY_AGENT_TEXT.unknown, detail: securityAgentReasonText(sa.reason) }
}

/** Tag chips from the asset, tidied the way the scope code treats tags. */
export function assetTags(tags: readonly string[]): string[] {
  return cleanTags([...tags])
}

/** Toast text after an export: the server's own row count when it sent one, the list total otherwise; a capped file says so. */
export function exportStartedText(total: number, file?: { rows?: number | null; truncated?: boolean }): { title: string; description: string; truncated: boolean } {
  const rows = file?.rows ?? total
  const noun = rows === 1 ? 'row' : 'rows'
  if (file?.truncated) {
    return { title: 'Export started, cut at the row limit', description: `Only the first ${fmtInt(rows)} of ${fmtInt(total)} rows are in the file. Narrow the filters to export the rest.`, truncated: true }
  }
  return { title: 'Export started', description: `${fmtInt(rows)} ${noun} as CSV`, truncated: false }
}
