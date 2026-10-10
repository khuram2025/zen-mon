// The Inventory asset filter vocabulary: the ONE place that names the filter keys, the allowed
// values and how they read from / write to a query string.
//
// Doc 04 scope decision S6: the URL of every list page carries its filters as query parameters,
// and those parameters are IDENTICAL to the API query parameters of the matching endpoint. So the
// same keys serve GET /inventory/assets, GET /inventory/assets/facets, GET /inventory/export
// (dataset=assets) and the address bar of /inventory/assets. Adding a key means adding it here
// first; DrillLink, FacetPanel, useInventoryUrlState and the pages all follow this file.
//
// Pure TypeScript (no React, no router): scripts/test-inventory-ui.mjs exercises it directly.

/* ── Vocabulary ──────────────────────────────────────────────────────────── */

/** Filter keys, in the order they are written to a URL. */
export const ASSET_FILTER_KEYS = [
  'q',
  'kind',
  'os_family',
  'os_product',
  'os_release',
  'os_version',
  'vendor',
  'model',
  'site',
  'tag',
  'source',
  'freshness',
  'health',
  'product',
  'product_version',
  // Server hardware and endpoint-protection drill-downs (the Hardware page and the Overview's coverage line).
  // Servers only: a network device never matches them.
  'cpu_model',
  'memory',
  'disks',
  'virtualization',
  'security_agent',
  // ENTITY-MIB component drill-downs of the Hardware page. Network devices only: a server never matches them.
  'module_class',
  'module_model',
] as const
export type AssetFilterKey = (typeof ASSET_FILTER_KEYS)[number]

export const ASSET_KINDS = ['server', 'device'] as const
export type AssetKind = (typeof ASSET_KINDS)[number]

/**
 * `stale_or_never` is the union the Inventory health worklist uses: "not reporting". `not_applicable` is an
 * asset that cannot report an inventory by design (a ping-only or controller-managed device, a server whose
 * collection mode cannot upload one): it is listed and counted but never "never" and never in `stale_or_never`.
 */
export const FRESHNESS_VALUES = ['fresh', 'aging', 'stale', 'never', 'not_applicable', 'stale_or_never'] as const
export type FreshnessFilter = (typeof FRESHNESS_VALUES)[number]
/** The five states one asset can be in (`stale_or_never` only exists as a filter). */
export type Freshness = Exclude<FreshnessFilter, 'stale_or_never'>

/**
 * Inventory-health predicates. Each key of `summary.health` maps one to one to a value here, so
 * every health count is a worklist (doc 02 section 8). Scope decision S3: the three hardware
 * identity predicates only ever match network devices in Phase 1; `missing_security_agent` and
 * `software_truncated` only match servers.
 */
export const HEALTH_PREDICATES = [
  'missing_os_version',
  'missing_model',
  'missing_serial',
  'unknown_vendor',
  'stale',
  'never',
  'software_truncated',
  'missing_security_agent',
] as const
export type HealthPredicate = (typeof HEALTH_PREDICATES)[number]

/** Physical memory buckets of a server (`memory`); `unknown` = no memory figure reported. */
export const MEMORY_BUCKETS = ['le8', 'le16', 'le32', 'le64', 'le128', 'gt128', 'unknown'] as const
export type MemoryBucket = (typeof MEMORY_BUCKETS)[number]

/** Physical disk count buckets of a server (`disks`); `unknown` = no disk list reported. */
export const DISK_BUCKETS = ['1', '2', '3-4', '5-8', 'ge9', 'unknown'] as const
export type DiskBucket = (typeof DISK_BUCKETS)[number]

/**
 * Endpoint protection (the filter keeps its pinned name `security_agent`). `unknown`: no complete software list
 * (not reported, cut off, still being processed) or, for built-in Defender, no service list, so nothing can be said.
 */
export const SECURITY_AGENT_VALUES = ['present', 'missing', 'unknown'] as const
export type SecurityAgentFilter = (typeof SECURITY_AGENT_VALUES)[number]

