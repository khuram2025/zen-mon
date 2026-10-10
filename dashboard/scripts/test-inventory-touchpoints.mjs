// Inventory touchpoints in existing pages (Documentation/Inventory/04-ROADMAP task 1.9, 03-UI-UX-SPEC section 4):
//   (a) the pure logic of components/devices/deviceInventory.ts: unknown is a value, case-insensitive grouping,
//       facet counts that equal the list after choosing the option, numeric-aware sorting, the CSV guard and
//       the link from a device's OS to the Inventory list behind it,
//   (b) the Devices list rendered for real: Vendor / Model / OS version columns (opt-in; a saved column choice keeps
//       its layout), the vendor and OS version filters in the
//       address bar, menu counts over ALL devices (the API's facets), "first N of M" when the list is cut short,
//       sorting with unknown last in both directions, the pre-existing columns and the status filter untouched,
//       the CSV with its legacy column positions pinned,
//   (c) the device detail Inventory & Configuration card: the OS / firmware value is a link only with
//       inventory.view and only when Inventory identified the OS, on the row that was the evidence,
//   (d) the server Software tab: package links to the catalog (inventory.view and a product key), the two
//       banners (the agent's 1,000-title cap and the response limit), nothing else changed,
//   (e) the Server Overview OS tile: the Inventory link for inventory.view, the rows' own links unchanged.
// Run from dashboard/: node scripts/test-inventory-touchpoints.mjs
import assert from 'node:assert/strict'
import path from 'node:path'
import { build } from 'esbuild'

process.env.TZ = 'UTC'
const root = process.cwd()
const src = path.join(root, 'src')

/* ── bundle the code under test (the app's own alias, the real pages) ──────────────────────── */

