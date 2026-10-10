// Pure logic behind the Inventory touchpoints in the Devices list and the device detail page
// (Documentation/Inventory/03-UI-UX-SPEC.md section 4, roadmap task 1.9). No React, no router, no API:
// scripts/test-inventory-touchpoints.mjs exercises it directly and the pages only render what it returns.
//
//   * Vendor, Model and OS version columns of the Devices list: what each cell, sort key, CSV cell and
//     filter option is made of. Vendor and model are the device's own columns (as the poller stored them);
//     the OS version shown is the API's `os_version_display`: the normalised Inventory version where there is
//     one, else the device's own value (RG-11).
//   * Which columns a user sees by default (the identity columns are opt-in, except the OS version for a user
//     who has no saved column choice yet) and where the CSV puts them (at the END, so positions never shift).
//   * The request: the filters the API applies exactly, the first 200 devices, and the facets over ALL devices.
//   * Unknown is a value (principle 2): an empty or blank field is the option "Unknown" with its count, it
//     can be filtered on, and it is never folded into another value.
//   * The facet counts of a filter are the number of rows the list would show if that option were picked
//     (every other filter stays in force), so the number in the menu equals the total after choosing it.
//   * The link from a device's OS / firmware value to the Inventory list behind it.

import { assetsHref, type AssetFilters } from '@/pages/inventory/filters'

/** The address and menu spelling of "no value". A vendor literally called "unknown" is the same bucket. */
export const UNKNOWN = 'unknown'

export type IdentityField = 'vendor' | 'model' | 'os_version'
export const IDENTITY_FIELDS: readonly IdentityField[] = ['vendor', 'model', 'os_version']

export interface IdentityRow {
  vendor?: string | null
  model?: string | null
  /** The device's own OS version, as the poller stored it. */
  os_version?: string | null
  /** The normalised version of the Inventory identity, else the device's own (GET /devices, every item). */
  os_version_display?: string | null
}

/** The active identity filters, in the Devices list's own URL vocabulary (`?vendor=`, `?model=`, `?os_version=`). */
export type IdentityFilters = Partial<Record<IdentityField, string>>

/* ── Text ────────────────────────────────────────────────────────────────── */

/** Trimmed text; blank or non-text is `null`. */
export function cleanText(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const t = value.trim()
  return t === '' ? null : t
}

/** Case- and space-insensitive comparison key; every spelling of "no value" (including the word unknown) is `unknown`. */
export function identityKey(value: unknown): string {
  const t = cleanText(value)
  if (t === null) return UNKNOWN
  const k = t.toLowerCase()
  return k === UNKNOWN ? UNKNOWN : k
}

/** What a table cell shows: the text, or a dash for unknown (the muted look is the component's). */
export function cellText(value: unknown): string {
  return cleanText(value) ?? '—'
}

/** `true` when the field has no usable value. */
export const isUnknownValue = (value: unknown): boolean => identityKey(value) === UNKNOWN

/**
 * The value a column shows, sorts, filters and exports for a field. For the OS version that is
 * `os_version_display` (the same value the API filters `os_version` on and the Inventory pages show), falling
 * back to the device's own column when the API did not send it.
 */
export function identityValue(row: IdentityRow, field: IdentityField): string | null | undefined {
  if (field === 'os_version') return cleanText(row.os_version_display) ?? row.os_version
  return row[field]
}

/* ── Filtering ───────────────────────────────────────────────────────────── */

/** `true` when the row passes every non-empty filter. */
export function matchesIdentity(row: IdentityRow, filters: IdentityFilters): boolean {
  for (const field of IDENTITY_FIELDS) {
    const wanted = cleanText(filters[field])
    if (wanted !== null && identityKey(identityValue(row, field)) !== identityKey(wanted)) return false
  }
  return true
}

/** The filters without the given field (the others stay in force while that field's menu is counted). */
export function without(filters: IdentityFilters, field: IdentityField): IdentityFilters {
  const rest = { ...filters }
  delete rest[field]
  return rest
}