/**
 * ENTITY-MIB component classes (`module_class`), spelled as the API spells them: `powerSupply` keeps its
 * capital S, which is why a read maps the value back to this canonical spelling.
 */
export const MODULE_CLASSES = ['chassis', 'backplane', 'module', 'powerSupply', 'fan', 'cpu', 'stack'] as const
export type ModuleClass = (typeof MODULE_CLASSES)[number]

export const SORT_ORDERS = ['asc', 'desc'] as const
export type SortOrder = (typeof SORT_ORDERS)[number]

/** Sortable asset columns: the keys the API accepts for `sort` (inventory_query.SORT_KEYS). */
export const ASSET_SORT_KEYS = [
  'name',
  'kind',
  'ip',
  'asset_type',
  'status',
  'vendor',
  'model',
  'serial',
  'os_family',
  'os_product',
  'os_release',
  'os_version',
  'os',
  'site',
  'last_seen',
  'inventory_at',
  'freshness',
] as const
export type AssetSortKey = (typeof ASSET_SORT_KEYS)[number]

export const DEFAULT_ASSET_SORT: { sort: AssetSortKey; order: SortOrder } = { sort: 'name', order: 'asc' }
export const DEFAULT_PAGE_SIZE = 50
export const MAX_PAGE_SIZE = 500
export const MAX_PAGE = 100_000

export interface AssetFilters {
  /** Free text over name, ip, serial, model, vendor, os product and version. */
  q?: string
  kind?: AssetKind
  os_family?: string
  os_product?: string
  os_release?: string
  os_version?: string
  vendor?: string
  model?: string
  site?: string
  /** Repeatable. Every tag must be carried by the asset. Kept sorted: tags are a set. */
  tag?: string[]
  source?: string
  freshness?: FreshnessFilter
  health?: HealthPredicate
  /** A software product_key: assets that have it installed. */
  product?: string
  /** Only together with `product`. */
  product_version?: string
  /** Servers by CPU model; `unknown` for none reported. */
  cpu_model?: string
  memory?: MemoryBucket
  disks?: DiskBucket
  /** Servers by virtualisation (physical, vmware, ...); `unknown` for none reported. */
  virtualization?: string
  security_agent?: SecurityAgentFilter
  /** Network devices with an ENTITY-MIB component of this class. */
  module_class?: ModuleClass
  /** Network devices with a component of this exact model; `unknown` for a component with no model. */
  module_model?: string
}

/** Paging and ordering shared by every list endpoint. */
export interface ListState {
  sort?: AssetSortKey
  order?: SortOrder
  page?: number
  page_size?: number
}

/** What GET /inventory/assets (and the export with dataset=assets) accepts. */
export type AssetQuery = AssetFilters & ListState

/** Why a value in the URL was not used. */
export interface RejectedFilter {
  key: string
  value: string
  reason: string
}

/* ── Labels ──────────────────────────────────────────────────────────────── */

export const FILTER_LABELS: Record<AssetFilterKey, string> = {
  q: 'Search',
  kind: 'Kind',
  os_family: 'OS family',
  os_product: 'OS product',
  os_release: 'OS release',
  os_version: 'OS version',
  vendor: 'Vendor',
  model: 'Model',
  site: 'Site',
  tag: 'Tag',
  source: 'Source',
  freshness: 'Freshness',
  health: 'Health',
  product: 'Software',
  product_version: 'Software version',
  cpu_model: 'CPU model',
  memory: 'Memory',
  disks: 'Physical disks',
  virtualization: 'Virtualization',
  security_agent: 'Endpoint protection',
  module_class: 'Component class',
  module_model: 'Component model',
}

export const KIND_LABELS: Record<AssetKind, string> = { server: 'Server', device: 'Network device' }

export const FRESHNESS_LABELS: Record<FreshnessFilter, string> = {
  fresh: 'Fresh',
  aging: 'Aging',
  stale: 'Stale',
  never: 'Never reported',
  not_applicable: 'Not reported by design',
  stale_or_never: 'Stale or never reported',
}