const entry = `
export * as DI from '@/components/devices/deviceInventory'
export * as F from '@/pages/inventory/filters'
export { DevicesPage } from '@/pages/DevicesPage'
export { InventoryConfigCard } from '@/pages/DeviceDetailPage'
export { SystemTab } from '@/pages/servers/detail/tabs/SystemTab'
export { ServersDashboardPage } from '@/pages/servers/ServersDashboardPage'
export { useAuth } from '@/stores/auth'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server'
export { MemoryRouter, Routes, Route } from 'react-router-dom'
export { QueryClient, QueryClientProvider } from '@tanstack/react-query'
`
const aliasPlugin = {
  name: 'app-alias',
  setup(b) {
    b.onResolve({ filter: /^@\// }, (args) => b.resolve(`./${args.path.slice(2)}`, { resolveDir: src, kind: args.kind }))
  },
}
const bundle = await build({
  stdin: { contents: entry, resolveDir: root, loader: 'ts' },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  plugins: [aliasPlugin],
  // The device page pulls in a stylesheet and a few images; none of them matters for markup.
  loader: { '.css': 'empty', '.svg': 'empty', '.png': 'empty', '.woff2': 'empty' },
  logLevel: 'silent',
})
const m = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const { DI, F } = m
const h = m.createElement

/* ═══ (a) the pure logic ═══════════════════════════════════════════════════════════════════ */

{
  assert.equal(DI.cleanText('  Cisco '), 'Cisco')
  assert.equal(DI.cleanText('   '), null)
  assert.equal(DI.cleanText(null), null)
  assert.equal(DI.cleanText(42), null, 'only text is a value')
  // Every spelling of "no value", and the word itself, is one key.
  for (const v of [null, undefined, '', '  ', 'unknown', 'Unknown', ' UNKNOWN ']) assert.equal(DI.identityKey(v), 'unknown', JSON.stringify(v))
  assert.equal(DI.identityKey(' Cisco '), 'cisco')
  assert.equal(DI.cellText(null), '—')
  assert.equal(DI.cellText(' 7.2.8 '), '7.2.8')
  assert.equal(DI.isUnknownValue(''), true)
  assert.equal(DI.isUnknownValue('Cisco'), false)
}

// Filtering: every non-empty filter must match; a blank filter is not a filter.
{
  const row = { vendor: 'Cisco', model: 'C9300-48P', os_version: '17.9.4a' }
  assert.equal(DI.matchesIdentity(row, {}), true)
  assert.equal(DI.matchesIdentity(row, { vendor: '', os_version: '  ' }), true, 'blank filters are ignored')
  assert.equal(DI.matchesIdentity(row, { vendor: 'CISCO' }), true, 'case-insensitive')
  assert.equal(DI.matchesIdentity(row, { vendor: 'Cis' }), false, 'exact, not a fragment')
  assert.equal(DI.matchesIdentity(row, { vendor: 'cisco', model: 'c9300-48p', os_version: '17.9.4A' }), true)
  assert.equal(DI.matchesIdentity(row, { vendor: 'cisco', os_version: '9.1' }), false, 'AND')
  assert.equal(DI.matchesIdentity({ vendor: null }, { vendor: 'unknown' }), true, 'unknown selects the rows with no value')
  assert.equal(DI.matchesIdentity({ vendor: '  ' }, { vendor: 'unknown' }), true)
  assert.equal(DI.matchesIdentity({ vendor: 'Cisco' }, { vendor: 'unknown' }), false)
  assert.deepEqual(DI.without({ vendor: 'a', os_version: 'b' }, 'vendor'), { os_version: 'b' })
}

const FLEET = [
  { vendor: 'Cisco', model: 'C9300-48P', os_version: '17.9.4a', status: 'up' },
  { vendor: 'cisco', model: 'C9300-24T', os_version: '17.9.4a', status: 'up' },
  { vendor: 'Cisco', model: 'C9300-48P', os_version: '9.1', status: 'down' },
  { vendor: 'Fortinet', model: 'FortiGate-100F', os_version: '7.2.8', status: 'up' },
  { vendor: 'Fortinet', model: 'FortiGate-100F', os_version: '10.2.4', status: 'up' },
  { vendor: 'Palo Alto Networks', model: 'PA-3220', os_version: '10.2.4', status: 'up' },
  { vendor: null, model: null, os_version: null, status: 'down' },
  { vendor: '  ', model: '', os_version: '', status: 'up' },
  { vendor: 'Unknown', model: null, os_version: '=cmd|x', status: 'up' },
]

// Facets: merged case variants, the commonest spelling, order, unknown last, counts that sum to the rows.
{
  const vendors = DI.buildFacet(FLEET, 'vendor')
  assert.deepEqual(vendors.map((o) => [o.value, o.label, o.count]), [
    ['Cisco', 'Cisco', 3], ['Fortinet', 'Fortinet', 2], ['Palo Alto Networks', 'Palo Alto Networks', 1], ['unknown', 'Unknown', 3],
  ])
  assert.equal(vendors.reduce((s, o) => s + o.count, 0), FLEET.length, 'every row is in exactly one option, unknown included')
  const versions = DI.buildFacet(FLEET, 'os_version')
  assert.equal(versions.at(-1).value, 'unknown')
  assert.equal(versions.at(-1).count, 2, 'a null and a blank version')
  assert.deepEqual(versions.slice(0, 2).map((o) => [o.value, o.count]), [['10.2.4', 2], ['17.9.4a', 2]], 'ties by name, numbers as numbers')
  // The principle: choosing an option leaves exactly its count in the list.
  for (const field of DI.IDENTITY_FIELDS) {
    for (const o of DI.buildFacet(FLEET, field)) {
      assert.equal(FLEET.filter((r) => DI.matchesIdentity(r, { [field]: o.value })).length, o.count, `${field}=${o.value}`)
    }
  }
  // With the other filter in force the counts follow it (the page narrows the rows before counting).
  const ciscoRows = FLEET.filter((r) => DI.matchesIdentity(r, { vendor: 'cisco' }))
  const inCisco = DI.buildFacet(ciscoRows, 'os_version')
  assert.deepEqual(inCisco.map((o) => [o.value, o.count]), [['17.9.4a', 2], ['9.1', 1]])
  for (const o of inCisco) assert.equal(FLEET.filter((r) => DI.matchesIdentity(r, { vendor: 'cisco', os_version: o.value })).length, o.count)
  // A selected value nobody has is still listed (count 0), so a dead filter can be seen and cleared.
  const dead = DI.buildFacet(ciscoRows, 'os_version', '99.0')
  assert.deepEqual(dead.find((o) => o.value === '99.0'), { value: '99.0', label: '99.0', count: 0 })
  assert.equal(DI.buildFacet([], 'vendor', 'unknown')[0].count, 0)
  assert.deepEqual(DI.buildFacet([], 'vendor'), [])
  // An address typed in another case selects the option it names.
  assert.equal(DI.selectedValue(vendors, 'CISCO'), 'Cisco')
  assert.equal(DI.selectedValue(vendors, 'Unknown'), 'unknown')
  assert.equal(DI.selectedValue(vendors, ''), '')
  assert.equal(DI.selectedValue(vendors, 'Nokia'), 'Nokia')
}

// Sorting: numbers compare as numbers, unknown is null (last in both directions in the list).
{
  const keys = ['10.2.4', '9.1', '17.9.4a', '7.2.8'].map(DI.identitySortValue)
  assert.deepEqual([...keys].sort(), ['9.1', '7.2.8', '10.2.4', '17.9.4a'].map(DI.identitySortValue).sort())
  const ordered = ['10.2.4', '9.1', '17.9.4a', '7.2.8'].sort((a, b) => (DI.identitySortValue(a) < DI.identitySortValue(b) ? -1 : 1))
  assert.deepEqual(ordered, ['7.2.8', '9.1', '10.2.4', '17.9.4a'])
  assert.equal(DI.identitySortValue(null), null)
  assert.equal(DI.identitySortValue('  '), null)
  assert.equal(DI.identitySortValue('Unknown'), null)
  assert.equal(DI.identitySortValue('Cisco'), DI.identitySortValue('cisco'))
}

// CSV: the three columns, with the formula guard on device-reported text.
{
  assert.deepEqual(DI.IDENTITY_CSV_COLUMNS.map(([name]) => name), ['vendor', 'model', 'os_version'])
  const cell = (name, row) => DI.IDENTITY_CSV_COLUMNS.find(([n]) => n === name)[1](row)
  assert.equal(cell('vendor', { vendor: 'Cisco' }), 'Cisco')
  assert.equal(cell('vendor', { vendor: null }), '', 'unknown is an empty cell')
  assert.equal(cell('model', { model: '  ' }), '')
  for (const bad of ['=HYPERLINK("http://x")', '+1+1', '-2+3', '@SUM(A1)']) {
    assert.equal(cell('os_version', { os_version: bad }), `'${bad}`, bad)
  }
  // Leading white space (a tab or CR would hide the formula from a quick look) is trimmed before the check.
  for (const bad of ['\t=1+1', '\r=1+1', ' =1+1']) assert.equal(cell('os_version', { os_version: bad }), "'=1+1", JSON.stringify(bad))
  assert.equal(cell('os_version', { os_version: '\tx' }), 'x')
  assert.equal(cell('os_version', { os_version: '17.9.4a' }), '17.9.4a')
  assert.equal(cell('os_version', { os_version: 'v7.2.5,build1517 (GA.F)' }), 'v7.2.5,build1517 (GA.F)', 'commas stay for the page to quote')
}

// The device OS link.
{
  const base = { kind: 'device', os_family: 'fortios', os_product: 'FortiOS', os_release: '7.2', os_version: '7.2.8', os_source: 'template' }
  const link = DI.deviceOsLink(base)
  assert.equal(link.row, 'os')
  assert.equal(link.href, '/inventory/assets?kind=device&os_family=fortios&os_product=FortiOS&os_release=7.2')
  assert.equal(link.release, 'FortiOS 7.2')
  // The href is inside the asset vocabulary: the Assets page reads it back without rejecting anything.
  const parsed = F.parseAssetFilters(new URL(link.href, 'http://x').searchParams)
  assert.deepEqual(parsed.rejected, [])
  assert.deepEqual(parsed.filters, { kind: 'device', os_family: 'fortios', os_product: 'FortiOS', os_release: '7.2' })
  // The row that was the evidence carries the link.
  assert.equal(DI.deviceOsLink({ ...base, os_source: 'entity' }).row, 'firmware')
  for (const s of ['profile', 'template', 'template_stale', 'sysdescr', null]) assert.equal(DI.deviceOsLink({ ...base, os_source: s }).row, 'os', String(s))
  // Unknown is a value in the list, so a known family with no product or release still has a list behind it.
  assert.equal(
    DI.deviceOsLink({ ...base, os_family: 'other', os_product: null, os_release: null }).href,
    '/inventory/assets?kind=device&os_family=other&os_product=unknown&os_release=unknown',
  )
  // Nothing to filter by, nothing to link.
  assert.equal(DI.deviceOsLink({ ...base, os_family: 'unknown' }), null)
  assert.equal(DI.deviceOsLink({ ...base, os_family: '' }), null)
  assert.equal(DI.deviceOsLink({ ...base, kind: 'server' }), null, 'a server is not a device')
  assert.equal(DI.deviceOsLink(null), null)
  assert.equal(DI.deviceOsLink(undefined), null)
}

// RG-11: the OS version a list shows, sorts, filters and exports is os_version_display, then the device's own column.
{
  assert.equal(DI.identityValue({ os_version: 'v7.2.8,build1639', os_version_display: '7.2.8' }, 'os_version'), '7.2.8')
  assert.equal(DI.identityValue({ os_version: ' 9.1 ', os_version_display: null }, 'os_version'), ' 9.1 ', 'the device\'s own value when the API sent none')
  assert.equal(DI.identityValue({ os_version: null, os_version_display: '17.1.1' }, 'os_version'), '17.1.1')
  assert.equal(DI.identityValue({ os_version: null }, 'os_version'), null)
  assert.equal(DI.identityValue({ vendor: 'Cisco', os_version_display: 'x' }, 'vendor'), 'Cisco', 'vendor and model are the device\'s own columns')
  const tpl = { vendor: 'F5', os_version: '', os_version_display: '17.1.1' }
  assert.equal(DI.matchesIdentity(tpl, { os_version: '17.1.1' }), true, 'the filter matches the displayed version')
  assert.equal(DI.matchesIdentity(tpl, { os_version: 'unknown' }), false, 'a device Inventory knows the version of is not unknown (RG-10, RG-11)')
  assert.deepEqual(DI.buildFacet([tpl, { os_version: '17.1.1' }, { os_version: null }], 'os_version').map((o) => [o.value, o.count]), [['17.1.1', 2], ['unknown', 1]])
  assert.equal(DI.identitySortValue(DI.identityValue({ os_version: 'banner 1', os_version_display: '10.2' }, 'os_version')), DI.identitySortValue('10.2'))
}

// RG-03, RG-10: the request. The filters the API applies exactly are sent, with the facets over ALL devices; unknown goes as it is.
{
  assert.deepEqual(DI.deviceListParams({}), { limit: '200', facets: 'true' })
  assert.equal(DI.DEVICE_LIST_LIMIT, 200)
  assert.deepEqual(
    DI.deviceListParams({ search: ' sw ', vendor: 'Cisco', os_version: 'unknown', type: 'switch', group: 'g1', location: 'DC1', managedBy: 'c1', tags: ['a', ' b '], tagMode: 'all' }),
    { limit: '200', facets: 'true', search: 'sw', vendor: 'Cisco', os_version: 'unknown', device_type: 'switch', group_id: 'g1', location: 'DC1', managed_by: 'c1', tags: 'a,b', tags_match: 'all' },
  )
  assert.equal(DI.deviceListParams({ tags: ['a'], tagMode: 'any' }).tags_match, undefined, 'any is the API default')
  assert.deepEqual(DI.deviceListParams({ vendor: '  ', os_version: '' }), { limit: '200', facets: 'true' }, 'a blank filter is not sent')
  assert(!('status' in DI.deviceListParams({ search: 'x' })), 'the health status chip is derived in the browser: it is never sent as the API\'s status')
  const read = DI.deviceListFiltersFromParams(new URLSearchParams('q=old&vendor=cisco&os_version=unknown&type=switch&group=g1&loc=DC1&managed_by=c1&tag=a,b&tagmode=all&status=critical'))
  assert.deepEqual(read, { search: 'old', vendor: 'cisco', os_version: 'unknown', type: 'switch', group: 'g1', location: 'DC1', managedBy: 'c1', tags: ['a', 'b'], tagMode: 'all' })
  assert.equal(DI.deviceListFiltersFromParams(new URLSearchParams('search=new&q=old')).search, 'new', 'search wins over the older q')
  // The first N of M.
  assert.deepEqual(DI.listCoverage(200, 284), { truncated: true, text: `first ${(200).toLocaleString()} of ${(284).toLocaleString()}` })
  assert(DI.listCoverage(200, 284).text.startsWith('first ') && DI.listCoverage(200, 284).text.includes(' of '), 'the sentence is the same in any locale; only the digits follow the browser (NF-EP-06)')
  assert.deepEqual(DI.listCoverage(284, 284), { truncated: false, text: null })
  assert.deepEqual(DI.listCoverage(3, undefined), { truncated: false, text: null })
  assert.deepEqual(DI.listCoverage(3, null), { truncated: false, text: null })
  assert.equal(DI.listCoverage(1234, 56789).text, `first ${(1234).toLocaleString()} of ${(56789).toLocaleString()}`, 'formatted for the browser, like every other number of the page')
}

// RG-03: the menu is the API's facet (over ALL devices) when the browser applies no filter of its own, else the loaded rows' counts.
{
  const server = { items: [{ value: 'Cisco', label: 'Cisco', count: 123 }, { value: 'unknown', label: 'Unknown', count: 150 }, { value: 'Fortinet', label: 'Fortinet', count: 11 }], distinct: 3 }
  const rows = [{ vendor: 'Cisco' }, { vendor: 'Cisco' }, { vendor: null }]
  const all = DI.facetMenu({ field: 'vendor', server, rows, useServer: true })
  assert.equal(all.basis, 'all')
  assert.deepEqual(all.options.map((o) => [o.value, o.count]), [['Cisco', 123], ['Fortinet', 11], ['unknown', 150]], 'the API\'s counts; Unknown is last whatever its count')
  const loaded = DI.facetMenu({ field: 'vendor', server, rows, useServer: false })
  assert.equal(loaded.basis, 'loaded')
  assert.deepEqual(loaded.options.map((o) => [o.value, o.count]), [['Cisco', 2], ['unknown', 1]], 'the loaded rows\' counts when the API cannot reproduce a filter')
  assert.equal(DI.facetMenu({ field: 'vendor', server: undefined, rows, useServer: true }).basis, 'loaded', 'no facet in the response: the rows')
  // A selected value is always listed: count 0 when the facet is complete, no count when the API cut it off.
  assert.deepEqual(DI.facetMenu({ field: 'vendor', server, rows, selected: 'Nokia', useServer: true }).options.find((o) => o.value === 'Nokia'), { value: 'Nokia', label: 'Nokia', count: 0 })
  const capped = DI.facetMenu({ field: 'vendor', server: { ...server, distinct: 4000 }, rows, selected: 'Nokia', useServer: true }).options.find((o) => o.value === 'Nokia')
  assert.deepEqual(capped, { value: 'Nokia', label: 'Nokia' }, 'a count would be a guess')
  assert.equal(DI.facetMenu({ field: 'vendor', server, rows, selected: 'CISCO', useServer: true }).options.filter((o) => o.value.toLowerCase() === 'cisco').length, 1, 'no duplicate for another spelling')
  assert.equal(DI.facetMenu({ field: 'vendor', server: { items: [], distinct: 0 }, rows, selected: 'Unknown', useServer: true }).options[0].value, 'unknown')
}

// RG-08: the CSV keeps the positions the columns always had; the identity columns are appended.
{
  const legacy = ['hostname', 'ip_address', 'managed_via', 'serial_number', 'device_type', 'group', 'location', 'tags', 'status', 'cpu_pct', 'memory_pct', 'uptime', 'last_seen']
  assert.deepEqual([...DI.LEGACY_CSV_HEADERS], legacy, 'the constant is the legacy order')
  const row = (over = {}) => ({
    device: { hostname: 'sw-a', ip_address: '10.0.0.1', managed_ip: null, managed_by_hostname: 'ctl', serial_number: 'SN1', device_type: 'switch', group_name: 'Core', location: 'DC1', tags: ['a', 'b'], last_seen: '2026-10-08T11:59:00Z', vendor: 'Cisco', model: 'C9300-48P', os_version: 'banner', os_version_display: '17.9.4a', ...over },
    health: 'healthy', snmpNoData: false, cpu: 12, mem: null, uptime: '3d 2h',
  })
  const csv = DI.buildDevicesCsv([row(), row({ hostname: 'x,1', vendor: '=HYPERLINK("x")', model: null, os_version: null, os_version_display: null, tags: [] })])
  const lines = csv.split('\n')
  assert.deepEqual(lines[0].split(','), [...legacy, 'vendor', 'model', 'os_version'], 'legacy headers first, in order; the three new ones at the end')
  const first = lines[1].split(',')
  assert.deepEqual(first.slice(0, 13), ['sw-a', '10.0.0.1', 'ctl', 'SN1', 'switch', 'Core', 'DC1', 'a; b', 'healthy', '12', '', '3d 2h', '2026-10-08T11:59:00Z'], 'every legacy value sits where it always did')
  assert.deepEqual(first.slice(13), ['Cisco', 'C9300-48P', '17.9.4a'], 'the exported version is the one the list shows')
  assert.equal(first.length, 16)
  assert(lines[2].startsWith('"x,1",'), 'a comma is quoted')
  assert(lines[2].includes(`"'=HYPERLINK(""x"")"`), 'a device-reported formula is neutralised and quoted')
  assert(lines[2].endsWith(',,'), 'unknown model and version are empty cells')
  assert.deepEqual(DI.devicesCsvColumns().map(([h]) => h).slice(0, 13), legacy)
  assert.equal(DI.csvEscape(null), '')
  assert.equal(DI.csvEscape('a"b'), '"a""b"')
}

/* ═══ rendering helpers ═════════════════════════════════════════════════════════════════════ */

const USER = (permissions) => ({ id: 'u', username: 'u', email: 'u@x', role: 'custom', permissions })
function setUser(user) {
  // Server rendering reads a zustand store's initial state, so the user goes there as well as into the live state.
  m.useAuth.setState({ user, token: 't' })
  const initial = typeof m.useAuth.getInitialState === 'function' ? m.useAuth.getInitialState() : null
  if (initial) Object.assign(initial, { user, token: 't' })
}
const newClient = () => new m.QueryClient({ defaultOptions: { queries: { retry: false, retryOnMount: false } } })
const renderAt = (qc, url, element) =>
  m.renderToStaticMarkup(h(m.QueryClientProvider, { client: qc }, h(m.MemoryRouter, { initialEntries: [url] }, h(m.Routes, null, h(m.Route, { path: '*', element })))))
const strip = (html) => html.replace(/<[^>]*>/g, '').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim()
const rowsOf = (html) => {
  const body = html.split('<tbody')[1]?.split('</tbody>')[0] ?? ''
  return [...body.matchAll(/<tr[^>]*>(.*?)<\/tr>/gs)].map((r) => [...r[1].matchAll(/<td[^>]*>(.*?)<\/td>/gs)].map((c) => strip(c[1])))
}
const headersOf = (html) => [...(html.split('<thead')[1]?.split('</thead>')[0] ?? '').matchAll(/<th[^>]*>(.*?)<\/th>/gs)].map((c) => strip(c[1]))

/* ═══ (b) the Devices list ═════════════════════════════════════════════════════════════════ */

const device = (id, hostname, over = {}) => ({
  id, hostname, ip_address: `10.0.0.${id.length}${id.charCodeAt(id.length - 1) % 10}`, device_type: 'switch', location: null, group_id: null,
  group_name: null, tags: [], status: 'up', last_seen: '2026-10-08T11:59:00Z', last_rtt_ms: 1.2, ping_enabled: true, snmp_enabled: false,
  vendor: null, model: null, os_version: null, ...over,
})
const DEVICES = [
  device('d1', 'sw-a', { vendor: 'Cisco', model: 'C9300-48P', os_version: '17.9.4a' }),
  device('d2', 'sw-b', { vendor: 'cisco', model: 'C9300-24T', os_version: '17.9.4a' }),
  device('d3', 'sw-c', { vendor: 'Cisco', model: 'C9300-48P', os_version: '9.1', status: 'down' }),
  device('d4', 'fg-a', { vendor: 'Fortinet', model: 'FortiGate-100F', os_version: '7.2.8', device_type: 'firewall' }),
  device('d5', 'fg-b', { vendor: 'Fortinet', model: 'FortiGate-100F', os_version: '10.2.4', device_type: 'firewall' }),
  device('d6', 'pa-a', { vendor: 'Palo Alto Networks', model: 'PA-3220', os_version: '10.2.4', device_type: 'firewall' }),
  device('d7', 'x-1', { status: 'down' }),
  device('d8', 'x-2', { vendor: '  ', model: '', os_version: '' }),
  device('d9', 'w-1', { vendor: 'Unknown', os_version: '=cmd|x' }),
]
// The API adds `os_version_display` to every item: the normalised version of the Inventory identity, else the device's own.
for (const d of DEVICES) d.os_version_display = d.os_version && d.os_version.trim() ? d.os_version.trim() : null
// RG-11: a device whose version comes from a monitoring template has no os_version of its own; Inventory knows it.
DEVICES.find((d) => d.id === 'd7').os_version_display = '17.1.1'
// ...and one whose raw string is a banner the normaliser reads as 17.9.4a.
Object.assign(DEVICES.find((d) => d.id === 'd1'), { os_version: 'Cisco IOS XE Software, Version 17.9.4a', os_version_display: '17.9.4a' })

// The query key of the list for an address: the request the page makes for it (the page and this test share the parser).
const listKey = (url) => ['devices', 'list', DI.deviceListParams(DI.deviceListFiltersFromParams(new URL(url, 'http://x').searchParams))]

function devicesPage(url, { devices = DEVICES, permissions = ['devices.view'], stored, total, facets } = {}) {
  setUser(USER(permissions))
  globalThis.localStorage = {
    getItem: (k) => (k === 'zp-devices-prefs-v3' ? (stored === undefined ? null : JSON.stringify(stored)) : null),
    setItem() {}, removeItem() {},
  }
  const qc = newClient()
  qc.setQueryData(listKey(url), { data: devices, meta: { total: total ?? devices.length }, ...(facets ? { facets } : {}) })
  const html = renderAt(qc, url, h(m.DevicesPage))
  delete globalThis.localStorage
  return html
}
// The hostname links of the rows, in order (the name cell also holds the IP).
const hostnames = (html) => [...html.matchAll(/<a ([^>]*)>([^<]*)<\/a>/g)]
  .filter((a) => /href="\/devices\/[^"/]+"/.test(a[1]) && a[1].includes(`title="${a[2]}"`))
  .map((a) => a[2])

{
  const html = devicesPage('/devices')
  const heads = headersOf(html)
  // RG-02 / UI-17: the identity columns are opt-in (Columns). On by default they push Status, CPU and Memory off the
  // first screen: measured in the mock harness, the OS version alone (90 px) moves Status past the edge of the table box at 1280 px.
  assert.deepEqual(heads.slice(1, 7), ['Device Name/IP', 'Type', 'Group / Location', 'Tags', 'Status', 'CPU'], `headers: ${heads}`)
  assert.deepEqual(heads.slice(7), ['Memory', 'Uptime (24h)', 'Last Seen', 'Actions'], 'the existing columns are where they were')
  assert(!['Vendor', 'Model', 'OS version'].some((c) => heads.includes(c)), 'none of the three is on by default')
  const rows = rowsOf(html)
  assert.equal(rows.length, DEVICES.length)
  assert.equal(rows[0].length, heads.length, 'every row has a cell under every header')
  // The vendor / OS version filters live in the extra filters panel: closed by default.
  assert(!html.includes('All vendors'))
  assert(!html.includes('Filtered:'))
  assert(!html.includes('data-list-coverage'), 'a complete list says nothing about being cut short')

  // Opting in shows them, with their cells and sort buttons.
  const all = devicesPage('/devices', { stored: { visible: { vendor: true, model: true, os_version: true }, pageSize: 25 } })
  const allHeads = headersOf(all)
  assert.deepEqual(allHeads.slice(1, 8), ['Device Name/IP', 'Type', 'Vendor', 'Model', 'OS version', 'Group / Location', 'Tags'])
  const allRows = rowsOf(all)
  const at = (name, col) => allRows.find((r) => r[1].startsWith(name))[allHeads.indexOf(col)]
  assert.equal(at('sw-a', 'Vendor'), 'Cisco')
  assert.equal(at('sw-a', 'Model'), 'C9300-48P')
  assert.equal(at('sw-b', 'OS version'), '17.9.4a')
  assert.match(at('x-1', 'Vendor'), /Not reported/, 'unknown is said, not left empty')
  assert.match(at('x-2', 'OS version'), /Not reported/, 'a blank is unknown too')
  assert.match(at('w-1', 'Vendor'), /Not reported/, 'a vendor literally called Unknown says nothing: the same bucket as the filter\'s Unknown')
  assert.equal(at('w-1', 'OS version'), '=cmd|x', 'the page shows text as it is; only the CSV guards it')
  // RG-11: the version shown is the normalised one when Inventory has it, even where the device's own column is empty.
  assert.equal(at('x-1', 'OS version'), '17.1.1', 'os_version_display wins over an empty os_version')
  assert.equal(at('sw-a', 'OS version'), '17.9.4a', 'and over a raw banner string')
  assert(all.includes('title="17.9.4a"'), 'the full value is in the tooltip of a clipped cell')
  assert.equal(allRows[0].length, allHeads.length)
  for (const label of ['Vendor', 'Model', 'OS version']) assert(new RegExp(`<button[^>]*>${label}</button>`).test(all), `${label} sorts`)
}

// Saved column preferences (RG-02, UI-17): a choice saved before the identity columns existed keeps its layout.
{
  const OLD = { visible: { type: true, group_location: false, tags: true, cpu: false, memory: false, uptime: true, last_seen: true }, pageSize: 25 }
  const old = devicesPage('/devices', { stored: OLD })
  const heads = headersOf(old)
  assert(['Vendor', 'Model', 'OS version'].every((c) => !heads.includes(c)), 'an old choice does not grow three columns under its owner')
  assert(!heads.includes('Group / Location') && !heads.includes('CPU'), 'and still hides what it hid')
  assert.deepEqual(heads.slice(1), ['Device Name/IP', 'Type', 'Tags', 'Status', 'Uptime (24h)', 'Last Seen', 'Actions'], `old choice: ${heads}`)
  const some = devicesPage('/devices', { stored: { visible: { vendor: false, model: true, os_version: false }, pageSize: 10 } })
  const sh = headersOf(some)
  assert(!sh.includes('Vendor') && sh.includes('Model') && !sh.includes('OS version'), 'each added column can be chosen on its own')
  assert.equal(rowsOf(some)[0].length, sh.length, 'the cells follow the headers')
  // A choice that names an added column keeps it; one that does not leaves it off; garbage falls back to the defaults.
  assert(headersOf(devicesPage('/devices', { stored: { visible: { os_version: true } } })).includes('OS version'))
  assert(!headersOf(devicesPage('/devices', { stored: { visible: { os_version: false } } })).includes('OS version'))
  for (const bad of [null, [], 'x', { visible: null }, { visible: [] }, { visible: 'x' }]) {
    assert.doesNotThrow(() => devicesPage('/devices', { stored: bad }), JSON.stringify(bad))
  }
  // The rule itself.
  const defaults = { type: true, vendor: false, model: false, os_version: true, cpu: true }
  assert.deepEqual(DI.resolveColumnVisibility(defaults, undefined, DI.ADDED_COLUMNS), defaults, 'no saved choice: the defaults')
  assert.deepEqual(DI.resolveColumnVisibility(defaults, null, DI.ADDED_COLUMNS), defaults)
  assert.deepEqual(DI.resolveColumnVisibility(defaults, { type: false, cpu: false }, DI.ADDED_COLUMNS), { type: false, vendor: false, model: false, os_version: false, cpu: false }, 'a saved choice: what it saved, and the added columns off')
  assert.deepEqual(DI.resolveColumnVisibility(defaults, { os_version: true, vendor: true }, DI.ADDED_COLUMNS), { type: true, vendor: true, model: false, os_version: true, cpu: true }, 'named added columns keep their value; a column that is not an added one takes the default')
  assert.deepEqual(DI.resolveColumnVisibility(defaults, { type: 'no', cpu: 1 }, DI.ADDED_COLUMNS), { type: true, vendor: false, model: false, os_version: false, cpu: true }, 'only booleans count')
  assert.deepEqual([...DI.ADDED_COLUMNS], ['vendor', 'model', 'os_version'])
}

// Filters in the address bar.
{
  const html = devicesPage('/devices?vendor=cisco')
  assert.deepEqual(hostnames(html), ['sw-a', 'sw-b', 'sw-c'], 'case-insensitive, merged spellings')
  assert(html.includes('<span class="truncate font-medium">Cisco</span>'), 'the menu shows the option the address selects')
  assert(strip(html).includes(`Filtered: ${(3).toLocaleString()} of ${(9).toLocaleString()} devices that match the other filters`), 'the KPI line says how many remain')
  assert(html.includes('Clear (1)'))
  assert(html.includes('cards and charts reflect the filter'))

  const unknown = devicesPage('/devices?vendor=unknown')
  assert.deepEqual(hostnames(unknown), ['w-1', 'x-1', 'x-2'], 'unknown: no value, blank, and the word itself')
  assert(unknown.includes('<span class="truncate font-medium">Unknown</span>'))

  const both = devicesPage('/devices?vendor=cisco&os_version=17.9.4a')
  assert.deepEqual(hostnames(both), ['sw-a', 'sw-b'])
  assert(both.includes('Clear (2)'))
  assert.deepEqual(hostnames(devicesPage('/devices?os_version=10.2.4')), ['fg-b', 'pa-a'])
  assert.deepEqual(hostnames(devicesPage('/devices?os_version=unknown')), ['x-2'], 'x-1 has a normalised version (RG-11), so it is not unknown')
  assert.deepEqual(hostnames(devicesPage('/devices?os_version=17.1.1')), ['x-1'], 'and the filter finds it by that version')
  // A filter nobody matches is a visible, clearable state, not a hidden one.
  const none = devicesPage('/devices?vendor=Nokia')
  assert.deepEqual(hostnames(none), [])
  assert(none.includes('<span class="truncate font-medium">Nokia</span>') && none.includes('No devices match'))
  // NF-EP-03: the API applies these filters, so an empty list no longer means "no devices": the hint follows what narrows it.
  const apiNone = devicesPage('/devices?vendor=Nokia', { devices: [] })
  assert(strip(apiNone).includes('No devices match') && strip(apiNone).includes('Try clearing filters') && !strip(apiNone).includes('run a discovery sweep'), 'a filter that matches nothing says so, not "add a device"')
  assert(strip(devicesPage('/devices?type=nonexistent', { devices: [] })).includes('Try clearing filters'))
  assert(strip(devicesPage('/devices?tag=nonexistent', { devices: [] })).includes('Try clearing filters'))
  const noSearch = strip(devicesPage('/devices?search=zzzz', { devices: [] }))
  assert(noSearch.includes('Try a different search') && !noSearch.includes('discovery sweep'), 'a search that matches nothing')
  assert(strip(devicesPage('/devices?search=zzzz&vendor=Nokia', { devices: [] })).includes('Try clearing filters or changing the search'))
  assert(strip(devicesPage('/devices', { devices: [] })).includes('run a discovery sweep'), 'an empty estate still points at discovery')
  // ...and the filtered line is honest when the API did the narrowing (no "123 of 123").
  const narrowed = strip(devicesPage('/devices?vendor=cisco', { devices: DEVICES.filter((d) => d.vendor?.toLowerCase() === 'cisco'), total: 3 }))
  assert(narrowed.includes(`Filtered: ${(3).toLocaleString()} devices`) && !narrowed.includes(`Filtered: ${(3).toLocaleString()} of ${(3).toLocaleString()}`), narrowed.slice(0, 400))
  // The status chip is the browser's: the cut-short notice says the status filter applies to the loaded rows only.
  const statusCut = strip(devicesPage('/devices?vendor=cisco&status=healthy', { total: 284 }))
  assert(statusCut.includes(`devices that match the other filters, in name order`) && statusCut.includes('status and location filters are applied to these devices only'), statusCut.slice(0, 600))
  // NF-EP-02: a controller's children are listed without the controller; the banner still names it.
  const wlcKids = [device('k1', 'ap-1', { managed_by_device_id: 'c1', managed_by_hostname: 'wlc-main' }), device('k2', 'ap-2', { managed_by_device_id: 'c1', managed_by_hostname: 'wlc-main' })]
  const bannerOf = (html) => html.match(/Showing devices managed by<\/span><a [^>]*href="\/devices\/c1"[^>]*>([^<]*)<\/a>/)?.[1]
  assert.equal(bannerOf(devicesPage('/devices?managed_by=c1', { devices: wlcKids })), 'wlc-main', 'NF-EP-02: the controller keeps its name')
  assert.equal(bannerOf(devicesPage('/devices?managed_by=c1', { devices: [] })), 'controller', 'and a generic word only when nothing can name it')
  // NF-5: every combobox and icon button has a name (a combobox takes none from its content).
  const named = devicesPage('/devices')
  for (const label of ['Status: All', 'Type: All', 'Location: All', 'Rows per page', 'Previous page', 'Next page', 'More filters']) assert(named.includes(`aria-label="${label}"`), `NF-5: ${label}`)
  assert(devicesPage('/devices?status=critical').includes('aria-label="Status: Critical"'), 'the name carries the selected value')
  const unnamed = [...named.matchAll(/<button\b[^>]*role="combobox"[^>]*>/g)].filter((b) => !/aria-label=/.test(b[0]))
  assert.equal(unnamed.length, 0, 'no combobox without a name')
  // The status filter and the others compose with the new ones.
  assert.deepEqual(hostnames(devicesPage('/devices?vendor=cisco&status=healthy')), ['sw-a', 'sw-b'])
  assert.deepEqual(hostnames(devicesPage('/devices?vendor=cisco&status=critical')), ['sw-c'])
  assert.deepEqual(hostnames(devicesPage('/devices?vendor=fortinet&type=firewall')), ['fg-a', 'fg-b'])
  assert.deepEqual(hostnames(devicesPage('/devices?status=bogus')), DEVICES.map((d) => d.hostname).sort(), 'an unrecognised status still keeps everything, as before')
}

// RG-03: more devices than one page holds. The menus count ALL of them (the API's facets) and the page says what it shows.
{
  const facets = {
    vendor: { items: [{ value: 'Cisco', label: 'Cisco', count: 123 }, { value: 'Fortinet', label: 'Fortinet', count: 60 }, { value: 'unknown', label: 'Unknown', count: 101 }], distinct: 3 },
    model: { items: [], distinct: 0 },
    os_version: { items: [{ value: '17.9.4a', label: '17.9.4a', count: 90 }, { value: 'unknown', label: 'Unknown', count: 150 }], distinct: 2 },
  }
  const open = (query, over = {}) => devicesPage(`/devices?${query}`, { devices: DEVICES, total: 284, facets, ...over })
  const cut = open('vendor=cisco')
  assert(cut.includes('data-list-coverage'), 'the list says it is cut short')
  assert(strip(cut).includes(`Showing the first ${DEVICES.length.toLocaleString()} of ${(284).toLocaleString()} devices that match, in name order.`), 'first N of M')
  assert(strip(cut).includes(`Filtered: ${(3).toLocaleString()} of ${DEVICES.length.toLocaleString()} devices that match the other filters (first ${DEVICES.length.toLocaleString()} of ${(284).toLocaleString()} loaded)`))
  assert(!strip(cut).includes('count only the devices shown here'), 'with nothing but identity filters the menus are exact: no caveat')
  // A status chip is derived in the browser: the menus fall back to the loaded rows and say so.
  const withStatus = open('vendor=cisco&status=healthy')
  assert(strip(withStatus).includes('count only the devices shown here'), 'the caveat appears with a status filter on')
  const withLoc = open('loc=DC1')
  assert(strip(withLoc).includes('count only the devices shown here'), 'and with a location filter on (the API matches it as a fragment)')
  // Nothing is cut short: no notice, whatever the facets say.
  const whole = devicesPage('/devices', { devices: DEVICES, total: DEVICES.length, facets })
  assert(!whole.includes('data-list-coverage'))
  // The request that produced the list carries the filters, the limit and the facets flag.
  const k = listKey('/devices?vendor=cisco&os_version=unknown&type=switch')
  assert.deepEqual(k[2], { limit: '200', facets: 'true', vendor: 'cisco', os_version: 'unknown', device_type: 'switch' })
}

// The number in a menu is the list's total once chosen, over the real page: every vendor option, with a status in force.
{
  for (const query of ['', '?status=healthy', '?type=firewall', '?os_version=10.2.4']) {
    const filters = Object.fromEntries(new URLSearchParams(query))
    const visible = DEVICES.filter((d) => (!filters.status || (filters.status === 'healthy' ? d.status === 'up' : d.status === 'down')) && (!filters.type || d.device_type === filters.type) && DI.matchesIdentity(d, { os_version: filters.os_version }))
    for (const o of DI.buildFacet(visible, 'vendor')) {
      const url = `/devices?${new URLSearchParams({ ...filters, vendor: o.value })}`
      assert.equal(hostnames(devicesPage(url)).length, o.count, `${url}: ${o.label}`)
    }
  }
}

// Sorting by the new columns: ascending and descending, unknown always last, numbers as numbers.
{
  const order = (q) => hostnames(devicesPage(`/devices?${q}`))
  const known = (names) => names.filter((n) => !['x-1', 'x-2'].includes(n))
  const vendorAsc = order('sort=vendor&order=asc')
  const vendorDesc = order('sort=vendor&order=desc')
  assert.deepEqual(vendorAsc.slice(0, 6), ['sw-a', 'sw-b', 'sw-c', 'fg-a', 'fg-b', 'pa-a'], 'Cisco, Fortinet, Palo Alto (ties keep the list order)')
  assert.deepEqual(vendorDesc.slice(0, 6), ['pa-a', 'fg-a', 'fg-b', 'sw-a', 'sw-b', 'sw-c'])
  for (const list of [vendorAsc, vendorDesc]) assert.deepEqual(new Set(list.slice(-3)), new Set(['x-1', 'x-2', 'w-1']), 'unknown vendors are last either way')
  const osAsc = order('sort=os_version&order=asc')
  assert.deepEqual(osAsc.slice(0, 7), ['fg-a', 'sw-c', 'fg-b', 'pa-a', 'x-1', 'sw-a', 'sw-b'], '7.2.8, 9.1, 10.2.4, 10.2.4, 17.1.1 (the normalised version of x-1), 17.9.4a, 17.9.4a')
  const osDesc = order('sort=os_version&order=desc')
  assert.deepEqual(osDesc.slice(1, 3), ['sw-a', 'sw-b'], '17.9.4a is above 10.2.4 and 9.1 when descending')
  assert.equal(osAsc.at(-1), 'x-2', 'unknown versions are last ascending')
  assert.equal(osDesc.at(-1), 'x-2', 'and descending')
  assert.equal(order('sort=model&order=asc')[0], 'sw-b', 'C9300-24T first')
  assert.deepEqual(known(order('sort=hostname&order=asc')), known(DEVICES.map((d) => d.hostname)).sort(), 'the default sort is unchanged')
}

// Empty and loading: the placeholder rows span every visible column.
{
  const empty = devicesPage('/devices', { devices: [] })
  assert(/colspan="11"/i.test(empty), 'the checkbox, name, type, group, tags, status, CPU, memory, uptime, last seen and actions')
  const all = devicesPage('/devices', { devices: [], stored: { visible: { vendor: true, model: true, os_version: true } } })
  assert(/colspan="14"/i.test(all), 'the span follows the columns shown: all fourteen')
  const fewer = devicesPage('/devices', { devices: [], stored: { visible: { type: false, tags: false, vendor: false, model: false, os_version: false } } })
  assert(/colspan="9"/i.test(fewer))
}

/* ═══ (c) the device detail Inventory & Configuration card ═════════════════════════════════ */

const DEVICE = { id: 'dev-1', hostname: 'fg-01', snmp_enabled: false, serial_number: 'FG100FTK2000', os_version: 'v7.2.8,build1639', firmware_version: 'v7.2.8', profile_name: 'FortiGate', tags: [], sys_object_id: '1.3.6.1.4.1.12356', updated_at: '2026-10-08T11:00:00Z' }
const ASSET = (over = {}) => ({ asset: { key: 'device:dev-1', kind: 'device', id: 'dev-1', os_family: 'fortios', os_product: 'FortiOS', os_release: '7.2', os_version: '7.2.8', os_source: 'profile', ...over } })
function card({ permissions = ['devices.view', 'inventory.view'], asset = ASSET(), failed = false, device = DEVICE } = {}) {
  setUser(USER(permissions))
  const qc = newClient()
  if (failed) qc.getQueryCache().build(qc, { queryKey: ['inventory', 'asset', `device:${device.id}`] }).setState({ status: 'error', error: new Error('404'), data: undefined, fetchStatus: 'idle', errorUpdateCount: 1 })
  else if (asset) qc.setQueryData(['inventory', 'asset', `device:${device.id}`], asset)
  return renderAt(qc, '/devices/dev-1', h(m.InventoryConfigCard, { device, entities: [], snmpHasData: false, onDetails() {} }))
}
// The value cell (inner markup) of one row of the card, found by its label.
const cardRow = (html, label) => {
  const at = html.indexOf(`</svg>${label}</span>`)
  if (at < 0) return ''
  const start = html.indexOf('<span class="min-w-0 break-words text-right font-medium">', at)
  return html.slice(start, html.indexOf('</div>', start))
}
const LINK = '/inventory/assets?kind=device&amp;os_family=fortios&amp;os_product=FortiOS&amp;os_release=7.2'

{
  const html = card()
  const os = cardRow(html, 'OS / version')
  const fw = cardRow(html, 'Firmware')
  assert(os.includes(`href="${LINK}"`), 'the OS / version value links to the release list')
  assert.equal(strip(os), 'v7.2.8,build1639')
  assert(os.includes('Open the network devices running FortiOS 7.2 in Inventory'))
  assert(!fw.includes('<a '), 'the Firmware row is not the evidence here')
  assert.equal(strip(fw), 'v7.2.8')
  // Everything else on the card is as it was.
  for (const [label, value] of [['Serial number', 'FG100FTK2000'], ['SNMP', 'Disabled'], ['Ping', 'Disabled'], ['System OID', '1.3.6.1.4.1.12356'], ['Hardware', '—']]) {
    assert.equal(strip(cardRow(html, label)), value, label)
  }
  assert(html.includes('href="/settings/general?tab=templates"'), 'the monitoring template link is kept')

  // The chassis firmware was the evidence: the Firmware row carries the link instead.
  const entity = card({ asset: ASSET({ os_source: 'entity' }) })
  assert(cardRow(entity, 'Firmware').includes(`href="${LINK}"`) && !cardRow(entity, 'OS / version').includes('<a '))

  // Without inventory.view: plain text, whatever the cache holds, and no request is made for it.
  const plain = card({ permissions: ['devices.view'] })
  assert(!plain.includes('/inventory/') && strip(cardRow(plain, 'OS / version')) === 'v7.2.8,build1639')
  // With the permission but nothing known yet (loading), an error (404: an SNMP twin or out of scope), or an unidentified OS.
  for (const html2 of [card({ asset: null }), card({ failed: true }), card({ asset: ASSET({ os_family: 'unknown', os_product: null, os_release: null }) })]) {
    assert(!html2.includes('/inventory/assets'), 'no link')
    assert.equal(strip(cardRow(html2, 'OS / version')), 'v7.2.8,build1639', 'the value is kept')
  }
  // A value that was not reported is never a link, and the fallback words are kept.
  const none = card({ device: { ...DEVICE, os_version: null, firmware_version: null } })
  assert(!none.includes('/inventory/assets'))
  assert.equal(strip(cardRow(none, 'OS / version')), 'Not reported')
  assert.equal(strip(cardRow(none, 'Firmware')), 'Not reported')
  // system.admin holds every permission.
  assert(card({ permissions: ['system.admin'] }).includes(`href="${LINK}"`))
}

/* ═══ (d) the server Software tab ═══════════════════════════════════════════════════════════ */

const SERVER = { id: 'srv-1', display_name: 'srv-w22-01', collection_mode: 'agent', last_seen: '2026-10-08T11:59:00Z', physical_disks: [] }
const pkg = (name, key, over = {}) => ({ package_name: name, version: '1.0', vendor: 'Acme', install_date: '20260101', updated_at: '2026-10-08T10:00:00Z', product_key: key, category: key ? 'other' : null, ...over })
const SOFTWARE = {
  items: [pkg('Git', 'git'), pkg('Google Chrome', 'google-chrome'), pkg('Odd / name & more', 'odd name/with slash'), pkg('Mystery Tool', null)],
  total: 4, truncated: false, agent_truncated: false,
}
function softwareTab({ permissions = ['servers.view', 'inventory.view'], data = SOFTWARE } = {}) {
  setUser(USER(permissions))
  const qc = newClient()
  if (data) qc.setQueryData(['servers', SERVER.id, 'software'], data)
  return renderAt(qc, '/servers/srv-1?sub=software', h(m.SystemTab, { server: SERVER, sub: 'software', setSub() {} }))
}
const BANNER_AGENT = 'The agent reported its first 1,000 software titles only; titles beyond that are not listed'

{
  const html = softwareTab()
  const links = [...html.matchAll(/<a [^>]*href="(\/inventory\/software\/[^"]*)"[^>]*>(.*?)<\/a>/gs)].map((x) => [x[1], strip(x[2])])
  assert.deepEqual(links, [
    ['/inventory/software/git', 'Git'],
    ['/inventory/software/google-chrome', 'Google Chrome'],
    ['/inventory/software/odd%20name%2Fwith%20slash', 'Odd / name & more'],
  ], 'one link per package that has a product key, the key encoded')
  assert(!html.includes('/inventory/software/null') && !html.includes('/inventory/software/undefined'))
  const rows = rowsOf(html)
  assert.equal(rows.length, 4, 'every package is still a row')
  assert.deepEqual(rows.map((r) => r[0]), ['Git', 'Google Chrome', 'Mystery Tool', 'Odd / name & more'], 'sorted by package name as before')
  const mystery = html.slice(html.indexOf('Mystery Tool') - 200, html.indexOf('Mystery Tool') + 20)
  assert(!mystery.includes('<a ') || mystery.lastIndexOf('<a ') < mystery.lastIndexOf('<td'), 'a package with no key stays text')
  assert(!html.includes(BANNER_AGENT) && !html.includes('stored packages'), 'no banner on a complete list')
  assert(strip(html).includes(`${(4).toLocaleString()} of ${(4).toLocaleString()} packages`))
  // Without inventory.view: no links at all, same rows.
  const plain = softwareTab({ permissions: ['servers.view'] })
  assert(!plain.includes('/inventory/'))
  assert.equal(rowsOf(plain).length, 4)
  assert.deepEqual(rowsOf(plain).map((r) => r[0]), rows.map((r) => r[0]))
  // The rest of the section is untouched: headers, tiles, filter box, export button.
  assert.deepEqual(headersOf(html), ['Package↑', 'Version', 'Vendor↕', 'Installed↕', 'Last reported'], 'the same five columns and sort indicators')
  assert(strip(html).includes(`Packages${(4).toLocaleString()}Installed on this host`) && strip(html).includes('Installed in 30 days'), 'the stat tiles')
  assert(html.includes('placeholder="Filter packages, vendors or versions…"'))
}

{
  const agent = softwareTab({ data: { ...SOFTWARE, agent_truncated: true } })
  assert(strip(agent).includes(`${BANNER_AGENT}.`), 'the agent banner')
  assert(!agent.includes('stored packages'))
  assert(/<div role="status"[^>]*>/.test(agent), 'announced politely')
  // The page formats numbers for the browser's locale (6.234 in German): the expectation uses the same formatter (RG-07).
  // (French groups digits with a narrow no-break space, which `strip` folds into a plain one like every other white space.)
  const n6234 = (6234).toLocaleString().replace(/\s+/g, ' ')
  const cut = softwareTab({ data: { ...SOFTWARE, truncated: true, total: 6234 } })
  assert(strip(cut).includes(`Showing the first ${(4).toLocaleString()} of ${n6234} stored packages; filtering and export cover only these.`), 'the response-limit banner')
  assert(!cut.includes(BANNER_AGENT))
  const both = softwareTab({ data: { ...SOFTWARE, truncated: true, total: 6234, agent_truncated: true } })
  assert(strip(both).includes(BANNER_AGENT) && strip(both).includes(`of ${n6234} stored packages`), 'both flags are independent')
  // A response from before the field existed: no banner, no crash.
  const { agent_truncated: _drop, ...legacy } = SOFTWARE
  assert(!strip(softwareTab({ data: legacy })).includes(BANNER_AGENT))
  // Loading and empty states are as they were.
  assert(strip(softwareTab({ data: null })).includes('Installed software'))
  assert(strip(softwareTab({ data: { items: [], total: 0, truncated: false, agent_truncated: false } })).includes('No software inventory yet'))
  assert(strip(softwareTab({ data: { items: [], total: 0, truncated: false, agent_truncated: true } })).includes(BANNER_AGENT), 'an empty list can still be a cut one')
}

/* ═══ (e) the Server Overview OS tile ═══════════════════════════════════════════════════════ */

const OVERVIEW = { total: 10, status_counts: { healthy: 8, critical: 1, warning: 1 }, os_counts: { windows: 6, linux: 3, unknown: 1 }, agent_counts: { online: 9, offline: 1 }, sites: [], top_cpu: [], top_memory: [], top_disk: [], top_network: [] }
function serversDashboard(permissions) {
  setUser(USER(permissions))
  const qc = newClient()
  qc.setQueryData(['server-monitoring', 'overview'], OVERVIEW)
  return renderAt(qc, '/servers', h(m.ServersDashboardPage))
}
{
  const osCard = (html) => html.split('By operating system')[1].split('Agent fleet')[0]
  const withView = osCard(serversDashboard(['servers.view', 'inventory.view']))
  assert(withView.includes('href="/inventory/os?kind=server"'), 'the tile opens the OS breakdown of the servers')
  assert(strip(withView).includes('OS inventory →'))
  const without = osCard(serversDashboard(['servers.view']))
  assert(!without.includes('/inventory/'), 'no link without inventory.view')
  // The rows are the servers by os_type and keep their own list, with or without the permission.
  for (const part of [withView, without]) {
    for (const os of ['windows', 'linux', 'unknown']) assert(part.includes(`href="/servers/inventory?os=${os}"`), os)
    assert(strip(part).includes('windows6') && strip(part).includes('linux3'))
  }
}

console.log('Inventory touchpoint tests passed: Devices list columns, filters, counts and sorting; device OS link; server software links and banners; Server Overview OS tile.')