/* ── Facet options (menu entries with counts) ────────────────────────────── */

export interface FacetOption {
  /** What goes into the address bar and the menu item: the commonest spelling, or `unknown`. */
  value: string
  label: string
  /** Rows that have this value among the rows given. */
  count: number
}

const collator = new Intl.Collator('en', { sensitivity: 'base', numeric: true })

/**
 * The menu of one filter over `rows`, which the caller has already narrowed by every OTHER filter.
 * Case variants of one value are one entry (counted together, shown as their commonest spelling),
 * ordered by count, then name, with Unknown last. The selected value is always listed, with count 0
 * when no row has it, so a filter that matches nothing can be seen and cleared rather than hidden.
 */
export function buildFacet(rows: readonly IdentityRow[], field: IdentityField, selected?: string | null): FacetOption[] {
  const groups = new Map<string, { count: number; spellings: Map<string, number> }>()
  for (const row of rows) {
    const key = identityKey(identityValue(row, field))
    const g = groups.get(key) ?? { count: 0, spellings: new Map<string, number>() }
    g.count += 1
    const text = cleanText(identityValue(row, field))
    if (text !== null && key !== UNKNOWN) g.spellings.set(text, (g.spellings.get(text) ?? 0) + 1)
    groups.set(key, g)
  }
  const options: FacetOption[] = []
  for (const [key, g] of groups) {
    if (key === UNKNOWN) { options.push({ value: UNKNOWN, label: 'Unknown', count: g.count }); continue }
    const spelling = [...g.spellings.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))[0][0]
    options.push({ value: spelling, label: spelling, count: g.count })
  }
  const want = cleanText(selected)
  if (want !== null && !groups.has(identityKey(want))) {
    options.push(identityKey(want) === UNKNOWN ? { value: UNKNOWN, label: 'Unknown', count: 0 } : { value: want, label: want, count: 0 })
  }
  return options.sort((a, b) => {
    if (a.value === UNKNOWN) return 1
    if (b.value === UNKNOWN) return -1
    return b.count - a.count || collator.compare(a.label, b.label)
  })
}

/** The option whose key matches `selected` (an address typed as `?vendor=cisco` selects "Cisco"); `selected` itself when none does. */
export function selectedValue(options: readonly { value: string }[], selected: string | null | undefined): string {
  const want = cleanText(selected)
  if (want === null) return ''
  const key = identityKey(want)
  return options.find((o) => identityKey(o.value) === key)?.value ?? want
}

/* ── Sorting ─────────────────────────────────────────────────────────────── */

const pad = (s: string) => s.replace(/\d+/g, (d) => d.padStart(12, '0'))

/**
 * The sort value of a field: lower-case text with numbers compared as numbers ("9.1" before "10.2"),
 * `null` for unknown, which the list's comparator always places last whichever way it sorts.
 */
export function identitySortValue(value: unknown): string | null {
  const t = cleanText(value)
  if (t === null || identityKey(t) === UNKNOWN) return null
  return pad(t.toLowerCase())
}

/* ── CSV ─────────────────────────────────────────────────────────────────── */

/**
 * A device-reported string as a CSV cell. A leading = + - @ tab or CR would be evaluated by a spreadsheet
 * (vendor, model and version strings come from the device, not from an administrator), so it is
 * prefixed with an apostrophe, as the other exports of the product do. Unknown is an empty cell.
 */
export function csvDeviceText(value: unknown): string {
  const t = cleanText(value)
  if (t === null) return ''
  return /^[=+\-@\t\r]/.test(t) ? `'${t}` : t
}

/** The three columns the Devices list adds to its CSV, header first; `csvDeviceText` of what the list shows. */
export const IDENTITY_CSV_COLUMNS: ReadonlyArray<readonly [header: string, cell: (row: IdentityRow) => string]> = [
  ['vendor', (r) => csvDeviceText(identityValue(r, 'vendor'))],
  ['model', (r) => csvDeviceText(identityValue(r, 'model'))],
  ['os_version', (r) => csvDeviceText(identityValue(r, 'os_version'))],
]