export const HEALTH_LABELS: Record<HealthPredicate, string> = {
  missing_os_version: 'Missing OS version',
  missing_model: 'Missing model',
  missing_serial: 'Missing serial',
  unknown_vendor: 'Unknown vendor',
  stale: 'Stale',
  never: 'Never reported',
  software_truncated: 'Software list truncated',
  missing_security_agent: 'No endpoint protection',
}

export const MEMORY_LABELS: Record<MemoryBucket, string> = {
  le8: 'Up to 8 GB',
  le16: '9 to 16 GB',
  le32: '17 to 32 GB',
  le64: '33 to 64 GB',
  le128: '65 to 128 GB',
  gt128: 'Over 128 GB',
  unknown: 'Unknown',
}

export const DISK_LABELS: Record<DiskBucket, string> = {
  '1': '1 disk',
  '2': '2 disks',
  '3-4': '3 to 4 disks',
  '5-8': '5 to 8 disks',
  ge9: '9 or more disks',
  unknown: 'Unknown',
}

export const SECURITY_AGENT_LABELS: Record<SecurityAgentFilter, string> = {
  present: 'Present',
  missing: 'Missing',
  unknown: 'Unknown (no complete software or service list)',
}

export const MODULE_CLASS_LABELS: Record<ModuleClass, string> = {
  chassis: 'Chassis',
  backplane: 'Backplane',
  module: 'Module',
  powerSupply: 'Power supply',
  fan: 'Fan',
  cpu: 'CPU',
  stack: 'Stack',
}

export const SORT_LABELS: Record<AssetSortKey, string> = {
  name: 'Name',
  kind: 'Kind',
  ip: 'IP address',
  asset_type: 'Type',
  status: 'Status',
  vendor: 'Vendor',
  model: 'Model',
  serial: 'Serial',
  os_family: 'OS family',
  os_product: 'OS product',
  os_release: 'OS release',
  os_version: 'OS version',
  os: 'OS / Firmware',
  site: 'Site',
  last_seen: 'Last seen',
  inventory_at: 'Last inventory',
  freshness: 'Freshness',
}

/**
 * How "Unknown" is addressed (principle 2: unknown is a value, shown, counted and drillable). It
 * is never a free string: the OS identity fields (family, product, release) take the one sentinel
 * `unknown` (so do OS version, vendor, model and the server hardware fields), and the field that has none (serial)
 * has a health predicate for "missing". Use these patches for the "Unknown" slice, bar or facet row.
 */
export const UNKNOWN_FILTER = {
  os_family: { os_family: 'unknown' },
  os_product: { os_product: 'unknown' },
  os_release: { os_release: 'unknown' },
  os_version: { os_version: 'unknown' },
  vendor: { vendor: 'unknown' },
  model: { model: 'unknown' },
  serial: { health: 'missing_serial' },
  cpu_model: { cpu_model: 'unknown' },
  memory: { memory: 'unknown' },
  disks: { disks: 'unknown' },
  virtualization: { virtualization: 'unknown' },
  module_model: { module_model: 'unknown' },
} as const satisfies Record<string, AssetFilters>

/* ── Parsing one value ───────────────────────────────────────────────────── */

/** The API's own limits (server/app/api/v1/inventory.py), so a value the API would refuse is reported here instead of failing the page. */
export const MAX_TEXT = 200
export const MAX_PRODUCT_KEY = 160
export const MAX_SOURCE = 32
export const MAX_VIRTUALIZATION = 32
const MAX_TAG = 64
const MAX_TAGS = 20
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/
const SLUG = /^[a-z0-9][a-z0-9._+-]{0,31}$/

export type Parsed = { value: string } | { error: string }

/**
 * What the API removes from both ends of every text parameter: Python's `str.strip()`, which is wider than the
 * ASCII space that SQL `btrim` removes from a stored value and differs from JavaScript's `trim()` (it also removes
 * U+001C to U+001F and U+0085, and keeps U+FEFF). A client that trims with anything else sends a value the API then
 * reads differently (review findings NF-FE-6, NB-05).
 */