/** The columns of the Devices CSV BEFORE Inventory existed, in their order. A script that reads the file by position depends on it (RG-08). */
export const LEGACY_CSV_HEADERS = [
  'hostname', 'ip_address', 'managed_via', 'serial_number', 'device_type', 'group', 'location', 'tags',
  'status', 'cpu_pct', 'memory_pct', 'uptime', 'last_seen',
] as const

/** What a CSV row is made of: the device as GET /devices returns it and the page's derived figures. */
export interface CsvDevice extends IdentityRow {
  hostname: string
  ip_address?: string | null
  managed_ip?: string | null
  managed_by_hostname?: string | null
  serial_number?: string | null
  device_type: string
  group_name?: string | null
  location?: string | null
  tags?: readonly string[] | null
  last_seen?: string | null
}
export interface CsvRow {
  device: CsvDevice
  health: string
  snmpNoData: boolean
  cpu: number | null
  mem: number | null
  uptime: string
}

/**
 * The CSV of the Devices list: the columns that always existed, in their original positions, then the identity
 * columns (vendor, model, os_version) appended at the END so no existing position moves (RG-08).
 */
export function devicesCsvColumns(): Array<[header: string, cell: (row: CsvRow) => string]> {
  return [
    ['hostname', (r) => r.device.hostname],
    ['ip_address', (r) => r.device.ip_address || r.device.managed_ip || ''],
    ['managed_via', (r) => r.device.managed_by_hostname || ''],
    ['serial_number', (r) => r.device.serial_number || ''],
    ['device_type', (r) => r.device.device_type],
    ['group', (r) => r.device.group_name || ''],
    ['location', (r) => r.device.location || ''],
    ['tags', (r) => (r.device.tags || []).join('; ')],
    ['status', (r) => (r.snmpNoData ? 'warning (snmp: no data)' : r.health)],
    ['cpu_pct', (r) => (r.cpu == null ? '' : String(r.cpu))],
    ['memory_pct', (r) => (r.mem == null ? '' : String(r.mem))],
    ['uptime', (r) => r.uptime],
    ['last_seen', (r) => r.device.last_seen || ''],
    ...IDENTITY_CSV_COLUMNS.map(([header, cell]): [string, (row: CsvRow) => string] => [header, (r) => cell(r.device)]),
  ]
}

/** One CSV field: quoted when it holds a comma, a quote or a line break. */
export function csvEscape(v: string | null | undefined): string {
  if (v == null) return ''
  if (/[",\n]/.test(v)) return `"${v.replace(/"/g, '""')}"`
  return v
}

/** The whole file (header row first), rows in the order given. */
export function buildDevicesCsv(rows: readonly CsvRow[]): string {
  const cols = devicesCsvColumns()
  return [cols.map(([h]) => h).join(','), ...rows.map((r) => cols.map(([, f]) => csvEscape(f(r))).join(','))].join('\n')
}

/* ── Which columns show by default ───────────────────────────────────────── */

/**
 * The columns Inventory added to the list; a user who saved a column choice before they existed never chose them.
 * They are opt-in for everyone (the page's defaults have them off): each is 70 to 90 px, and the table already scrolls
 * sideways at 1280 and 1440 px, so on by default they push Status, CPU and Memory out of the first screen (RG-02).
 */
export const ADDED_COLUMNS = ['vendor', 'model', 'os_version'] as const

/**
 * The visible columns from the saved choice (RG-02, UI-17):
 *   - no saved choice: the defaults;
 *   - a saved choice: each column the user saved keeps its saved value; a column that did not exist then (the
 *     added ones) is OFF, so the table of someone who set it up long ago does not grow under them.
 */
export function resolveColumnVisibility<K extends string>(defaults: Record<K, boolean>, saved: unknown, added: readonly string[]): Record<K, boolean> {
  const out = { ...defaults }
  if (saved === null || typeof saved !== 'object' || Array.isArray(saved)) return out
  const chosen = saved as Record<string, unknown>
  for (const key of Object.keys(defaults) as K[]) {
    if (typeof chosen[key] === 'boolean') out[key] = chosen[key] as boolean
    else if (added.includes(key)) out[key] = false
  }
  return out
}

/* ── The request ─────────────────────────────────────────────────────────── */

/** GET /devices answers at most this many devices; the page loads one page of that size. */
export const DEVICE_LIST_LIMIT = 200

/** What the Devices page narrows by, in the vocabulary of its address bar. */
export interface DeviceListFilters {
  search?: string
  vendor?: string
  os_version?: string
  type?: string
  group?: string
  location?: string
  managedBy?: string
  tags?: readonly string[]
  tagMode?: 'any' | 'all'
}

/** The page's filters as its address bar spells them (`search` or the older `q`, `type`, `group`, `loc`, `tag`, `tagmode`, `managed_by`, `vendor`, `os_version`). */
export function deviceListFiltersFromParams(params: URLSearchParams): DeviceListFilters {
  return {
    search: params.get('search') ?? params.get('q') ?? '',
    vendor: params.get('vendor') ?? '',
    os_version: params.get('os_version') ?? '',
    type: params.get('type') ?? '',
    group: params.get('group') ?? '',
    location: params.get('loc') ?? '',
    managedBy: params.get('managed_by') ?? '',
    tags: (params.get('tag') ?? '').split(',').map((t) => t.trim()).filter(Boolean),
    tagMode: params.get('tagmode') === 'all' ? 'all' : 'any',
  }
}

/**
 * The query of GET /devices for the page's filters. Every filter the API applies exactly is sent, so the 200
 * devices that come back are the right 200 (and `meta.total` the right total), and `facets=true` asks for the
 * Vendor / Model / OS version menus over ALL devices. `unknown` is sent as it is: the API reads it as "no value"
 * (NULL, blank or the word), the meaning it has everywhere in Inventory (RG-10). The health status chip is not sent:
 * it is derived in the browser from alerts and metrics, and the API's `status` means something else.
 */
export function deviceListParams(f: DeviceListFilters): Record<string, string> {
  const out: Record<string, string> = { limit: String(DEVICE_LIST_LIMIT), facets: 'true' }
  const put = (key: string, value: string | undefined) => {
    const t = cleanText(value)
    if (t !== null) out[key] = t
  }
  put('search', f.search)
  put('vendor', f.vendor)
  put('os_version', f.os_version)
  put('device_type', f.type)
  put('group_id', f.group)
  put('location', f.location)
  put('managed_by', f.managedBy)
  const tags = (f.tags ?? []).map((t) => t.trim()).filter(Boolean)
  if (tags.length > 0) {
    out.tags = tags.join(',')
    if (f.tagMode === 'all') out.tags_match = 'all'
  }
  return out
}

/** "first 200 of 284" when the list holds fewer devices than match; null when it holds them all. */
export function listCoverage(loaded: number, total: number | null | undefined): { truncated: boolean; text: string | null } {
  if (typeof total !== 'number' || !Number.isFinite(total) || total <= loaded) return { truncated: false, text: null }
  return { truncated: true, text: `first ${loaded.toLocaleString()} of ${total.toLocaleString()}` }
}

/* ── Menus from the API's facets ─────────────────────────────────────────── */

/** One facet of GET /devices?facets=true: over ALL devices, every active filter except its own applied. */
export interface ServerFacet {
  items: ReadonlyArray<{ value: string; label: string; count: number }>
  distinct: number
}

/** A menu entry; `count` is absent when it cannot be known (a selected value beyond the API's item cap). */
export interface MenuOption { value: string; label: string; count?: number }

export interface FacetMenu {
  options: MenuOption[]
  /** `all`: counts over every device the API holds for the other filters; `loaded`: over the devices on this page's list only. */
  basis: 'all' | 'loaded'
}

/**
 * The menu of one identity filter.
 *
 * With `useServer` (nothing narrows the list in the browser alone) and the API's facet at hand, the counts are the
 * API's: over ALL devices, and each equals the `meta.total` the list gives once chosen (RG-03). Otherwise they are
 * counted over the loaded rows, as before, which is exact whenever the list is not truncated; the caller says when
 * it is. The selected value is always listed, so a filter that matches nothing can be seen and cleared.
 */
export function facetMenu(args: {
  field: IdentityField
  server: ServerFacet | null | undefined
  rows: readonly IdentityRow[]
  selected?: string | null
  useServer: boolean
}): FacetMenu {
  const { field, server, rows, selected, useServer } = args
  if (!useServer || !server) return { options: buildFacet(rows, field, selected), basis: 'loaded' }
  const options: MenuOption[] = server.items.map((i) => ({ value: i.value, label: i.label, count: i.count }))
  const want = cleanText(selected)
  if (want !== null && !options.some((o) => identityKey(o.value) === identityKey(want))) {
    const capped = server.distinct > server.items.length
    options.push({
      value: identityKey(want) === UNKNOWN ? UNKNOWN : want,
      label: identityKey(want) === UNKNOWN ? 'Unknown' : want,
      ...(capped ? {} : { count: 0 }),
    })
  }
  // Unknown is a value of its own and goes last, whatever its count.
  const unknown = options.filter((o) => o.value === UNKNOWN)
  return { options: [...options.filter((o) => o.value !== UNKNOWN), ...unknown], basis: 'all' }
}

/* ── Device detail: the OS / firmware value as a link into Inventory ─────── */

/** What the asset flyout (`GET /inventory/assets/device/<id>`) reports about a device's OS: the part of `AssetRow` this needs. */
export interface DeviceOsIdentity {
  kind: string
  os_family: string
  os_product: string | null
  os_release: string | null
  os_version: string | null
  os_source: string | null
}

/** The row of the Inventory & Configuration card that carries the link. */
export type OsLinkRow = 'os' | 'firmware'

export interface DeviceOsLink {
  row: OsLinkRow
  /** The Inventory asset list of the network devices on the same family, product and release. */
  href: string
  /** Words for the tooltip, for example `FortiOS 7.2`. */
  release: string
}

/**
 * The link from a device's OS / firmware value to the list behind it: the network devices on the same
 * family, product and release, which is the list the OS tree's release row opens (its count equals that
 * list's total). The Operating Systems page itself reads only `?kind=` and `?sort=`, so a link there with
 * the family and release would be silently unfiltered; the list is the page that honours them.
 *
 * The normaliser takes the OS version from a template value, the OS version the profile extracted, the
 * chassis firmware or the sysDescr, in that order (`os_source`). The value that was the evidence carries
 * the link: the Firmware row when the chassis firmware was used, the OS / version row otherwise; a row
 * showing something else is never linked to a release it did not produce. An unidentified OS
 * (`unknown`) has no link: there is nothing to filter by.
 */
export function deviceOsLink(identity: DeviceOsIdentity | null | undefined): DeviceOsLink | null {
  if (!identity || identity.kind !== 'device') return null
  const family = cleanText(identity.os_family)?.toLowerCase()
  if (!family || family === UNKNOWN) return null
  const filters: AssetFilters = {
    kind: 'device',
    os_family: family,
    os_product: cleanText(identity.os_product) ?? UNKNOWN,
    os_release: cleanText(identity.os_release) ?? UNKNOWN,
  }
  const product = cleanText(identity.os_product)
  const release = cleanText(identity.os_release)
  return {
    row: identity.os_source === 'entity' ? 'firmware' : 'os',
    href: assetsHref(filters),
    release: [product ?? family, release].filter(Boolean).join(' '),
  }
}