// eslint-disable-next-line no-control-regex
const API_EDGE = /^[\t\n\v\f\r\u001c-\u001f \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\v\f\r\u001c-\u001f \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g

/** `raw` the way the API reads a parameter. */
export const apiTrim = (raw: string): string => raw.replace(API_EDGE, '')

/**
 * Free text (the search box `q`): the API treats it as words, so runs of white space collapse to one space.
 */
export function freeText(raw: string, max = MAX_TEXT): Parsed {
  const t = apiTrim(raw.replace(/\s+/g, ' '))
  if (!t) return { error: 'empty' }
  if (t.length > max) return { error: `longer than ${max} characters` }
  if (CONTROL.test(t)) return { error: 'contains control characters' }
  return { value: t }
}

/**
 * An EXACT-match value (a site, a CPU model, a software version, a tag...): only the ends are trimmed, because
 * the API compares the stored string and a name such as `Intel(R) Xeon(R) CPU           X5650` or `DR  /  rack 2`
 * really does contain runs of spaces. Control characters (a tab or newline inside the value too) are refused.
 * A value over the API's limit is reported, never cut or dropped without a word.
 *
 * The ends are trimmed the way the API trims them ({@link apiTrim}). `strict` is for a value that comes FROM the
 * API (a facet row, a count's drill-down) and must come back unchanged to select the rows it counts: the stored
 * `Annex<NBSP>` is listed as `Annex<NBSP>`, but the API reads the parameter as `Annex` and finds nothing, so a
 * value that has anything other than plain spaces to trim cannot round-trip and is refused instead of opening
 * a different (empty or wider) list than its number says. A person's own edit of the address is read leniently.
 */
export function exactText(raw: string, max = MAX_TEXT, strict = false): Parsed {
  const t = apiTrim(raw)
  if (strict && t !== raw.replace(/^ +| +$/g, '')) return { error: 'starts or ends with white space the API would drop' }
  if (!t) return { error: 'empty' }
  if (t.length > max) return { error: `longer than ${max} characters` }
  if (CONTROL.test(t)) return { error: 'contains control characters' }
  return { value: t }
}

function slug(raw: string): Parsed {
  const t = raw.trim().toLowerCase()
  if (!SLUG.test(t)) return { error: 'not a valid identifier' }
  return { value: t }
}

/** Case-insensitive on the way in, canonical on the way out (for values with a capital in the middle, like `powerSupply`). */
function oneOfCanonical<T extends string>(allowed: readonly T[]) {
  return (raw: string): Parsed => {
    const t = raw.trim().toLowerCase()
    const hit = allowed.find((a) => a.toLowerCase() === t)
    return hit ? { value: hit } : { error: `expected one of ${allowed.join(', ')}` }
  }
}

function oneOf<T extends string>(allowed: readonly T[]) {
  return (raw: string): Parsed => {
    const t = raw.trim().toLowerCase()
    return (allowed as readonly string[]).includes(t) ? { value: t } : { error: `expected one of ${allowed.join(', ')}` }
  }
}

const SINGLE: Record<Exclude<AssetFilterKey, 'tag'>, (raw: string, strict?: boolean) => Parsed> = {
  q: (raw) => freeText(raw),
  kind: oneOf(ASSET_KINDS),
  // `unknown` is the API's one sentinel for a missing OS family, product, release or version, vendor or model.
  os_family: slug,
  os_product: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  os_release: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  os_version: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  vendor: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  model: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  site: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  source: slug,
  freshness: oneOf(FRESHNESS_VALUES),
  health: oneOf(HEALTH_PREDICATES),
  product: (raw, strict) => exactText(raw, MAX_PRODUCT_KEY, strict),
  product_version: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  // `unknown` is the API's sentinel for a server that reported no CPU model or virtualisation.
  cpu_model: (raw, strict) => exactText(raw, MAX_TEXT, strict),
  memory: oneOf(MEMORY_BUCKETS),
  disks: oneOf(DISK_BUCKETS),
  virtualization: (raw, strict) => exactText(raw, MAX_VIRTUALIZATION, strict),
  security_agent: oneOf(SECURITY_AGENT_VALUES),
  module_class: oneOfCanonical(MODULE_CLASSES),
  // `unknown` is the API's sentinel for a component that reports no model.
  module_model: (raw, strict) => exactText(raw, MAX_TEXT, strict),
}

/* ── Reading and writing a query string ──────────────────────────────────── */

function asParams(input: URLSearchParams | string): URLSearchParams {
  return typeof input === 'string' ? new URLSearchParams(input.replace(/^\?/, '')) : input
}

function normaliseTags(values: readonly string[], strict: boolean): { tags: string[]; rejected: RejectedFilter[] } {
  const tags: string[] = []
  const seen = new Set<string>()
  const rejected: RejectedFilter[] = []
  for (const raw of values) {
    if (!raw.trim()) continue // a blank tag is no tag, not an error
    const t = exactText(raw, MAX_TAG, strict)
    if ('error' in t) {
      rejected.push({ key: 'tag', value: raw, reason: t.error })
      continue
    }
    const key = t.value.toLowerCase()
    if (seen.has(key)) continue
    if (tags.length >= MAX_TAGS) {
      rejected.push({ key: 'tag', value: raw, reason: `more than ${MAX_TAGS} tags` })
      continue
    }
    seen.add(key)
    tags.push(t.value)
  }
  // A set, so a canonical order: the same tags in any order are one URL and one cache entry.
  tags.sort((a, b) => (a.toLowerCase() < b.toLowerCase() ? -1 : a.toLowerCase() > b.toLowerCase() ? 1 : 0))
  return { tags, rejected }
}

/**
 * Validates a filter object the way a query string is validated: unknown enum values, unknown
 * words and over-long text are dropped and reported, never passed on. Keys outside the vocabulary
 * are ignored (this is also what keeps the object typed). `product_version` needs `product`.
 */
export function normaliseAssetFilters(input: AssetFilters, options: { lenient?: boolean } = {}): { filters: AssetFilters; rejected: RejectedFilter[] } {
  // A value written into an address by the page (a count's drill-down, a facet row) must come back from the API
  // unchanged; only a person's own edit of the address (`parseAssetFilters`) is read leniently (see exactText).
  const strict = !options.lenient
  const filters: Record<string, unknown> = {}
  const rejected: RejectedFilter[] = []
  const source = input as Record<string, unknown>
  for (const key of ASSET_FILTER_KEYS) {
    const raw = source[key]
    if (raw === undefined || raw === null || raw === '') continue
    if (key === 'tag') {
      const list = Array.isArray(raw) ? raw.map(String) : [String(raw)]
      const { tags, rejected: bad } = normaliseTags(list, strict)
      rejected.push(...bad)
      if (tags.length) filters.tag = tags
      continue
    }
    const parsed = SINGLE[key](String(raw), strict)
    if ('error' in parsed) rejected.push({ key, value: String(raw), reason: parsed.error })
    else filters[key] = parsed.value
  }
  if (filters.product_version !== undefined && filters.product === undefined) {
    rejected.push({ key: 'product_version', value: String(filters.product_version), reason: 'needs product' })
    delete filters.product_version
  }
  return { filters: filters as AssetFilters, rejected }
}

/** Reads the asset filter vocabulary out of a query string. Repeated `tag`s are kept; for any other key the first value wins. */
export function parseAssetFilters(input: URLSearchParams | string): { filters: AssetFilters; rejected: RejectedFilter[] } {
  const params = asParams(input)
  const raw: Record<string, unknown> = {}
  for (const key of ASSET_FILTER_KEYS) {
    if (key === 'tag') {
      const tags = params.getAll('tag')
      if (tags.length) raw.tag = tags
    } else {
      const v = params.get(key)
      if (v !== null) raw[key] = v
    }
  }
  return normaliseAssetFilters(raw as AssetFilters, { lenient: true })
}

/** Writes the vocabulary keys into `into`, replacing any it already holds. Values are validated like a read. */
export function writeAssetFilters(filters: AssetFilters, into: URLSearchParams = new URLSearchParams()): URLSearchParams {
  for (const key of ASSET_FILTER_KEYS) into.delete(key)
  const { filters: clean } = normaliseAssetFilters(filters)
  for (const key of ASSET_FILTER_KEYS) {
    if (key === 'tag') for (const t of clean.tag ?? []) into.append('tag', t)
    else {
      const v = clean[key]
      if (v !== undefined) into.set(key, v)
    }
  }
  return into
}

export const isAssetFiltersEmpty = (f: AssetFilters): boolean => ASSET_FILTER_KEYS.every((k) => k === 'tag' ? !f.tag?.length : f[k] === undefined)

/**
 * What the API calls the OS families (`inventory_query.OS_FAMILY_LABELS`): the filter value is the slug
 * (`ios-xe`), a person reads "IOS XE". A family the table does not know is shown as its slug.
 */
export const OS_FAMILY_LABELS: Readonly<Record<string, string>> = {
  windows: 'Windows', linux: 'Linux', macos: 'macOS', bsd: 'BSD', fortios: 'FortiOS', fortiswitchos: 'FortiSwitchOS',
  fortiap: 'FortiAP', panos: 'PAN-OS', ios: 'IOS', 'ios-xe': 'IOS XE', 'nx-os': 'NX-OS', asa: 'ASA', junos: 'Junos',
  tmos: 'TMOS', 'aos-cx': 'AOS-CX', arubaos: 'ArubaOS', dellos10: 'Dell OS10', routeros: 'RouterOS', eos: 'EOS',
  other: 'Other', unknown: 'Unknown',
}

/** Display names the page has learnt for values that are keys (a software product key, a source slug). */
export interface ValueNames {
  /** `google-chrome` -> `Google Chrome`. */
  product?: (key: string) => string | undefined
  source?: (slug: string) => string
}

/** One chip per active value (a tag counts once per tag). */
export function activeFilterChips(f: AssetFilters, names: ValueNames = {}): { key: AssetFilterKey; value: string; label: string; text: string }[] {
  const out: { key: AssetFilterKey; value: string; label: string; text: string }[] = []
  for (const key of ASSET_FILTER_KEYS) {
    if (key === 'tag') {
      for (const t of f.tag ?? []) out.push({ key, value: t, label: FILTER_LABELS.tag, text: t })
      continue
    }
    const v = f[key]
    if (v === undefined) continue
    out.push({ key, value: v, label: FILTER_LABELS[key], text: filterValueLabel(key, v, names) })
  }
  return out
}

/** Filters whose value `unknown` is the API's sentinel for "missing" (shown as the word Unknown). */
const UNKNOWN_SENTINEL_KEYS: ReadonlySet<string> = new Set(['os_family', 'os_product', 'os_release', 'os_version', 'vendor', 'model', 'cpu_model', 'virtualization', 'module_model'])

/** Human text for a value: enum values and slugs get their label, everything else is shown as written. */
export function filterValueLabel(key: AssetFilterKey, value: string, names: ValueNames = {}): string {
  if (key === 'kind') return KIND_LABELS[value as AssetKind] ?? value
  if (key === 'freshness') return FRESHNESS_LABELS[value as FreshnessFilter] ?? value
  if (key === 'health') return HEALTH_LABELS[value as HealthPredicate] ?? value
  if (key === 'memory') return MEMORY_LABELS[value as MemoryBucket] ?? value
  if (key === 'disks') return DISK_LABELS[value as DiskBucket] ?? value
  if (key === 'security_agent') return SECURITY_AGENT_LABELS[value as SecurityAgentFilter] ?? value
  if (key === 'module_class') return MODULE_CLASS_LABELS[value as ModuleClass] ?? value
  if (key === 'os_family') return OS_FAMILY_LABELS[value.toLowerCase()] ?? value
  if (key === 'source') return names.source ? names.source(value) : value
  if (key === 'product') return names.product?.(value) ?? value
  if (UNKNOWN_SENTINEL_KEYS.has(key) && value.toLowerCase() === 'unknown') return 'Unknown'
  return value
}

/* ── Paging, ordering ────────────────────────────────────────────────────── */

function clampInt(raw: unknown, max: number): number | undefined {
  const n = typeof raw === 'number' ? raw : typeof raw === 'string' && /^\d{1,9}$/.test(raw.trim()) ? Number(raw) : NaN
  return Number.isInteger(n) && n > 0 ? Math.min(n, max) : undefined
}

/** Keeps the valid parts of a list state: a known sort column, a direction, whole positive numbers (page_size is capped at the API maximum). */
export function normaliseListState(input: ListState, sorts: readonly string[] = ASSET_SORT_KEYS): ListState {
  const out: ListState = {}
  const sort = typeof input.sort === 'string' ? input.sort.trim().toLowerCase() : ''
  if (sort && sorts.includes(sort)) out.sort = sort as AssetSortKey
  const order = typeof input.order === 'string' ? input.order.trim().toLowerCase() : ''
  if ((SORT_ORDERS as readonly string[]).includes(order)) out.order = order as SortOrder
  const page = clampInt(input.page, MAX_PAGE)
  if (page !== undefined) out.page = page
  const size = clampInt(input.page_size, MAX_PAGE_SIZE)
  if (size !== undefined) out.page_size = size
  return out
}

/** Reads sort, order, page and page_size; anything missing or invalid is simply absent. */
export function parseListState(input: URLSearchParams | string, sorts: readonly string[] = ASSET_SORT_KEYS): ListState {
  const params = asParams(input)
  return normaliseListState(
    {
      sort: (params.get('sort') ?? undefined) as AssetSortKey | undefined,
      order: (params.get('order') ?? undefined) as SortOrder | undefined,
      page: (params.get('page') ?? undefined) as unknown as number | undefined,
      page_size: (params.get('page_size') ?? undefined) as unknown as number | undefined,
    },
    sorts,
  )
}

/** Sorting by a column: a new column starts ascending, the same column flips the direction. */
export function nextSort(
  current: { sort: string; order: SortOrder },
  key: string,
): { sort: string; order: SortOrder } {
  if (current.sort === key) return { sort: key, order: current.order === 'asc' ? 'desc' : 'asc' }
  return { sort: key, order: 'asc' }
}

export interface QueryStringOptions {
  /**
   * `false` (the address bar): leave out what is already the default, so a plain list has a plain
   * URL. `true` (the API request): always send sort, order, page and page_size explicitly.
   */
  explicit?: boolean
  defaults?: { sort: string; order: SortOrder; page_size: number }
}

/** Filters plus paging and ordering as one query string, vocabulary order, deterministic. */
export function assetQueryToParams(
  query: AssetQuery,
  options: QueryStringOptions = {},
  into: URLSearchParams = new URLSearchParams(),
): URLSearchParams {
  const defaults = options.defaults ?? { ...DEFAULT_ASSET_SORT, page_size: DEFAULT_PAGE_SIZE }
  writeAssetFilters(query, into)
  for (const key of ['sort', 'order', 'page', 'page_size']) into.delete(key)
  const list = normaliseListState(query)
  const sort = list.sort ?? defaults.sort
  const order = list.order ?? defaults.order
  const page = list.page ?? 1
  const size = list.page_size ?? defaults.page_size
  if (options.explicit || sort !== defaults.sort || order !== defaults.order) {
    into.set('sort', sort)
    into.set('order', order)
  }
  if (options.explicit || page > 1) into.set('page', String(page))
  if (options.explicit || size !== defaults.page_size) into.set('page_size', String(size))
  return into
}

/* ── Assets and links ────────────────────────────────────────────────────── */

export const ASSETS_PATH = '/inventory/assets'

const ASSET_ADDRESS = /^(server|device):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i

/** An asset is addressed as `<kind>:<uuid>` (for example `device:3f2a...`). */
export function assetAddress(kind: AssetKind, id: string): string {
  return `${kind}:${id}`
}

export function parseAssetAddress(value: string | null | undefined): { kind: AssetKind; id: string } | null {
  const m = ASSET_ADDRESS.exec((value ?? '').trim())
  return m ? { kind: m[1].toLowerCase() as AssetKind, id: m[2].toLowerCase() } : null
}

/** `/inventory/assets?...` for a filter set; the list a clicked number stands for. */
export function assetsHref(filters: AssetFilters, extra: ListState & { asset?: string } = {}): string {
  const { asset, ...list } = extra
  const qs = assetQueryToParams({ ...filters, ...list }).toString()
  const params = new URLSearchParams(qs)
  const address = parseAssetAddress(asset)
  if (address) params.set('asset', assetAddress(address.kind, address.id))
  const out = params.toString()
  return out ? `${ASSETS_PATH}?${out}` : ASSETS_PATH
}

/**
 * {@link assetsHref} plus what could not be written into it. A value the API would refuse (over its length
 * limit, with a control character) is left out of the address; the caller must NOT present such a link as the
 * list behind its number (it would open a wider list), so it shows the number as text and says why.
 */
export function assetsLink(filters: AssetFilters, extra: ListState & { asset?: string } = {}): { href: string; rejected: RejectedFilter[] } {
  const { rejected } = normaliseAssetFilters(filters)
  return { href: assetsHref(filters, extra), rejected }
}

/** "cpu_model is longer than 200 characters": one rejected value in words, for a tooltip. */
export function rejectedText(r: RejectedFilter): string {
  return `${FILTER_LABELS[r.key as AssetFilterKey] ?? r.key} ${r.reason}`
}

/* ── A codec lets one URL-state hook serve every list page ──────────────── */

/** How a page's filter object reads from and writes to its query string. */
export interface FilterCodec<F> {
  /** The query keys this codec owns (so a reset can remove them). */
  readonly keys: readonly string[]
  parse(params: URLSearchParams): { filters: F; rejected: RejectedFilter[] }
  /** Replaces the codec's keys in `into` with `filters`. */
  write(filters: F, into: URLSearchParams): URLSearchParams
  isEmpty(filters: F): boolean
  /** Number of chips the filters would show. */
  count(filters: F): number
}

export const assetFilterCodec: FilterCodec<AssetFilters> = {
  keys: ASSET_FILTER_KEYS,
  parse: parseAssetFilters,
  write: writeAssetFilters,
  isEmpty: isAssetFiltersEmpty,
  count: (f) => activeFilterChips(f).length,
}

/**
 * Applies a patch to a filter object: `undefined`, `null`, an empty string or an empty list removes
 * the key. The result is validated like a read, so a bad patch value is dropped, not stored.
 */
export function patchAssetFilters(current: AssetFilters, patch: { [K in keyof AssetFilters]?: AssetFilters[K] | null }): AssetFilters {
  const merged: Record<string, unknown> = { ...current }
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined || value === null || value === '' || (Array.isArray(value) && value.length === 0)) delete merged[key]
    else merged[key] = value
  }
  return normaliseAssetFilters(merged as AssetFilters).filters
}

/** Adds the tag when absent, removes it when present (case-insensitive). */
export function toggleTag(current: AssetFilters, tag: string): AssetFilters {
  const tags = current.tag ?? []
  const lower = tag.toLowerCase()
  const next = tags.some((t) => t.toLowerCase() === lower) ? tags.filter((t) => t.toLowerCase() !== lower) : [...tags, tag]
  return patchAssetFilters(current, { tag: next })
}
